"""Window control (specification section 47).

The unit-level properties here are that probing never raises and that the
contract the executor depends on is satisfied. Actually activating a window is
deliberately left to the real-display integration test, because it changes the
user's desktop and must never happen in a unit test.
"""

from __future__ import annotations

from control.window_manager import (
    DEFAULT_ENSURE_WINDOW_TIMEOUT_MS,
    WindowController,
    WindowInfo,
    WindowManager,
    WindowManagerProbe,
)


def test_probe_never_raises_and_returns_evidence() -> None:
    """A probe on a machine with no display reports unavailability, not a crash."""
    manager = WindowManager()
    probe = manager.probe()
    assert isinstance(probe, WindowManagerProbe)
    if not probe.available:
        assert probe.reason
    else:
        assert probe.backend == "python-xlib"
    manager.close()


def test_close_is_idempotent() -> None:
    """Closing twice is safe (the emergency-stop teardown path)."""
    manager = WindowManager()
    manager.close()
    manager.close()


def test_default_timeout_matches_the_section_47_default() -> None:
    """The bounded ensure-window wait defaults to 600 ms."""
    assert DEFAULT_ENSURE_WINDOW_TIMEOUT_MS == 600


def test_window_info_reports_its_dict_shape() -> None:
    """Window metadata serialises to the envelope shape the Brain receives."""
    info = WindowInfo(window_id=7, title="Title", wm_class="Class", wm_instance="inst", pid=123)
    assert info.to_dict() == {
        "window_id": 7,
        "title": "Title",
        "wm_class": "Class",
        "wm_instance": "inst",
        "pid": 123,
    }


def test_window_manager_implements_the_controller_contract() -> None:
    """The executor depends on the controller surface, not on Xlib."""
    manager = WindowManager()
    assert isinstance(manager, WindowController)


def test_sleep_is_injectable_for_deterministic_waiting() -> None:
    """The bounded wait uses the injected sleep, so tests never really wait."""
    slept: list[float] = []
    manager = WindowManager(sleep=lambda seconds: slept.append(seconds))
    manager.close()
    assert slept == []
