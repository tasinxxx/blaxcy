"""GUI composition and self-exclusion (specification sections 54, 56, 71).

Two classes of property are asserted here.

**Self-exclusion (section 71).** BLAXCY must not click itself. The guarantee is
applied by code in the composition root, after the user's config is loaded, so a
user config cannot remove it -- and the test proves it end to end by running the
resulting list through the real policy guard.

**The window is a view, not a second path.** It reports what the Body did (a
refused or forced mode is what the selector shows), it calls the same
``set_mode``/stop/takeover entry points the Brain does, and it has no affordance
that could start an arbitrary action.
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest

import gui
from config.settings import SafetySettings, Settings
from core.event_bus import EventBus
from gui.app import self_excluded_settings
from gui.main_window import SELF_EXCLUSION_TOKEN, MainWindow
from policy.guards import check_blocked_application
from schemas.enums import ErrorCode, PolicyMode

pytestmark = pytest.mark.gui


class _Session:
    """The piece of ``SessionInfo`` the window reads."""

    class _Type:
        value = "x11"

    session_type = _Type()


class FakeBody:
    """A stand-in for ``BlaxcyApplication``: records calls, reports a status."""

    def __init__(self, *, report: dict[str, Any] | None = None) -> None:
        self.session = _Session()
        self.bus = EventBus()
        self.capabilities = None
        self.report = report if report is not None else _report()
        self.mode_calls: list[tuple[PolicyMode, str | None]] = []
        self.stop_calls: list[str] = []
        self.takeover_calls: list[str] = []
        self.resume_calls = 0
        self.raise_on_status: Exception | None = None

    def status(self) -> dict[str, Any]:
        if self.raise_on_status is not None:
            raise self.raise_on_status
        return self.report

    def set_mode(self, mode: PolicyMode, *, reason: str | None = None) -> Any:
        self.mode_calls.append((mode, reason))
        # A real Body may refuse or force a mode; the window must show that.
        self.report = {**self.report, "mode": mode.value}
        return None

    def trigger_emergency_stop(self, reason: str = "request") -> Any:
        self.stop_calls.append(reason)
        return _StopResult()

    def reset_emergency_stop(self, *, reason: str = "re-arm") -> bool:
        return True

    def begin_takeover(self, reason: str = "human") -> Any:
        self.takeover_calls.append(reason)
        return None

    def resume_takeover(self) -> Any:
        self.resume_calls += 1
        return None


class _StopResult:
    latched = True
    latency_ms = 0.5
    released_buttons = ()
    released_keys = ()
    errors = ()
    already_latched = False
    forced_observe = True


def _report(**overrides: Any) -> dict[str, Any]:
    report: dict[str, Any] = {
        "started": True,
        "session_type": "x11",
        "mode": "OBSERVE",
        "mode_status": {
            "mode": "OBSERVE",
            "configured_mode": "OBSERVE",
            "autonomous_expires_in_seconds": None,
            "autonomous_active": False,
            "forced": True,
            "reason": "a fresh run starts in OBSERVE",
        },
        "emergency_stop": {"latched": False, "triggers": 0, "last_latency_ms": None},
        "takeover": {"active": False, "reason": None},
        "sequence": None,
        "brain": {"adapter": None, "reason": "no key stored"},
        "state": {"frame_id": 1, "state_version": 1, "generation": 0, "age_ms": 10.0},
        "startup": {"notes": []},
    }
    report.update(overrides)
    return report


# -- Self-exclusion ------------------------------------------------------------


def test_self_exclusion_is_added_by_code_not_by_configuration() -> None:
    """The running GUI's own identity always ends up on the blocked list."""
    settings = Settings()
    assert SELF_EXCLUSION_TOKEN not in settings.safety.blocked_applications
    excluded = self_excluded_settings(settings)
    assert SELF_EXCLUSION_TOKEN in excluded.safety.blocked_applications


def test_self_exclusion_is_idempotent() -> None:
    """Applying it twice does not duplicate the entry."""
    once = self_excluded_settings(Settings())
    twice = self_excluded_settings(once)
    assert twice.safety.blocked_applications.count(SELF_EXCLUSION_TOKEN) == 1


def test_self_exclusion_preserves_the_users_own_entries() -> None:
    """It adds a guarantee; it does not rewrite the user's deny-list."""
    settings = Settings(
        safety=SafetySettings(blocked_applications=("keepassxc", "my-bank"))
    )
    excluded = self_excluded_settings(settings)
    assert "keepassxc" in excluded.safety.blocked_applications
    assert "my-bank" in excluded.safety.blocked_applications
    assert SELF_EXCLUSION_TOKEN in excluded.safety.blocked_applications


def test_self_exclusion_cannot_be_opted_out_of() -> None:
    """Even a config that removes every blocked entry still excludes the GUI."""
    empty = Settings(safety=SafetySettings(blocked_applications=()))
    excluded = self_excluded_settings(empty)
    outcome = check_blocked_application(
        ["BLAXCY Control"], excluded.safety.blocked_applications
    )
    assert outcome.ok is False
    assert outcome.code is ErrorCode.BLOCKED_APPLICATION


def test_the_policy_guard_refuses_the_windows_own_identity() -> None:
    """The guarantee is only real if the real guard refuses it."""
    from policy.guards import check_protected_application

    excluded = self_excluded_settings(Settings())
    for candidate in (SELF_EXCLUSION_TOKEN, SELF_EXCLUSION_TOKEN.lower()):
        blocked = check_blocked_application([candidate], excluded.safety.blocked_applications)
        assert blocked.ok is False
        assert blocked.code is ErrorCode.BLOCKED_APPLICATION
    # And the same token cannot be uploaded to a visual model either.
    uploaded = check_protected_application(
        [SELF_EXCLUSION_TOKEN], excluded.safety.blocked_applications
    )
    assert uploaded.ok is False


# -- The window ----------------------------------------------------------------


def test_the_window_title_carries_the_exclusion_token(qt_app: Any) -> None:
    window = MainWindow(FakeBody())
    try:
        assert SELF_EXCLUSION_TOKEN in window.windowTitle()
    finally:
        window.close()
        window.deleteLater()


def test_the_window_shows_the_bodys_mode_not_the_requested_one(qt_app: Any) -> None:
    """A forced/refused mode is what the operator must see (section 71)."""
    body = FakeBody(report=_report(mode="OBSERVE"))
    window = MainWindow(body)
    try:
        assert window.mode_selector.currentText() == "OBSERVE"
        assert "forced" in window.mode_note.text().lower()
        # Now the Body reports a change the window never asked for.
        window.render_status(_report(mode="AUTONOMOUS"))
        assert window.mode_selector.currentText() == "AUTONOMOUS"
    finally:
        window.close()
        window.deleteLater()


def test_selecting_a_mode_calls_the_bodys_set_mode(qt_app: Any) -> None:
    """The GUI has no mode logic of its own; it asks the Body."""
    body = FakeBody()
    window = MainWindow(body)
    try:
        index = window.mode_selector.findData(PolicyMode.ASSIST.value)
        assert index >= 0
        window.mode_selector.setCurrentIndex(index)
        assert body.mode_calls[-1][0] is PolicyMode.ASSIST
        assert window.mode_selector.currentText() == "ASSIST"
    finally:
        window.close()
        window.deleteLater()


def test_a_failing_status_read_is_shown_rather_than_crashing(qt_app: Any) -> None:
    """A broken status read must be visible, not a silent empty window."""
    body = FakeBody()
    window = MainWindow(body)
    try:
        body.raise_on_status = RuntimeError("the body fell over")
        assert window.refresh() is None
        assert "status read failed" in window.connection.text()
    finally:
        window.close()
        window.deleteLater()


def test_takeover_and_resume_are_explicit_and_reflected(qt_app: Any) -> None:
    body = FakeBody()
    window = MainWindow(body)
    try:
        window.takeover_button.click()
        assert body.takeover_calls
        # The Body now reports an active takeover, so resume becomes available.
        window.render_status(_report(takeover={"active": True, "reason": "operator"}))
        assert window.resume_button.isEnabled() is True
        window.resume_button.click()
        assert body.resume_calls == 1
    finally:
        window.close()
        window.deleteLater()


def test_the_window_cannot_start_an_arbitrary_action(qt_app: Any) -> None:
    """Regression guard: the window must not expose a second dispatch path."""
    window = MainWindow(FakeBody())
    try:
        assert not hasattr(window, "dispatch")
        assert not hasattr(window, "run_task")
        assert not hasattr(window, "build_loop")
    finally:
        window.close()
        window.deleteLater()


# -- Reachability --------------------------------------------------------------


def test_every_documented_gui_module_exists() -> None:
    """``gui/__init__`` names modules; this fails if the names drift from reality."""
    assert len(gui.MODULES) == 7
    for module in gui.MODULES:
        importlib.import_module(module)


def test_importing_the_gui_package_does_not_require_qt() -> None:
    """The package header stays importable without a Qt binding (see gui/__init__)."""
    assert "PySide6" not in getattr(gui, "__dict__", {})
    assert hasattr(gui, "MODULES")


def test_the_cli_exposes_the_gui_command() -> None:
    """Section 71's window must be reachable, and only import Qt when used."""
    import main

    parser = main._build_parser()
    args = parser.parse_args(["gui", "--offscreen"])
    assert args.command == "gui"
    assert args.func is main._cmd_gui
    assert args.offscreen is True
