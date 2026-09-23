"""Keyboard control (specification sections 50, 51, 52).

Section 50 decides *how* text is typed:

* plain ASCII text up to ``ascii_direct_max`` characters is typed directly with
  XTEST key events, spaced by ``key_interval_ms`` so the target application can
  keep up;
* long or non-ASCII text is typed by putting it on the clipboard and injecting a
  paste (Ctrl+V), which is the only honest way to produce characters the current
  keyboard mapping cannot express;
* when neither is possible, the call fails with ``UNICODE_UNSUPPORTED`` rather
  than typing an approximation of the requested text.

The clipboard is a pluggable :class:`ClipboardPaster`. No clipboard
implementation ships in Phase 7, so until one is wired in the controller simply
says so -- it never fakes a paste.

Section 51's focus guard runs *before* any key is injected: if the caller names
the element it intends to type into, the guard verifies (against the current
observed state) that this exact element is present, accepts text, and is
actually focused; otherwise the call fails with ``FOCUS_MISMATCH``.

Section 52's modifier hygiene is mechanical: every modifier pressed here is
tracked by the backend and released again, and any unexpected failure releases
everything before propagating.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from config.settings import InputSettings
from control.backends.base import InputBackend, KeyResolution
from control.backends.keys import canonical_key_name, keysym_for_char, keysym_for_name
from schemas.elements import UIElement
from schemas.enums import ErrorCode
from schemas.errors import BlaxcyError
from schemas.screen_state import ScreenState

#: A sleep function, injectable so tests do not actually wait.
SleepFn = Callable[[float], None]


class ClipboardPaster(Protocol):
    """A clipboard able to hold text for a paste, then restore prior content."""

    def set_text(self, text: str) -> bool:
        """Place ``text`` on the clipboard; return False when unavailable."""

    def restore(self) -> None:
        """Restore whatever the clipboard held before :meth:`set_text`."""


@dataclass(frozen=True)
class FocusCheck:
    """Result of the section 51 focus guard."""

    ok: bool
    reason: str | None = None
    expected_element_id: str | None = None
    actual_element_id: str | None = None


@dataclass(frozen=True)
class TypeResult:
    """Outcome of a typing request (section 50)."""

    ok: bool
    method: str
    characters: int
    focus: FocusCheck | None = None
    reason: str | None = None


@dataclass(frozen=True)
class PressResult:
    """Outcome of a key press or hotkey (section 50)."""

    ok: bool
    keys: tuple[str, ...]
    keycodes: tuple[int, ...]
    reason: str | None = None


def check_focus(expected: UIElement, state: ScreenState | None) -> FocusCheck:
    """Verify ``expected`` is the focused text-entry element (section 51).

    Fails closed: without a current state, or when the target is absent,
    non-text-entry or unfocused, the check is ``ok=False``. It never guesses that
    focus is correct.
    """
    if not expected.is_text_entry:
        return FocusCheck(
            False,
            f"focus guard target {expected.element_id!r} is not a text-entry element",
            expected.element_id,
            None,
        )
    if state is None:
        return FocusCheck(
            False,
            "focus cannot be verified without a current ScreenState",
            expected.element_id,
            None,
        )
    observed = _find_in_state(expected, state)
    if observed is None:
        return FocusCheck(
            False,
            "focus guard target is not present in the current state",
            expected.element_id,
            None,
        )
    if not observed.is_text_entry:
        return FocusCheck(
            False,
            f"element {observed.element_id!r} no longer accepts text",
            expected.element_id,
            observed.element_id,
        )
    if not observed.focused:
        return FocusCheck(
            False,
            f"element {observed.element_id!r} is not the focused element",
            expected.element_id,
            observed.element_id,
        )
    return FocusCheck(True, None, expected.element_id, observed.element_id)


class KeyboardController:
    """Turns requested text/keys into ordered physical key sequences."""

    def __init__(
        self,
        backend: InputBackend,
        settings: InputSettings | None = None,
        *,
        clipboard: ClipboardPaster | None = None,
        sleep: SleepFn = time.sleep,
    ) -> None:
        self._backend = backend
        self._settings = settings if settings is not None else InputSettings()
        self._clipboard = clipboard
        self._sleep = sleep

    @property
    def backend_name(self) -> str:
        """Name of the backend performing input."""
        return self._backend.name

    # -- Section 50: typing ---------------------------------------------------

    def type_text(
        self,
        text: str,
        *,
        expected_focus: UIElement | None = None,
        state: ScreenState | None = None,
    ) -> TypeResult:
        """Type ``text``, choosing the direct or clipboard path (section 50).

        Args:
            text: The exact text to type.
            expected_focus: When given (and the guard is enabled), the element
                that must be focused before any key is injected (section 51).
            state: The current observation the focus guard checks against.

        Raises:
            BlaxcyError: ``FOCUS_MISMATCH`` when the focus guard fails,
                ``UNICODE_UNSUPPORTED`` when the text cannot be produced, or
                ``CLIPBOARD_FAILED`` when a paste was required but failed.
        """
        focus: FocusCheck | None = None
        if expected_focus is not None and self._settings.guard_focus:
            focus = check_focus(expected_focus, state)
            if not focus.ok:
                raise BlaxcyError(
                    ErrorCode.FOCUS_MISMATCH,
                    focus.reason or "focus guard failed",
                    details={"expected_element_id": focus.expected_element_id},
                )

        strokes = self._resolve_text(text)
        limit = self._settings.ascii_direct_max
        if strokes is not None and (len(text) <= limit or self._clipboard is None):
            self._type_direct(strokes)
            return TypeResult(True, "xtest_direct", len(text), focus)

        if self._clipboard is not None:
            if not self._paste_via_clipboard(text):
                raise BlaxcyError(
                    ErrorCode.CLIPBOARD_FAILED,
                    "clipboard-assisted typing failed",
                    details={"characters": len(text)},
                )
            return TypeResult(True, "clipboard", len(text), focus)

        raise BlaxcyError(
            ErrorCode.UNICODE_UNSUPPORTED,
            "text cannot be produced: it is non-ASCII (or not on this keyboard mapping) "
            "and no clipboard is available",
            details={"characters": len(text), "ascii_direct_max": limit},
        )

    # -- Section 50: named keys and hotkeys -----------------------------------

    def press_key(self, key: str, *, modifiers: Sequence[str] = ()) -> PressResult:
        """Press ``key`` (optionally with held ``modifiers``) and release it.

        Raises:
            BlaxcyError: ``BACKEND_UNAVAILABLE`` when a name or character has no
                keycode on the current keyboard mapping.
        """
        keycode = self._require_keycode(key)
        modifier_names = tuple(canonical_key_name(m) for m in modifiers)
        modifier_keycodes = tuple(self._require_keycode(name) for name in modifier_names)
        try:
            for keycode_mod in modifier_keycodes:
                self._backend.key_press(keycode_mod)
            self._backend.key_press(keycode)
            self._backend.key_release(keycode)
            for keycode_mod in reversed(modifier_keycodes):
                self._backend.key_release(keycode_mod)
            self._backend.flush()
        except BaseException:
            # Section 52: never leave a modifier held after an unexpected failure.
            self._backend.release_all()
            raise
        return PressResult(True, (key, *modifier_names), (keycode, *modifier_keycodes))

    def hotkey(self, combo: str) -> PressResult:
        """Press a ``"ctrl+shift+t"``-style combination (section 50).

        The last component is the key; everything before it is a held modifier.
        """
        parts = [part.strip() for part in combo.split("+") if part.strip()]
        if not parts:
            raise ValueError("a hotkey needs at least one key")
        if len(parts) == 1:
            return self.press_key(parts[0])
        return self.press_key(parts[-1], modifiers=parts[:-1])

    # -- Hygiene --------------------------------------------------------------

    def release_all(self) -> None:
        """Release every held key/button (sections 52, 63)."""
        self._backend.release_all()

    def close(self) -> None:
        """Release held input and close the backend."""
        self._backend.close()

    # -- Internal --------------------------------------------------------------

    def _resolve_text(self, text: str) -> list[KeyResolution] | None:
        """Resolve every character to a direct key stroke, or ``None``.

        Returning ``None`` means at least one character cannot be produced by
        direct injection. All characters are resolved *before* any is typed, so a
        mid-string failure can never leave half the text typed.
        """
        strokes: list[KeyResolution] = []
        for char in text:
            keysym = keysym_for_char(char)
            if keysym is None:
                return None
            resolution = self._backend.resolve_key(keysym)
            if resolution is None or not resolution.directly_typable:
                return None
            strokes.append(resolution)
        return strokes

    def _type_direct(self, strokes: Sequence[KeyResolution]) -> None:
        """Type pre-resolved strokes with XTEST, tracking Shift per character."""
        shift_keycode: int | None = None
        interval_s = self._settings.key_interval_ms / 1000.0
        try:
            for index, stroke in enumerate(strokes):
                if stroke.shift:
                    if shift_keycode is None:
                        shift_keycode = self._require_keycode("shift")
                    self._backend.key_press(shift_keycode)
                self._backend.key_press(stroke.keycode)
                self._backend.key_release(stroke.keycode)
                if stroke.shift and shift_keycode is not None:
                    self._backend.key_release(shift_keycode)
                self._backend.flush()
                if interval_s > 0 and index < len(strokes) - 1:
                    self._sleep(interval_s)
        except BaseException:
            self._backend.release_all()
            raise

    def _paste_via_clipboard(self, text: str) -> bool:
        """Put ``text`` on the clipboard and inject Ctrl+V (section 50)."""
        clipboard = self._clipboard
        if clipboard is None:
            return False
        if not clipboard.set_text(text):
            return False
        try:
            ctrl_keycode = self._require_keycode("ctrl")
            v_keycode = self._require_keycode("v")
            self._backend.key_press(ctrl_keycode)
            self._backend.key_press(v_keycode)
            self._backend.key_release(v_keycode)
            self._backend.key_release(ctrl_keycode)
            self._backend.flush()
        except BaseException:
            self._backend.release_all()
            raise
        finally:
            clipboard.restore()
        return True

    def _require_keycode(self, key: str) -> int:
        """Resolve a key name or character to a keycode or raise honestly."""
        keysym = keysym_for_name(key) if len(key) > 1 else keysym_for_char(key)
        if keysym is None:
            raise BlaxcyError(
                ErrorCode.BACKEND_UNAVAILABLE,
                f"unknown key name {key!r}",
            )
        resolution = self._backend.resolve_key(keysym)
        if resolution is None or not resolution.directly_typable:
            raise BlaxcyError(
                ErrorCode.BACKEND_UNAVAILABLE,
                f"key {key!r} has no keycode on the current keyboard mapping",
                details={"keysym": keysym},
            )
        return resolution.keycode


def _find_in_state(expected: UIElement, state: ScreenState) -> UIElement | None:
    """Find the observed element matching ``expected`` by identity, then by id."""
    identity = expected.identity
    for element in state.elements:
        if element.identity == identity:
            return element
    return state.element_by_id(expected.element_id)
