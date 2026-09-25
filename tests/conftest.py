"""Shared pytest fixtures for the BLAXCY suite.

Deliberately minimal. The one fixture registered here is the section 71 Qt
application: pytest resolves fixtures from ``conftest.py``, and importing the
same fixture into each test module shadows the import (which lints as a
redefinition while telling a reader nothing extra). The fixture itself lives in
``tests/harness/gui.py`` beside the rest of the GUI harness.

Importing this module must not require Qt: ``tests/harness/gui`` imports
PySide6 lazily, inside the functions that need it, so a host without a Qt
binding can still collect the suite and skip the GUI tests honestly.
"""

import os
import tempfile

import pytest

from tests.harness.gui import qt_app

#: Keep the suite's section 70 log files out of the real state directory. The CLI
#: configures logging on every command (``main._configure_logging``), so a test
#: that runs ``main`` would otherwise write into ``~/.local/state/blaxcy``.
#: ``BLAXCY_LOG_DIR`` is the one override ``core.logging_setup`` reads.
os.environ.setdefault("BLAXCY_LOG_DIR", tempfile.mkdtemp(prefix="blaxcy-logs-"))


@pytest.fixture(scope="session", autouse=True)
def _blaxcy_log_dir() -> str:
    """Pin the log directory for the whole session and report where it lives."""
    return os.environ["BLAXCY_LOG_DIR"]


__all__ = ["qt_app"]
