"""Input backends (XTEST, xdotool, RemoteDesktop portal, ydotool).

Phase 7 implements the XTEST backend only. It is selected through
:func:`select_backend`, which requires a passing *functional* probe (section 28
rule 12) -- a backend is never chosen because a binary or module merely exists.

``xdotool`` is named in section 30 as a *fallback*, not the primary pointer and
keyboard implementation, and no xdotool backend is implemented yet. When XTEST
is absent, selection returns ``None`` and the capability report says so honestly
rather than silently claiming a fallback works.
"""

from __future__ import annotations

from control.backends.base import BackendProbe, InputBackend, KeyResolution, PointerButton
from control.backends.keys import (
    canonical_key_name,
    is_modifier_name,
    keysym_for_char,
    keysym_for_name,
)
from control.backends.unavailable import UnavailableBackend
from control.backends.xtest import XtestBackend

__all__ = [
    "BackendProbe",
    "InputBackend",
    "KeyResolution",
    "PointerButton",
    "UnavailableBackend",
    "XtestBackend",
    "canonical_key_name",
    "is_modifier_name",
    "keysym_for_char",
    "keysym_for_name",
    "select_backend",
]


def select_backend() -> InputBackend | None:
    """Return the first backend whose functional probe passes, else ``None``.

    Only XTEST is implemented in Phase 7, so this either returns an
    ``XtestBackend`` or nothing at all (section 30). The caller decides what to
    report when nothing is available.
    """
    xtest = XtestBackend()
    probe = xtest.probe()
    if probe.available:
        return xtest
    return None


def resolve_backend() -> InputBackend:
    """A usable backend, or one that refuses every injection honestly.

    The composition root needs *a* backend to construct the input controllers
    with, even on a session where none works. Handing it a real backend whenever
    one passed its probe and :class:`UnavailableBackend` otherwise keeps the
    capability report truthful and the refusal structured (sections 11, 28, 80).
    """
    return select_backend() or UnavailableBackend()
