"""State cache and typed event emission (specification sections 32, 65).

The state cache is the single, locked view of *what BLAXCY currently believes*:
the current and previous :class:`ScreenState`, the :class:`ScreenDelta` between
them, the live element leases, the capability report, the active task/action,
the last verification result, and the active sequence's progress record.

Its one job that must never be weakened is **rejecting stale state**. A newer
observation is accepted only when it advances the perception generation and the
frame counter; anything older is dropped, counted, and never allowed to overwrite
a fresher truth (section 32):

* ``incoming.frame_id <= current.frame_id`` -> stale, rejected
* ``incoming.generation < current.generation`` -> stale, rejected

State is *fail-closed* on invalidation. A new generation, a monitor-layout change,
or a structural (``MEANINGFUL``/``MAJOR``) change clears the live leases: removing
an authorization can only prevent input, never authorise it, so this can never be
a safety bypass (sections 32, 34, 44).

``state_version`` is assigned here, independently of ``frame_id``: the cache is the
sole authority on the version stamp of an accepted state, so a skipped or dropped
capture shows up as a version gap rather than being silently renumbered. The
perception pipeline constructs a :class:`ScreenState` with the frame's identity
and the cache re-stamps the version on acceptance.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from core.event_bus import EventBus
from schemas.capability import CapabilityReport
from schemas.enums import CapabilityStatus, ChangeClass, VerificationState
from schemas.events import Event, EventType
from schemas.leases import ElementLease
from schemas.screen_state import ScreenDelta, ScreenState
from schemas.sequences import SequenceProgress


@dataclass(frozen=True)
class StateSnapshot:
    """An immutable, consistent read of everything the cache holds.

    Taken under the cache lock so a GUI/consumer never observes a half-applied
    update (for example a new ``ScreenState`` beside the previous one's leases).
    """

    current: ScreenState | None
    previous: ScreenState | None
    delta: ScreenDelta | None
    leases: tuple[ElementLease, ...]
    capabilities: CapabilityReport | None
    active_task: str | None
    active_action: str | None
    verification: VerificationState
    sequence: SequenceProgress | None
    state_version: int
    generation: int
    accepted_updates: int
    rejected_updates: int

    def to_dict(self) -> dict[str, Any]:
        """A JSON-shaped view for logs and the GUI status panel."""
        return {
            "current": self.current.to_model_context() if self.current is not None else None,
            "previous_frame_id": self.previous.frame_id if self.previous is not None else None,
            "delta": None
            if self.delta is None
            else {
                "from_frame_id": self.delta.from_frame_id,
                "to_frame_id": self.delta.to_frame_id,
                "change_class": self.delta.change_class.value,
            },
            "lease_count": len(self.leases),
            "capabilities": None
            if self.capabilities is None
            else len(self.capabilities.capabilities),
            "active_task": self.active_task,
            "active_action": self.active_action,
            "verification": self.verification.value,
            "sequence": self.sequence.to_dict() if self.sequence is not None else None,
            "state_version": self.state_version,
            "generation": self.generation,
            "accepted_updates": self.accepted_updates,
            "rejected_updates": self.rejected_updates,
        }


class StateCache:
    """The locked, versioned, fail-closed current-state view (section 65)."""

    def __init__(self, *, event_bus: EventBus | None = None) -> None:
        """Create a cache.

        Args:
            event_bus: Optional bus the cache emits onto. When ``None`` the cache
                is fully functional but silent (useful for isolated tests).
        """
        self._lock = threading.RLock()
        self._bus = event_bus
        self._current: ScreenState | None = None
        self._previous: ScreenState | None = None
        self._delta: ScreenDelta | None = None
        self._leases: dict[str, ElementLease] = {}
        self._capabilities: CapabilityReport | None = None
        self._active_task: str | None = None
        self._active_action: str | None = None
        self._verification: VerificationState = VerificationState.NOT_APPLICABLE
        self._sequence: SequenceProgress | None = None
        self._state_version = 0
        self._accepted_updates = 0
        self._rejected_updates = 0

    # -- Screen state (section 32/65) ----------------------------------------

    @property
    def current(self) -> ScreenState | None:
        """The most recently accepted perception state, or ``None``."""
        with self._lock:
            return self._current

    @property
    def previous(self) -> ScreenState | None:
        """The state that preceded :attr:`current`, or ``None``."""
        with self._lock:
            return self._previous

    @property
    def delta(self) -> ScreenDelta | None:
        """The last accepted :class:`ScreenDelta`, or ``None``."""
        with self._lock:
            return self._delta

    @property
    def state_version(self) -> int:
        """The cache-assigned version of the current state (0 before any)."""
        with self._lock:
            return self._state_version

    @property
    def generation(self) -> int:
        """The current perception generation (0 before any accepted state)."""
        with self._lock:
            return self._current.generation if self._current is not None else 0

    @property
    def accepted_updates(self) -> int:
        """Number of perception updates accepted."""
        with self._lock:
            return self._accepted_updates

    @property
    def rejected_updates(self) -> int:
        """Number of stale perception updates rejected (never silently dropped)."""
        with self._lock:
            return self._rejected_updates

    def is_stale_update(self, state: ScreenState) -> bool:
        """True when ``state`` is not newer than the current state (section 32).

        A rejected update is one that fails either staleness rule: an equal or
        older frame id, or a state from an older generation.
        """
        with self._lock:
            current = self._current
            if current is None:
                return False
            return state.frame_id <= current.frame_id or state.generation < current.generation

    def update_screen_state(
        self,
        state: ScreenState,
        *,
        delta: ScreenDelta | None = None,
    ) -> ScreenState | None:
        """Offer a new perception observation to the cache.

        The state's ``state_version`` is assigned by the cache (see the module
        docstring); the caller-supplied value is superseded on acceptance.

        Args:
            state: The freshly perceived state. Its ``frame_id`` and
                ``generation`` decide staleness.
            delta: The classified change that produced ``state``, if any. It is
                re-stamped with the cache's version and generation so the delta
                and the state it describes can never disagree.

        Returns:
            The accepted, re-stamped :class:`ScreenState`, or ``None`` when the
            update was stale and therefore rejected. A rejection never mutates
            cache state.
        """
        events: list[Event] = []
        with self._lock:
            current = self._current
            if current is not None and (
                state.frame_id <= current.frame_id or state.generation < current.generation
            ):
                self._rejected_updates += 1
                return None

            version = self._state_version + 1
            layout_changed = current is not None and current.layout != state.layout
            generation_changed = current is not None and state.generation > current.generation
            stamped = state.model_copy(update={"state_version": version})

            stamped_delta: ScreenDelta | None = None
            if delta is not None:
                stamped_delta = delta.model_copy(
                    update={
                        "from_state_version": self._state_version,
                        "to_state_version": version,
                        "generation": stamped.generation,
                    }
                )

            self._previous = current
            self._current = stamped
            self._state_version = version
            self._accepted_updates += 1
            self._delta = stamped_delta

            structural = stamped_delta is not None and stamped_delta.is_structural
            reasons: list[str] = []
            if generation_changed:
                reasons.append("generation changed")
            if layout_changed:
                reasons.append("monitor layout changed")
            if structural and stamped_delta is not None:
                reasons.append(f"{stamped_delta.change_class.value} change")
            # Only a real invalidation clears leases: an ordinary update leaves
            # live leases to expire on their own TTL (section 44), and a lease is
            # never refreshed here (that would be a safety bypass).
            if reasons:
                for lease in self._clear_leases_locked():
                    events.append(
                        Event.create(
                            EventType.LEASE_REJECTED,
                            {
                                "lease_id": lease.lease_id,
                                "element_id": lease.element_id,
                                "reason": "invalidated: " + ", ".join(reasons),
                            },
                        )
                    )
            else:
                # Section 44: an expired lease is already invalid, so it must not
                # keep occupying the live-lease set. Pruning on an otherwise
                # ordinary update is what keeps a run of non-structural updates
                # (e.g. typing, whose repaint is below MEANINGFUL) from
                # accumulating spent leases; it can only ever remove a lease
                # revalidation would already reject, and it runs only when no
                # invalidation already reported the leases it removes. The lock is
                # reentrant, so this reuses the same method callers can invoke.
                self.prune_expired_leases()

            events.append(
                Event.create(
                    EventType.STATE_UPDATED,
                    {
                        "frame_id": stamped.frame_id,
                        "state_version": stamped.state_version,
                        "generation": stamped.generation,
                        "element_count": len(stamped.elements),
                    },
                )
            )
            if layout_changed:
                events.append(
                    Event.create(
                        EventType.LAYOUT_CHANGED,
                        {"frame_id": stamped.frame_id, "monitor_count": len(stamped.layout.monitors)},
                    )
                )
            if stamped_delta is not None and stamped_delta.change_class is not ChangeClass.NONE:
                events.append(
                    Event.create(
                        EventType.SCREEN_CHANGED,
                        {
                            "frame_id": stamped.frame_id,
                            "from_frame_id": stamped_delta.from_frame_id,
                            "change_class": stamped_delta.change_class.value,
                            "region_count": len(stamped_delta.regions),
                        },
                    )
                )
            accepted = stamped
        self._emit(events)
        return accepted

    def invalidate_leases(self, *, reason: str = "explicit invalidation") -> int:
        """Clear every live lease and emit one ``LEASE_REJECTED`` per lease.

        Returns:
            The number of leases cleared.
        """
        with self._lock:
            cleared = self._clear_leases_locked()
        self._emit(
            [
                Event.create(
                    EventType.LEASE_REJECTED,
                    {
                        "lease_id": lease.lease_id,
                        "element_id": lease.element_id,
                        "reason": reason,
                    },
                )
                for lease in cleared
            ]
        )
        return len(cleared)

    def _clear_leases_locked(self) -> tuple[ElementLease, ...]:
        """Empty the lease map, returning what was removed. Caller holds the lock."""
        cleared = tuple(self._leases.values())
        self._leases.clear()
        return cleared

    # -- Leases (section 44) --------------------------------------------------

    @property
    def leases(self) -> tuple[ElementLease, ...]:
        """Every live lease, in insertion order."""
        with self._lock:
            return tuple(self._leases.values())

    def lease(self, lease_id: str) -> ElementLease | None:
        """Return the live lease with ``lease_id``, or ``None``."""
        with self._lock:
            return self._leases.get(lease_id)

    def store_lease(self, lease: ElementLease) -> None:
        """Record a freshly issued lease."""
        with self._lock:
            self._leases[lease.lease_id] = lease

    def drop_lease(self, lease_id: str) -> bool:
        """Discard one lease. Returns ``True`` when it existed."""
        with self._lock:
            return self._leases.pop(lease_id, None) is not None

    def prune_expired_leases(self, *, now_monotonic: float | None = None) -> int:
        """Drop leases whose TTL has elapsed, returning how many were dropped."""
        now = time.monotonic() if now_monotonic is None else now_monotonic
        with self._lock:
            expired = [lid for lid, lease in self._leases.items() if lease.is_expired(now)]
            for lease_id in expired:
                del self._leases[lease_id]
            return len(expired)

    # -- Capabilities (section 28) -------------------------------------------

    @property
    def capabilities(self) -> CapabilityReport | None:
        """The most recent capability report, or ``None``."""
        with self._lock:
            return self._capabilities

    def set_capabilities(self, report: CapabilityReport) -> None:
        """Record a capability report, emitting ``CAPABILITY_CHANGED`` when it differs."""
        events: list[Event] = []
        with self._lock:
            previous = self._capabilities
            self._capabilities = report
            if previous is None or previous.to_dict() != report.to_dict():
                events.append(
                    Event.create(
                        EventType.CAPABILITY_CHANGED,
                        {
                            "session_type": report.session_type,
                            "available": len(report.by_status(CapabilityStatus.AVAILABLE)),
                            "capability_count": len(report.capabilities),
                        },
                    )
                )
        self._emit(events)

    # -- Active task / action / verification (sections 65, 78) ----------------

    @property
    def active_task(self) -> str | None:
        """Identifier of the task currently being worked, if any."""
        with self._lock:
            return self._active_task

    def set_active_task(self, task: str | None) -> None:
        """Set (or clear) the active task identifier."""
        with self._lock:
            self._active_task = task

    @property
    def active_action(self) -> str | None:
        """Description/identifier of the action currently executing, if any."""
        with self._lock:
            return self._active_action

    def set_active_action(self, action: str | None) -> None:
        """Set (or clear) the active action. Does not authorise input by itself."""
        with self._lock:
            self._active_action = action

    @property
    def verification(self) -> VerificationState:
        """The last verification result (section 60)."""
        with self._lock:
            return self._verification

    def set_verification(self, state: VerificationState) -> None:
        """Record the latest verification result."""
        with self._lock:
            self._verification = state

    # -- Sequence progress (sections 65, 71, 66.1) ---------------------------

    @property
    def sequence(self) -> SequenceProgress | None:
        """The active sequence's progress record, or ``None``."""
        with self._lock:
            return self._sequence

    def set_sequence_progress(self, progress: SequenceProgress | None) -> None:
        """Record (or clear) the active sequence's progress."""
        with self._lock:
            self._sequence = progress

    # -- Snapshot / lifecycle -------------------------------------------------

    def snapshot(self) -> StateSnapshot:
        """Take a consistent, immutable read of the whole cache."""
        with self._lock:
            return StateSnapshot(
                current=self._current,
                previous=self._previous,
                delta=self._delta,
                leases=tuple(self._leases.values()),
                capabilities=self._capabilities,
                active_task=self._active_task,
                active_action=self._active_action,
                verification=self._verification,
                sequence=self._sequence,
                state_version=self._state_version,
                generation=self.generation,
                accepted_updates=self._accepted_updates,
                rejected_updates=self._rejected_updates,
            )

    def clear(self) -> None:
        """Reset every stored field (shutdown/restart). Clears leases without emitting."""
        with self._lock:
            self._current = None
            self._previous = None
            self._delta = None
            self._leases.clear()
            self._capabilities = None
            self._active_task = None
            self._active_action = None
            self._verification = VerificationState.NOT_APPLICABLE
            self._sequence = None
            self._state_version = 0
            self._accepted_updates = 0
            self._rejected_updates = 0

    # -- Internals ------------------------------------------------------------

    def _emit(self, events: Iterable[Event]) -> None:
        """Publish ``events`` on the bus, if one was configured."""
        bus = self._bus
        if bus is None:
            return
        for event in events:
            bus.publish(event)
