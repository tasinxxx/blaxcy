"""Keysym helpers for keyboard injection (specification sections 50, 52).

X11 keysyms for Latin-1 (``0x20``-``0xff``) equal their Unicode code points, so
a printable character maps to a keysym by :func:`ord`. Named keys (``Return``,
``F5``, ``Shift_L``) use the X keysym name table, exposed through ``Xlib.XK``.

``Xlib`` is imported lazily so this module stays importable (and this package
stays probeable) on a host without python-xlib -- the probe then reports the
capability honestly rather than the import failing the whole process.
"""

from __future__ import annotations

from typing import Any

#: Friendly names mapped to their canonical X keysym names.
MODIFIER_ALIASES: dict[str, str] = {
    "ctrl": "Control_L",
    "control": "Control_L",
    "shift": "Shift_L",
    "alt": "Alt_L",
    "super": "Super_L",
    "meta": "Super_L",
    "win": "Super_L",
    "cmd": "Super_L",
}

#: Friendly names mapped to canonical X keysym names for non-modifier keys.
KEY_ALIASES: dict[str, str] = {
    "enter": "Return",
    "return": "Return",
    "esc": "Escape",
    "escape": "Escape",
    "del": "Delete",
    "delete": "Delete",
    "backspace": "BackSpace",
    "space": "space",
    "tab": "Tab",
    "pgup": "Page_Up",
    "pgdn": "Page_Down",
    "pageup": "Page_Up",
    "pagedown": "Page_Down",
}

#: Names that identify a modifier key (used for section 52 hygiene tracking).
MODIFIER_NAMES: frozenset[str] = frozenset(
    {
        "Shift_L",
        "Shift_R",
        "Control_L",
        "Control_R",
        "Alt_L",
        "Alt_R",
        "Super_L",
        "Super_R",
        "Meta_L",
        "Meta_R",
        "Caps_Lock",
    }
)

#: Characters that map to a named key rather than to their own code point.
CONTROL_CHAR_KEYSYMS: dict[str, int] = {
    "\n": 0xFF0D,  # Return
    "\r": 0xFF0D,
    "\t": 0xFF09,  # Tab
}


def _xk() -> Any:
    """Return the ``Xlib.XK`` module, or ``None`` when Xlib is absent."""
    try:
        from Xlib import XK
    except Exception:
        return None
    return XK


def canonical_key_name(name: str) -> str:
    """Map a friendly key name to its canonical X keysym name.

    Modifier aliases win, then key aliases; an unknown name is returned as-is
    so a caller may still pass an exact X keysym name (``"F5"``).
    """
    stripped = name.strip()
    if not stripped:
        return stripped
    lowered = stripped.casefold()
    if lowered in MODIFIER_ALIASES:
        return MODIFIER_ALIASES[lowered]
    if lowered in KEY_ALIASES:
        return KEY_ALIASES[lowered]
    return stripped


def keysym_for_char(char: str) -> int | None:
    """Return the keysym for a single character, or ``None``.

    Printable ASCII maps by code point (X11 Latin-1); newline/tab map to their
    named keysyms. Anything else (a multi-character string, a non-Latin-1 code
    point) returns ``None`` -- direct injection cannot produce it, and the caller
    must decide between the clipboard path and an honest ``UNICODE_UNSUPPORTED``
    (section 50).
    """
    if len(char) != 1:
        return None
    if char in CONTROL_CHAR_KEYSYMS:
        return CONTROL_CHAR_KEYSYMS[char]
    code = ord(char)
    if 0x20 <= code <= 0x7E:
        return code
    return None


def keysym_for_name(name: str) -> int | None:
    """Return the keysym for a key name, or ``None`` when unknown.

    Accepts friendly aliases (``"ctrl"``, ``"enter"``) and exact X keysym names
    (``"F5"``, ``"Return"``). A single printable character is accepted too, for
    symmetry with :func:`keysym_for_char`.
    """
    canonical = canonical_key_name(name)
    if len(canonical) == 1:
        return keysym_for_char(canonical)
    xk = _xk()
    if xk is None:
        return None
    keysym = int(xk.string_to_keysym(canonical))
    return keysym if keysym > 0 else None


def is_modifier_name(name: str) -> bool:
    """True when ``name`` names a modifier key."""
    return canonical_key_name(name) in MODIFIER_NAMES
