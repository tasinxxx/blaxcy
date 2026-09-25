"""Qt test harness for the section 71 GUI tests.

The GUI is the only part of BLAXCY that needs Qt, so these tests build their own
``QApplication`` instead of relying on a pytest plugin. That is not a workaround
for convenience: on this host PySide6 ships no ``QtTest`` module, and a harness
that cannot start is worse than one that skips honestly.

Three rules match the rest of the suite:

* a test that needs a Qt widget stack skips with a reason when Qt cannot provide
  one, rather than failing;
* nothing here injects input or touches the user's desktop -- the platform is
  forced to ``offscreen``;
* the application is created once per process, because Qt permits exactly one
  ``QApplication`` and destroying it proves nothing.
"""

from __future__ import annotations

import os
import time
from typing import Any

import pytest


def qt_available() -> tuple[bool, str]:
    """Whether a Qt widget stack can be constructed here, plus the honest reason."""
    try:
        from PySide6.QtWidgets import QApplication  # noqa: F401
    except Exception as exc:  # pragma: no cover - depends on the host
        return False, f"PySide6.QtWidgets is not importable: {exc!r}"
    return True, ""


def qt_application() -> Any:
    """Return the live ``QApplication``, creating an offscreen one if needed.

    ``offscreen`` is set before the first ``QApplication`` exists, which is the
    only point at which Qt reads the platform plugin choice.
    """
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    instance = QApplication.instance()
    if instance is None:
        instance = QApplication([])
    return instance


@pytest.fixture(scope="session")
def qt_app() -> Any:
    """A session-scoped offscreen ``QApplication``, or an honest skip."""
    ok, reason = qt_available()
    if not ok:
        pytest.skip(reason)
    return qt_application()


class FakeClock:
    """A monotonic clock a test can advance by hand.

    Used for the section 71 confirmation countdown: the floor is a real constant
    that must not be configurable, so the way to test it quickly is to move the
    clock rather than to shorten the wait.
    """

    def __init__(self, start: float = 1000.0) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        """Move the clock forward."""
        self.now += float(seconds)


def pump(*, times: int = 3, delay: float = 0.0) -> None:
    """Let queued Qt work run (the GUI modules marshal everything through signals)."""
    from PySide6.QtWidgets import QApplication

    for _ in range(max(1, times)):
        QApplication.processEvents()
        if delay:
            time.sleep(delay)


def wait_until(predicate: Any, *, timeout: float = 3.0, delay: float = 0.005) -> bool:
    """Poll ``predicate`` until it is true or ``timeout`` elapses.

    Returns whether it became true; callers assert on the result so a timeout is
    a visible failure rather than a silent wait.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(delay)
    return bool(predicate())


class FakeDialog:
    """A stand-in dialog for the confirmation bridge.

    ``ConfirmationBridge`` accepts a dialog factory so the bridge's *decision*
    logic -- grant, deny, raise, preempt -- can be tested without Qt timing.
    """

    def __init__(self, *, approved: bool = False, raises: bool = False) -> None:
        self.approved = approved
        self._raises = raises
        self.rejected = False
        self.deleted = False
        self.exec_count = 0

    def exec(self) -> int:
        """Simulate the modal dialog returning."""
        self.exec_count += 1
        if self._raises:
            raise RuntimeError("the dialog broke")
        return 1 if self.approved else 0

    def reject(self) -> None:
        """Simulate Escape/a stop dismissing the dialog."""
        self.rejected = True
        self.approved = False

    def deleteLater(self) -> None:  # noqa: N802 - Qt's name
        """Simulate Qt's deferred destruction."""
        self.deleted = True


__all__ = [
    "FakeClock",
    "FakeDialog",
    "pump",
    "qt_app",
    "qt_application",
    "qt_available",
    "wait_until",
]
