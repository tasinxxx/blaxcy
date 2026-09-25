"""Input backend contract (specification sections 30, 48, 50).

An input backend is the *how* of physical input: it moves the pointer, presses
a button, injects a keycode. It is deliberately the narrowest layer in BLAXCY.
It does **not** decide whether an action is allowed, whether a target is fresh,
or whether a lease is valid -- that is the executor's job (sections 13, 44, 45).
A backend that refuses to inject for policy reasons would be a second, weaker
safety authority; a backend that injects without the executor's authorisation
would be a bypass. Neither belongs here.

Every backend must answer :meth:`probe` functionally. A backend is only usable
when a real probe passed (section 28 rule 12); the presence of a binary or an
importable module is never sufficient on its own.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class PointerButton(StrEnum):
    """The pointer buttons BLAXCY injects (section 48)."""

    LEFT = "left"
    MIDDLE = "middle"
    RIGHT = "right"


@dataclass(frozen=True)
class BackendProbe:
    """The result of a functional backend probe.

    ``available`` is only True when the probe actually exercised the backend
    mechanism (an XTEST version query, a live pointer readback), never because a
    module imported or an executable existed.
    """

    name: str
    available: bool
    reason: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class KeyResolution:
    """How one keysym is produced on the current keyboard mapping."""

    keysym: int
    keycode: int
    shift: bool

    @property
    def directly_typable(self) -> bool:
        """True when this keysym resolved to a real keycode."""
        return self.keycode > 0


class InputBackend(ABC):
    """Abstract physical-input backend.

    Implementations are stateful (they hold a display connection and track the
    keys/buttons they have pressed), so :meth:`release_all` can always undo them
    (sections 52, 63). Only one physical action executes at a time (section 32),
    so a backend is not required to be concurrently injectable; it *is* required
    to be safe to :meth:`release_all` and :meth:`close` from another thread
    (the emergency-stop path).
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Short backend identifier (e.g. ``"xtest"``)."""

    @abstractmethod
    def probe(self) -> BackendProbe:
        """Functionally probe this backend without injecting any input."""

    # -- Pointer --------------------------------------------------------------

    @abstractmethod
    def get_pointer_position(self) -> tuple[int, int] | None:
        """Return the current pointer position in INPUT pixels, or ``None``.

        ``None`` means readback is unsupported or failed -- an honest "cannot
        confirm", never a fabricated coordinate.
        """

    @property
    def supports_pointer_readback(self) -> bool:
        """True when this backend can read the pointer back (section 48)."""
        return self.get_pointer_position() is not None

    @abstractmethod
    def move_pointer(self, x: int, y: int) -> None:
        """Inject an absolute pointer move to ``(x, y)`` in INPUT pixels."""

    @abstractmethod
    def press_button(self, button: PointerButton) -> None:
        """Press a pointer button and record it as held."""

    @abstractmethod
    def release_button(self, button: PointerButton) -> None:
        """Release a pointer button and clear it from the held set."""

    @abstractmethod
    def scroll(self, *, vertical: int = 0, horizontal: int = 0) -> None:
        """Inject scroll events; positive is down/right, negative up/left.

        Values are wheel clicks, not pixels. Sign convention is documented here
        so a caller never has to guess the direction.
        """

    # -- Keyboard -------------------------------------------------------------

    @abstractmethod
    def resolve_key(self, keysym: int) -> KeyResolution | None:
        """Resolve ``keysym`` to a keycode on the live keyboard mapping.

        Returns ``None`` when the keysym has no keycode -- the character cannot
        be produced by direct injection (section 50).
        """

    @abstractmethod
    def key_press(self, keycode: int) -> None:
        """Press a keycode and record it as held."""

    @abstractmethod
    def key_release(self, keycode: int) -> None:
        """Release a keycode and clear it from the held set."""

    # -- Ownership reporting (sections 63, 64, 78) ----------------------------

    @property
    @abstractmethod
    def held_keys(self) -> tuple[int, ...]:
        """Every keycode this backend currently holds down, in press order.

        Section 64 requires explicit ownership (``owned_keys_down``); a backend
        that cannot say what it is holding cannot be reliably cleaned up.
        """

    @property
    @abstractmethod
    def held_buttons(self) -> tuple[PointerButton, ...]:
        """Every pointer button this backend currently holds down."""

    # -- Synchronisation and cleanup ------------------------------------------

    @abstractmethod
    def flush(self) -> None:
        """Flush queued input so the server has actually processed it."""

    def release_buttons(self) -> None:
        """Release every held pointer button (section 63 step 3).

        Buttons are released before keys so a drag in progress is ended by
        letting go of the button rather than by dropping a modifier mid-drag.
        """
        for button in tuple(self.held_buttons):
            self.release_button(button)

    def release_keys(self) -> None:
        """Release every held key/modifier, most recent first (section 63 step 4)."""
        for keycode in reversed(tuple(self.held_keys)):
            self.key_release(keycode)

    def release_all(self) -> None:
        """Release every key and button this backend is holding.

        This is the section 52 modifier-hygiene hook and the section 63
        emergency-stop hook: after it, no BLAXCY-owned input remains held. A
        backend may override it to make the release robust when the underlying
        connection is already gone; the ordering (buttons, then keys) must not
        change.
        """
        self.release_buttons()
        self.release_keys()

    @abstractmethod
    def close(self) -> None:
        """Release held input and tear down the backend connection."""

    def __enter__(self) -> InputBackend:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
