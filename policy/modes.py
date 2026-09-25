"""Policy modes and the AUTONOMOUS session ceiling (specification section 56).

The three modes are behavioural contracts, not hints:

* ``OBSERVE`` -- perception only. No mouse/keyboard injection, no drag, no
  terminal submission, no ``run_sequence`` execution. This is the mode BLAXCY
  starts in, the mode a crash resets to, and the mode human takeover and
  emergency stop force (sections 56, 62, 63).
* ``ASSIST`` -- normal navigation and input are permitted; destructive actions
  additionally require an explicit human confirmation, never a silent proceed.
* ``AUTONOMOUS`` -- the same permissions as ASSIST plus autonomous operation,
  but strictly time-boxed: it must expire, it never survives a restart, and once
  its ceiling passes the controller falls back to ``OBSERVE`` on its own.

The controller owns one piece of real state -- the current mode and, for
AUTONOMOUS, the monotonic deadline. It emits ``MODE_CHANGED`` onto the shared
event bus for every transition, including a mode it applies to itself on
expiry, so the GUI and the state cache see the same stream as everything else.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from config.settings import SafetySettings
from core.event_bus import EventBus
from schemas.enums import PolicyMode
from schemas.events import ModeChangedEvent

#: Injected clock, ``time.monotonic`` by default so an AUTONOMOUS ceiling is
#: immune to wall-clock jumps (an NTP step must never extend autonomy).
ClockFn = Callable[[], float]


@dataclass(frozen=True)
class ModeStatus:
    """A consistent read of the mode controller, for the GUI and logs."""

    mode: PolicyMode
    configured_mode: PolicyMode
    autonomous_expires_in_seconds: float | None
    autonomous_active: bool
    forced: bool
    reason: str | None

    def to_dict(self) -> dict[str, object]:
        """JSON-shaped status."""
        return {
            "mode": self.mode.value,
            "configured_mode": self.configured_mode.value,
            "autonomous_expires_in_seconds": self.autonomous_expires_in_seconds,
            "autonomous_active": self.autonomous_active,
            "forced": self.forced,
            "reason": self.reason,
        }


class ModeController:
    """Owns the current policy mode and the AUTONOMOUS session deadline.

    Args:
        settings: The ``[safety]`` configuration. ``mode`` is the configured
            starting mode; ``autonomous_max_session_duration_minutes`` is the
            ceiling applied whenever AUTONOMOUS is entered.
        event_bus: Optional bus to emit ``MODE_CHANGED`` on. ``None`` keeps the
            controller functional but silent (isolated tests).
        clock: Monotonic clock, injectable for deterministic tests.
    """

    def __init__(
        self,
        settings: SafetySettings | None = None,
        *,
        event_bus: EventBus | None = None,
        clock: ClockFn = time.monotonic,
    ) -> None:
        self._settings = settings if settings is not None else SafetySettings()
        self._bus = event_bus
        self._clock = clock
        self._lock = threading.RLock()
        self._mode: PolicyMode = self._settings.mode
        self._deadline: float | None = None
        self._forced = False
        self._reason: str | None = None
        if self._mode is PolicyMode.AUTONOMOUS:
            self._deadline = self._clock() + self._ceiling_seconds()

    # -- Reading --------------------------------------------------------------

    @property
    def mode(self) -> PolicyMode:
        """The effective mode, expiring a lapsed AUTONOMOUS session first.

        Reading the mode may therefore *latch* an expired AUTONOMOUS session back
        to ``OBSERVE`` (and emit ``MODE_CHANGED``). That is deliberate: the
        ceiling must hold even if nothing else calls :meth:`tick`, so no code
        path can keep acting autonomously because a timer was never polled.
        """
        self._expire_if_due()
        with self._lock:
            return self._mode

    @property
    def configured_mode(self) -> PolicyMode:
        """The mode that was requested, before any expiry/force to OBSERVE."""
        with self._lock:
            return self._settings.mode if not self._forced else PolicyMode.OBSERVE

    @property
    def is_autonomous(self) -> bool:
        """True when AUTONOMOUS is currently active (and not expired)."""
        return self.mode is PolicyMode.AUTONOMOUS

    def remaining_seconds(self) -> float | None:
        """Seconds left in the AUTONOMOUS session, or ``None`` when not autonomous."""
        self._expire_if_due()
        with self._lock:
            if self._mode is not PolicyMode.AUTONOMOUS or self._deadline is None:
                return None
            return max(0.0, self._deadline - self._clock())

    def status(self) -> ModeStatus:
        """A consistent snapshot for the GUI status panel."""
        self._expire_if_due()
        with self._lock:
            remaining = None
            if self._mode is PolicyMode.AUTONOMOUS and self._deadline is not None:
                remaining = max(0.0, self._deadline - self._clock())
            return ModeStatus(
                mode=self._mode,
                configured_mode=self._settings.mode,
                autonomous_expires_in_seconds=remaining,
                autonomous_active=self._mode is PolicyMode.AUTONOMOUS,
                forced=self._forced,
                reason=self._reason,
            )

    # -- Transitions ----------------------------------------------------------

    def set_mode(self, mode: PolicyMode, *, reason: str | None = None) -> ModeChangedEvent | None:
        """Enter ``mode``, starting the AUTONOMOUS ceiling when applicable.

        Returns:
            The emitted ``MODE_CHANGED`` event, or ``None`` when the mode did not
            actually change (a no-op transition emits nothing and does not reset
            an in-flight AUTONOMOUS deadline).
        """
        with self._lock:
            if mode is self._mode:
                return None
            previous = self._mode
            self._mode = mode
            self._forced = False
            self._reason = reason
            self._deadline = None
            if mode is PolicyMode.AUTONOMOUS:
                self._deadline = self._clock() + self._ceiling_seconds()
        event = ModeChangedEvent(
            event_id=uuid.uuid4().hex,
            timestamp=time.time(),
            previous_mode=previous.value,
            current_mode=mode.value,
            reason=reason,
        )
        self._emit(event)
        return event

    def force_observe(self, *, reason: str) -> ModeChangedEvent | None:
        """Force OBSERVE regardless of the configured mode (sections 62, 63).

        Used by human takeover and emergency stop. Idempotent: forcing OBSERVE
        while already in OBSERVE emits nothing.
        """
        with self._lock:
            previous = self._mode
            already = previous is PolicyMode.OBSERVE and self._forced
            self._mode = PolicyMode.OBSERVE
            self._deadline = None
            self._forced = True
            self._reason = reason
            if already:
                return None
        event = ModeChangedEvent(
            event_id=uuid.uuid4().hex,
            timestamp=time.time(),
            previous_mode=previous.value,
            current_mode=PolicyMode.OBSERVE.value,
            reason=reason,
        )
        self._emit(event)
        return event

    def tick(self) -> ModeChangedEvent | None:
        """Apply the AUTONOMOUS ceiling if it has lapsed.

        Returns the ``MODE_CHANGED`` event when a transition happened. Safe to
        call from any thread and as often as a caller likes.
        """
        return self._expire_if_due()

    # -- Internals ------------------------------------------------------------

    def _ceiling_seconds(self) -> float:
        """The configured AUTONOMOUS ceiling in seconds."""
        return float(self._settings.autonomous_max_session_duration_minutes) * 60.0

    def _expire_if_due(self) -> ModeChangedEvent | None:
        """Latch back to OBSERVE when the AUTONOMOUS deadline has passed."""
        with self._lock:
            if self._mode is not PolicyMode.AUTONOMOUS or self._deadline is None:
                return None
            if self._clock() < self._deadline:
                return None
            previous = self._mode
            self._mode = PolicyMode.OBSERVE
            self._forced = True
            self._reason = "autonomous session ceiling reached"
            self._deadline = None
        event = ModeChangedEvent(
            event_id=uuid.uuid4().hex,
            timestamp=time.time(),
            previous_mode=previous.value,
            current_mode=PolicyMode.OBSERVE.value,
            reason="autonomous session ceiling reached",
        )
        self._emit(event)
        return event

    def _emit(self, event: ModeChangedEvent) -> None:
        """Publish ``event`` when a bus was configured."""
        bus = self._bus
        if bus is not None:
            bus.publish(event)
