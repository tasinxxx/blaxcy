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

from tests.harness.gui import qt_app

__all__ = ["qt_app"]
