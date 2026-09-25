"""Real-display window-manager integration test (specification section 47).

This runs against the live X11 display and exercises only the **read** side of
window control: the functional probe, the active window, the client list and one
window's metadata. It deliberately never calls ``activate`` or
``ensure_active`` -- moving the user's focus is a real desktop change and belongs
behind policy, a lease and revalidation, not in a diagnostic test.

If there is no display, or no window manager publishing EWMH properties, the
test skips with an honest reason rather than fabricating a result.
"""

from __future__ import annotations

import os

import pytest

from control.window_manager import WindowInfo, WindowManager

pytestmark = pytest.mark.skipif(
    not os.environ.get("DISPLAY"),
    reason="real-display window test requires an X display (DISPLAY unset)",
)


def _require_manager() -> WindowManager:
    """Return a window manager, skipping when the functional probe fails."""
    manager = WindowManager()
    probe = manager.probe()
    if not probe.available:
        pytest.skip(f"window control is not available: {probe.reason}")
    return manager


def test_window_manager_probe_is_functional() -> None:
    """The probe opens a real connection and reads the live tree."""
    manager = WindowManager()
    probe = manager.probe()
    if not probe.available:
        pytest.skip(f"window control is not available: {probe.reason}")
    assert probe.backend == "python-xlib"
    # ``ewmh`` records whether the window manager publishes _NET_ACTIVE_WINDOW;
    # a session without it is a real (degraded) condition, not a test failure.
    assert "ewmh" in probe.details
    manager.close()


def test_active_window_is_reported_as_an_integer_or_honestly_unknown() -> None:
    """The active window is read from the window manager, never invented."""
    manager = _require_manager()
    active = manager.active_window()
    assert active is None or isinstance(active, int)
    manager.close()


def test_client_list_returns_window_metadata() -> None:
    """Every listed window carries the metadata shape the envelope uses."""
    manager = _require_manager()
    windows = manager.list_windows()
    for window in windows:
        assert isinstance(window, WindowInfo)
        assert window.window_id > 0
        # A window may legitimately have no title or class; what must not happen
        # is a fabricated value, so only the shape is asserted.
        assert window.title is None or isinstance(window.title, str)
    manager.close()


def test_window_info_for_the_active_window_reports_it() -> None:
    """Metadata lookup for a live window returns that window, not another."""
    manager = _require_manager()
    active = manager.active_window()
    if active is None:
        pytest.skip("the window manager does not publish an active window on this session")
    info = manager.window_info(active)
    assert info is not None
    assert info.window_id == active
    manager.close()
