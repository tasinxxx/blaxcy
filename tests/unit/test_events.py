"""Typed events (specification section 65)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from schemas.enums import ActionClass, ErrorCode, VerificationState
from schemas.events import (
    EmergencyStopEvent,
    Event,
    EventType,
    HumanTakeoverEvent,
    ModeChangedEvent,
    SequenceCompletedEvent,
    SequenceHaltedEvent,
    SequenceStartedEvent,
    SequenceStepCompletedEvent,
)


def test_generic_event_creation() -> None:
    """An event always carries an id, a type and a timestamp."""
    event = Event.create(EventType.STATE_UPDATED, {"frame_id": 3})
    payload = event.to_dict()
    assert payload["event_type"] == "STATE_UPDATED"
    assert payload["payload"] == {"frame_id": 3}
    assert event.event_id


def test_sequence_events_carry_progress_fields() -> None:
    """Section 71/65: sequence events expose step index and total."""
    started = SequenceStartedEvent(
        event_id="1",
        timestamp=0.0,
        sequence_id="seq-1",
        step_index=0,
        total_steps=5,
        action_class=ActionClass.MUTATING,
    )
    assert started.event_type is EventType.SEQUENCE_STARTED
    assert started.action_class is ActionClass.MUTATING

    step = SequenceStepCompletedEvent(
        event_id="2",
        timestamp=0.0,
        sequence_id="seq-1",
        step_index=1,
        total_steps=5,
        step_id="s2",
        ok=True,
        verification=VerificationState.VERIFIED,
    )
    assert step.ok is True
    assert step.verification is VerificationState.VERIFIED

    halted = SequenceHaltedEvent(
        event_id="3",
        timestamp=0.0,
        sequence_id="seq-1",
        step_index=2,
        total_steps=5,
        halt_reason="AMBIGUOUS",
        error_code=ErrorCode.TARGET_AMBIGUOUS,
        completed_count=2,
    )
    assert halted.halt_reason == "AMBIGUOUS"

    completed = SequenceCompletedEvent(
        event_id="4",
        timestamp=0.0,
        sequence_id="seq-1",
        step_index=4,
        total_steps=5,
        completed_count=5,
    )
    assert completed.completed_count == 5


def test_safety_events() -> None:
    """Emergency stop, takeover and mode change are first-class events."""
    stop = EmergencyStopEvent(event_id="a", timestamp=0.0, reason="user pressed stop")
    assert stop.event_type is EventType.EMERGENCY_STOP
    takeover = HumanTakeoverEvent(event_id="b", timestamp=0.0)
    assert takeover.event_type is EventType.HUMAN_TAKEOVER
    mode = ModeChangedEvent(
        event_id="c",
        timestamp=0.0,
        previous_mode="AUTONOMOUS",
        current_mode="OBSERVE",
        reason="emergency stop",
    )
    assert mode.to_dict()["payload"] == {}
    assert mode.current_mode == "OBSERVE"


def test_sequence_event_type_cannot_be_overridden_inconsistently() -> None:
    """A subclass's fixed event_type is part of its contract."""
    with pytest.raises(ValidationError):
        SequenceStartedEvent(
            event_id="x",
            timestamp=0.0,
            sequence_id="s",
            step_index=0,
            total_steps=1,
            event_type=EventType.SEQUENCE_HALTED,
        )
