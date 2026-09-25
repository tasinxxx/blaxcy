"""Confirmation and the thread bridge (specification sections 54, 56, 71).

The confirmation seam sits directly in front of a physical action: the executor
blocks on it while holding the single-writer action slot. These tests assert the
properties that make that safe:

* the countdown floor is a constant that cannot be configured away, and Confirm
  starts locked;
* Return cannot confirm on the user's behalf (no implicit default, auto-default
  off);
* **every** failure mode denies -- no ``QApplication``, a timeout, a second
  concurrent request, an exception raised inside the dialog, a closed bridge;
* a stop or a takeover while a dialog is open refuses the pending request,
  because the human has just overridden the question;
* credential-like content is never rendered into the dialog.

Nothing here injects input or opens a real dialog: the dialog factory is
injected, so the bridge's decision logic is tested without Qt timing.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from core.event_bus import EventBus
from gui.confirmation import (
    DEFAULT_TIMEOUT_SECONDS,
    MIN_COUNTDOWN_SECONDS,
    ConfirmationBridge,
    ConfirmationDialog,
    ConfirmationRequest,
)
from schemas.enums import ErrorCode
from schemas.events import Event, EventType
from tests.harness.gui import FakeClock, FakeDialog, pump, wait_until

pytestmark = pytest.mark.gui


# -- The countdown floor -------------------------------------------------------


def test_the_countdown_floor_is_at_least_three_seconds() -> None:
    """Section 71's minimum is a floor, not a default that can drift downward."""
    assert MIN_COUNTDOWN_SECONDS >= 3.0
    assert DEFAULT_TIMEOUT_SECONDS > 0.0


def test_confirm_starts_locked_and_unlocks_after_the_countdown(qt_app: Any) -> None:
    """Confirm is unusable until the countdown has genuinely elapsed."""
    clock = FakeClock()
    dialog = ConfirmationDialog(ConfirmationRequest.create("delete the file"), clock=clock)
    try:
        # Read into locals: a property re-read after a change is not the same
        # fact, and asserting the same expression twice would make the second
        # read look contradictory to a type checker.
        locked = dialog.confirm_enabled
        assert locked is False
        assert dialog.remaining_seconds == pytest.approx(MIN_COUNTDOWN_SECONDS)
        clock.advance(MIN_COUNTDOWN_SECONDS)
        dialog.refresh_countdown()
        unlocked = dialog.confirm_enabled
        assert unlocked is True
        assert dialog.remaining_seconds == 0.0
    finally:
        dialog.deleteLater()


def test_a_programmatic_confirm_cannot_shortcut_the_countdown(qt_app: Any) -> None:
    """Even a direct click is refused before the floor: the wait is not cosmetic."""
    clock = FakeClock()
    dialog = ConfirmationDialog(ConfirmationRequest.create("run rm -rf"), clock=clock)
    try:
        clock.advance(1.0)
        dialog.refresh_countdown()
        dialog.confirm_button.click()
        inside_countdown = dialog.approved
        assert inside_countdown is False, "Confirm was reachable inside the countdown"
        clock.advance(MIN_COUNTDOWN_SECONDS)
        dialog.refresh_countdown()
        dialog.confirm_button.click()
        after_countdown = dialog.approved
        assert after_countdown is True
    finally:
        dialog.deleteLater()


def test_confirm_has_no_implicit_default(qt_app: Any) -> None:
    """Return must not be able to press Confirm (section 71)."""
    dialog = ConfirmationDialog(ConfirmationRequest.create("submit the command"))
    try:
        for button in (dialog.confirm_button, dialog.cancel_button):
            assert button.isDefault() is False
            assert button.autoDefault() is False
    finally:
        dialog.deleteLater()


def test_cancelling_never_approves(qt_app: Any) -> None:
    """Cancel (and Escape, which is the same path) denies."""
    dialog = ConfirmationDialog(ConfirmationRequest.create("submit the command"))
    try:
        dialog.cancel_button.click()
        assert dialog.approved is False
    finally:
        dialog.deleteLater()


# -- What the dialog renders ---------------------------------------------------


def test_a_sensitive_payload_hides_text_bearing_fields() -> None:
    """Credential content is never rendered (sections 42, 55, 70)."""
    request = ConfirmationRequest.create(
        "type into the password field",
        {"tool": "type_text", "text": "hunter2", "sensitive": True, "target": "password"},
    )
    rendered = dict(request.display_fields())
    assert "text" not in rendered
    assert "hunter2" not in " ".join(rendered.values())
    assert rendered["tool"] == "type_text"
    assert rendered["target"] == "password"


def test_unknown_detail_keys_are_still_shown() -> None:
    """A confirmation that hid part of what would happen is a worse dialog."""
    request = ConfirmationRequest.create("do it", {"tool": "click", "window": "Fixture"})
    rendered = dict(request.display_fields())
    assert rendered["window"] == "Fixture"


# -- The bridge's failure modes ------------------------------------------------


def test_request_denies_when_no_qapplication_is_available(monkeypatch: Any) -> None:
    """No GUI thread means nobody can answer, so the request is refused at once."""

    class _NoQt:
        @staticmethod
        def instance() -> None:
            return None

    monkeypatch.setattr("gui.confirmation.QApplication", _NoQt)
    bridge = ConfirmationBridge(timeout_seconds=0.05)
    assert bridge.request("delete everything") is False
    assert bridge.stats()["unavailable"] == 1


def test_request_denies_on_timeout(qt_app: Any) -> None:
    """A dialog nobody answers must not hold the action slot forever."""
    bridge = ConfirmationBridge(
        timeout_seconds=0.05, dialog_factory=lambda request: FakeDialog()
    )
    # No event pumping: the queued dialog slot never runs, so this is the timeout.
    assert bridge.request("do it") is False
    assert bridge.stats()["timeouts"] == 1
    assert bridge.stats()["granted"] == 0


def _request_in_thread(
    bridge: ConfirmationBridge, message: str = "do the thing", *, timeout: float = 3.0
) -> bool | None:
    """Call ``bridge.request`` on a worker thread while the GUI thread pumps."""
    result: dict[str, Any] = {}

    def worker() -> None:
        result["value"] = bridge.request(message)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    deadline = time.monotonic() + timeout
    while thread.is_alive() and time.monotonic() < deadline:
        pump(times=1, delay=0.005)
    thread.join(timeout=1.0)
    assert not thread.is_alive(), "the bridge never answered the request"
    return result.get("value")


def test_a_confirmed_dialog_grants(qt_app: Any) -> None:
    """A human approval is what the executor receives -- and nothing is guessed."""
    dialog = FakeDialog(approved=True)
    bridge = ConfirmationBridge(dialog_factory=lambda request: dialog)
    assert _request_in_thread(bridge) is True
    assert dialog.exec_count == 1
    assert dialog.deleted is True
    assert bridge.stats()["granted"] == 1


def test_a_cancelled_dialog_denies(qt_app: Any) -> None:
    """A declined dialog denies, and the dialog is still torn down."""
    dialog = FakeDialog(approved=False)
    bridge = ConfirmationBridge(dialog_factory=lambda request: dialog)
    assert _request_in_thread(bridge) is False
    assert bridge.stats()["denied"] == 1


def test_a_broken_dialog_denies(qt_app: Any) -> None:
    """An exception inside the dialog must deny, never grant."""
    dialog = FakeDialog(approved=True, raises=True)
    bridge = ConfirmationBridge(dialog_factory=lambda request: dialog)
    assert _request_in_thread(bridge) is False
    assert bridge.stats()["denied"] == 1


def test_a_second_concurrent_request_is_refused(qt_app: Any) -> None:
    """One dialog at a time; a second request while one is pending fails closed."""
    bridge = ConfirmationBridge(
        timeout_seconds=5.0, dialog_factory=lambda request: FakeDialog()
    )
    result: dict[str, Any] = {}
    thread = threading.Thread(
        target=lambda: result.update(value=bridge.request("first")), daemon=True
    )
    thread.start()
    try:
        assert wait_until(lambda: bridge.pending), "the first request was never pending"
        assert bridge.request("second") is False
        assert bridge.stats()["busy"] == 1
    finally:
        bridge.deny_pending("test cleanup")
        thread.join(timeout=2.0)
    assert result["value"] is False


# -- Preemption (sections 62, 63) ----------------------------------------------


def _pending_bridge(bus: EventBus) -> tuple[ConfirmationBridge, dict[str, Any], Any]:
    """Start a request that stays pending (no event pumping, so no dialog runs)."""
    bridge = ConfirmationBridge(
        timeout_seconds=5.0, event_bus=bus, dialog_factory=lambda request: FakeDialog()
    )
    result: dict[str, Any] = {}
    thread = threading.Thread(
        target=lambda: result.update(value=bridge.request("submit the command")),
        daemon=True,
    )
    thread.start()
    return bridge, result, thread


def test_a_stop_while_a_dialog_is_pending_refuses_it(qt_app: Any) -> None:
    """Section 63: the human stopping mid-dialog answers the question with "no"."""
    bus = EventBus()
    bridge, result, thread = _pending_bridge(bus)
    try:
        assert wait_until(lambda: bridge.pending)
        bus.publish(Event.create(EventType.EMERGENCY_STOP, {"reason": "test stop"}))
        thread.join(timeout=2.0)
        assert result["value"] is False
        assert bridge.stats()["preempted"] == 1
    finally:
        bridge.close()
        thread.join(timeout=2.0)


def test_a_takeover_while_a_dialog_is_pending_refuses_it(qt_app: Any) -> None:
    """Section 62: a human taking control ends the question too."""
    bus = EventBus()
    bridge, result, thread = _pending_bridge(bus)
    try:
        assert wait_until(lambda: bridge.pending)
        bus.publish(Event.create(EventType.HUMAN_TAKEOVER, {"reason": "test takeover"}))
        thread.join(timeout=2.0)
        assert result["value"] is False
        assert bridge.stats()["preempted"] == 1
    finally:
        bridge.close()
        thread.join(timeout=2.0)


def test_close_refuses_anything_pending(qt_app: Any) -> None:
    """Shutting the bridge down denies rather than leaving a request hanging."""
    bus = EventBus()
    bridge, result, thread = _pending_bridge(bus)
    assert wait_until(lambda: bridge.pending)
    bridge.close()
    thread.join(timeout=2.0)
    assert result["value"] is False
    assert bridge.pending is False


def test_the_bridge_reports_its_own_outcomes(qt_app: Any) -> None:
    """Counters exist so the log can show what was asked and how it was answered."""
    bridge = ConfirmationBridge(dialog_factory=lambda request: FakeDialog(approved=False))
    assert bridge.stats() == {
        "granted": 0,
        "denied": 0,
        "timeouts": 0,
        "preempted": 0,
        "unavailable": 0,
        "busy": 0,
    }
    assert _request_in_thread(bridge) is False
    stats = bridge.stats()
    assert stats["denied"] == 1
    assert stats["granted"] == 0


def test_an_unknown_error_code_is_not_needed_for_a_refusal() -> None:
    """Guard: the refusal path uses a real taxonomy code (sections 77, 54)."""
    assert ErrorCode.CONFIRMATION_DENIED.value == "CONFIRMATION_DENIED"
