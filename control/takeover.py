"""Human takeover (specification sections 20, 62).

When a human takes control, BLAXCY gets out of the way completely and hands the
desktop back:

``HUMAN_TAKEOVER`` → pause automation → safely stop the current action → release
BLAXCY-tracked inputs → force OBSERVE → wait for an *explicit* resume → full
re-perception.

Two properties matter more than the mechanics.

**Resuming is never automatic.** There is no timeout that re-arms automation.
The human (or the GUI on their behalf) calls :meth:`TakeoverController.resume`,
and until then the abort hook keeps returning ``HUMAN_TAKEOVER``.

**An interrupted plan is not resumed.** A takeover stops an in-flight sequence
exactly like a single action, and resuming clears a *latch* -- it does not
replay anything. The state the interrupted plan was built against is no longer
trustworthy, so the Brain has to submit a fresh plan. Consequently
:meth:`resume` invalidates every live lease and requests a full re-perception:
nothing about the pre-takeover world is trusted afterwards.
"""

from __future__ import annotations

import contextlib
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from control.emergency_stop import InputReleaser, release_tracked_input
from core.event_bus import EventBus
from schemas.enums import ErrorCode
from schemas.events import HumanTakeoverEvent


@dataclass(frozen=True)
class ResumeResult:
    """What an explicit resume actually did."""

    resumed: bool
    leases_invalidated: int
    re_perceived: bool
    mode: str | None
    reason: str | None = None
    errors: tuple[str, ...] = field(default=())

    def to_dict(self) -> dict[str, Any]:
        """JSON-shaped result for the GUI."""
        return {
            "resumed": self.resumed,
            "leases_invalidated": self.leases_invalidated,
            "re_perceived": self.re_perceived,
            "mode": self.mode,
            "reason": self.reason,
            "errors": list(self.errors),
        }


class TakeoverController:
    """Pauses automation and hands control to a human (section 62).

    Args:
        event_bus: Optional bus; ``HUMAN_TAKEOVER`` is published on ``begin``.
        mode_controller: Optional mode controller forced to OBSERVE.
        clock: Monotonic clock, injectable for deterministic tests.
    """

    def __init__(
        self,
        *,
        event_bus: EventBus | None = None,
        mode_controller: Any = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._lock = threading.RLock()
        self._bus = event_bus
        self._modes = mode_controller
        self._clock = clock
        self._active = False
        self._reason: str | None = None
        self._since: float | None = None
        self._releasers: list[InputReleaser] = []
        self._cancels: list[Callable[[], None]] = []
        self._reperceive: list[Callable[[], None]] = []
        self._invalidate_leases: list[Callable[[], int]] = []
        self._last_resume: ResumeResult | None = None

    # -- Registration ---------------------------------------------------------

    def register_input(self, releaser: InputReleaser) -> None:
        """Register something that can release tracked input."""
        with self._lock:
            if not any(existing is releaser for existing in self._releasers):
                self._releasers.append(releaser)

    def register_cancel(self, callback: Callable[[], None]) -> None:
        """Register a callback that pauses/cancels the in-flight action loop."""
        with self._lock:
            if not any(existing is callback for existing in self._cancels):
                self._cancels.append(callback)

    def register_reperceive(self, callback: Callable[[], None]) -> None:
        """Register the full re-perception hook applied on resume."""
        with self._lock:
            if not any(existing is callback for existing in self._reperceive):
                self._reperceive.append(callback)

    def register_lease_invalidator(self, callback: Callable[[], int]) -> None:
        """Register a hook that drops every live lease, returning how many."""
        with self._lock:
            if not any(existing is callback for existing in self._invalidate_leases):
                self._invalidate_leases.append(callback)

    # -- State ----------------------------------------------------------------

    @property
    def active(self) -> bool:
        """True while automation is paused for a human."""
        with self._lock:
            return self._active

    @property
    def reason(self) -> str | None:
        """Why the takeover began, or ``None``."""
        with self._lock:
            return self._reason

    @property
    def last_resume(self) -> ResumeResult | None:
        """The most recent resume result, or ``None``."""
        with self._lock:
            return self._last_resume

    def abort_code(self) -> ErrorCode | None:
        """``HUMAN_TAKEOVER`` while paused, else ``None`` (the executor's hook)."""
        return ErrorCode.HUMAN_TAKEOVER if self.active else None

    def status(self) -> dict[str, Any]:
        """JSON-shaped status for the GUI."""
        with self._lock:
            return {
                "active": self._active,
                "reason": self._reason,
                "since": self._since,
                "paused_seconds": None if self._since is None else self._clock() - self._since,
                "last_resume": None if self._last_resume is None else self._last_resume.to_dict(),
            }

    # -- Begin / resume -------------------------------------------------------

    def begin(self, reason: str = "human took control") -> HumanTakeoverEvent | None:
        """Pause automation and hand the desktop back (section 62).

        Returns:
            The emitted ``HUMAN_TAKEOVER`` event, or ``None`` when a takeover was
            already in progress (the release still runs, so anything grabbed
            since is also let go).
        """
        with self._lock:
            already = self._active
            self._active = True
            self._reason = reason
            if not already:
                self._since = self._clock()
            releasers = list(self._releasers)
            cancels = list(self._cancels)

        # Safely stop the current action and release what BLAXCY holds. The
        # release is shared with the emergency stop so both take one path.
        for callback in cancels:
            try:
                callback()
            except Exception:
                # A cancel callback that fails must not stop the input release.
                continue
        release_tracked_input(releasers)

        if self._modes is not None:
            # A mode controller that cannot be forced to OBSERVE must not prevent
            # the release above from having happened.
            with contextlib.suppress(Exception):
                self._modes.force_observe(reason=f"human takeover: {reason}")

        if already:
            return None
        event = HumanTakeoverEvent(
            event_id=uuid.uuid4().hex,
            timestamp=time.time(),
            reason=reason,
        )
        if self._bus is not None:
            self._bus.publish(event)
        return event

    def resume(self) -> ResumeResult:
        """Explicitly hand control back to BLAXCY (section 62).

        Clears the latch, invalidates every live lease and performs a full
        re-perception. It deliberately does **not** restore the previous policy
        mode: takeover overrides autonomous control (section 4 rule 20), so the
        mode stays OBSERVE until the human chooses one again.
        """
        with self._lock:
            if not self._active:
                return ResumeResult(False, 0, False, None, reason="no takeover is active")
            self._active = False
            self._reason = None
            self._since = None
            invalidators = list(self._invalidate_leases)
            reperceivers = list(self._reperceive)

        errors: list[str] = []
        invalidated = 0
        for invalidator in invalidators:
            try:
                invalidated += int(invalidator())
            except Exception as exc:
                errors.append(f"lease invalidation failed: {exc!r}")

        re_perceived = False
        for reperceiver in reperceivers:
            try:
                reperceiver()
                re_perceived = True
            except Exception as exc:
                errors.append(f"re-perception failed: {exc!r}")

        mode: str | None = None
        if self._modes is not None:
            try:
                mode = str(self._modes.mode.value)
            except Exception:
                mode = None

        result = ResumeResult(
            resumed=True,
            leases_invalidated=invalidated,
            re_perceived=re_perceived,
            mode=mode,
            errors=tuple(errors),
        )
        with self._lock:
            self._last_resume = result
        return result


__all__ = ["ResumeResult", "TakeoverController"]
