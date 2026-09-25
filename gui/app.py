"""GUI composition entry point (specification sections 56, 63, 71).

This module is the *only* place the GUI meets the Body, and it exists so that
meeting is explicit and testable:

```text
settings ──► self_excluded_settings() ──► BlaxcyApplication(confirmation=bridge.request)
                                                     │
                              ConfirmationBridge ◄───┴─── MainWindow (read-only view)
```

Three things are deliberately not configurable here:

* **Self-exclusion.** :func:`self_excluded_settings` adds BLAXCY's own window
  identity to the policy's blocked-application list *in code*, after the user's
  config has been loaded. A user config therefore cannot remove the guarantee
  that BLAXCY will not click itself (section 71).
* **The confirmation bridge.** The Body is constructed with
  ``confirmation=bridge.request``; without a confirmation callback the executor
  refuses a destructive action with ``CONFIRMATION_REQUIRED``, and the Brain loop
  halts rather than proceeding unattended. The GUI is what makes those
  confirmations answerable by a human.
* **No ``--yes``/``--confirm`` flag.** Answering a confirmation in advance is
  exactly the bypass sections 54 and 71 forbid, so there is no flag for it.

Qt is imported lazily inside :func:`run` so that ``import gui.app`` (and the rest
of BLAXCY) never requires a Qt binding to be present.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

from config.settings import Settings, load_settings
from gui.confirmation import ConfirmationBridge
from gui.main_window import BLAXCY_WINDOW_TITLE, SELF_EXCLUSION_TOKEN, MainWindow


def self_excluded_settings(settings: Settings) -> Settings:
    """Add BLAXCY's own window to the blocked-application list (section 71).

    The policy matcher is a case-folded substring test against the owning
    application, the window title and the window class, so putting this token on
    the list makes BLAXCY's own window unmatchable as a target. It is fail-closed
    and it costs at most a refusal: an unrelated window whose title happens to
    contain the token is refused too, which is the safe direction.
    """
    blocked = tuple(settings.safety.blocked_applications)
    token = SELF_EXCLUSION_TOKEN.casefold()
    if any(token in entry.casefold() for entry in blocked):
        return settings
    return settings.model_copy(
        update={
            "safety": settings.safety.model_copy(
                update={"blocked_applications": (*blocked, SELF_EXCLUSION_TOKEN)}
            )
        }
    )


def build_application(
    settings: Settings, *, bridge: ConfirmationBridge, **overrides: Any
) -> Any:
    """Construct the one Body the window is a view over.

    Imported inside the function so the GUI's composition stays independent of
    the import order between ``core.application`` and this module.

    ``overrides`` are passed straight to ``BlaxcyApplication``: those are the
    component overrides it already documents for testability, and they substitute
    for a *transport* (a fake backend, a temporary watchdog path) -- never for a
    safety decision. The confirmation bridge is not overridable.
    """
    from core.application import BlaxcyApplication

    application = BlaxcyApplication(settings, confirmation=bridge.request, **overrides)
    # The bridge refuses a pending request when a stop latches or a human takes
    # over, so it needs the same bus those events travel on (sections 62, 63).
    bridge.attach(application.bus)
    return application


def build_window(application: Any, *, bridge: ConfirmationBridge) -> MainWindow:
    """Build the window over an assembled Body."""
    return MainWindow(application, bridge=bridge)


def run(argv: list[str] | None = None, *, settings: Settings | None = None) -> int:
    """Start the GUI over a real Body and return a process exit code."""
    parser = argparse.ArgumentParser(
        prog="blaxcy gui",
        description="BLAXCY graphical interface (a view over the assembled Body).",
    )
    parser.add_argument(
        "--offscreen",
        action="store_true",
        help="force the Qt offscreen platform (no display server needed)",
    )
    parser.add_argument("--config", default=None, help="explicit TOML config path")
    args = parser.parse_args(argv)

    if args.offscreen:
        # Must be set before QApplication is constructed.
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from PySide6.QtWidgets import QApplication

    qt: Any = QApplication.instance()
    if qt is None:
        qt = QApplication(sys.argv[:1])
    # Both the window title and the Qt application name carry the exclusion
    # token, so the window class matches the blocked-list entry too.
    qt.setApplicationName(BLAXCY_WINDOW_TITLE)
    qt.setApplicationDisplayName(BLAXCY_WINDOW_TITLE)

    resolved = settings if settings is not None else load_settings(
        Path(args.config) if args.config else None
    )
    resolved = self_excluded_settings(resolved)

    bridge = ConfirmationBridge()
    application = build_application(resolved, bridge=bridge)
    application.start()
    window = build_window(application, bridge=bridge)
    window.start_refresh()
    window.show()
    try:
        return int(qt.exec())
    finally:
        # The Body is shut down in every exit path: held input is released, the
        # watchdog stops clean and the clipboard selection is handed back.
        window.stop_refresh()
        application.shutdown()
        bridge.close()


if __name__ == "__main__":  # pragma: no cover - manual entry
    raise SystemExit(run())
