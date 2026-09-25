"""Input ownership tracking (specification sections 52, 63, 78).

BLAXCY must never lose track of what it is currently doing to the desktop, and
it must never walk away from it either. This module is the one record of:

* the action currently executing, and the lease it was authorised by;
* which task/step/sequence it belongs to;
* when it started and when it must be done by;
* every key and button BLAXCY currently holds down.

Ownership is cleared on the *same* triggers in every case -- success, failure,
timeout, cancellation, an unexpected exception, emergency stop, human takeover,
and shutdown -- because a stuck modifier or a stuck mouse button after a crash
is one of the concrete failures section 85 lists. Clearing is idempotent: a
double clear (for example emergency stop during normal teardown) never raises.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from core.event_bus import EventBus
from schemas.leases import ElementLease


@dataclass(frozen=True)
class OwnershipSnapshot:
    """A consistent read of everything currently owned by BLAXCY."""

    active_action: str | None
    lease_id: str | None
    task_id: str | None
    step_id: str | None
    sequence_id: str | None
    started_at: float | None
    deadline: float | None
    held_keys: tuple[str, ...]
    held_buttons: tuple[str, ...]
    cleared_count: int

    @property
    def holds_input(self) -> bool:
        """True when any key or button is still held."""
        return bool(self.held_keys or self.held_buttons)

    def to_dict(self) -> dict[str, Any]:
        """JSON-shaped snapshot for logs and the section 24 report."""
        return {
            "active_action": self.active_action,
            "lease_id": self.lease_id,
            "task_id": self.task_id,
            "step_id": self.step_id,
            "sequence_id": self.sequence_id,
            "started_at": self.started_at,
            "deadline": self.deadline,
            "held_keys": list(self.held_keys),
            "held_buttons": list(self.held_buttons),
            "cleared_count": self.cleared_count,
        }


@dataclass
class _Ownership:
    """Mutable ownership record. Guarded by the tracker's lock."""

    action: str | None = None
    lease_id: str | None = None
    task_id: str | None = None
    step_id: str | None = None
    sequence_id: str | None = None
    started_at: float | None = None
    deadline: float | None = None
    keys: set[str] = field(default_factory=set)
    buttons: set[str] = field(default_factory=set)


class ActionTracker:
    """The single record of BLAXCY's active action and held input (section 78).

    Args:
        event_bus: Optional bus. When supplied, a clearing that actually removed
            ownership is observable through the same stream as everything else.
        clock: Monotonic clock, injectable for deterministic tests.
    """

    def __init__(
        self, *, event_bus: EventBus | None = None, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._lock = threading.RLock()
        self._bus = event_bus
        self._clock = clock
        self._state = _Ownership()
        self._cleared_count = 0

    # -- Beginning ------------------------------------------------------------

    def begin(
        self,
        action: str,
        *,
        lease: ElementLease | None = None,
        task_id: str | None = None,
        step_id: str | None = None,
        sequence_id: str | None = None,
        deadline_seconds: float | None = None,
    ) -> None:
        """Record that ``action`` is starting.

        Beginning a new action replaces the previous record's action/lease
        stamps but deliberately does **not** discard held keys/buttons: those are
        released by :meth:`clear` (through the backend), never forgotten by
        simply overwriting the record.
        """
        now = self._clock()
        with self._lock:
            self._state.action = action
            self._state.lease_id = lease.lease_id if lease is not None else None
            self._state.task_id = task_id
            self._state.step_id = step_id
            self._state.sequence_id = sequence_id
            self._state.started_at = now
            self._state.deadline = None if deadline_seconds is None else now + deadline_seconds

    def attach_lease(self, lease: ElementLease) -> None:
        """Record the lease authorising the current action (section 44)."""
        with self._lock:
            self._state.lease_id = lease.lease_id

    # -- Held input -----------------------------------------------------------

    def own_key(self, key: str) -> None:
        """Record that BLAXCY is holding ``key`` down (sections 52, 63)."""
        with self._lock:
            self._state.keys.add(key)

    def release_key(self, key: str) -> None:
        """Record that ``key`` is no longer held."""
        with self._lock:
            self._state.keys.discard(key)

    def own_button(self, button: str) -> None:
        """Record that BLAXCY is holding ``button`` down (sections 52, 63)."""
        with self._lock:
            self._state.buttons.add(button)

    def release_button(self, button: str) -> None:
        """Record that ``button`` is no longer held."""
        with self._lock:
            self._state.buttons.discard(button)

    # -- Reading --------------------------------------------------------------

    @property
    def active_action(self) -> str | None:
        """The action currently executing, or ``None``."""
        with self._lock:
            return self._state.action

    @property
    def active_lease_id(self) -> str | None:
        """The lease id authorising the current action, or ``None``."""
        with self._lock:
            return self._state.lease_id

    @property
    def active_sequence_id(self) -> str | None:
        """The sequence the current action belongs to, or ``None`` (section 78)."""
        with self._lock:
            return self._state.sequence_id

    @property
    def held_keys(self) -> tuple[str, ...]:
        """Every key BLAXCY currently holds, sorted for stable reporting."""
        with self._lock:
            return tuple(sorted(self._state.keys))

    @property
    def held_buttons(self) -> tuple[str, ...]:
        """Every pointer button BLAXCY currently holds, sorted."""
        with self._lock:
            return tuple(sorted(self._state.buttons))

    def snapshot(self) -> OwnershipSnapshot:
        """A consistent read of the whole ownership record."""
        with self._lock:
            return OwnershipSnapshot(
                active_action=self._state.action,
                lease_id=self._state.lease_id,
                task_id=self._state.task_id,
                step_id=self._state.step_id,
                sequence_id=self._state.sequence_id,
                started_at=self._state.started_at,
                deadline=self._state.deadline,
                held_keys=tuple(sorted(self._state.keys)),
                held_buttons=tuple(sorted(self._state.buttons)),
                cleared_count=self._cleared_count,
            )

    def is_over_deadline(self) -> bool:
        """True when the current action has passed its own deadline."""
        with self._lock:
            deadline = self._state.deadline
            if deadline is None:
                return False
            return self._clock() >= deadline

    # -- Clearing -------------------------------------------------------------

    def clear(self, *, reason: str = "completed") -> OwnershipSnapshot:
        """Drop every ownership record and return what was cleared.

        Called on every terminal path -- success, failure, timeout, cancel,
        exception, emergency stop, human takeover and shutdown. It does not
        itself release physical input (the backend's ``release_all`` does that,
        sections 52/63); it makes sure BLAXCY no longer *believes* it owns
        anything.
        """
        with self._lock:
            snapshot = OwnershipSnapshot(
                active_action=self._state.action,
                lease_id=self._state.lease_id,
                task_id=self._state.task_id,
                step_id=self._state.step_id,
                sequence_id=self._state.sequence_id,
                started_at=self._state.started_at,
                deadline=self._state.deadline,
                held_keys=tuple(sorted(self._state.keys)),
                held_buttons=tuple(sorted(self._state.buttons)),
                cleared_count=self._cleared_count,
            )
            self._state = _Ownership()
            self._cleared_count += 1
        bus = self._bus
        if bus is not None and (snapshot.active_action is not None or snapshot.holds_input):
            from schemas.events import Event, EventType

            bus.publish(
                Event.create(
                    EventType.ACTION_COMPLETED,
                    {
                        "action": snapshot.active_action,
                        "lease_id": snapshot.lease_id,
                        "reason": reason,
                        "released_keys": list(snapshot.held_keys),
                        "released_buttons": list(snapshot.held_buttons),
                    },
                )
            )
        return snapshot

    @contextmanager
    def tracking(
        self,
        action: str,
        *,
        lease: ElementLease | None = None,
        task_id: str | None = None,
        step_id: str | None = None,
        sequence_id: str | None = None,
        deadline_seconds: float | None = None,
    ) -> Iterator[ActionTracker]:
        """Track ``action`` for the duration of the block, clearing on every exit.

        The ``finally`` runs for a normal return, a raised exception, a
        cancellation and a ``BaseException`` such as ``KeyboardInterrupt``, so
        ownership can never survive an interrupted action.
        """
        self.begin(
            action,
            lease=lease,
            task_id=task_id,
            step_id=step_id,
            sequence_id=sequence_id,
            deadline_seconds=deadline_seconds,
        )
        try:
            yield self
        finally:
            self.clear(reason=f"{action} finished")
