"""Tool dispatch: the read path, the executor path and the limits (section 66).

The dispatcher is the seam the whole Brain integration rests on, so these tests
check three things at once: that each read-only tool reports what BLAXCY really
observes, that read-only work never touches the executor, and that the read path
stays bounded and preemptable (section 32.1).
"""

from __future__ import annotations

import threading

from ai.tool_protocol import ToolCall, ToolDispatcher
from config.settings import PerformanceSettings, Settings
from schemas.actions import ToolName
from schemas.enums import ChangeClass, ErrorCode, UIRole, VerificationState
from tests.harness.phase8 import make_delta, make_element, make_state
from tests.harness.phase10 import FakeWindows, build_dispatch_env


def _state_with_buttons() -> object:
    """A state with one button and one password field."""
    return make_state(
        elements=(
            make_element("e1", text="Send", clickable=True, effective_clickable=True),
            make_element(
                "pw",
                role=UIRole.PASSWORD_INPUT,
                password=True,
                text=None,
                accessible_name="Password",
                clickable=True,
                effective_clickable=True,
            ),
        )
    )


# -- get_screen_state ---------------------------------------------------------

def test_get_screen_state_reports_stamps_and_elements() -> None:
    """A read of the desktop carries the stamps an action would need."""
    env = build_dispatch_env(state=_state_with_buttons())
    envelope = env.dispatch(ToolName.GET_SCREEN_STATE)
    assert envelope.ok is True
    # The state cache is the authority on state_version, so the stamps are
    # present and consistent rather than an echo of the constructed input.
    assert envelope.state_version is not None
    assert envelope.frame_id == 5
    data = envelope.data
    assert data["state"]["element_count"] == 2
    assert len(data["elements"]) == 2
    assert data["stale"] is False
    assert data["max_action_state_age_ms"] == 1500
    assert data["monitors"][0]["width"] == 1920


def test_get_screen_state_never_includes_a_password_value() -> None:
    """Section 42/55: a credential field's content is structurally absent."""
    env = build_dispatch_env(state=_state_with_buttons())
    envelope = env.dispatch(ToolName.GET_SCREEN_STATE)
    password_entry = next(e for e in envelope.data["elements"] if e["password"])
    assert password_entry["text"] is None
    assert password_entry["accessible_name"] == "Password"


def test_get_screen_state_can_omit_elements_entirely() -> None:
    """A caller that only needs stamps does not have to pay for a dump."""
    env = build_dispatch_env(state=_state_with_buttons())
    envelope = env.dispatch(ToolName.GET_SCREEN_STATE, include_elements=False)
    assert "elements" not in envelope.data


def test_get_screen_state_without_an_observation_is_honest() -> None:
    """No perceived state yet is CAPTURE_FAILED, not an empty screen."""
    env = build_dispatch_env()
    envelope = env.dispatch(ToolName.GET_SCREEN_STATE)
    assert envelope.ok is False
    assert envelope.error_code is ErrorCode.CAPTURE_FAILED


def test_get_screen_state_caps_the_element_count() -> None:
    """The declared cap is enforced, not merely documented."""
    elements = tuple(
        make_element(f"e{index}", text=f"button {index}") for index in range(80)
    )
    env = build_dispatch_env(state=make_state(elements=elements))
    envelope = env.dispatch(ToolName.GET_SCREEN_STATE)
    assert len(envelope.data["elements"]) <= 60
    assert envelope.data["elements_truncated"] is True


# -- find_element -------------------------------------------------------------

def test_find_element_resolves_without_acting() -> None:
    """A read resolves and reports scores; it does not click anything."""
    env = build_dispatch_env(state=_state_with_buttons())
    envelope = env.dispatch(ToolName.FIND_ELEMENT, target="Send")
    assert envelope.ok is True
    assert envelope.data["resolution"]["status"] == "RESOLVED"
    assert env.env.backend.events == []


def test_find_element_reports_ambiguity_as_a_failure() -> None:
    """Two equal candidates are AMBIGUOUS, never a silent pick (section 43)."""
    state = make_state(
        elements=(
            make_element("a", text="Open"),
            make_element("b", text="Open"),
        )
    )
    env = build_dispatch_env(state=state)
    envelope = env.dispatch(ToolName.FIND_ELEMENT, target="Open")
    assert envelope.ok is False
    assert envelope.error_code is ErrorCode.TARGET_AMBIGUOUS
    assert len(envelope.data["resolution"]["candidates"]) >= 2


def test_find_element_reports_a_miss_as_not_found() -> None:
    """An absent label is TARGET_NOT_FOUND."""
    env = build_dispatch_env(state=_state_with_buttons())
    envelope = env.dispatch(ToolName.FIND_ELEMENT, target="Nonexistent widget")
    assert envelope.ok is False
    assert envelope.error_code is ErrorCode.TARGET_NOT_FOUND


# -- describe_region ----------------------------------------------------------

def test_describe_region_reports_perceived_elements_and_text() -> None:
    """A region description reports what accessibility really observed."""
    env = build_dispatch_env(state=_state_with_buttons())
    envelope = env.dispatch(ToolName.DESCRIBE_REGION, x=180, y=100, width=200, height=80)
    assert envelope.ok is True
    assert envelope.data["element_count"] >= 1
    assert "Send" in envelope.data["text_fragments"]
    # Phase 11's model-based description is honestly reported as unavailable.
    assert envelope.data["visual_description"]["available"] is False


def test_describe_region_outside_any_element_is_empty_not_invented() -> None:
    """Nothing perceived means nothing reported."""
    env = build_dispatch_env(state=_state_with_buttons())
    envelope = env.dispatch(ToolName.DESCRIBE_REGION, x=1500, y=900, width=10, height=10)
    assert envelope.ok is True
    assert envelope.data["element_count"] == 0
    assert envelope.data["elements"] == []


def test_describe_region_requires_positive_dimensions() -> None:
    """A zero-area region is a malformed request."""
    env = build_dispatch_env(state=_state_with_buttons())
    envelope = env.dispatch(ToolName.DESCRIBE_REGION, x=0, y=0, width=0, height=10)
    assert envelope.ok is False
    assert envelope.error_code is ErrorCode.INTERNAL_ERROR


# -- window / capability / stop ----------------------------------------------

def test_get_active_window_reports_window_and_state_view() -> None:
    """The window tool reports both the WM answer and the perceived state."""
    env = build_dispatch_env(state=_state_with_buttons())
    envelope = env.dispatch(ToolName.GET_ACTIVE_WINDOW)
    assert envelope.ok is True
    assert envelope.data["window_id"] == 42
    assert envelope.data["state_active_window_id"] == 42


def test_get_active_window_degrades_honestly_on_a_wm_failure() -> None:
    """A window-manager failure is structured, never a fabricated window."""
    env = build_dispatch_env(
        state=_state_with_buttons(), windows=FakeWindows(fail=ErrorCode.BACKEND_UNAVAILABLE)
    )
    envelope = env.dispatch(ToolName.GET_ACTIVE_WINDOW)
    assert envelope.ok is False
    assert envelope.error_code is ErrorCode.BACKEND_UNAVAILABLE


def test_get_active_window_without_a_window_manager_says_so() -> None:
    """Unavailable is reported, not assumed away."""
    env = build_dispatch_env(with_windows=False)
    envelope = env.dispatch(ToolName.GET_ACTIVE_WINDOW)
    assert envelope.ok is False
    assert envelope.error_code is ErrorCode.BACKEND_UNAVAILABLE


def test_get_capabilities_returns_the_evidence_bearing_report() -> None:
    """Capabilities travel with their status and reason (section 28)."""
    env = build_dispatch_env()
    envelope = env.dispatch(ToolName.GET_CAPABILITIES)
    assert envelope.ok is True
    names = [entry["name"] for entry in envelope.data["capabilities"]["capabilities"]]
    assert "capture" in names


def test_get_capabilities_without_a_report_is_refused() -> None:
    """Better to say "probe first" than to guess the capability states."""
    env = build_dispatch_env(with_capabilities=False)
    envelope = env.dispatch(ToolName.GET_CAPABILITIES)
    assert envelope.ok is False


def test_emergency_stop_tool_uses_a_real_latched_stop() -> None:
    """The Brain's stop request runs the real, latched section 63 sequence."""
    from control.emergency_stop import EmergencyStop

    stop = EmergencyStop()
    env = build_dispatch_env(state=_state_with_buttons())
    dispatcher = ToolDispatcher(
        env.settings,
        executor=env.env.executor,
        state_cache=env.cache,
        resolver=env.env.resolver,
        emergency_stop=stop,
        clock=env.clock,
        sleep=env.clock.sleep,
    )
    envelope = dispatcher.dispatch(
        ToolCall(name=ToolName.EMERGENCY_STOP, arguments={"reason": "brain request"})
    )
    assert envelope.ok is True
    assert stop.latched is True
    assert envelope.data["stop"]["latched"] is True


def test_emergency_stop_without_a_wired_stop_refuses() -> None:
    """BLAXCY never reports a stop it did not perform."""
    env = build_dispatch_env()
    envelope = env.dispatch(ToolName.EMERGENCY_STOP)
    assert envelope.ok is False
    assert envelope.error_code is ErrorCode.BACKEND_UNAVAILABLE


# -- waits --------------------------------------------------------------------

def test_wait_for_change_reports_a_real_observed_change() -> None:
    """A change is reported from an accepted observation and its delta."""
    env = build_dispatch_env(state=make_state())
    before = env.cache.current
    assert before is not None
    after = make_state(frame_id=6, state_version=4, elements=(make_element("e1"),))
    env.env.perceiver.script(after, make_delta(before=before, after=after))
    envelope = env.dispatch(ToolName.WAIT_FOR_CHANGE, timeout_ms=500)
    assert envelope.ok is True
    assert envelope.data["changed"] is True
    assert envelope.data["change_class"] == ChangeClass.MEANINGFUL.value


def test_wait_for_change_times_out_honestly() -> None:
    """No change within the timeout is ``changed: false``, not an error."""
    env = build_dispatch_env(state=make_state())
    envelope = env.dispatch(ToolName.WAIT_FOR_CHANGE, timeout_ms=200)
    assert envelope.ok is True
    assert envelope.data["changed"] is False
    assert env.clock.total_slept > 0.0


def test_wait_for_change_rejects_an_unknown_change_class() -> None:
    """The minimum class is validated against the section 34 vocabulary."""
    env = build_dispatch_env(state=make_state())
    envelope = env.dispatch(ToolName.WAIT_FOR_CHANGE, min_change_class="HUGE")
    assert envelope.ok is False
    assert envelope.error_code is ErrorCode.INTERNAL_ERROR


def test_wait_for_element_finds_a_later_observation() -> None:
    """The wait resolves against a fresh observation, not the stale one."""
    env = build_dispatch_env(state=make_state(elements=()))
    later = make_state(
        frame_id=7,
        state_version=5,
        elements=(make_element("e1", text="Play"),),
    )
    env.env.perceiver.script(later)
    envelope = env.dispatch(ToolName.WAIT_FOR_ELEMENT, target="Play", timeout_ms=500)
    assert envelope.ok is True
    assert envelope.data["found"] is True


def test_wait_for_element_times_out_honestly() -> None:
    """An element that never appears is ``found: false``."""
    env = build_dispatch_env(state=make_state(elements=()))
    envelope = env.dispatch(ToolName.WAIT_FOR_ELEMENT, target="Ghost", timeout_ms=300)
    assert envelope.ok is True
    assert envelope.data["found"] is False


# -- Safety properties of the read path --------------------------------------

def test_read_only_tools_never_reach_the_executor() -> None:
    """The single-writer executor only ever sees work that needs it (section 32.1)."""
    env = build_dispatch_env(state=_state_with_buttons())
    before = env.env.executor.stats()["actions"]
    for name, args in (
        (ToolName.GET_SCREEN_STATE, {}),
        (ToolName.FIND_ELEMENT, {"target": "Send"}),
        (ToolName.GET_ACTIVE_WINDOW, {}),
        (ToolName.GET_CAPABILITIES, {}),
        (ToolName.DESCRIBE_REGION, {"x": 0, "y": 0, "width": 10, "height": 10}),
        (ToolName.WAIT_FOR_CHANGE, {"timeout_ms": 0}),
        (ToolName.WAIT_FOR_ELEMENT, {"target": "Send", "timeout_ms": 0}),
    ):
        assert env.dispatch(name, **args) is not None
    assert env.env.executor.stats()["actions"] == before
    assert env.env.backend.events == []


def test_a_latched_stop_preempts_a_read_only_call() -> None:
    """A read is preemptable exactly like any other in-flight work (section 32.1)."""
    env = build_dispatch_env(state=_state_with_buttons(), abort_check=lambda: ErrorCode.EMERGENCY_STOP_ACTIVE)
    envelope = env.dispatch(ToolName.GET_SCREEN_STATE)
    assert envelope.ok is False
    assert envelope.error_code is ErrorCode.EMERGENCY_STOP_ACTIVE


def test_read_only_dispatch_is_bounded_by_configuration() -> None:
    """``max_concurrent_readonly_dispatch`` is enforced, not decorative."""
    settings = Settings(performance=PerformanceSettings(max_concurrent_readonly_dispatch=1))
    env = build_dispatch_env(settings=settings, state=_state_with_buttons())
    release = threading.Event()
    entered = threading.Event()

    def blocking_capabilities() -> object:
        entered.set()
        release.wait(timeout=5.0)
        return env.capabilities

    dispatcher = ToolDispatcher(
        settings,
        executor=env.env.executor,
        state_cache=env.cache,
        resolver=env.env.resolver,
        capabilities=blocking_capabilities,  # type: ignore[arg-type]
        clock=env.clock,
        sleep=env.clock.sleep,
    )
    holder = threading.Thread(
        target=lambda: dispatcher.dispatch(ToolCall(name=ToolName.GET_CAPABILITIES, arguments={}))
    )
    try:
        holder.start()
        assert entered.wait(timeout=5.0)
        # The single slot is taken, so a second read must be refused rather than
        # queued forever.
        import ai.tool_protocol as protocol

        original = protocol.READ_SLOT_TIMEOUT_SECONDS
        protocol.READ_SLOT_TIMEOUT_SECONDS = 0.0
        try:
            envelope = dispatcher.dispatch(ToolCall(name=ToolName.GET_SCREEN_STATE, arguments={}))
        finally:
            protocol.READ_SLOT_TIMEOUT_SECONDS = original
        assert envelope.ok is False
        assert envelope.error_code is ErrorCode.RATE_LIMITED
        assert dispatcher.stats.read_only_rejections == 1
    finally:
        release.set()
        holder.join(timeout=5.0)


def test_dispatch_stats_are_recorded_per_tool() -> None:
    """Dispatch keeps an auditable per-tool count."""
    env = build_dispatch_env(state=_state_with_buttons())
    env.dispatch(ToolName.GET_SCREEN_STATE)
    env.dispatch(ToolName.FIND_ELEMENT, target="Send")
    stats = env.dispatcher.stats.to_dict()
    assert stats["calls"][ToolName.GET_SCREEN_STATE] == 1
    assert stats["read_only_calls"] == 2
    assert stats["read_only_peak"] == 1


# -- Mutating tools go through the executor ----------------------------------

def test_a_mutating_tool_is_routed_through_the_executor() -> None:
    """An action tool enters the section 59 pipeline, never a direct input call."""
    from schemas.enums import PolicyMode

    env = build_dispatch_env(state=_state_with_buttons(), mode=PolicyMode.ASSIST)
    before = env.env.executor.stats()["actions"]
    envelope = env.dispatch(ToolName.ENSURE_WINDOW, window_id=42)
    assert env.env.executor.stats()["actions"] == before + 1
    # The executor has no window manager wired here, so it refuses honestly
    # rather than pretending the window was activated.
    assert envelope.ok is False
    assert envelope.error_code is ErrorCode.BACKEND_UNAVAILABLE


def test_an_unknown_argument_never_reaches_the_desktop() -> None:
    """A malformed call is refused by the dispatcher, not half-applied."""
    from schemas.enums import PolicyMode

    env = build_dispatch_env(state=_state_with_buttons(), mode=PolicyMode.ASSIST)
    before = env.env.executor.stats()["actions"]
    envelope = env.dispatch(ToolName.CLICK, target="Send", force=True)
    assert envelope.ok is False
    assert envelope.error_code is ErrorCode.INTERNAL_ERROR
    assert env.env.executor.stats()["actions"] == before
    assert env.env.backend.events == []


def test_verification_state_is_never_invented_for_read_only_work() -> None:
    """A read has no postcondition, so it reports NOT_APPLICABLE."""
    env = build_dispatch_env(state=_state_with_buttons())
    envelope = env.dispatch(ToolName.GET_SCREEN_STATE)
    assert envelope.verification is VerificationState.NOT_APPLICABLE
