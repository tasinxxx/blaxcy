"""In-process XTEST input backend (specification sections 30, 48-52).

XTEST is BLAXCY's preferred input path on X11: it injects at the X server level
through the same connection that captured the frame, so there is no subprocess,
no shell, and no extra process boundary between a resolved coordinate and the
pointer motion (section 30). ``xdotool`` is a fallback, not the primary path.

This backend injects exactly and only what it is told to inject. It performs no
policy, no lease check and no target validation -- those belong to the executor
(sections 13, 44, 45). What it *does* guarantee are the mechanical properties a
safe input layer needs: every pressed key/button is tracked so :meth:`release_all`
can undo it (sections 52, 63), and the connection is created lazily so the
backend can be constructed and probed without touching the display.

``python-xlib`` is imported lazily inside the methods, so importing this module
never requires Xlib: :meth:`probe` then reports the capability honestly instead
of failing the process (section 28).
"""

from __future__ import annotations

import threading
from contextlib import suppress
from typing import Any

from control.backends.base import BackendProbe, InputBackend, KeyResolution, PointerButton
from schemas.enums import ErrorCode
from schemas.errors import BlaxcyError

#: X11 core button numbers.
_BUTTON_NUMBERS: dict[PointerButton, int] = {
    PointerButton.LEFT: 1,
    PointerButton.MIDDLE: 2,
    PointerButton.RIGHT: 3,
}

#: X11 core wheel button numbers (vertical then horizontal).
_SCROLL_UP = 4
_SCROLL_DOWN = 5
_SCROLL_LEFT = 6
_SCROLL_RIGHT = 7


class XtestBackend(InputBackend):
    """Absolute pointer motion and key injection through the XTEST extension."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._display: Any = None
        self._held_buttons: list[int] = []
        self._held_keys: list[int] = []

    @property
    def name(self) -> str:
        return "xtest"

    # -- Connection management ------------------------------------------------

    def _connect(self) -> Any:
        """Open a fresh X display connection.

        Raises:
            BlaxcyError: ``BACKEND_UNAVAILABLE`` when Xlib or the display is
                missing -- never a silent no-op that looks like success.
        """
        try:
            from Xlib import display as xdisplay
        except Exception as exc:  # pragma: no cover - depends on the host
            raise BlaxcyError(
                ErrorCode.BACKEND_UNAVAILABLE,
                f"python-xlib is not importable: {exc!r}",
                details={"backend": self.name},
            ) from exc
        try:
            return xdisplay.Display()
        except Exception as exc:
            raise BlaxcyError(
                ErrorCode.BACKEND_UNAVAILABLE,
                f"cannot open an X display: {exc!r}",
                details={"backend": self.name},
            ) from exc

    def _require_display(self) -> Any:
        """Return the live display, opening it on first use."""
        with self._lock:
            if self._display is None:
                self._display = self._connect()
            return self._display

    # -- Probe ----------------------------------------------------------------

    def probe(self) -> BackendProbe:
        """Query the XTEST extension version without injecting any input."""
        try:
            from Xlib.ext import xtest
        except Exception as exc:
            return BackendProbe(self.name, False, f"python-xlib XTEST extension unavailable: {exc!r}")
        try:
            connection = self._connect()
        except BlaxcyError as exc:
            return BackendProbe(self.name, False, exc.message)
        try:
            version = xtest.get_version(connection, 2, 2)
            # The installed python-xlib (verified on this host) parses the reply
            # into the request object's private ``_data`` dict; ``reply`` is a
            # bound method, not the reply. Read it defensively so a different
            # layout degrades to "no version detail" rather than raising.
            data = getattr(version, "_data", None)
            if not isinstance(data, dict):
                data = getattr(version, "data", None)
            if not isinstance(data, dict):
                data = {}
            details: dict[str, Any] = {}
            if "major_version" in data:
                details["xtest_version"] = f"{data['major_version']}.{data.get('minor_version', 0)}"
            # A reply without version data still proves the extension answered.
            return BackendProbe(self.name, True, None, details)
        except Exception as exc:
            return BackendProbe(self.name, False, f"XTEST query failed: {exc!r}")
        finally:
            with suppress(Exception):
                connection.close()

    # -- Pointer --------------------------------------------------------------

    def get_pointer_position(self) -> tuple[int, int] | None:
        """Read the pointer position via a read-only X query."""
        try:
            display = self._require_display()
            with self._lock:
                pointer = display.screen().root.query_pointer()
                return (int(pointer.root_x), int(pointer.root_y))
        except Exception:
            return None

    def move_pointer(self, x: int, y: int) -> None:
        """Inject an absolute pointer move (XTEST motion, ``detail=0``)."""
        xtest = self._xtest()
        display = self._require_display()
        with self._lock:
            xtest.fake_input(display, self._x().MotionNotify, 0, 0, 0, int(x), int(y))

    def press_button(self, button: PointerButton) -> None:
        """Press a pointer button and record it as held."""
        xtest = self._xtest()
        display = self._require_display()
        number = _BUTTON_NUMBERS[button]
        with self._lock:
            xtest.fake_input(display, self._x().ButtonPress, number)
            if number not in self._held_buttons:
                self._held_buttons.append(number)

    def release_button(self, button: PointerButton) -> None:
        """Release a pointer button and clear it from the held set."""
        xtest = self._xtest()
        display = self._require_display()
        number = _BUTTON_NUMBERS[button]
        with self._lock:
            xtest.fake_input(display, self._x().ButtonRelease, number)
            while number in self._held_buttons:
                self._held_buttons.remove(number)

    def scroll(self, *, vertical: int = 0, horizontal: int = 0) -> None:
        """Inject wheel clicks; positive is down/right, negative up/left."""
        xtest = self._xtest()
        display = self._require_display()
        x = self._x()
        with self._lock:
            for _ in range(abs(int(vertical))):
                button = _SCROLL_DOWN if vertical > 0 else _SCROLL_UP
                xtest.fake_input(display, x.ButtonPress, button)
                xtest.fake_input(display, x.ButtonRelease, button)
            for _ in range(abs(int(horizontal))):
                button = _SCROLL_RIGHT if horizontal > 0 else _SCROLL_LEFT
                xtest.fake_input(display, x.ButtonPress, button)
                xtest.fake_input(display, x.ButtonRelease, button)

    # -- Keyboard -------------------------------------------------------------

    def resolve_key(self, keysym: int) -> KeyResolution | None:
        """Resolve ``keysym`` to a keycode plus whether Shift is required.

        Only keyboard levels 0 (unshifted) and 1 (Shift) are supported. A keysym
        reachable only at a higher level (e.g. AltGr) returns ``None``: direct
        injection cannot produce it, so the caller must not pretend it can.
        """
        display = self._require_display()
        with self._lock:
            keycode = int(display.keysym_to_keycode(keysym))
            if keycode <= 0:
                return None
            mapping = display.get_keyboard_mapping(keycode, 1)
            if not mapping:
                return None
            row = list(mapping[0])
        if not row or (row[0] != keysym and not (len(row) > 1 and row[1] == keysym)):
            return None
        return KeyResolution(keysym=keysym, keycode=keycode, shift=bool(row and row[0] != keysym))

    def key_press(self, keycode: int) -> None:
        """Press a keycode and record it as held."""
        xtest = self._xtest()
        display = self._require_display()
        with self._lock:
            xtest.fake_input(display, self._x().KeyPress, int(keycode))
            if int(keycode) not in self._held_keys:
                self._held_keys.append(int(keycode))

    def key_release(self, keycode: int) -> None:
        """Release a keycode and clear it from the held set."""
        xtest = self._xtest()
        display = self._require_display()
        with self._lock:
            xtest.fake_input(display, self._x().KeyRelease, int(keycode))
            while int(keycode) in self._held_keys:
                self._held_keys.remove(int(keycode))

    # -- Ownership reporting (sections 63, 64) --------------------------------

    @property
    def held_keys(self) -> tuple[int, ...]:
        """Every keycode currently held down, in press order."""
        with self._lock:
            return tuple(self._held_keys)

    @property
    def held_buttons(self) -> tuple[PointerButton, ...]:
        """Every pointer button currently held down."""
        with self._lock:
            number_to_button = {number: button for button, number in _BUTTON_NUMBERS.items()}
            return tuple(
                number_to_button.get(number, PointerButton.LEFT) for number in self._held_buttons
            )

    # -- Synchronisation and cleanup ------------------------------------------

    def flush(self) -> None:
        """Flush queued input so the X server has processed it."""
        with self._lock:
            if self._display is not None:
                self._display.flush()

    def release_all(self) -> None:
        """Release every button and key this backend still holds.

        Safe to call from another thread (the emergency-stop path) and safe to
        call when nothing is held or the connection is already gone. Overrides
        the base implementation so the X calls are guarded individually and the
        held sets are cleared even when the connection has already died -- a
        cleanup that raises would leave BLAXCY believing it still holds input.
        """
        with self._lock:
            display = self._display
            if display is None:
                self._held_keys.clear()
                self._held_buttons.clear()
                return
            try:
                from Xlib.ext import xtest
            except Exception:  # pragma: no cover - Xlib vanished mid-session
                self._held_keys.clear()
                self._held_buttons.clear()
                return
            try:
                x = self._x()
                for keycode in reversed(self._held_keys):
                    with suppress(Exception):
                        xtest.fake_input(display, x.KeyRelease, keycode)
                for number in reversed(self._held_buttons):
                    with suppress(Exception):
                        xtest.fake_input(display, x.ButtonRelease, number)
                with suppress(Exception):
                    display.flush()
            finally:
                self._held_keys.clear()
                self._held_buttons.clear()

    def close(self) -> None:
        """Release any held input and close the display connection."""
        with self._lock:
            self.release_all()
            display = self._display
            self._display = None
            if display is not None:
                with suppress(Exception):
                    display.close()

    # -- Internal helpers ------------------------------------------------------

    @staticmethod
    def _xtest() -> Any:
        """Import and return the XTEST extension module."""
        try:
            from Xlib.ext import xtest
        except Exception as exc:  # pragma: no cover - depends on the host
            raise BlaxcyError(
                ErrorCode.BACKEND_UNAVAILABLE,
                f"XTEST extension is not importable: {exc!r}",
            ) from exc
        return xtest

    @staticmethod
    def _x() -> Any:
        """Import and return ``Xlib.X`` (the core event constants)."""
        try:
            from Xlib import X
        except Exception as exc:  # pragma: no cover - depends on the host
            raise BlaxcyError(
                ErrorCode.BACKEND_UNAVAILABLE,
                f"python-xlib is not importable: {exc!r}",
            ) from exc
        return X
