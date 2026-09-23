"""A recording fake input backend for unit tests.

Mouse and keyboard unit tests must assert the exact *order* of injected events
without touching a real display. This backend records every call as a tuple and
lets a test control the pointer readback and inject failures, so the section 48
correction loop and the section 52 release-on-failure paths are testable.
"""

from __future__ import annotations

from collections.abc import Callable

from control.backends.base import BackendProbe, InputBackend, KeyResolution, PointerButton
from schemas.enums import ErrorCode
from schemas.errors import BlaxcyError


class FakeInputBackend(InputBackend):
    """An input backend that records calls instead of injecting them."""

    def __init__(
        self,
        *,
        readback_fn: Callable[[], tuple[int, int] | None] | None = None,
        readback_supported: bool = True,
        keymap: dict[int, int] | None = None,
        shifted: set[int] | None = None,
        unresolvable: set[int] | None = None,
        raise_on_move: int | None = None,
        raise_on_key_press: int | None = None,
    ) -> None:
        self.events: list[tuple[object, ...]] = []
        self.held_buttons: list[int] = []
        self.held_keys: list[int] = []
        self.pointer: tuple[int, int] = (0, 0)
        self._readback_fn = readback_fn
        self._readback_supported = readback_supported
        self._keymap = keymap or {}
        self._shifted = shifted or set()
        self._unresolvable = unresolvable or set()
        self._raise_on_move = raise_on_move
        self._raise_on_key_press = raise_on_key_press
        self._move_count = 0
        self._key_press_count = 0

    @property
    def name(self) -> str:
        return "fake"

    def probe(self) -> BackendProbe:
        return BackendProbe("fake", True, None, {"fake": True})

    def get_pointer_position(self) -> tuple[int, int] | None:
        if not self._readback_supported:
            return None
        if self._readback_fn is not None:
            return self._readback_fn()
        return self.pointer

    def move_pointer(self, x: int, y: int) -> None:
        self._move_count += 1
        self.events.append(("move", x, y))
        if self._raise_on_move is not None and self._move_count == self._raise_on_move:
            raise BlaxcyError(ErrorCode.INTERNAL_ERROR, "injected move failure")
        self.pointer = (x, y)

    def press_button(self, button: PointerButton) -> None:
        number = {PointerButton.LEFT: 1, PointerButton.MIDDLE: 2, PointerButton.RIGHT: 3}[button]
        self.events.append(("press", number))
        if number not in self.held_buttons:
            self.held_buttons.append(number)

    def release_button(self, button: PointerButton) -> None:
        number = {PointerButton.LEFT: 1, PointerButton.MIDDLE: 2, PointerButton.RIGHT: 3}[button]
        self.events.append(("release", number))
        while number in self.held_buttons:
            self.held_buttons.remove(number)

    def scroll(self, *, vertical: int = 0, horizontal: int = 0) -> None:
        self.events.append(("scroll", vertical, horizontal))

    def resolve_key(self, keysym: int) -> KeyResolution | None:
        if keysym in self._unresolvable:
            return None
        keycode = self._keymap.get(keysym, keysym)
        return KeyResolution(keysym=keysym, keycode=keycode, shift=keysym in self._shifted)

    def key_press(self, keycode: int) -> None:
        self._key_press_count += 1
        self.events.append(("key_press", keycode))
        if self._raise_on_key_press is not None and self._key_press_count == self._raise_on_key_press:
            raise BlaxcyError(ErrorCode.INTERNAL_ERROR, "injected key failure")
        if keycode not in self.held_keys:
            self.held_keys.append(keycode)

    def key_release(self, keycode: int) -> None:
        self.events.append(("key_release", keycode))
        while keycode in self.held_keys:
            self.held_keys.remove(keycode)

    def flush(self) -> None:
        self.events.append(("flush",))

    def release_all(self) -> None:
        self.events.append(("release_all",))
        self.held_keys.clear()
        self.held_buttons.clear()

    def close(self) -> None:
        self.release_all()
        self.events.append(("close",))

    # -- Test helpers ---------------------------------------------------------

    def event_names(self) -> list[str]:
        """The ordered event kinds, ignoring payloads."""
        return [str(event[0]) for event in self.events]

    def payloads(self, kind: str) -> list[tuple[object, ...]]:
        """Every recorded event of ``kind``, in order."""
        return [event for event in self.events if event[0] == kind]
