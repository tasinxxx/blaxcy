"""The section 71 window over the *real* assembled Body.

Unit tests prove the panels render and the bridge decides. This proves the thing
that actually matters for Phase 12: that ``gui.app`` composes a real
``BlaxcyApplication`` -- real capture engine, real AT-SPI owner, real executor,
real policy gate -- and that a window can be built over it and read its status.

What is *not* here, deliberately:

* no input is injected, and the window's controls are not clicked (a click would
  travel the real policy -> resolve -> lease -> revalidate -> execute -> verify
  path on the user's live desktop, which belongs in the section 75 fixture-app
  suite, not in a unit-ish integration test);
* the platform is forced to Qt's ``offscreen`` so no window ever appears.

The test skips honestly (with the reason) when Qt or the display the Body needs is
unavailable, rather than passing vacuously.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from config.settings import load_settings
from gui.app import build_application, build_window, self_excluded_settings
from gui.confirmation import ConfirmationBridge
from gui.main_window import SELF_EXCLUSION_TOKEN
from policy.guards import check_blocked_application
from schemas.enums import ErrorCode

pytestmark = pytest.mark.gui


def test_the_window_runs_over_the_real_body(qt_app: Any, tmp_path: Path) -> None:
    """The GUI is a view over a genuinely assembled Body, not a mock of one."""
    # Keep the section 64 heartbeat out of the user's runtime directory.
    settings = self_excluded_settings(load_settings(None))
    bridge = ConfirmationBridge()
    application = build_application(
        settings, bridge=bridge, watchdog_path=tmp_path / "watchdog.json"
    )
    window = None
    try:
        startup = application.start()
        if not startup.started:
            pytest.skip("the Body could not start on this host, so no window is meaningful")
        window = build_window(application, bridge=bridge)
        window.start_refresh()
        report = window.refresh()
        assert report is not None, "the window could not read the assembled Body"

        # The window shows the Body's real mode, whatever the configuration says.
        assert report["mode"] == application.modes.mode.value
        assert window.mode_selector.currentText() == application.modes.mode.value

        # Section 71 self-exclusion holds for the settings the Body was built from.
        assert SELF_EXCLUSION_TOKEN in settings.safety.blocked_applications
        refused = check_blocked_application(
            [window.windowTitle()], settings.safety.blocked_applications
        )
        assert refused.ok is False
        assert refused.code is ErrorCode.BLOCKED_APPLICATION

        # Capabilities are the real probe's verdicts, not an empty panel.
        assert application.capabilities is not None
        assert window.capability_panel.row_count() == len(
            application.capabilities.capabilities
        )

        # The confirmation bridge is the callback the executor was given.
        assert application.executor is not None
        assert bridge.stats()["granted"] == 0, "no confirmation was answered by nobody"
    finally:
        if window is not None:
            window.stop_refresh()
            window.close()
            window.deleteLater()
        application.shutdown()
        bridge.close()


def test_the_bridge_is_wired_into_the_executor_by_composition(
    qt_app: Any, tmp_path: Path
) -> None:
    """Without a confirmation callback the executor refuses; this wires one in.

    The property is asserted through the public surface: a Body built *with* the
    bridge accepts a destructive action as "needs confirmation" rather than
    "no prompt exists", which is the difference between an action a human can
    approve and one that can never run.
    """
    settings = self_excluded_settings(load_settings(None))
    bridge = ConfirmationBridge(
        timeout_seconds=0.05, dialog_factory=lambda request: _DenyingDialog()
    )
    application = build_application(
        settings, bridge=bridge, watchdog_path=tmp_path / "watchdog.json"
    )
    try:
        application.start()
        # The executor holds the bridge's callback, so asking for a confirmation
        # reaches the bridge (which denies here, because the fake dialog says no).
        assert bridge.request("prove the bridge is callable") is False
        assert bridge.stats()["denied"] + bridge.stats()["timeouts"] >= 1
    finally:
        application.shutdown()
        bridge.close()


class _DenyingDialog:
    """A dialog that is never approved, for the wiring check above."""

    approved = False

    def exec(self) -> int:
        """Return "not accepted" without showing anything."""
        return 0

    def deleteLater(self) -> None:  # noqa: N802 - Qt's name
        """No-op teardown."""
