"""Emergency stop (specification sections 32, 63).

The emergency stop is the one mechanism in BLAXCY that must work *regardless of
what else is happening*. It therefore obeys three rules that shape every line
below:

1. **It latches first.** The stop flag is set before any cleanup runs, so the
   executor's abort check sees it immediately rather than after the release
   finishes.
2. **It never waits on the normal execution path.** It takes no lock that a
   running action holds, so a stop cannot be delayed behind the action it is
   trying to stop. Releasing input is a short, self-guarded operation on the
   input backend, which is documented as safe to call from another thread.
3. **It always finishes the release.** A releaser that raises is recorded and
   skipped; it never stops the remaining releasers from running, because the
   failure mode that matters most here is a held mouse button or a held
   modifier surviving the stop.

The fixed sequence is section 63's, in order: latch → stop active execution →
release tracked buttons → release tracked keys → cancel the action loop → force
OBSERVE → emit ``EMERGENCY_STOP``. The ``<= 150 ms`` figure in the specification
is a benchmark target, not a guarantee, so the measured latency is reported
rather than assumed.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from core.event_bus import EventBus
from schemas.enums import ErrorCode
from schemas.events import EmergencyStopEvent


@runtime_checkable
class InputReleaser(Protocol):
    """Anything that can release the input BLAXCY currently holds (section 63).

    Implemented by the input backends and by :class:`~control.mouse.MouseController`
    and :class:`~control.keyboard.KeyboardController`. ``release_buttons`` must run
    before ``release_keys`` so a drag in progress ends by letting go of the button.
    """

    def release_buttons(self) -> None:
        """Release every held pointer button."""

    def release_keys(self) -> None:
        """Release every held key/modifier."""


@dataclass(frozen=True)
class EmergencyStopResult:
    """What the stop actually did, with its measured latency."""

    latched: bool
    steps: tuple[tuple[str, bool], ...]
    released_buttons: tuple[str, ...]
    released_keys: tuple[str, ...]
    cancelled: int
    forced_observe: bool
    already_latched: bool
    latency_ms: float
    errors: tuple[str, ...] = ()

    def step_ok(self, name: str) -> bool:
        """True when ``name`` ran without error."""
        return dict(self.steps).get(name, False)

    def to_dict(self) -> dict[str, Any]:
        """JSON-shaped result for the GUI and the report."""
        return {
            "latched": self.latched,
            "steps": dict(self.steps),
            "released_buttons": list(self.released_buttons),
            "released_keys": list(self.released_keys),
            "cancelled": self.cancelled,
            "forced_observe": self.forced_observe,
            "already_latched": self.already_latched,
            "latency_ms": self.latency_ms,
            "errors": list(self.errors),
        }


def release_tracked_input(
    releasers: Sequence[InputReleaser],
) -> tuple[tuple[str, ...], tuple[str, ...], list[str]]:
    """Release buttons then keys on every releaser (section 63 steps 3 and 4).

    Shared by the emergency stop and human takeover so both take exactly the
    same path. A releaser that raises is recorded and skipped: completing the
    rest of the release matters more than surfacing one failure.

    Returns:
        ``(released_buttons, released_keys, errors)`` -- the key/button *names*
        that were held, which is what a user-facing report needs to say.
    """
    buttons: list[str] = []
    keys: list[str] = []
    errors: list[str] = []
    for releaser in releasers:
        try:
            held_buttons = _held(releaser, "held_buttons")
            releaser.release_buttons()
            buttons.extend(held_buttons)
        except Exception as exc:
            errors.append(f"release_buttons failed: {exc!r}")
        try:
            held_keys = _held(releaser, "held_keys")
            releaser.release_keys()
            keys.extend(held_keys)
        except Exception as exc:
            errors.append(f"release_keys failed: {exc!r}")
    return tuple(buttons), tuple(keys), errors


def _held(releaser: InputReleaser, attribute: str) -> tuple[str, ...]:
    """Read what ``releaser`` says it holds, tolerating an object that cannot say.

    The release itself is the safety-critical part, so an object that exposes
    only the release methods still gets released. When it cannot report what it
    was holding this returns an empty tuple rather than inventing a name: a
    report that lists input BLAXCY never held would be a fabrication (section 8
    rule 8).
    """
    getter = getattr(releaser, attribute, None)
    if getter is None:
        return ()
    try:
        values = getter() if callable(getter) else getter
    except Exception:
        return ()
    if values is None:
        return ()
    return tuple(str(value) for value in values)


class EmergencyStop:
    """The latched emergency stop (section 63).

    Args:
        event_bus: Optional bus; ``EMERGENCY_STOP`` is published on step 7.
        mode_controller: Optional mode controller forced to OBSERVE on step 6.
        clock: Monotonic clock, injectable for deterministic latency tests.
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
        self._latched = False
        self._releasers: list[InputReleaser] = []
        self._cancels: list[Callable[[], None]] = []
        self._last_result: EmergencyStopResult | None = None
        self._triggers = 0

    # -- Registration ---------------------------------------------------------

    def register_input(self, releaser: InputReleaser) -> None:
        """Register something that can release tracked input.

        Registration is additive and idempotent by identity, so wiring the same
        controller twice cannot make the release run twice for it.
        """
        with self._lock:
            if not any(existing is releaser for existing in self._releasers):
                self._releasers.append(releaser)

    def register_cancel(self, callback: Callable[[], None]) -> None:
        """Register a callback that cancels the in-flight action/agent loop."""
        with self._lock:
            if not any(existing is callback for existing in self._cancels):
                self._cancels.append(callback)

    # -- State ----------------------------------------------------------------

    @property
    def latched(self) -> bool:
        """True once a stop has been triggered and not yet reset."""
        with self._lock:
            return self._latched

    @property
    def trigger_count(self) -> int:
        """How many times a stop has been triggered since the last reset."""
        with self._lock:
            return self._triggers

    @property
    def last_result(self) -> EmergencyStopResult | None:
        """The most recent stop result, or ``None``."""
        with self._lock:
            return self._last_result

    @property
    def last_latency_ms(self) -> float | None:
        """Latency of the most recent stop in milliseconds, or ``None``."""
        result = self.last_result
        return None if result is None else result.latency_ms

    def abort_code(self) -> ErrorCode | None:
        """``EMERGENCY_STOP_ACTIVE`` while latched, else ``None``.

        This is the executor's abort hook: it is cheap, lock-light and safe to
        call immediately before injecting input.
        """
        return ErrorCode.EMERGENCY_STOP_ACTIVE if self.latched else None

    # -- The stop -------------------------------------------------------------

    def trigger(self, reason: str = "user request") -> EmergencyStopResult:
        """Latch the stop and run the full section 63 sequence.

        Calling this while already latched still runs the release (anything
        grabbed since the first stop must also come off), and reports
        ``already_latched`` so the caller can tell the difference.
        """
        started = self._clock()
        steps: list[tuple[str, bool]] = []
        errors: list[str] = []

        # Step 1: latch first. Everything after this is cleanup.
        with self._lock:
            already = self._latched
            self._latched = True
            self._triggers += 1
        steps.append(("latch", True))

        releasers = list(self._releasers)
        cancels = list(self._cancels)

        # Step 2: stop / terminate the active input execution. There is no
        # preemption primitive for an in-flight injection, so "stop" means the
        # loop is told to stop (step 5) and any state it holds is released
        # (steps 3-4) without waiting for it.
        steps.append(("stop_execution", True))

        # Steps 3 and 4: release tracked buttons, then keys.
        buttons, keys, release_errors = release_tracked_input(releasers)
        steps.append(("release_buttons", not any("release_buttons" in e for e in release_errors)))
        steps.append(("release_keys", not any("release_keys" in e for e in release_errors)))
        errors.extend(release_errors)

        # Step 5: cancel the agent/action loop.
        cancelled = 0
        for callback in cancels:
            try:
                callback()
                cancelled += 1
            except Exception as exc:
                errors.append(f"cancel callback failed: {exc!r}")
        steps.append(("cancel_loop", cancelled == len(cancels)))

        # Step 6: force OBSERVE.
        forced = False
        if self._modes is not None:
            try:
                self._modes.force_observe(reason=f"emergency stop: {reason}")
                forced = True
            except Exception as exc:
                errors.append(f"force OBSERVE failed: {exc!r}")
        steps.append(("force_observe", forced or self._modes is None))

        # Step 7: emit.
        if self._bus is not None:
            try:
                self._bus.publish(
                    EmergencyStopEvent(
                        event_id=uuid.uuid4().hex,
                        timestamp=time.time(),
                        reason=reason,
                    )
                )
            except Exception as exc:
                errors.append(f"event emission failed: {exc!r}")
        steps.append(("emit", True))

        latency_ms = max(0.0, (self._clock() - started) * 1000.0)
        result = EmergencyStopResult(
            latched=True,
            steps=tuple(steps),
            released_buttons=buttons,
            released_keys=keys,
            cancelled=cancelled,
            forced_observe=forced or self._modes is None,
            already_latched=already,
            latency_ms=latency_ms,
            errors=tuple(errors),
        )
        with self._lock:
            self._last_result = result
        return result

    def reset(self, *, reason: str = "explicit re-arm") -> bool:
        """Clear the latch. Returns whether it had been latched.

        Re-arming is deliberately explicit and never automatic: a stop that
        cleared itself after a timeout would not be a stop.
        """
        with self._lock:
            was_latched = self._latched
            self._latched = False
            self._triggers = 0
            self._last_result = None
        del reason  # kept for call-site clarity; the latch carries no state with it
        return was_latched


def combine_abort_checks(
    *checks: Callable[[], ErrorCode | None],
) -> Callable[[], ErrorCode | None]:
    """Combine stop latches into the single hook the executor consults.

    The first non-``None`` code wins, with emergency stop ordered before
    takeover by the caller: an emergency stop is the stronger statement and its
    code should be the one reported when both are latched.
    """

    def combined() -> ErrorCode | None:
        for check in checks:
            code = check()
            if code is not None:
                return code
        return None

    return combined


__all__ = [
    "EmergencyStop",
    "EmergencyStopResult",
    "InputReleaser",
    "combine_abort_checks",
    "release_tracked_input",
]
