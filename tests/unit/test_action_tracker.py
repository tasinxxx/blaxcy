"""Input ownership tracking (specification sections 52, 63, 78).

Ownership must never survive an action, whatever ended it. The tests below cover
the normal exit, the exception exit and the idempotent double-clear that
emergency-stop-during-teardown produces.
"""

from __future__ import annotations

import pytest

from control.action_tracker import ActionTracker
from core.event_bus import EventBus
from schemas.events import EventType
from schemas.leases import issue_lease
from tests.harness.phase8 import make_element


class _Clock:
    """A controllable monotonic clock."""

    def __init__(self, start: float = 500.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _lease() -> object:
    """A real lease for a fixture element."""
    return issue_lease(make_element(), frame_id=1, state_version=2, generation=0, lease_id="L1")


def test_begin_records_the_action_and_its_identifiers() -> None:
    """The active action, lease and task/step/sequence ids are all recorded."""
    tracker = ActionTracker()
    tracker.begin(
        "click",
        lease=_lease(),  # type: ignore[arg-type]
        task_id="t1",
        step_id="s2",
        sequence_id="seq3",
    )
    snapshot = tracker.snapshot()
    assert snapshot.active_action == "click"
    assert snapshot.lease_id == "L1"
    assert snapshot.task_id == "t1"
    assert snapshot.step_id == "s2"
    assert snapshot.sequence_id == "seq3"
    assert snapshot.started_at is not None


def test_held_input_is_visible_in_the_snapshot() -> None:
    """Held keys and buttons are reported so they can be released (section 63)."""
    tracker = ActionTracker()
    tracker.own_key("Control_L")
    tracker.own_key("Shift_L")
    tracker.own_button("left")
    snapshot = tracker.snapshot()
    assert snapshot.held_keys == ("Control_L", "Shift_L")
    assert snapshot.held_buttons == ("left",)
    assert snapshot.holds_input is True


def test_releasing_input_clears_only_that_entry() -> None:
    """Releasing one key leaves the others held."""
    tracker = ActionTracker()
    tracker.own_key("a")
    tracker.own_key("b")
    tracker.release_key("a")
    assert tracker.held_keys == ("b",)
    tracker.release_button("never-held")  # idempotent, no error
    assert tracker.held_buttons == ()


def test_clear_drops_everything_and_reports_what_it_cleared() -> None:
    """A clear removes the action, the lease and all held input."""
    bus = EventBus()
    tracker = ActionTracker(event_bus=bus)
    tracker.begin("type_text", lease=_lease(), task_id="t")  # type: ignore[arg-type]
    tracker.own_key("Shift_L")

    cleared = tracker.clear(reason="done")

    assert cleared.held_keys == ("Shift_L",)
    after = tracker.snapshot()
    assert after.active_action is None
    assert after.lease_id is None
    assert after.holds_input is False
    assert after.cleared_count == 1
    assert len(bus.history_of(EventType.ACTION_COMPLETED)) == 1


def test_clear_is_idempotent() -> None:
    """Clearing twice never raises (emergency stop during teardown)."""
    tracker = ActionTracker()
    tracker.begin("click")
    tracker.clear()
    second = tracker.clear(reason="emergency stop")
    assert second.active_action is None
    assert tracker.snapshot().cleared_count == 2


def test_clearing_nothing_emits_nothing() -> None:
    """A clear with nothing owned is silent but still counted."""
    bus = EventBus()
    tracker = ActionTracker(event_bus=bus)
    tracker.clear()
    assert bus.history_of(EventType.ACTION_COMPLETED) == ()
    assert tracker.snapshot().cleared_count == 1


def test_tracking_context_manager_clears_on_success() -> None:
    """The block form clears ownership on a normal exit."""
    tracker = ActionTracker()
    with tracker.tracking("click", lease=_lease()):  # type: ignore[arg-type]
        tracker.own_key("Control_L")
        assert tracker.active_action == "click"
    assert tracker.snapshot().active_action is None
    assert tracker.held_keys == ()


def test_tracking_context_manager_clears_on_exception() -> None:
    """An unexpected failure must not leave ownership behind."""
    tracker = ActionTracker()
    with pytest.raises(RuntimeError), tracker.tracking("click"):
        tracker.own_button("left")
        raise RuntimeError("boom")
    assert tracker.snapshot().active_action is None
    assert tracker.snapshot().holds_input is False


def test_deadline_is_measured_on_the_injected_clock() -> None:
    """A deadline is real, not decorative."""
    clock = _Clock()
    tracker = ActionTracker(clock=clock)
    tracker.begin("click", deadline_seconds=1.0)
    assert tracker.is_over_deadline() is False
    clock.advance(1.5)
    assert tracker.is_over_deadline() is True


def test_begin_does_not_silently_forget_held_input() -> None:
    """Starting a new action must not drop the record of what is still held."""
    tracker = ActionTracker()
    tracker.own_key("Control_L")
    tracker.begin("click")
    assert tracker.held_keys == ("Control_L",)


def test_attach_lease_updates_the_active_lease_id() -> None:
    """The lease can be attached after the action begins (section 44)."""
    tracker = ActionTracker()
    tracker.begin("click")
    tracker.attach_lease(_lease())  # type: ignore[arg-type]
    assert tracker.active_lease_id == "L1"
