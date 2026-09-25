"""Builders for the Phase 10.1 (batching layer) tests.

The batching layer sits on top of everything built so far: the executor pipeline
(Phase 8), the policy gate, the state cache, the resolver and the tool door
(Phase 10). Testing it needs one thing that is easy to get wrong -- a
*deterministic multi-step desktop*. Scripting five observations and the five
deltas between them by hand in every test file would bury the assertions, so it
lives here.

Two pieces are shared:

* the **section 74 workflow fixture** as data: five non-overlapping controls
  (search icon, search field, submit, result list, play) plus a state builder
  that advances frames and can carry typed text or extra controls (used to inject
  a duplicate label mid-sequence and force an ``AMBIGUOUS`` halt);
* :class:`SequenceEnv`, a fully wired :class:`~control.sequence_runner.SequenceRunner`
  over the Phase 8 fakes, able to record leases, capture-boost entries and
  speculation, and to drive a plan through the *dispatcher* so a test exercises
  the same single door the Brain does.

Nothing here injects real input: the mouse and keyboard run on
:class:`~tests.harness.fake_input_backend.FakeInputBackend`.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from ai.tool_protocol import ToolCall
from config.settings import Settings
from control.sequence_runner import SequenceRunner
from core.resolver_cache import ResolverCache
from core.speculative_perceiver import SpeculativePerceiver
from schemas.actions import ToolEnvelope, ToolName
from schemas.elements import UIElement
from schemas.enums import ChangeClass, CoordinateSpace, PerceptionSource, UIRole
from schemas.events import EventType
from schemas.geometry import Rect
from schemas.screen_state import ChangeRegion, ScreenDelta, ScreenState
from tests.harness.phase8 import make_element, make_state
from tests.harness.phase10 import DispatchEnv, build_dispatch_env


#: The five section 74 workflow controls, deliberately non-overlapping so no
#: control occludes another (fresh, distinct boxes are what keep the section 46
#: occlusion check from blocking a legitimate step).
def _box(x: float, y: float, width: float, height: float) -> Rect:
    """A DESKTOP-space rectangle, so no comparison ever crosses spaces (§31)."""
    return Rect(x=x, y=y, width=width, height=height, space=CoordinateSpace.DESKTOP)


WORKFLOW_BOXES: dict[str, Rect] = {
    "search_icon": _box(100.0, 100.0, 32.0, 32.0),
    "search_box": _box(200.0, 100.0, 300.0, 32.0),
    "submit": _box(600.0, 100.0, 80.0, 32.0),
    "results": _box(100.0, 300.0, 600.0, 200.0),
    "play": _box(100.0, 600.0, 64.0, 64.0),
}


def workflow_elements(
    *,
    typed: str = "",
    extra: Sequence[UIElement] = (),
    window_id: int = 42,
) -> tuple[UIElement, ...]:
    """The five workflow controls, with the search field's content settable."""
    elements = [
        make_element(
            "search_icon",
            text="Search",
            role=UIRole.BUTTON,
            bbox=WORKFLOW_BOXES["search_icon"],
            center=WORKFLOW_BOXES["search_icon"].center,
            owner_window_id=window_id,
        ),
        make_element(
            "search_box",
            text=typed,
            accessible_name="Search field",
            role=UIRole.TEXT_INPUT,
            clickable=True,
            effective_clickable=True,
            focusable=True,
            focused=True,
            bbox=WORKFLOW_BOXES["search_box"],
            center=WORKFLOW_BOXES["search_box"].center,
            owner_window_id=window_id,
        ),
        make_element(
            "submit",
            text="Go",
            role=UIRole.BUTTON,
            bbox=WORKFLOW_BOXES["submit"],
            center=WORKFLOW_BOXES["submit"].center,
            owner_window_id=window_id,
        ),
        make_element(
            "results",
            text="Song results",
            role=UIRole.LIST_ITEM,
            bbox=WORKFLOW_BOXES["results"],
            center=WORKFLOW_BOXES["results"].center,
            owner_window_id=window_id,
        ),
        make_element(
            "play",
            text="Play",
            role=UIRole.BUTTON,
            bbox=WORKFLOW_BOXES["play"],
            center=WORKFLOW_BOXES["play"].center,
            owner_window_id=window_id,
        ),
    ]
    elements.extend(extra)
    return tuple(elements)


def workflow_state(
    frame_id: int,
    *,
    typed: str = "",
    extra: Sequence[UIElement] = (),
    active_window_id: int | None = 42,
    active_app: str | None = "fixture",
    elements: Sequence[UIElement] | None = None,
    generation: int = 1,
) -> ScreenState:
    """A workflow observation at ``frame_id`` (which the cache restamps)."""
    return make_state(
        frame_id=frame_id,
        state_version=frame_id,
        generation=generation,
        elements=elements if elements is not None else workflow_elements(typed=typed, extra=extra),
        active_window_id=active_window_id,
        active_app=active_app,
    )


def workflow_delta(
    before: ScreenState,
    after: ScreenState,
    *,
    region: Rect,
    change_class: ChangeClass = ChangeClass.MEANINGFUL,
) -> ScreenDelta:
    """A delta whose changed region is the target's own box.

    Verification (section 60) requires the change to overlap the target, so a
    delta whose region is somewhere else would correctly report ``UNVERIFIED``.
    """
    return ScreenDelta(
        from_frame_id=before.frame_id,
        to_frame_id=after.frame_id,
        from_state_version=before.state_version,
        to_state_version=after.state_version,
        generation=after.generation,
        regions=(ChangeRegion(rect=region, change_class=change_class),)
        if change_class is not ChangeClass.NONE
        else (),
        computed_at=1000.0,
    )


#: The section 74 workflow, expressed as the plan the Brain would submit.
WORKFLOW_PLAN: list[dict[str, Any]] = [
    {"step_id": "s1", "tool": "click", "target": "Search", "role": "BUTTON"},
    {"step_id": "s2", "tool": "type_text", "target": "Search field", "text": "song name"},
    {"step_id": "s3", "tool": "press_key", "key": "Return"},
    {"step_id": "s4", "tool": "click", "target": "Song results", "role": "LIST_ITEM"},
    {"step_id": "s5", "tool": "click", "target": "Play", "role": "BUTTON"},
]


def sequence_settings(
    *,
    enabled: bool = True,
    capture_boost: bool = False,
    speculative_perception: bool = False,
    resolver_cache: bool = False,
    **sequence_overrides: Any,
) -> Settings:
    """Settings with the batching layer configured for a test."""
    settings = Settings()
    sequence = settings.sequence.model_copy(
        update={
            "enabled": enabled,
            "capture_boost": capture_boost,
            "speculative_perception": speculative_perception,
            **sequence_overrides,
        }
    )
    cache = settings.resolver_cache.model_copy(update={"enabled": resolver_cache})
    return settings.model_copy(update={"sequence": sequence, "resolver_cache": cache})


@dataclass
class SequenceEnv:
    """A wired sequence runner over the fakes, plus what the tests observe."""

    dispatch_env: DispatchEnv
    runner: SequenceRunner
    settings: Settings
    resolver_cache: ResolverCache | None = None
    speculative: SpeculativePerceiver | None = None
    boosts: list[str] = field(default_factory=list)
    leases: list[Any] = field(default_factory=list)

    @property
    def cache(self) -> Any:
        """The state cache the runner reads."""
        return self.dispatch_env.cache

    @property
    def backend(self) -> Any:
        """The recording input backend (the only thing that would inject)."""
        return self.dispatch_env.env.backend

    @property
    def executor(self) -> Any:
        """The executor, for action/denial counts."""
        return self.dispatch_env.env.executor

    def script(self, state: ScreenState, delta: ScreenDelta | None = None) -> None:
        """Queue one post-action observation."""
        self.dispatch_env.env.perceiver.script(state, delta)

    def run(self, plan: Sequence[Any], **kwargs: Any) -> ToolEnvelope:
        """Submit a plan through the dispatcher -- one Brain round trip."""
        arguments: dict[str, Any] = {"steps": _as_arguments(plan)}
        if kwargs.get("halt_on") is not None:
            arguments["halt_on"] = kwargs["halt_on"]
        return self.dispatch_env.dispatcher.dispatch(
            ToolCall(name=ToolName.RUN_SEQUENCE, arguments=arguments)
        )

    def run_sequence_call(self, plan: Sequence[Any], **kwargs: Any) -> ToolEnvelope:
        """Alias for :meth:`run`, named for readability at the call site."""
        return self.run(plan, **kwargs)

    def step_envelopes(self, envelope: ToolEnvelope) -> list[dict[str, Any]]:
        """The per-step result array from a ``run_sequence`` envelope."""
        return list(envelope.data["steps"])

    def input_events(self) -> list[Any]:
        """Every physical input the fake backend recorded."""
        return list(self.backend.events)


def _as_arguments(plan: Sequence[Any]) -> list[dict[str, Any]]:
    """Normalize a plan into raw step dicts."""
    steps: list[dict[str, Any]] = []
    for step in plan:
        if isinstance(step, dict):
            steps.append(dict(step))
        else:  # a SequenceStep
            steps.append(step.to_dict())
    return steps


def build_sequence_env(
    *,
    settings: Settings | None = None,
    state: ScreenState | None = None,
    mode: Any = None,
    script_workflow: bool = False,
    with_windows: bool = True,
    confirmation: Callable[[str, dict[str, Any]], bool] | None = None,
    resolver_cache: bool = False,
    speculative_perception: bool = False,
    capture_boost: bool = False,
    clock: Callable[[], float] | None = None,
    **env_kwargs: Any,
) -> SequenceEnv:
    """Build a fully wired sequence runner over the Phase 8/10 fakes.

    Args:
        settings: Overrides the default (which has the layer enabled).
        state: The initial observation; defaults to a workflow state at frame 1.
        mode: Policy mode; defaults to ASSIST, because OBSERVE never runs a
            sequence at all (section 56).
        script_workflow: Script the five post-action observations and deltas that
            make :data:`WORKFLOW_PLAN` verify end to end.
        with_windows: Wire the fake window manager.
        confirmation: The section 56 confirmation callback.
        resolver_cache: Enable the section 43.1 cache (default off, §4 rule 31).
        speculative_perception: Enable the section 33.2 prefetcher.
        capture_boost: Enable the section 33.1 capture boost.
        clock: The runner's monotonic clock, for the wall-clock-limit tests.
            Defaults to the dispatch environment's, which only advances on an
            injected sleep (so a normal test never trips a real deadline).
        env_kwargs: Passed through to :class:`~tests.harness.phase8.ExecutorEnv`
            (``abort_check``, ``calibration``, ``capabilities``, ...).
    """
    from schemas.enums import PolicyMode

    active_settings = settings if settings is not None else sequence_settings(
        resolver_cache=resolver_cache,
        speculative_perception=speculative_perception,
        capture_boost=capture_boost,
    )
    if mode is None:
        mode = PolicyMode.ASSIST
    initial = state if state is not None else workflow_state(1)
    env_kwargs.setdefault("mode", mode)
    if confirmation is not None:
        env_kwargs["confirmation"] = confirmation

    # The section 43.1 cache belongs to the *resolver the executor uses*, or a
    # resolution could never record a hint. Wiring it here mirrors what the
    # application composition root has to do.
    cache = ResolverCache(active_settings.resolver_cache) if resolver_cache else None
    env_kwargs.setdefault("resolver_cache", cache)

    dispatch_env = build_dispatch_env(
        settings=active_settings, state=initial, with_windows=with_windows, **env_kwargs
    )
    runner_clock = clock if clock is not None else dispatch_env.clock

    speculative = (
        SpeculativePerceiver(active_settings.sequence, _speculative_resolver(), background=False)
        if speculative_perception
        else None
    )
    boosts: list[str] = []
    leases: list[Any] = []
    dispatch_env.bus.subscribe(
        lambda event: leases.append(event.payload) if event.event_type is EventType.LEASE_ISSUED else None
    )

    def boost() -> Any:
        """A capture-boost context manager that records entry and exit."""

        class _Boost:
            def __enter__(self) -> None:
                boosts.append("enter")

            def __exit__(self, *_exc: object) -> None:
                boosts.append("exit")

        return _Boost()

    runner = SequenceRunner(
        active_settings,
        dispatcher=dispatch_env.dispatcher,
        state_cache=dispatch_env.cache,
        permissions=dispatch_env.env.permissions,
        mode_controller=dispatch_env.env.modes,
        event_bus=dispatch_env.bus,
        recovery=dispatch_env.env.recovery,
        cache=cache,
        speculative=speculative,
        capture_boost=boost if capture_boost else None,
        abort_check=dispatch_env.aborted,
        clock=runner_clock,
        sequence_id_factory=_IdFactory(),
        calibration=env_kwargs.get("calibration"),
    )
    dispatch_env.dispatcher.attach_sequence_runner(runner)
    result = SequenceEnv(
        dispatch_env=dispatch_env,
        runner=runner,
        settings=active_settings,
        resolver_cache=cache,
        speculative=speculative,
        boosts=boosts,
        leases=leases,
    )
    if script_workflow:
        script_workflow_steps(result)
    return result


class _IdFactory:
    """Deterministic sequence ids, so a failure message names a known sequence."""

    def __init__(self) -> None:
        self._count = 0

    def __call__(self) -> str:
        self._count += 1
        return f"seq-{self._count}"


def _speculative_resolver() -> Any:
    """A dedicated resolver for the speculative perceiver."""
    from config.settings import Settings as _Settings
    from core.resolver_cache import scorer_or_none
    from core.target_resolver import TargetResolver

    settings = _Settings()
    return TargetResolver(
        settings.resolver, semantic_scorer=scorer_or_none(), semantic_match_floor=70.0
    )


def script_workflow_steps(env: SequenceEnv, *, typed: str = "song name") -> None:
    """Script the five observations :data:`WORKFLOW_PLAN` needs to verify.

    Each step's post-action observation advances the frame and reports a
    ``MEANINGFUL`` change inside the control that step acted on, which is exactly
    the evidence section 60 requires. Step 2 additionally shows the typed text in
    the field, so ``verify_text`` can establish the postcondition honestly.
    """
    previous = env.cache.current
    assert previous is not None
    boxes = WORKFLOW_BOXES
    steps = (
        (boxes["search_icon"], ""),
        (boxes["search_box"], typed),
        (boxes["submit"], typed),
        (boxes["results"], typed),
        (boxes["play"], typed),
    )
    for index, (region, text) in enumerate(steps, start=2):
        state = workflow_state(index, typed=text)
        env.script(state, workflow_delta(previous, state, region=region))
        previous = state


def workflow_plan_with_duplicate_label(step_index: int = 3) -> list[dict[str, Any]]:
    """A plan whose step at ``step_index`` targets a label the desktop duplicates.

    The duplicate controls exist only from frame 3 onward, so the earlier steps
    resolve cleanly and the ambiguity is injected *mid-sequence* -- which is what
    makes it a test of halting rather than of refusing the whole plan.
    """
    plan = [dict(step) for step in WORKFLOW_PLAN]
    plan[step_index] = {
        "step_id": "s3",
        "tool": "click",
        "target": "Duplicate action",
        "role": "BUTTON",
    }
    return plan


def duplicate_controls() -> tuple[UIElement, ...]:
    """Two visible controls sharing a normalized label and a role (section 43)."""
    first = _box(100.0, 700.0, 80.0, 32.0)
    second = _box(200.0, 700.0, 80.0, 32.0)
    return (
        make_element(
            "dup_a",
            text="Duplicate action",
            role=UIRole.BUTTON,
            bbox=first,
            center=first.center,
            source=PerceptionSource.ATSPI,
        ),
        make_element(
            "dup_b",
            text="Duplicate action",
            role=UIRole.BUTTON,
            bbox=second,
            center=second.center,
            source=PerceptionSource.ATSPI,
        ),
    )


def script_workflow_with_duplicates(env: SequenceEnv, *, typed: str = "song name") -> None:
    """Script the workflow, introducing the duplicate controls from frame 3 on."""
    previous = env.cache.current
    assert previous is not None
    boxes = WORKFLOW_BOXES
    steps = (
        (boxes["search_icon"], "", ()),
        (boxes["search_box"], typed, ()),
        (boxes["submit"], typed, duplicate_controls()),
        (boxes["results"], typed, duplicate_controls()),
        (boxes["play"], typed, duplicate_controls()),
    )
    for index, (region, text, extra) in enumerate(steps, start=2):
        state = workflow_state(index, typed=text, extra=extra)
        env.script(state, workflow_delta(previous, state, region=region))
        previous = state


__all__ = [
    "WORKFLOW_BOXES",
    "WORKFLOW_PLAN",
    "SequenceEnv",
    "build_sequence_env",
    "duplicate_controls",
    "script_workflow_steps",
    "script_workflow_with_duplicates",
    "sequence_settings",
    "workflow_delta",
    "workflow_elements",
    "workflow_plan_with_duplicate_label",
    "workflow_state",
]
