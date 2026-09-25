"""The honest "no input backend works here" backend (sections 11, 28, 30).

BLAXCY has to be able to *start* on a session where no input mechanism passed a
functional probe -- a native Wayland session with no XTEST and no working portal,
for instance. The alternative would be for the application to refuse to launch,
which would also remove the read-only and OBSERVE behaviour the session can still
support.

This backend exists for exactly that case, and it claims nothing:

* :meth:`probe` reports ``available=False`` with the reason, so
  ``control.backends.select_backend`` never selects it on its own and the
  capability report keeps saying ``UNAVAILABLE`` (section 28 rule 12).
* every *injection* raises ``BACKEND_UNAVAILABLE``, so no caller can mistake a
  composed-but-unavailable Body for a working one (section 8 rule 8).
* every *release* is a no-op that reports **no** held input, which is the truth:
  a backend that never pressed anything holds nothing. It therefore cannot make
  the section 63 emergency-stop path report a release it did not perform.
"""

from __future__ import annotations

from typing import Final

from control.backends.base import BackendProbe, InputBackend, KeyResolution, PointerButton
from schemas.enums import ErrorCode
from schemas.errors import BlaxcyError

#: The reason every refusal carries, so the cause is visible in one place.
_UNAVAILABLE_REASON: Final[str] = (
    "no input backend passed its functional probe on this session, so physical "
    "input is unavailable (section 30: XTEST under X11, or a verified portal/"
    "ydotool path under Wayland)"
)


class UnavailableBackend(InputBackend):
    """A backend that refuses every injection and never claims availability."""

    @property
    def name(self) -> str:
        """Short backend identifier."""
        return "unavailable"

    def probe(self) -> BackendProbe:
        """Report honestly that this backend cannot inject input."""
        return BackendProbe(name=self.name, available=False, reason=_UNAVAILABLE_REASON)

    # -- Pointer ---------------------------------------------------------------

    def get_pointer_position(self) -> tuple[int, int] | None:
        """Readback is unavailable; ``None`` is the honest answer."""
        return None

    def move_pointer(self, x: int, y: int) -> None:
        """Refuse to move the pointer."""
        raise self._unavailable("move_pointer")

    def press_button(self, button: PointerButton) -> None:
        """Refuse to press a pointer button."""
        raise self._unavailable("press_button")

    def release_button(self, button: PointerButton) -> None:
        """Refuse to release a button it never pressed.

        This raises rather than silently succeeding: a release that did nothing
        would make a mouse controller believe a button it thought it held is now
        up when BLAXCY never injected it at all.
        """
        raise self._unavailable("release_button")

    def scroll(self, *, vertical: int = 0, horizontal: int = 0) -> None:
        """Refuse to scroll."""
        raise self._unavailable("scroll")

    # -- Keyboard --------------------------------------------------------------

    def resolve_key(self, keysym: int) -> KeyResolution | None:
        """No keyboard mapping is available, so no keysym resolves."""
        return None

    def key_press(self, keycode: int) -> None:
        """Refuse to press a key."""
        raise self._unavailable("key_press")

    def key_release(self, keycode: int) -> None:
        """Refuse to release a key it never pressed (see :meth:`release_button`)."""
        raise self._unavailable("key_release")

    # -- Ownership -------------------------------------------------------------

    @property
    def held_keys(self) -> tuple[int, ...]:
        """Always empty: this backend has never pressed a key."""
        return ()

    @property
    def held_buttons(self) -> tuple[PointerButton, ...]:
        """Always empty: this backend has never pressed a button."""
        return ()

    # -- Synchronisation and cleanup ------------------------------------------

    def flush(self) -> None:
        """Nothing is queued, so nothing needs flushing."""
        return None

    def close(self) -> None:
        """Nothing to tear down."""
        return None

    def _unavailable(self, operation: str) -> BlaxcyError:
        """Build the structured refusal every injection raises."""
        return BlaxcyError(
            ErrorCode.BACKEND_UNAVAILABLE,
            f"{operation} is unavailable: {_UNAVAILABLE_REASON}",
            details={"backend": self.name, "operation": operation},
        )


__all__ = ["UnavailableBackend"]
