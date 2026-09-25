"""Builders for the Phase 10 (Brain / tools / context) tests.

Phase 10 is the seam between a Brain and the Body, so the tests need three things
that would otherwise be rebuilt in every file:

* a fully wired :class:`~ai.tool_protocol.ToolDispatcher` over the Phase 8 fakes
  (:class:`~tests.harness.phase8.ExecutorEnv`) -- no real input, no real screen;
* a :class:`ScriptedBrain`, an adapter that replays a fixed list of model turns,
  so the manual tool loop can be tested without a network or an API key;
* a deterministic :class:`TickClock`, so ``wait_for_*`` tests assert on the
  timeout logic rather than on wall-clock timing.

Nothing here injects physical input: the mouse and keyboard run on
:class:`~tests.harness.fake_input_backend.FakeInputBackend`.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from ai.brain_adapter import BrainAdapter, ConversationTurn, ModelToolCall, ModelTurn
from ai.context_manager import ContextManager
from ai.tool_protocol import ToolDeclaration, ToolDispatcher
from config.settings import Settings
from control.window_manager import WindowInfo
from core.event_bus import EventBus
from core.state_cache import StateCache
from schemas.capability import Capability, CapabilityReport
from schemas.enums import CapabilityName, CapabilityStatus, ErrorCode
from schemas.events import Event
from tests.harness.phase8 import ExecutorEnv, make_layout


class TickClock:
    """A monotonic clock that only advances when ``sleep`` is called.

    The dispatcher's wait tools are driven entirely by ``clock`` and ``sleep``,
    so this makes a 5-second timeout complete in microseconds and, more
    importantly, makes the test assert on the *logic* rather than on timing.
    """

    def __init__(self, start: float = 100.0) -> None:
        """Create a clock starting at ``start`` seconds."""
        self.now = start
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        """Read the current time."""
        return self.now

    def sleep(self, seconds: float) -> None:
        """Advance the clock by ``seconds``."""
        self.sleeps.append(seconds)
        self.now += max(0.0, seconds)

    @property
    def total_slept(self) -> float:
        """Total simulated time spent sleeping."""
        return sum(self.sleeps)


class FakeWindows:
    """A read-only window controller double for the window tools."""

    def __init__(
        self,
        *,
        active: int | None = 42,
        windows: Sequence[WindowInfo] = (),
        fail: ErrorCode | None = None,
    ) -> None:
        """Create the fake with an active window and a client list."""
        self._active = active
        self._windows = tuple(windows)
        self._fail = fail
        self.activations: list[int] = []

    def active_window(self) -> int | None:
        """Report the active window id."""
        if self._fail is not None:
            raise _window_error(self._fail)
        return self._active

    def list_windows(self) -> tuple[WindowInfo, ...]:
        """Report the client window list."""
        if self._fail is not None:
            raise _window_error(self._fail)
        return self._windows

    def window_info(self, window_id: int) -> WindowInfo | None:
        """Return metadata for ``window_id``, or ``None`` (part of the contract)."""
        if self._fail is not None:
            raise _window_error(self._fail)
        for window in self._windows:
            if window.window_id == window_id:
                return window
        return None

    def ensure_active(self, window_id: int, *, timeout_ms: int | None = None) -> bool:
        """Record an activation request and accept it."""
        self.activations.append(window_id)
        self._active = window_id
        return True

    def probe(self) -> Any:
        """Report the fake as available."""
        from control.window_manager import WindowManagerProbe

        return WindowManagerProbe(available=True, backend="fake")


def _window_error(code: ErrorCode) -> Any:
    """Build the structured error the real controller would raise."""
    from schemas.errors import BlaxcyError

    return BlaxcyError(code, "fake window manager failure")


def make_capability_report(*, brain_available: bool = False) -> CapabilityReport:
    """A capability report with honest-looking evidence, for tool responses."""
    capabilities = [
        Capability(name=CapabilityName.CAPTURE, status=CapabilityStatus.AVAILABLE, backend="fake"),
        Capability(name=CapabilityName.MOUSE, status=CapabilityStatus.AVAILABLE, backend="fake"),
        Capability(
            name=CapabilityName.BRAIN,
            status=CapabilityStatus.AVAILABLE if brain_available else CapabilityStatus.UNAVAILABLE,
            backend="fake",
            reason=None if brain_available else "no API key stored in the test keyring",
        ),
    ]
    return CapabilityReport(capabilities=tuple(capabilities), generated_at=1000.0, session_type="x11")


@dataclass
class DispatchEnv:
    """A dispatcher wired over the Phase 8 fakes, plus the pieces it needs."""

    settings: Settings
    bus: EventBus
    cache: StateCache
    env: ExecutorEnv
    dispatcher: ToolDispatcher
    clock: TickClock
    windows: FakeWindows | None = None
    capabilities: CapabilityReport | None = None
    stop: Any = None
    aborted: Callable[[], ErrorCode | None] | None = None

    def dispatch(self, name: str, **arguments: Any) -> Any:
        """Dispatch one tool call by name and return its envelope."""
        from ai.tool_protocol import ToolCall

        return self.dispatcher.dispatch(ToolCall(name=name, arguments=arguments))


def build_dispatch_env(
    *,
    settings: Settings | None = None,
    state: Any = None,
    with_windows: bool = True,
    with_capabilities: bool = True,
    abort_check: Callable[[], ErrorCode | None] | None = None,
    perceive_override: Callable[[], Any] | None = None,
    windows: Any = None,
    **env_kwargs: Any,
) -> DispatchEnv:
    """Build a dispatcher over the Phase 8 fakes.

    Args:
        settings: Full configuration; defaults to :class:`Settings`.
        state: The initial :class:`ScreenState` seeded into the cache.
        with_windows: Wire a :class:`FakeWindows` for the window tools.
        with_capabilities: Wire a capability report callable.
        abort_check: The combined stop/takeover hook.
        perceive_override: Replace the perceiver the dispatcher refreshes
            through (the wait tools need to script observations).
        windows: An explicit window-controller double, replacing ``FakeWindows``
            (used to script a window-manager failure).
        env_kwargs: Passed through to :class:`ExecutorEnv`.
    """
    active_settings = settings if settings is not None else Settings()
    if state is not None:
        env_kwargs["state"] = state
    if abort_check is not None:
        env_kwargs["abort_check"] = abort_check
    env = ExecutorEnv(settings=active_settings, **env_kwargs)
    clock = TickClock()
    windows = windows if windows is not None else (FakeWindows() if with_windows else None)
    capabilities = make_capability_report() if with_capabilities else None
    dispatcher = ToolDispatcher(
        active_settings,
        executor=env.executor,
        state_cache=env.cache,
        resolver=env.resolver,
        event_bus=env.bus,
        window_manager=windows,
        capabilities=(lambda: capabilities) if capabilities is not None else None,
        perceive=perceive_override if perceive_override is not None else env.perceiver,
        abort_check=abort_check,
        clock=clock,
        sleep=clock.sleep,
    )
    return DispatchEnv(
        settings=active_settings,
        bus=env.bus,
        cache=env.cache,
        env=env,
        dispatcher=dispatcher,
        clock=clock,
        windows=windows,
        capabilities=capabilities,
        aborted=abort_check,
    )


class ScriptedBrain(BrainAdapter):
    """An adapter that replays fixed model turns and records what it was sent."""

    name = "scripted"

    def __init__(
        self,
        turns: Sequence[ModelTurn],
        *,
        error: Exception | None = None,
        on_call: Callable[[], None] | None = None,
        error_after: int | None = None,
    ) -> None:
        """Create the adapter with the turns to replay (then a final empty turn).

        Args:
            turns: The model turns to replay, in order.
            error: An exception to raise on every call.
            on_call: A hook run before each call, so a test can cancel the loop
                or latch a stop from inside the model turn.
            error_after: Raise the error only once this many calls have happened.
        """
        self._turns = list(turns)
        self._error = error
        #: Public so a test can attach a hook after the loop exists (for
        #: example ``brain.on_call = loop.cancel`` to test cancellation).
        self.on_call = on_call
        self._error_after = error_after
        self.calls: list[dict[str, Any]] = []

    def generate(
        self,
        *,
        system_instruction: str,
        turns: Sequence[ConversationTurn],
        tools: Sequence[ToolDeclaration],
    ) -> ModelTurn:
        """Return the next scripted turn, or raise the scripted error."""
        self.calls.append(
            {
                "system_instruction": system_instruction,
                "turns": tuple(turns),
                "tools": tuple(tools),
            }
        )
        if self.on_call is not None:
            self.on_call()
        if self._error is not None and (
            self._error_after is None or len(self.calls) > self._error_after
        ):
            raise self._error
        if not self._turns:
            return ModelTurn(text="done", finish_reason="STOP")
        return self._turns.pop(0)


def model_turn(
    *,
    text: str | None = None,
    calls: Sequence[tuple[str, dict[str, Any]]] = (),
) -> ModelTurn:
    """Build a model turn with optional text and tool calls."""
    return ModelTurn(
        text=text,
        tool_calls=tuple(
            ModelToolCall(name=name, args=dict(args), call_id=f"c{index}")
            for index, (name, args) in enumerate(calls)
        ),
    )


@dataclass
class LoopEnv:
    """An :class:`~ai.brain_adapter.AgentLoop` over the dispatch fakes."""

    dispatch_env: DispatchEnv
    context: ContextManager
    collected: list[Any] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)


def build_loop_env(
    *,
    dispatch_env: DispatchEnv | None = None,
    turns: Sequence[ModelTurn] = (),
    brain_error: Exception | None = None,
    brain_error_after: int | None = None,
    confirmation_prompt: Callable[[Any], bool] | None = None,
    abort_check: Callable[[], ErrorCode | None] | None = None,
    mode_provider: Callable[[], str] | None = None,
    max_model_turns: int | None = None,
    max_task_wall_clock_seconds: float | None = None,
    **dispatch_kwargs: Any,
) -> tuple[Any, ScriptedBrain, LoopEnv]:
    """Build an agent loop, its scripted brain and the surrounding environment."""
    from ai.brain_adapter import AgentLoop

    env = dispatch_env if dispatch_env is not None else build_dispatch_env(**dispatch_kwargs)
    brain = ScriptedBrain(turns, error=brain_error, error_after=brain_error_after)
    context = ContextManager(env.settings)
    collected: list[Any] = []
    loop_env = LoopEnv(dispatch_env=env, context=context, collected=collected)
    env.bus.subscribe(lambda event: loop_env.events.append(event))
    loop = AgentLoop(
        brain,
        dispatcher=env.dispatcher,
        context_manager=context,
        settings=env.settings,
        event_bus=env.bus,
        abort_check=abort_check if abort_check is not None else env.aborted,
        state_cache=env.cache,
        confirmation_prompt=confirmation_prompt,
        on_tool_result=collected.append,
        mode_provider=mode_provider,
        clock=env.clock,
        max_model_turns=max_model_turns,
        max_task_wall_clock_seconds=max_task_wall_clock_seconds,
    )
    return loop, brain, loop_env


__all__ = [
    "DispatchEnv",
    "FakeWindows",
    "LoopEnv",
    "ScriptedBrain",
    "TickClock",
    "build_dispatch_env",
    "build_loop_env",
    "make_capability_report",
    "make_layout",
    "model_turn",
]
