"""GUI panels (specification sections 28, 63, 65, 71).

These are the read-and-report widgets. The rules under test are the ones that
keep a GUI from becoming a second, optimistic source of truth:

* a fact the Body has not established renders as ``UNKNOWN``, never as an empty
  string (an empty field reads like a success);
* a refusal is displayed as the refusal it was -- a latched stop, a takeover, a
  capability that is DEGRADED with its reason and fix hint;
* the action log is a bounded view of the *one* bus, so it cannot grow without
  bound or report an event that never happened;
* the stop UI reports what the stop returned, including latency and failures.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from core.event_bus import EventBus
from gui.action_log import ActionLog, format_event
from gui.capability_panel import NOT_PROBED, CapabilityPanel
from gui.emergency_stop_ui import STOP_SHORTCUT, EmergencyStopUI, describe_stop
from gui.status_panel import UNKNOWN, StatusPanel, summarise_status
from schemas.capability import Capability, CapabilityReport
from schemas.enums import CapabilityName, CapabilityStatus, VerificationState
from schemas.events import Event, EventType
from tests.harness.gui import pump

pytestmark = pytest.mark.gui


def _report(**overrides: Any) -> dict[str, Any]:
    """A status report shaped like ``BlaxcyApplication.status()``."""
    report: dict[str, Any] = {
        "started": True,
        "session_type": "x11",
        "mode": "OBSERVE",
        "mode_status": {
            "mode": "OBSERVE",
            "configured_mode": "OBSERVE",
            "autonomous_expires_in_seconds": None,
            "autonomous_active": False,
            "forced": False,
            "reason": None,
        },
        "emergency_stop": {"latched": False, "triggers": 0, "last_latency_ms": None},
        "takeover": {"active": False, "reason": None},
        "sequence": None,
        "brain": {"adapter": None, "reason": "BACKEND_UNAVAILABLE: no key stored"},
        "state": {
            "frame_id": 12,
            "state_version": 4,
            "generation": 2,
            "age_ms": 41.5,
            "fresh": True,
            "element_count": 63,
            "active_window_id": 5,
            "active_app": "xfce4-terminal",
            "accepted_updates": 4,
            "rejected_updates": 0,
        },
        "startup": {"notes": []},
    }
    report.update(overrides)
    return report


# -- The honest-rendering rules ------------------------------------------------


def test_an_absent_report_renders_every_row_as_unknown() -> None:
    """Missing data is UNKNOWN, never an empty success."""
    rows = summarise_status(None)
    assert rows
    assert set(rows.values()) == {UNKNOWN}


def test_false_renders_as_no_and_none_renders_as_unknown() -> None:
    """A false fact is a fact; an absent one is not a fact."""
    rows = summarise_status(_report(started=False))
    assert rows["started"] == "no"
    rows = summarise_status(_report(verification=None, state={}))
    assert rows["verification"] == UNKNOWN
    assert rows["state"] == UNKNOWN


def test_a_latched_stop_and_a_takeover_are_shown_as_the_error_state() -> None:
    """Section 71's error state reports the real override in force."""
    latched = summarise_status(
        _report(emergency_stop={"latched": True, "triggers": 2, "last_latency_ms": 0.4})
    )
    assert latched["error"] == "EMERGENCY_STOP_ACTIVE"
    taken_over = summarise_status(
        _report(takeover={"active": True, "reason": "operator sat down"})
    )
    assert "HUMAN_TAKEOVER" in taken_over["error"]
    assert "operator sat down" in taken_over["error"]


def test_a_stale_state_is_labelled_stale_not_fresh() -> None:
    """Freshness is displayed from the Body's verdict, not recomputed here."""
    rows = summarise_status(_report(state={**_report()["state"], "fresh": False}))
    assert "STALE" in rows["state"]


# -- Status panel --------------------------------------------------------------


def test_the_status_panel_starts_unknown_and_then_shows_the_body(qt_app: Any) -> None:
    panel = StatusPanel()
    assert panel.value("mode") == UNKNOWN
    panel.update_status(_report(mode="ASSIST", verification=None))
    assert panel.value("mode") == "ASSIST"
    assert panel.value("session") == "x11"
    try:
        del qt_app
    finally:
        panel.deleteLater()


def test_the_sequence_indicator_reports_step_progress_and_halts(qt_app: Any) -> None:
    """Section 71: a batched run must never be an opaque multi-second wait."""
    panel = StatusPanel()
    try:
        progress = {
            "sequence_id": "seq-1",
            "current_index": 2,
            "total_steps": 5,
            "completed_indices": (0, 1),
            "halt_reason": None,
        }
        panel.update_status(_report(sequence=progress))
        text = panel.sequence.label_text()
        assert "step 3 of 5" in text
        assert "2 completed" in text

        halted = {**progress, "halt_reason": "AMBIGUOUS"}
        panel.update_status(_report(sequence=halted))
        assert "HALTED" in panel.sequence.label_text()
        assert "AMBIGUOUS" in panel.sequence.label_text()

        done = {
            "sequence_id": "seq-1",
            "current_index": 5,
            "total_steps": 5,
            "completed_indices": (0, 1, 2, 3, 4),
            "halt_reason": None,
        }
        panel.update_status(_report(sequence=done))
        assert "complete" in panel.sequence.label_text()

        panel.update_status(_report(sequence=None))
        assert "no sequence" in panel.sequence.label_text()
    finally:
        panel.deleteLater()


# -- Capability panel ----------------------------------------------------------


def _capability(
    name: CapabilityName, status: CapabilityStatus, *, reason: str | None = None
) -> Capability:
    return Capability(
        name=name,
        status=status,
        backend="xtest" if status is CapabilityStatus.AVAILABLE else None,
        latency_ms=5.0,
        reason=reason,
        fix_hint="install the backend" if reason else None,
    )


def test_the_capability_panel_says_not_probed_rather_than_showing_nothing(
    qt_app: Any,
) -> None:
    panel = CapabilityPanel()
    try:
        panel.set_report(None)
        assert panel.summary_text() == NOT_PROBED
        assert panel.row_count() == 0
    finally:
        panel.deleteLater()


def test_the_capability_panel_shows_the_verdict_and_its_evidence(qt_app: Any) -> None:
    report = CapabilityReport(
        generated_at=1.0,
        session_type="x11",
        capabilities=(
            _capability(CapabilityName.MOUSE, CapabilityStatus.AVAILABLE),
            _capability(
                CapabilityName.OCR,
                CapabilityStatus.DEGRADED,
                reason="tesseract missing",
            ),
        ),
    )
    panel = CapabilityPanel()
    try:
        panel.set_report(report)
        assert panel.row_count() == 2
        assert panel.cell(0, 0) == "mouse"
        assert panel.cell(0, 1) == "AVAILABLE"
        assert panel.cell(1, 1) == "DEGRADED"
        assert panel.cell(1, 4) == "tesseract missing"
        assert panel.cell(1, 5) == "install the backend"
        assert panel.summary() == {"AVAILABLE": 1, "DEGRADED": 1, "UNAVAILABLE": 0}
    finally:
        panel.deleteLater()


# -- Action log ----------------------------------------------------------------


def test_the_action_log_formats_an_event_with_its_type_and_payload() -> None:
    event = Event.create(
        EventType.VERIFICATION_RESULT,
        {"verification": VerificationState.VERIFIED.value, "tool": "click"},
    )
    line = format_event(event)
    assert "VERIFICATION_RESULT" in line
    assert "verification=VERIFIED" in line
    assert "tool=click" in line


def test_the_action_log_is_bounded(qt_app: Any) -> None:
    """A long session must not grow an unbounded widget."""
    log = ActionLog(max_lines=5)
    try:
        for index in range(10):
            log.append(Event.create(EventType.STATE_UPDATED, {"i": index}))
        pump(times=4)
        assert log.line_count() <= 5
        body = log.text()
        assert "i=9" in body
        assert "i=0" not in body, "the oldest entries should have been dropped"
    finally:
        log.deleteLater()


def test_the_action_log_observes_the_one_bus(qt_app: Any) -> None:
    """The log is a view of the single event stream, not a parallel account."""
    bus = EventBus()
    log = ActionLog(max_lines=50)
    try:
        log.attach(bus)
        bus.publish(Event.create(EventType.MODE_CHANGED, {"mode": "ASSIST"}))
        bus.publish(Event.create(EventType.SEQUENCE_HALTED, {"halt_reason": "AMBIGUOUS"}))
        pump(times=4)
        assert "MODE_CHANGED" in log.event_types_seen()
        assert "SEQUENCE_HALTED" in log.event_types_seen()
        log.detach()
        bus.publish(Event.create(EventType.ERROR, {"message": "after detach"}))
        pump(times=3)
        assert "ERROR" not in log.event_types_seen()
    finally:
        log.deleteLater()


def test_appending_from_another_thread_does_not_touch_the_widget_directly(
    qt_app: Any,
) -> None:
    """Section 71: only the GUI thread modifies widgets (the log marshals)."""
    import threading

    log = ActionLog(max_lines=20)
    try:
        thread = threading.Thread(
            target=lambda: log.append(Event.create(EventType.ERROR, {"message": "x"})),
            daemon=True,
        )
        thread.start()
        thread.join(timeout=2.0)
        assert not thread.is_alive()
        pump(times=4)
        assert "ERROR" in log.event_types_seen()
    finally:
        log.deleteLater()


# -- Emergency stop UI ---------------------------------------------------------


@dataclass
class _StopResult:
    """A stand-in for ``EmergencyStopResult`` (only the fields the UI reads)."""

    latched: bool = True
    latency_ms: float = 0.42
    released_buttons: tuple[str, ...] = ("button1",)
    released_keys: tuple[str, ...] = ("Control_L",)
    errors: tuple[str, ...] = ()
    already_latched: bool = False
    forced_observe: bool = True
    cancelled: int = 1
    steps: tuple[tuple[str, bool], ...] = field(default_factory=tuple)


def test_describe_stop_reports_the_measured_result() -> None:
    text = describe_stop(_StopResult())
    assert "latched" in text
    assert "0.42 ms" in text
    assert "released 1 buttons" in text


def test_describe_stop_never_claims_a_stop_that_did_not_latch() -> None:
    text = describe_stop(_StopResult(latched=False))
    assert "DID NOT LATCH" in text


def test_describe_stop_surfaces_failed_release_steps() -> None:
    """A stuck key is the failure that matters, so it must be visible."""
    text = describe_stop(_StopResult(errors=("release_keys: RuntimeError",)))
    assert "FAILED STEPS" in text
    assert "release_keys" in text


def test_the_stop_button_calls_the_body_and_shows_the_latency(qt_app: Any) -> None:
    calls: list[str] = []

    def on_stop(reason: str) -> Any:
        calls.append(reason)
        return _StopResult()

    ui = EmergencyStopUI(on_stop=on_stop, on_reset=lambda: True)
    try:
        assert ui.reset_button.isEnabled() is False
        ui.stop_button.click()
        assert len(calls) == 1
        assert ui.latched is True
        assert "0.42 ms" in ui.status_text()
        assert ui.reset_button.isEnabled() is True
    finally:
        ui.deleteLater()


def test_re_arm_is_explicit_and_reports_a_refusal(qt_app: Any) -> None:
    """A stop that cleared itself would not be a stop (section 63)."""
    ui = EmergencyStopUI(on_stop=lambda reason: _StopResult(), on_reset=lambda: False)
    try:
        ui.stop_button.click()
        ui.reset()
        assert ui.latched is True, "the latch survived a refused re-arm"
        assert "still latched" in ui.status_text()
    finally:
        ui.deleteLater()


def test_the_stop_shortcut_is_application_wide(qt_app: Any) -> None:
    """The stop must be reachable wherever focus is."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QKeySequence

    ui = EmergencyStopUI(on_stop=lambda reason: _StopResult(), on_reset=lambda: True)
    try:
        # Compared as a sequence: Qt canonicalises the *display* name
        # (Ctrl+Shift+Esc), which must not be mistaken for a different binding.
        assert ui.stop_shortcut.key() == QKeySequence(STOP_SHORTCUT)
        assert ui.stop_shortcut.context() == Qt.ShortcutContext.ApplicationShortcut
    finally:
        ui.deleteLater()
