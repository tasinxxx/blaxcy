"""Builders for the Phase 8 (policy / executor) tests.

The executor pipeline needs a perceived state, a resolver, input controllers, a
lease store and a scripted "next observation" so that verification can be
exercised deterministically. Assembling that in every test file would bury the
assertions, so the wiring lives here and the tests stay about behaviour.

Nothing in this module injects real input: the mouse and keyboard controllers run
on :class:`~tests.harness.fake_input_backend.FakeInputBackend` with a no-op
sleep, and :class:`FakePerceiver` pushes scripted states straight into a
:class:`~core.state_cache.StateCache`.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence

from config.settings import SafetySettings, Settings, VerificationSettings
from control.action_tracker import ActionTracker
from control.executor import Executor
from control.keyboard import KeyboardController
from control.mouse import MouseController
from control.verifier import Verifier
from core.calibration import identity_geometry_map
from core.event_bus import EventBus
from core.state_cache import StateCache
from core.target_resolver import TargetResolver
from policy.modes import ModeController
from policy.permissions import PermissionEngine
from schemas.elements import UIElement
from schemas.enums import (
    ChangeClass,
    CoordinateSpace,
    PerceptionSource,
    PolicyMode,
    UIRole,
)
from schemas.geometry import MonitorGeometry, MonitorLayout, Rect
from schemas.screen_state import ChangeRegion, ScreenDelta, ScreenState
from tests.harness.fake_input_backend import FakeInputBackend

#: A generous default element box in DESKTOP space.
DEFAULT_BOX: Rect = Rect(x=200.0, y=120.0, width=120.0, height=32.0, space=CoordinateSpace.DESKTOP)


def make_layout(width: int = 1920, height: int = 1080) -> MonitorLayout:
    """A single-monitor desktop layout."""
    return MonitorLayout(
        monitors=(MonitorGeometry(monitor_id=0, width=width, height=height, is_primary=True),)
    )


def make_element(element_id: str = "e1", **overrides: object) -> UIElement:
    """Build a clickable AT-SPI element, overriding any field."""
    base: dict[str, object] = {
        "element_id": element_id,
        "role": UIRole.BUTTON,
        "text": "Send",
        "source": PerceptionSource.ATSPI,
        "confidence": 0.95,
        "clickable": True,
        "effective_clickable": True,
        "bbox": DEFAULT_BOX,
        "center": DEFAULT_BOX.center,
        "atspi_path": f"/p/{element_id}",
        "owner_window_id": 42,
    }
    base.update(overrides)
    return UIElement.model_validate(base)


def box_of(element: UIElement) -> Rect:
    """The element's DESKTOP-space box, asserting it has one.

    Section 60 verification needs a change region that overlaps the control the
    step acted on, so most sequence tests need a real box rather than an
    optional one. Asserting here keeps the test bodies about behaviour.
    """
    assert element.bbox is not None, f"{element.element_id} has no bbox"
    return element.bbox


def make_text_input(element_id: str = "field", **overrides: object) -> UIElement:
    """Build a focused text-entry element (for the section 51 focus guard)."""
    base: dict[str, object] = {
        "role": UIRole.TEXT_INPUT,
        "text": "",
        "clickable": True,
        "effective_clickable": True,
        "focusable": True,
        "focused": True,
    }
    base.update(overrides)
    return make_element(element_id, **base)


def make_state(
    *,
    frame_id: int = 5,
    state_version: int = 3,
    generation: int = 1,
    elements: Sequence[UIElement] = (),
    active_window_id: int | None = 42,
    active_app: str | None = "fixture",
    monotonic: float | None = None,
    layout: MonitorLayout | None = None,
) -> ScreenState:
    """A perceived state carrying real stamps; ``monotonic`` defaults to "now"."""
    import time

    return ScreenState(
        frame_id=frame_id,
        state_version=state_version,
        generation=generation,
        timestamp=1000.0,
        monotonic=time.monotonic() if monotonic is None else monotonic,
        layout=layout if layout is not None else make_layout(),
        elements=tuple(elements),
        active_window_id=active_window_id,
        active_app=active_app,
    )


def make_delta(
    *,
    before: ScreenState,
    after: ScreenState,
    change_class: ChangeClass = ChangeClass.MEANINGFUL,
) -> ScreenDelta:
    """Build a delta describing a change between two states."""
    regions: tuple[ChangeRegion, ...] = ()
    if change_class is not ChangeClass.NONE:
        regions = (
            ChangeRegion(rect=DEFAULT_BOX, change_class=change_class),
        )
    return ScreenDelta(
        from_frame_id=before.frame_id,
        to_frame_id=after.frame_id,
        from_state_version=before.state_version,
        to_state_version=after.state_version,
        generation=after.generation,
        regions=regions,
        computed_at=1000.0,
    )


class FakePerceiver:
    """A scripted ``perceive`` callable that feeds the state cache.

    Each scripted ``(state, delta)`` pair is offered to the cache on the next
    call, so the executor's post-action observation sees a real accepted state
    and the classified delta that goes with it. When the script runs out the
    cache's current state is returned unchanged -- which is exactly the "nothing
    changed" case verification must report honestly.
    """

    def __init__(self, cache: StateCache) -> None:
        """Create a perceiver bound to ``cache``."""
        self._cache = cache
        self._queue: deque[tuple[ScreenState, ScreenDelta | None]] = deque()
        self.calls = 0

    def script(self, state: ScreenState, delta: ScreenDelta | None = None) -> None:
        """Queue one observation for the next call."""
        self._queue.append((state, delta))

    def __call__(self) -> ScreenState | None:
        """Offer the next scripted observation, or return the current state."""
        self.calls += 1
        if self._queue:
            state, delta = self._queue.popleft()
            accepted = self._cache.update_screen_state(state, delta=delta)
            return accepted if accepted is not None else self._cache.current
        return self._cache.current


class ExecutorEnv:
    """A fully wired executor over fakes, for policy/executor tests."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        mode: PolicyMode | None = None,
        state: ScreenState | None = None,
        confirmation: object | None = None,
        abort_check: object | None = None,
        window_manager: object | None = None,
        calibration: object | None = None,
        capabilities: object | None = None,
        verifier_settings: VerificationSettings | None = None,
        safety: SafetySettings | None = None,
        recovery: object | None = None,
        clipboard: object | None = None,
        resolver_cache: object | None = None,
        activate: object | None = None,
        with_perceive_fresh: bool = False,
    ) -> None:
        """Build the environment and seed the cache with ``state``.

        ``resolver_cache`` is the section 43.1 hint cache. It is passed to the
        resolver exactly as the application composition root must pass it; the
        resolver still re-verifies every hint against live elements.
        """
        self.settings = settings if settings is not None else Settings()
        safety_overrides: dict[str, object] = {}
        if mode is not None:
            safety_overrides["mode"] = mode
        if safety is not None:
            safety_overrides.update(safety.model_dump())
        if safety_overrides:
            self.settings = self.settings.model_copy(
                update={"safety": self.settings.safety.model_copy(update=safety_overrides)}
            )
        self.bus = EventBus()
        self.cache = StateCache(event_bus=self.bus)
        self.modes = ModeController(self.settings.safety, event_bus=self.bus)
        self.permissions = PermissionEngine(self.settings)
        self.resolver = TargetResolver(self.settings.resolver, cache=resolver_cache)  # type: ignore[arg-type]
        self.backend = FakeInputBackend()
        layout = state.layout if state is not None else make_layout()
        self.mouse = MouseController(
            self.backend,
            identity_geometry_map(layout),
            self.settings.input,
            sleep=lambda _seconds: None,
        )
        self.keyboard = KeyboardController(
            self.backend,
            self.settings.input,
            clipboard=clipboard,  # type: ignore[arg-type]
            sleep=lambda _seconds: None,
        )
        self.verifier = Verifier(verifier_settings or self.settings.verification)
        self.tracker = ActionTracker(event_bus=self.bus)
        #: Kept so callers (the Phase 10.1 sequence runner) can scope per-step
        #: budgets over the same controller the executor retries through (§61).
        self.recovery = recovery
        self.perceiver = FakePerceiver(self.cache)
        #: A second scripted observer, wired as the executor's section 60
        #: verification-confirmation hook (``perceive_fresh``). ``None`` by default
        #: so the ordinary pipeline behaviour -- and every existing test -- is
        #: unchanged; a test that wants to exercise the re-check asks for it.
        self.fresh_perceiver: FakePerceiver | None = (
            FakePerceiver(self.cache) if with_perceive_fresh else None
        )
        if state is not None:
            self.cache.update_screen_state(state)
        self.executor = Executor(
            self.settings,
            permissions=self.permissions,
            mode_controller=self.modes,
            state_cache=self.cache,
            resolver=self.resolver,
            mouse=self.mouse,
            keyboard=self.keyboard,
            verifier=self.verifier,
            tracker=self.tracker,
            event_bus=self.bus,
            window_manager=window_manager,  # type: ignore[arg-type]
            perceive=self.perceiver,
            perceive_fresh=self.fresh_perceiver,
            abort_check=abort_check,  # type: ignore[arg-type]
            confirmation=confirmation,  # type: ignore[arg-type]
            capabilities=capabilities,  # type: ignore[arg-type]
            calibration=calibration,  # type: ignore[arg-type]
            recovery=recovery,  # type: ignore[arg-type]
            activate=activate,  # type: ignore[arg-type]
        )


__all__ = [
    "DEFAULT_BOX",
    "ExecutorEnv",
    "FakePerceiver",
    "box_of",
    "make_delta",
    "make_element",
    "make_layout",
    "make_state",
    "make_text_input",
]
