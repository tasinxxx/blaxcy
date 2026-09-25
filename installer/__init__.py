"""BLAXCY's installer (specification section 73).

A package rather than a shell script with the logic inline, for one reason: the
installer is the component most likely to be run in a state the developer cannot
see, and the process it performs -- preflight, staged copy, checksums, rollback --
is exactly the kind of thing that has to be *tested* rather than hoped at.
``install.sh``/``uninstall.sh``/``update.sh`` remain the documented entry points
(§26) and each one is a three-line wrapper around this package.

**Standard library only.** This code runs before the virtualenv and the project's
dependencies exist, so it must not import anything BLAXCY itself depends on (no
pydantic, no tomlkit). ``tomllib`` is used to read the version because it is
stdlib in Python 3.11+, and the application is reached only by running it as a
subprocess. Nothing in this package may import from ``core``/``config``/``schemas``.
"""

from installer.installer import (
    APP_NAME,
    Installer,
    InstallError,
    InstallReport,
    StepResult,
)

__all__ = ["APP_NAME", "InstallError", "InstallReport", "Installer", "StepResult"]
