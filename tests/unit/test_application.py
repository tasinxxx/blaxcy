"""The application composition root (specification sections 1-3, 32, 65, 71).

These tests build the *whole* Body over scripted transports: a real frame engine
over a scripted capture backend, a real accessibility service over a scripted
AT-SPI backend, real OCR/visual engines over scripted backends, and the recording
:class:`~tests.harness.fake_input_backend.FakeInputBackend` instead of XTEST.

What they establish is exactly what composition is responsible for:

* one pipeline -- the dispatcher's executor is the Body's executor, and
  ``run_sequence`` is attached to that same dispatcher, not to a parallel one;
* the safety components reach the action path: a latched stop preempts a click,
  OBSERVE refuses one, and a confirmation is consulted rather than assumed;
* an action that does run is verified against a real post-action observation;
* the lifecycle is honest: shutdown releases input, stops the watchdog and
  uninstalls the crash guard.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest

from ai.tool_protocol import ToolCall
from config.settings import Settings
from control.backends.unavailable import UnavailableBackend
from core.application import BlaxcyApplication, ConfirmationFn
from core.frame_engine import FrameEngine
from core.ocr import OcrEngine
from core.visual_grounder import RawVisualMatch, VisualGrounder
from schemas.actions import ToolName
from schemas.elements import UIElement
from schemas.enums import ErrorCode, PolicyMode, VerificationState
from schemas.errors import BlaxcyError
from tests.harness.fake_input_backend import FakeInputBackend
from tests.harness.perception import (
    ScriptedA11yBackend,
    ScriptedCaptureBackend,
    ScriptedOcrBackend,
    ScriptedVisualBackend,
    desktop_box,
)
from tests.harness.phase8 import make_element
from tests.harness.phase10 import (
    FakeWindows,
    ScriptedBrain,
    make_capability_report,
    model_turn,
)

_APPLICATIONS = ("fixture",)
_SEND_BOX = desktop_box(200.0, 300.0, 120.0, 32.0)


def _elements(*, app: str = "fixture") -> tuple[UIElement, ...]:
    """One uniquely labelled button, owned by the fixture application."""
    box = _SEND_BOX
    return (
        make_element(
            "send",
            text="Send",
            bbox=box,
            center=box.center,
            owner_app=app,
            atspi_path="/p/send",
        ),
    )


def _app(
    *,
    settings: Settings | None = None,
    elements: Sequence[UIElement] = (),
    confirmation: ConfirmationFn | None = None,
    capture: ScriptedCaptureBackend | None = None,
    visual_matches: Sequence[RawVisualMatch] = (),
    windows: Any = None,
    with_watchdog: bool = False,
    watchdog_path: Path | None = None,
) -> tuple[BlaxcyApplication, ScriptedCaptureBackend, FakeInputBackend]:
    """Build an assembled Body over scripted transports, started and ready."""
    active = settings if settings is not None else Settings()
    backend = (
        capture
        if capture is not None
        else ScriptedCaptureBackend(width=1920, height=1080)
    )
    frame_engine = FrameEngine(
        active.capture, backend_factory=lambda: backend, thumbnail_size=(480, 270)
    )
    from core.accessibility import AccessibilityService

    a11y = AccessibilityService(
        active.accessibility, backend_factory=lambda: ScriptedA11yBackend(elements)
    )
    inputs = FakeInputBackend()
    app = BlaxcyApplication(
        active,
        confirmation=confirmation,
        collect_capabilities=False,
        with_watchdog=with_watchdog,
        frame_engine=frame_engine,
        accessibility=a11y,
        ocr=OcrEngine(active.ocr, backend=ScriptedOcrBackend("Play")),
        visual=VisualGrounder(active.visual, backend=ScriptedVisualBackend(visual_matches)),
        backend=inputs,
        clipboard=None,
        window_manager=FakeWindows() if windows is None else windows,
        watchdog_path=watchdog_path,
    )
    app.start()
    return app, backend, inputs


#: How a test asks for an assembled Body. The keywords mirror :func:`_app`.
AppFactory = Callable[..., tuple[BlaxcyApplication, ScriptedCaptureBackend, FakeInputBackend]]


@pytest.fixture
def app_factory() -> Iterator[AppFactory]:
    """Build applications and shut them down afterwards."""
    built: list[BlaxcyApplication] = []

    def build(
        **kwargs: Any,
    ) -> tuple[BlaxcyApplication, ScriptedCaptureBackend, FakeInputBackend]:
        app, capture, backend = _app(**kwargs)
        built.append(app)
        return app, capture, backend

    yield build
    for app in built:
        app.shutdown()


# -- Composition --------------------------------------------------------------


def test_the_body_is_one_pipeline_not_two(app_factory: AppFactory) -> None:
    """The dispatcher's executor *is* the Body's, and the runner uses that door."""
    app, _, _ = app_factory()

    assert app.dispatcher is not None
    assert app.dispatcher.sequence_runner is app.sequence_runner
    assert app.sequence_runner is not None
    # Section 32.1: the read path and the executor share one abort hook, so a stop
    # cannot reach one and miss the other.
    assert app.executor is not None
    assert app.stop.latched is False
    assert app.abort_check() is None


def test_declared_tools_are_exactly_the_section_66_set(app_factory: AppFactory) -> None:
    """Composition does not change the advertised protocol."""
    app, _, _ = app_factory()

    tools = set(app.declared_tools())

    assert ToolName.RUN_SEQUENCE in tools
    assert ToolName.CLICK in tools
    assert len(tools) == 19


def test_an_observation_reaches_the_state_cache(app_factory: AppFactory) -> None:
    """The perception orchestrator is wired as the one perception entry point."""
    app, _, _ = app_factory(elements=_elements())

    state = app.perceive()

    assert state is not None
    assert app.cache.current is state
    assert [element.element_id for element in state.elements] == ["send"]
    assert state.active_window_id == 42


def test_the_input_geometry_is_measured_from_the_capture_backend(app_factory: AppFactory) -> None:
    """The layout comes from the backend's real monitor report, not a placeholder."""
    app, _, _ = app_factory()

    assert app.layout.monitors[0].width == 1920
    assert app.layout_notes == []


def test_a_capture_failure_is_recorded_rather_than_hidden(app_factory: AppFactory) -> None:
    """A Body that cannot see says so, refuses to act, and still runs."""
    failing = ScriptedCaptureBackend(width=1920, height=1080, fail=True)
    app, _, backend = app_factory(capture=failing)
    app.set_mode(PolicyMode.ASSIST)

    assert app.perceive() is None
    report = app.status()
    assert report["started"] is True
    assert isinstance(report["perception"], dict)
    assert report["perception"]["capture_failures"] >= 1

    # And with no observation there is no state to act on, so nothing is injected.
    envelope = app.dispatch(ToolCall(name=ToolName.CLICK, arguments={"target": "Send"}))
    assert envelope.ok is False
    assert envelope.error_code is ErrorCode.CAPTURE_FAILED
    assert backend.events == []


# -- The action path ----------------------------------------------------------


def test_observe_refuses_a_click_and_injects_nothing(app_factory: AppFactory) -> None:
    """Section 56: the default mode denies input outright."""
    app, _, backend = app_factory(elements=_elements())
    app.perceive()

    envelope = app.dispatch(ToolCall(name=ToolName.CLICK, arguments={"target": "Send"}))

    assert envelope.ok is False
    assert envelope.error_code is ErrorCode.PERMISSION_DENIED
    assert backend.events == []


def test_a_latched_stop_preempts_a_click(app_factory: AppFactory) -> None:
    """Section 63: the stop reaches the executor's own abort hook."""
    app, _, backend = app_factory(elements=_elements())
    app.set_mode(PolicyMode.ASSIST)
    app.trigger_emergency_stop("test")

    envelope = app.dispatch(ToolCall(name=ToolName.CLICK, arguments={"target": "Send"}))

    assert envelope.ok is False
    assert envelope.error_code is ErrorCode.EMERGENCY_STOP_ACTIVE
    assert backend.events == []
    # The stop also forced OBSERVE, so the mode is no longer permissive.
    assert app.status()["mode"] == PolicyMode.OBSERVE.value


def test_a_click_in_assist_is_injected_and_verified_against_a_real_observation(
    app_factory: AppFactory,
) -> None:
    """The full section 59 pipeline, over recording transports."""
    app, capture, backend = app_factory(elements=_elements())
    app.set_mode(PolicyMode.ASSIST)
    app.perceive()
    # The post-click desktop: a real change inside the control that was clicked,
    # which is the evidence section 60 requires.
    capture.add_patch(200, 300, 400, 150, level=220)

    envelope = app.dispatch(ToolCall(name=ToolName.CLICK, arguments={"target": "Send"}))

    assert envelope.ok is True, envelope.message
    assert envelope.verification is VerificationState.VERIFIED
    assert envelope.data["lease"]["frame_id"] == 0  # bound to the observation it resolved against
    assert envelope.data["revalidation"]["ok"] is True
    assert backend.events  # the click really reached the input backend
    assert backend.held_buttons == ()


def test_a_click_on_an_ambiguous_target_is_refused(app_factory: AppFactory) -> None:
    """Section 43's absolute rule survives composition."""
    first = make_element("a1", text="Open", bbox=_SEND_BOX, center=_SEND_BOX.center)
    second = make_element(
        "a2", text="Open", bbox=desktop_box(400.0, 300.0, 120.0, 32.0), center=None
    )
    app, _, backend = app_factory(elements=(first, second))
    app.set_mode(PolicyMode.ASSIST)
    app.perceive()

    envelope = app.dispatch(ToolCall(name=ToolName.CLICK, arguments={"target": "Open"}))

    assert envelope.ok is False
    assert envelope.error_code is ErrorCode.TARGET_AMBIGUOUS
    assert backend.events == []


def test_a_destructive_action_is_gated_on_a_real_confirmation(app_factory: AppFactory) -> None:
    """Section 56: the answer comes from the human, and a refusal is honoured."""
    answers: list[bool] = []

    def confirm(message: str, details: dict[str, Any]) -> bool:
        answers.append(True)
        return False

    app, _, backend = app_factory(elements=_elements(), confirmation=confirm)
    app.set_mode(PolicyMode.ASSIST)
    app.perceive()

    envelope = app.dispatch(
        ToolCall(name=ToolName.DRAG, arguments={"target": "Send", "destination": "Trash"})
    )

    assert answers, "the confirmation callback was never consulted"
    assert envelope.ok is False
    assert envelope.error_code in {
        ErrorCode.CONFIRMATION_REQUIRED,
        ErrorCode.CONFIRMATION_DENIED,
    }
    assert backend.events == []
    # Both halves of the ask are published, so a GUI can show and resolve it.
    published = {event.event_type.value for event in app.bus.history}
    assert "CONFIRMATION_REQUESTED" in published
    assert "CONFIRMATION_RESOLVED" in published


def test_no_confirmation_callback_means_no_confirmation(app_factory: AppFactory) -> None:
    """Headless, an unattended destructive action is refused rather than assumed."""
    app, _, backend = app_factory(elements=_elements())
    app.set_mode(PolicyMode.AUTONOMOUS)
    app.perceive()

    envelope = app.dispatch(
        ToolCall(name=ToolName.DRAG, arguments={"target": "Send", "destination": "Trash"})
    )

    assert envelope.ok is False
    assert envelope.error_code in {
        ErrorCode.CONFIRMATION_REQUIRED,
        ErrorCode.CONFIRMATION_DENIED,
    }
    assert backend.events == []


def test_an_unavailable_input_backend_refuses_rather_than_pretending() -> None:
    """Composition over a backend that cannot inject is still fail-closed."""
    active = Settings()
    backend = ScriptedCaptureBackend()
    frame_engine = FrameEngine(active.capture, backend_factory=lambda: backend)
    from core.accessibility import AccessibilityService

    app = BlaxcyApplication(
        active,
        collect_capabilities=False,
        with_watchdog=False,
        frame_engine=frame_engine,
        accessibility=AccessibilityService(
            active.accessibility, backend_factory=lambda: ScriptedA11yBackend(_elements())
        ),
        backend=UnavailableBackend(),
        clipboard=None,
        window_manager=FakeWindows(),
    )
    app.start()
    try:
        app.set_mode(PolicyMode.ASSIST)
        app.perceive()
        envelope = app.dispatch(
            ToolCall(name=ToolName.CLICK, arguments={"target": "Send"})
        )
        assert envelope.ok is False
        assert envelope.error_code is ErrorCode.BACKEND_UNAVAILABLE
        assert app.status()["startup"]["input_backend"] is None
    finally:
        app.shutdown()


# -- Sequences ----------------------------------------------------------------


def test_run_sequence_executes_through_the_same_dispatcher(app_factory: AppFactory) -> None:
    """Section 66.1: batching is a container over the one door, not a second path."""
    app, capture, backend = app_factory(elements=_elements())
    app.set_mode(PolicyMode.ASSIST)
    app.perceive()
    capture.add_patch(200, 300, 400, 150, level=220)

    envelope = app.dispatch(
        ToolCall(
            name=ToolName.RUN_SEQUENCE,
            arguments={"steps": [{"step_id": "s1", "tool": "click", "target": "Send"}]},
        )
    )

    assert envelope.ok is True, envelope.message
    steps = envelope.data["steps"]
    assert len(steps) == 1
    assert steps[0]["ok"] is True
    assert steps[0]["verification"] == VerificationState.VERIFIED.value
    # The step's own lease is bound to the observation *it* resolved against.
    assert steps[0]["data"]["lease"]["frame_id"] == 0
    assert backend.events


def test_a_sequence_halts_on_an_ambiguous_step_and_marks_the_tail_not_executed(
    app_factory: AppFactory,
) -> None:
    """The tail is reported, never silently dropped."""
    first = make_element("a1", text="Send", bbox=_SEND_BOX, center=_SEND_BOX.center)
    second = make_element(
        "a2", text="Send", bbox=desktop_box(400.0, 300.0, 120.0, 32.0), center=None
    )
    app, _, backend = app_factory(elements=(first, second))
    app.set_mode(PolicyMode.ASSIST)
    app.perceive()

    envelope = app.dispatch(
        ToolCall(
            name=ToolName.RUN_SEQUENCE,
            arguments={
                "steps": [
                    {"step_id": "s1", "tool": "click", "target": "Send"},
                    {"step_id": "s2", "tool": "press_key", "key": "Return"},
                ]
            },
        )
    )

    steps = envelope.data["steps"]
    assert len(steps) == 2
    assert steps[0]["error_code"] == ErrorCode.TARGET_AMBIGUOUS.value
    assert steps[1]["error_code"] == ErrorCode.NOT_EXECUTED.value
    assert backend.events == []


# -- Brain, status and lifecycle ---------------------------------------------


def test_the_brain_loop_is_wired_to_the_real_dispatcher(app_factory: AppFactory) -> None:
    """Section 67: the loop walks the Body's own dispatcher, never the SDK's."""
    app, capture, backend = app_factory(elements=_elements())
    app.set_mode(PolicyMode.ASSIST)
    app.perceive()
    capture.add_patch(200, 300, 400, 150, level=220)
    brain = ScriptedBrain(
        [model_turn(calls=[("click", {"target": "Send"})]), model_turn(text="done")]
    )

    loop = app.build_loop(brain)
    result = loop.run("click Send")

    assert result.tool_calls == 1
    assert result.failed == 0
    assert result.verified == 1
    assert backend.events


def test_run_task_through_an_injected_brain_uses_the_whole_body(app_factory: AppFactory) -> None:
    """One task, one manual tool call, one verified action, no leftover loop."""
    app, capture, _ = app_factory(elements=_elements())
    app.set_mode(PolicyMode.ASSIST)
    app.perceive()
    capture.add_patch(200, 300, 400, 150, level=220)
    brain = ScriptedBrain(
        [model_turn(calls=[("click", {"target": "Send"})]), model_turn(text="done")]
    )

    result = app.run_task("click Send", adapter=brain)

    assert result.halted is False
    assert result.verified == 1
    # The loop is unregistered when the task ends, so a stop cannot find a stale one.
    assert app._loops == []


def test_status_is_a_measurement_with_honest_notes(app_factory: AppFactory) -> None:
    """Section 71: the GUI reads facts here, including the ones that are not good."""
    app, _, _ = app_factory(elements=_elements())
    app.perceive()

    report = app.status()

    assert report["started"] is True
    assert report["session_type"]
    # The input transport is named as the one that was actually wired.
    assert report["startup"]["input_backend"] == app.backend.name
    assert report["startup"]["input_backend"]
    state = report["state"]
    assert isinstance(state, dict)
    assert state["element_count"] == 1
    assert state["frame_id"] == 0
    assert state["fresh"] in {True, False}
    assert isinstance(report["perception"]["cycles"], int)
    assert isinstance(report["startup"]["notes"], list)


def test_a_missing_brain_is_reported_with_its_reason_never_as_ready(app_factory: AppFactory) -> None:
    """An unconnected Brain is a reason, not an empty success."""
    app, _, _ = app_factory()

    brain = app.status()["brain"]
    assert isinstance(brain, dict)
    if brain["adapter"] is None:
        assert brain["reason"]


def test_shutdown_stops_the_watchdog_and_uninstalls_the_crash_guard(tmp_path: Path) -> None:
    """Section 64: a clean exit leaves no heartbeat thread and no signal hook."""
    settings = Settings()
    backend = ScriptedCaptureBackend()
    frame_engine = FrameEngine(settings.capture, backend_factory=lambda: backend)
    from core.accessibility import AccessibilityService

    app = BlaxcyApplication(
        settings,
        collect_capabilities=False,
        with_watchdog=True,
        frame_engine=frame_engine,
        accessibility=AccessibilityService(
            settings.accessibility, backend_factory=lambda: ScriptedA11yBackend(())
        ),
        backend=FakeInputBackend(),
        clipboard=None,
        window_manager=FakeWindows(),
        watchdog_path=tmp_path / "watchdog.json",
    )
    app.start()
    try:
        assert app.watchdog is not None
        assert app.watchdog.running is True
        assert app.crash_guard is not None
        assert app.crash_guard.installed is True
    finally:
        app.shutdown()
        assert app.watchdog is not None
        assert app.watchdog.running is False
        assert app.crash_guard is not None
        assert app.crash_guard.installed is False
        assert app.startup.started is False


def test_build_loop_reports_backend_unavailable_without_a_brain(app_factory: AppFactory) -> None:
    """Section 80: no Brain yields a structured refusal, never a silent no-op."""
    app, _, _ = app_factory()
    if app.brain_reason is None:  # a key really is stored on this host
        pytest.skip("a Gemini key is present on this host, so a Brain can be built")
    with pytest.raises(BlaxcyError) as error:
        app.build_loop()
    assert error.value.code is ErrorCode.BACKEND_UNAVAILABLE


def test_capabilities_reach_the_state_cache_when_probed() -> None:
    """A probed report is the Body's view of its own abilities (section 28)."""
    settings = Settings()
    report = make_capability_report()
    app = BlaxcyApplication(
        settings,
        collect_capabilities=False,
        capability_report=report,
        with_watchdog=False,
        backend=FakeInputBackend(),
        clipboard=None,
        window_manager=FakeWindows(),
    )
    try:
        assert app.cache.capabilities is not None
        assert app.capability(report.capabilities[0].name) is not None
        assert app.status()["capabilities"] is not None
    finally:
        app.shutdown()
