"""Session detection classification (specification sections 29, 30)."""

from __future__ import annotations

import pytest

from core.session_detector import detect_session
from schemas.enums import SessionType


def _clear_display_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every variable that influences session classification."""
    for var in ("XDG_SESSION_TYPE", "DISPLAY", "WAYLAND_DISPLAY"):
        monkeypatch.delenv(var, raising=False)


def test_detects_x11(monkeypatch: pytest.MonkeyPatch) -> None:
    """An X11 session is reported as X11."""
    _clear_display_env(monkeypatch)
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    monkeypatch.setenv("DISPLAY", ":0")
    info = detect_session()
    assert info.session_type is SessionType.X11
    assert info.is_x11 is True
    assert info.has_x_display is True


def test_detects_xwayland_as_not_native_x11(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wayland + DISPLAY is XWayland, never treated as a real X11 session."""
    _clear_display_env(monkeypatch)
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setenv("DISPLAY", ":0")
    info = detect_session()
    assert info.session_type is SessionType.XWAYLAND
    assert info.is_x11 is False
    assert info.has_x_display is True
    assert info.notes  # the XWayland caveat is recorded


def test_detects_native_wayland(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wayland without DISPLAY is native Wayland."""
    _clear_display_env(monkeypatch)
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    info = detect_session()
    assert info.session_type is SessionType.WAYLAND
    assert info.has_x_display is False


def test_unknown_when_nothing_signals_a_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """Absence of evidence yields UNKNOWN, never a guessed session type."""
    _clear_display_env(monkeypatch)
    info = detect_session()
    assert info.session_type is SessionType.UNKNOWN
