"""Input backend tests (specification sections 28, 30, 50).

The XTEST backend is exercised without a display: a fake X display object stands
in for the real connection so keycode/Shift resolution and the probe's failure
paths are tested deterministically. Nothing here injects input.
"""

from __future__ import annotations

import pytest

from control.backends import (
    PortalRemoteDesktopBackend,
    XtestBackend,
    canonical_key_name,
    is_modifier_name,
    keysym_for_char,
    keysym_for_name,
    select_backend,
)
from control.backends.base import BackendProbe, PointerButton
from schemas.enums import ErrorCode
from schemas.errors import BlaxcyError


class FakeDisplay:
    """A stand-in for ``Xlib.display.Display`` with a fixed keyboard mapping."""

    def __init__(self, keycodes: dict[int, int], keymap: dict[int, list[int]]) -> None:
        self._keycodes = keycodes
        self._keymap = keymap
        self.flushes = 0
        self.closed = False

    def keysym_to_keycode(self, keysym: int) -> int:
        return self._keycodes.get(keysym, 0)

    def get_keyboard_mapping(self, keycode: int, _count: int) -> list[list[int]]:
        return [self._keymap.get(keycode, [])]

    def flush(self) -> None:
        self.flushes += 1

    def close(self) -> None:
        self.closed = True


def _backend_with_display() -> XtestBackend:
    """An XTEST backend whose display is a fake with a US-style mapping."""
    backend = XtestBackend()
    # Injecting a test double for the X display connection.
    backend._display = FakeDisplay(
        keycodes={ord("a"): 38, ord("A"): 38, ord("!"): 10, ord("1"): 10},
        keymap={38: [ord("a"), ord("A"), ord("a"), ord("A"), 0, 0, 0], 10: [49, 33, 49, 33, 0, 0, 0]},
    )
    return backend


# -- Keysym helpers -----------------------------------------------------------


def test_keysym_helpers_map_characters_and_names() -> None:
    """Printable ASCII maps by code point; names and aliases resolve too."""
    assert keysym_for_char("a") == ord("a")
    assert keysym_for_name("ctrl") == 65507
    assert keysym_for_name("enter") == 65293
    assert keysym_for_name("F5") == 65474
    assert keysym_for_name("NotAKey") is None
    assert canonical_key_name("CTRL") == "Control_L"
    assert is_modifier_name("shift") is True
    assert is_modifier_name("Return") is False


# -- XTEST key resolution -----------------------------------------------------


def test_resolve_key_marks_shift_for_uppercase_and_symbols() -> None:
    """Shift is required exactly when the keysym is the shifted level."""
    backend = _backend_with_display()
    plain = backend.resolve_key(ord("a"))
    assert plain is not None and plain.keycode == 38 and plain.shift is False

    upper = backend.resolve_key(ord("A"))
    assert upper is not None and upper.keycode == 38 and upper.shift is True

    symbol = backend.resolve_key(ord("!"))
    assert symbol is not None and symbol.keycode == 10 and symbol.shift is True


def test_resolve_key_returns_none_when_unmapped() -> None:
    """A keysym with no keycode, or not present at levels 0/1, is unresolvable."""
    backend = _backend_with_display()
    assert backend.resolve_key(9999) is None

    backend._display = FakeDisplay(
        keycodes={0x1234: 38}, keymap={38: [ord("a"), ord("A"), 0, 0, 0, 0, 0]}
    )
    assert backend.resolve_key(0x1234) is None


# -- Probe and failure paths --------------------------------------------------


def test_probe_reports_unavailable_when_the_display_cannot_be_opened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connection failure is reported, never turned into a fake success."""

    def _refuse(_self: XtestBackend) -> object:
        raise BlaxcyError(ErrorCode.BACKEND_UNAVAILABLE, "no display")

    monkeypatch.setattr(XtestBackend, "_connect", _refuse)
    probe = XtestBackend().probe()
    assert probe.available is False
    assert probe.reason is not None and "no display" in probe.reason


def test_injection_without_a_display_raises_backend_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Injection must fail loudly, not silently no-op, without a display."""

    def _refuse(_self: XtestBackend) -> object:
        raise BlaxcyError(ErrorCode.BACKEND_UNAVAILABLE, "no display")

    monkeypatch.setattr(XtestBackend, "_connect", _refuse)
    backend = XtestBackend()
    with pytest.raises(BlaxcyError) as excinfo:
        backend.move_pointer(1, 1)
    assert excinfo.value.code is ErrorCode.BACKEND_UNAVAILABLE
    with pytest.raises(BlaxcyError):
        backend.press_button(PointerButton.LEFT)


def test_release_all_and_close_are_safe_without_a_connection() -> None:
    """Hygiene/teardown must never raise when there is nothing to release."""
    backend = XtestBackend()
    backend.release_all()
    backend.close()
    assert backend._display is None


def test_select_backend_returns_none_when_both_probes_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Selection is driven by the probes, not by module availability."""

    def _down(_self: object) -> BackendProbe:
        return BackendProbe("x", False, "unavailable")

    monkeypatch.setattr(XtestBackend, "probe", _down)
    monkeypatch.setattr(PortalRemoteDesktopBackend, "probe", _down)
    assert select_backend() is None


def test_select_backend_returns_xtest_when_the_probe_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A passing XTEST probe is preferred, so the portal is not even consulted."""

    def _up(_self: XtestBackend) -> BackendProbe:
        return BackendProbe("xtest", True, None, {"xtest_version": "2.2"})

    monkeypatch.setattr(XtestBackend, "probe", _up)
    backend = select_backend()
    assert isinstance(backend, XtestBackend)
    assert backend.name == "xtest"


def test_select_backend_falls_back_to_the_portal(monkeypatch: pytest.MonkeyPatch) -> None:
    """When XTEST has no display, a probed RemoteDesktop portal is selected."""

    def _down(_self: object) -> BackendProbe:
        return BackendProbe("xtest", False, "no display")

    def _up(_self: object) -> BackendProbe:
        return BackendProbe("portal-remotedesktop", True, None, {"interface_version": 2})

    monkeypatch.setattr(XtestBackend, "probe", _down)
    monkeypatch.setattr(PortalRemoteDesktopBackend, "probe", _up)
    backend = select_backend()
    assert isinstance(backend, PortalRemoteDesktopBackend)
    assert backend.name == "portal-remotedesktop"
