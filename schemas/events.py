"""Typed events (specification section 65).

Everything that happens inside BLAXCY is emitted as a typed event onto one bus,
so the GUI action log, the state cache and any future watchdog all observe the
same ordered stream. Sequence events carry the same cancellation semantics as
every other event -- the batching layer adds visibility, never a second path.
"""

from __future__ import annotations

import time
import uuid
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from schemas.enums import ActionClass, ErrorCode, VerificationState


class EventType(StrEnum):
    """The event vocabulary emitted on the BLAXCY event bus."""

    # Perception / state
    CAPTURE_FAILED = "CAPTURE_FAILED"
    SCREEN_CHANGED = "SCREEN_CHANGED"
    STATE_UPDATED = "STATE_UPDATED"
    LAYOUT_CHANGED = "LAYOUT_CHANGED"

    # Execution
    ACTION_STARTED = "ACTION_STARTED"
    ACTION_COMPLETED = "ACTION_COMPLETED"
    VERIFICATION_RESULT = "VERIFICATION_RESULT"
    ERROR = "ERROR"

    # Leases
    LEASE_ISSUED = "LEASE_ISSUED"
    LEASE_REJECTED = "LEASE_REJECTED"

    # Safety / control
    MODE_CHANGED = "MODE_CHANGED"
    EMERGENCY_STOP = "EMERGENCY_STOP"
    HUMAN_TAKEOVER = "HUMAN_TAKEOVER"
    CONFIRMATION_REQUESTED = "CONFIRMATION_REQUESTED"
    CONFIRMATION_RESOLVED = "CONFIRMATION_RESOLVED"

    # Capability / Brain
    CAPABILITY_CHANGED = "CAPABILITY_CHANGED"
    BRAIN_CONNECTED = "BRAIN_CONNECTED"
    BRAIN_DISCONNECTED = "BRAIN_DISCONNECTED"

    # Sequences (section 66.1)
    SEQUENCE_STARTED = "SEQUENCE_STARTED"
    SEQUENCE_STEP_COMPLETED = "SEQUENCE_STEP_COMPLETED"
    SEQUENCE_HALTED = "SEQUENCE_HALTED"
    SEQUENCE_COMPLETED = "SEQUENCE_COMPLETED"


class Event(BaseModel):
    """The base envelope every BLAXCY event shares."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str = Field(min_length=1)
    event_type: EventType
    timestamp: float = Field(description="Unix timestamp (seconds) the event was emitted.")
    payload: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def create(cls, event_type: EventType, payload: dict[str, Any] | None = None) -> Event:
        """Create a generic event with a fresh id and current timestamp."""
        return cls(
            event_id=uuid.uuid4().hex,
            event_type=event_type,
            timestamp=time.time(),
            payload=payload or {},
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON-shaped event for logging and the GUI action log."""
        return {
            "event_id": self.event_id,
            "event_type": self.event_type.value,
            "timestamp": self.timestamp,
            "payload": self.payload,
        }


class SequenceEvent(Event):
    """Shared fields for the four sequence lifecycle events."""

    sequence_id: str = Field(min_length=1)
    step_index: int = Field(ge=0)
    total_steps: int = Field(ge=1)


class SequenceStartedEvent(SequenceEvent):
    """Emitted once when a ``run_sequence`` begins executing."""

    event_type: Literal[EventType.SEQUENCE_STARTED] = EventType.SEQUENCE_STARTED
    action_class: ActionClass = ActionClass.READ_ONLY


class SequenceStepCompletedEvent(SequenceEvent):
    """Emitted after each sequence step resolves, verified or not."""

    event_type: Literal[EventType.SEQUENCE_STEP_COMPLETED] = EventType.SEQUENCE_STEP_COMPLETED
    step_id: str = Field(min_length=1)
    ok: bool
    verification: VerificationState = VerificationState.NOT_APPLICABLE


class SequenceHaltedEvent(SequenceEvent):
    """Emitted when a sequence stops before delivering every step."""

    event_type: Literal[EventType.SEQUENCE_HALTED] = EventType.SEQUENCE_HALTED
    halt_reason: str | None = None
    error_code: ErrorCode | None = None
    completed_count: int = Field(default=0, ge=0)


class SequenceCompletedEvent(SequenceEvent):
    """Emitted when a sequence runs to completion with no halt."""

    event_type: Literal[EventType.SEQUENCE_COMPLETED] = EventType.SEQUENCE_COMPLETED
    completed_count: int = Field(ge=0)


class ModeChangedEvent(Event):
    """Emitted whenever the policy mode changes (including forced OBSERVE)."""

    event_type: Literal[EventType.MODE_CHANGED] = EventType.MODE_CHANGED
    previous_mode: str
    current_mode: str
    reason: str | None = None


class EmergencyStopEvent(Event):
    """Emitted when the emergency stop latches (section 63)."""

    event_type: Literal[EventType.EMERGENCY_STOP] = EventType.EMERGENCY_STOP
    reason: str | None = None


class HumanTakeoverEvent(Event):
    """Emitted when a human takes control (section 62)."""

    event_type: Literal[EventType.HUMAN_TAKEOVER] = EventType.HUMAN_TAKEOVER
    reason: str | None = None
