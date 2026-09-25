"""Mouse control (specification sections 31, 48, 49).

This is the physical mouse *sequence*, expressed as an explicit, ordered set of
steps so nothing about it is implicit:

1. transform the target into INPUT space (``FRAME/MONITOR/DESKTOP -> INPUT``) and
   clamp it into the desktop and input bounds -- section 31/48;
2. inject the motion;
3. flush, so the X server has processed it before anything else happens;
4. read the pointer back and correct it, bounded, where the backend supports
   readback -- section 48; where it does not, the move is reported ``verified=False``
   rather than assumed;
5. settle ``mouse_settle_ms`` before pressing a button;
6. press/release;
7. settle ``post_click_settle_ms`` so perception sees the result.

**This module does not decide whether to act.** It performs no policy check, no
lease check and no target revalidation -- those belong to the executor
(sections 13, 44, 45), which is Phase 8. It never retries a failed action: a
section 49 drag whose drop target is unresolved is classed ``DESTRUCTIVE`` by the
action model, and destructive actions are never blindly retried (section 4 rule 21).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from config.settings import InputSettings
from control.backends.base import InputBackend, PointerButton
from schemas.geometry import GeometryMap, MonitorGeometry, Point

#: A sleep function, injectable so tests do not actually wait.
SleepFn = Callable[[float], None]


@dataclass(frozen=True)
class MoveResult:
    """Outcome of a pointer move (section 48)."""

    ok: bool
    injected: tuple[int, int]
    readback: tuple[int, int] | None
    readback_available: bool
    verified: bool
    corrected: bool
    corrections: int
    reason: str | None = None


@dataclass(frozen=True)
class ClickResult:
    """Outcome of a click sequence (section 48)."""

    ok: bool
    button: PointerButton
    count: int
    move: MoveResult
    reason: str | None = None


@dataclass(frozen=True)
class ScrollResult:
    """Outcome of a scroll sequence (section 48)."""

    ok: bool
    vertical: int
    horizontal: int
    move: MoveResult
    reason: str | None = None


@dataclass(frozen=True)
class DragResult:
    """Outcome of a drag (section 49)."""

    ok: bool
    source: tuple[int, int]
    destination: tuple[int, int]
    move: MoveResult
    reason: str | None = None


class MouseController:
    """Turns a resolved DESKTOP target into an ordered physical mouse sequence."""

    def __init__(
        self,
        backend: InputBackend,
        geometry: GeometryMap,
        settings: InputSettings | None = None,
        *,
        sleep: SleepFn = time.sleep,
    ) -> None:
        self._backend = backend
        self._geometry = geometry
        self._settings = settings if settings is not None else InputSettings()
        self._sleep = sleep

    @property
    def backend_name(self) -> str:
        """Name of the backend performing input."""
        return self._backend.name

    # -- Section 48: move -----------------------------------------------------

    def move_to(self, point: Point, *, monitor: MonitorGeometry | None = None) -> MoveResult:
        """Transform, clamp, inject, synchronise and (where possible) verify.

        Args:
            point: A DESKTOP (or FRAME/MONITOR, with ``monitor``) target.
            monitor: Required when ``point`` is FRAME/MONITOR space.
        """
        injected_point = self._geometry.prepare_input_point(point, monitor=monitor)
        x, y = self._rounded_within_input_bounds(injected_point)
        self._backend.move_pointer(x, y)
        self._backend.flush()

        readback = self._backend.get_pointer_position()
        corrections = 0
        corrected = False
        while (
            readback is not None
            and self._off_target(readback, (x, y))
            and corrections < self._settings.max_pointer_corrections
        ):
            self._backend.move_pointer(x, y)
            self._backend.flush()
            corrections += 1
            corrected = True
            readback = self._backend.get_pointer_position()

        readback_available = readback is not None
        if readback is not None and self._off_target(readback, (x, y)):
            # The pointer provably is not where we aimed even after correcting.
            # Failing closed here prevents a subsequent click landing elsewhere.
            return MoveResult(
                ok=False,
                injected=(x, y),
                readback=readback,
                readback_available=True,
                verified=False,
                corrected=corrected,
                corrections=corrections,
                reason=(
                    f"pointer readback remained off target after {corrections} correction(s): "
                    f"wanted ({x}, {y}), got {readback}"
                ),
            )

        return MoveResult(
            ok=True,
            injected=(x, y),
            readback=readback,
            readback_available=readback_available,
            verified=readback_available,
            corrected=corrected,
            corrections=corrections,
            reason=None if readback_available else "backend does not support pointer readback",
        )

    # -- Section 48: click / scroll -------------------------------------------

    def click(
        self,
        point: Point,
        *,
        button: PointerButton = PointerButton.LEFT,
        count: int = 1,
        monitor: MonitorGeometry | None = None,
    ) -> ClickResult:
        """Move to ``point``, settle, then press/release ``count`` times."""
        if count < 1:
            raise ValueError("click count must be at least 1")
        move = self.move_to(point, monitor=monitor)
        if not move.ok:
            return ClickResult(False, button, count, move, reason=f"pointer move failed: {move.reason}")
        self._sleep(self._settings.mouse_settle_ms / 1000.0)
        for _ in range(count):
            self._backend.press_button(button)
            self._backend.flush()
            self._backend.release_button(button)
            self._backend.flush()
        self._sleep(self._settings.post_click_settle_ms / 1000.0)
        return ClickResult(True, button, count, move)

    def double_click(self, point: Point, *, monitor: MonitorGeometry | None = None) -> ClickResult:
        """Left double-click (section 48)."""
        return self.click(point, button=PointerButton.LEFT, count=2, monitor=monitor)

    def right_click(self, point: Point, *, monitor: MonitorGeometry | None = None) -> ClickResult:
        """Right click (section 48)."""
        return self.click(point, button=PointerButton.RIGHT, count=1, monitor=monitor)

    def scroll(
        self,
        point: Point,
        *,
        vertical: int = 0,
        horizontal: int = 0,
        monitor: MonitorGeometry | None = None,
    ) -> ScrollResult:
        """Move to ``point`` and inject wheel clicks (positive = down/right)."""
        move = self.move_to(point, monitor=monitor)
        if not move.ok:
            return ScrollResult(
                False, vertical, horizontal, move, reason=f"pointer move failed: {move.reason}"
            )
        self._sleep(self._settings.mouse_settle_ms / 1000.0)
        self._backend.scroll(vertical=vertical, horizontal=horizontal)
        self._backend.flush()
        self._sleep(self._settings.post_click_settle_ms / 1000.0)
        return ScrollResult(True, vertical, horizontal, move)

    # -- Section 49: drag -----------------------------------------------------

    def drag(
        self,
        source: Point,
        destination: Point,
        *,
        monitor: MonitorGeometry | None = None,
        steps: int = 4,
    ) -> DragResult:
        """Press at ``source``, move to ``destination``, release.

        The move between the two points is interpolated so a drop lands where it
        was aimed rather than teleporting (which some toolkits ignore). There is
        no retry: a drag is classed ``DESTRUCTIVE`` when its drop target is
        unresolved (section 49), and destructive actions are never blindly
        retried (section 4 rule 21).
        """
        if steps < 1:
            raise ValueError("drag steps must be at least 1")
        move = self.move_to(source, monitor=monitor)
        if not move.ok:
            return DragResult(False, move.injected, move.injected, move, reason=f"pointer move failed: {move.reason}")

        end = self._geometry.prepare_input_point(destination, monitor=monitor)
        start_x, start_y = move.injected
        end_x, end_y = end.rounded()

        self._sleep(self._settings.mouse_settle_ms / 1000.0)
        self._backend.press_button(PointerButton.LEFT)
        self._backend.flush()
        try:
            for step in range(1, steps + 1):
                fraction = step / steps
                self._backend.move_pointer(
                    round(start_x + (end_x - start_x) * fraction),
                    round(start_y + (end_y - start_y) * fraction),
                )
                self._backend.flush()
            self._backend.release_button(PointerButton.LEFT)
            self._backend.flush()
        except BaseException:
            # Never leave the button held on an unexpected failure; re-raise after
            # restoring the invariant (section 52).
            self._backend.release_button(PointerButton.LEFT)
            self._backend.flush()
            raise
        self._sleep(self._settings.post_click_settle_ms / 1000.0)
        return DragResult(True, (start_x, start_y), (end_x, end_y), move)

    # -- Hygiene --------------------------------------------------------------

    def release_all(self) -> None:
        """Release every held button/key (sections 52, 63)."""
        self._backend.release_all()

    @property
    def held_buttons(self) -> tuple[PointerButton, ...]:
        """The buttons currently held down, so a stop can report what it released."""
        return tuple(self._backend.held_buttons)

    @property
    def held_keys(self) -> tuple[int, ...]:
        """The keycodes currently held down (the backend is the one authority)."""
        return tuple(self._backend.held_keys)

    def release_buttons(self) -> None:
        """Release every held pointer button (section 63 step 3)."""
        self._backend.release_buttons()

    def release_keys(self) -> None:
        """Release every held key/modifier (section 63 step 4)."""
        self._backend.release_keys()

    def close(self) -> None:
        """Release held input and close the backend."""
        self._backend.close()

    # -- Internal --------------------------------------------------------------

    def _off_target(self, readback: tuple[int, int], target: tuple[int, int]) -> bool:
        tolerance = self._settings.pointer_readback_tolerance_px
        return abs(readback[0] - target[0]) > tolerance or abs(readback[1] - target[1]) > tolerance

    def _rounded_within_input_bounds(self, point: Point) -> tuple[int, int]:
        """Round to integers, keeping a known INPUT bound strictly inside.

        The geometry map clamps in fractional space (a half-open rectangle), so
        the top-left/right edge value can round one pixel past the last real
        coordinate. When a backend declares its INPUT bounds we keep the integer
        strictly inside them; when it does not, the X server clamps the pointer
        and the readback step confirms where it actually landed (section 48).
        """
        x, y = point.rounded()
        bounds = self._geometry.input_bounds
        if bounds is not None:
            x = min(max(x, int(bounds.x)), int(bounds.right) - 1)
            y = min(max(y, int(bounds.y)), int(bounds.bottom) - 1)
        return x, y
