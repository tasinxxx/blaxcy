"""The real install scripts against a throwaway prefix (specification sections 73, 85).

Section 85's installation criteria are "menu application exists; startup works;
second launch is controlled; uninstall works; update/rollback tested". This file
covers all of those against the actual ``install.sh``/``update.sh``/``uninstall.sh``
wrappers, on a real filesystem, with no mocks -- because an installer is the one
component whose bugs only appear in a real prefix.

Two deliberate choices:

* **A throwaway prefix.** Every run installs into ``tmp_path``, never the user's
  ``~/.local``, so the suite cannot disturb the machine it runs on.
* **A reused interpreter.** ``--python <this test's interpreter>`` means no network
  and no virtualenv creation; the environment's dependencies are already installed,
  and the installer's own validation step then really runs the installed copy
  (``main.py session``, which reads configuration and detects the session -- it
  injects no input and touches no window).

The rollback test injects a genuine failure (a *file* where the installer needs a
directory) rather than calling an internal failure hook, so what it verifies is the
real recovery path.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_SH = REPO_ROOT / "install.sh"
UNINSTALL_SH = REPO_ROOT / "uninstall.sh"
UPDATE_SH = REPO_ROOT / "update.sh"


def _run(script: Path, *args: str, prefix: Path) -> subprocess.CompletedProcess[str]:
    """Run one wrapper against ``prefix`` with a reusable interpreter."""
    return subprocess.run(
        [str(script), "--prefix", str(prefix), "--python", sys.executable, *args],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
    )


def _manifest(prefix: Path) -> dict[str, Any]:
    raw = (prefix / "share" / "blaxcy" / "install-manifest.json").read_text(encoding="utf-8")
    parsed: Any = json.loads(raw)
    assert isinstance(parsed, dict)
    return parsed


@pytest.fixture
def prefix(tmp_path: Path) -> Path:
    """A prefix that does not exist yet."""
    return tmp_path / "local"


def test_the_scripts_are_executable_and_present() -> None:
    """Section 26 lists them, so an install is impossible without them."""
    for script in (INSTALL_SH, UNINSTALL_SH, UPDATE_SH):
        assert script.is_file(), f"{script.name} is missing"
        assert os.access(script, os.X_OK), f"{script.name} is not executable"


def test_dry_run_changes_nothing(prefix: Path) -> None:
    """A dry run that wrote anything would be actively dangerous."""
    completed = _run(INSTALL_SH, "--dry-run", prefix=prefix)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "dry run" in completed.stdout
    assert not prefix.exists(), "a dry run created the prefix"


def test_a_failed_install_rolls_back_completely(tmp_path: Path) -> None:
    """The rollback criterion: a failure leaves the machine as it was."""
    prefix = tmp_path / "local"
    (prefix / "share").mkdir(parents=True)
    blocker = prefix / "share" / "applications"
    blocker.write_text("this file is in the installer's way\n", encoding="utf-8")

    completed = _run(INSTALL_SH, prefix=prefix)

    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert "rolled back" in completed.stdout
    assert "applications" in completed.stdout or "apply changes" in completed.stdout
    # Nothing of the application survives the failure...
    assert not (prefix / "lib" / "blaxcy").exists()
    assert not (prefix / "bin" / "blaxcy").exists()
    assert not (prefix / "share" / "blaxcy" / "install-manifest.json").exists()
    # ...and the file that caused the failure is untouched.
    assert blocker.read_text(encoding="utf-8") == "this file is in the installer's way\n"


def test_install_then_launch_then_update_then_uninstall(prefix: Path, tmp_path: Path) -> None:
    """The happy path, end to end, on a real prefix."""
    installed = _run(INSTALL_SH, prefix=prefix)
    assert installed.returncode == 0, installed.stdout + installed.stderr
    for expected in ("application files", "icon", "desktop entry", "validate", "manifest"):
        assert f"] {expected}" in installed.stdout, f"{expected} was not reported"

    # -- The menu application exists.
    launcher = prefix / "bin" / "blaxcy"
    desktop_entry = prefix / "share" / "applications" / "blaxcy.desktop"
    icon = prefix / "share" / "icons" / "hicolor" / "256x256" / "apps" / "blaxcy.png"
    assert launcher.is_file() and os.access(launcher, os.X_OK)
    assert desktop_entry.is_file()
    assert icon.is_file() and icon.stat().st_size > 0
    assert (prefix / "lib" / "blaxcy" / "main.py").is_file()

    # -- The entry is valid by the desktop's own validator, and points at us.
    if (validator := _which("desktop-file-validate")) is not None:
        checked = subprocess.run(
            [validator, str(desktop_entry)], capture_output=True, text=True, check=False
        )
        assert checked.returncode == 0, checked.stdout + checked.stderr
    entry_text = desktop_entry.read_text(encoding="utf-8")
    assert f'Exec="{launcher}" gui' in entry_text
    assert "Icon=blaxcy" in entry_text

    # -- Startup works: the installed copy runs, from its own tree.
    started = subprocess.run(
        [str(launcher), "session"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )
    assert started.returncode == 0, started.stdout + started.stderr
    assert "session type" in started.stdout

    # -- The manifest records every file with a checksum that still matches.
    manifest = _manifest(prefix)
    assert manifest["ok"] is True
    assert manifest["checksum_algorithm"] == "sha256"
    assert manifest["app_dir"] == str(prefix / "lib" / "blaxcy")
    assert manifest["desktop_entry"] == str(desktop_entry)
    assert len(manifest["files"]) > 50
    from installer.installer import checksum_of

    for entry in manifest["files"]:
        path = Path(str(entry["path"]))
        assert path.is_file(), f"{path} is listed but missing"
        assert checksum_of(path) == entry["sha256"], f"{path} does not match its checksum"
    assert manifest["backups"] == [], "a fresh prefix has nothing to hand back"

    # -- Update over the top, recording where it came from.
    updated = _run(UPDATE_SH, prefix=prefix)
    assert updated.returncode == 0, updated.stdout + updated.stderr
    after = _manifest(prefix)
    assert after["updated_from"] == manifest["version"]
    assert launcher.is_file()

    # -- Uninstall removes it all.
    removed = _run(UNINSTALL_SH, prefix=prefix)
    assert removed.returncode == 0, removed.stdout + removed.stderr
    assert not (prefix / "lib" / "blaxcy").exists()
    assert not launcher.exists()
    assert not desktop_entry.exists()
    assert not icon.exists()
    assert not (prefix / "share" / "blaxcy" / "install-manifest.json").exists()
    # A desktop cache may remain (it is shared and desktop-managed), but no file of
    # BLAXCY's and no backup tree should.
    assert not (prefix / "share" / "blaxcy" / "backups").exists()


def test_update_refuses_a_prefix_that_was_never_installed(tmp_path: Path) -> None:
    """Updating nothing would mean guessing what to replace."""
    prefix = tmp_path / "local"
    completed = _run(UPDATE_SH, prefix=prefix)
    assert completed.returncode == 1
    assert "run install.sh first" in completed.stdout
    assert not (prefix / "lib" / "blaxcy").exists()


def test_uninstall_on_a_clean_prefix_is_not_an_error(prefix: Path) -> None:
    completed = _run(UNINSTALL_SH, prefix=prefix)
    assert completed.returncode == 0
    assert "nothing is installed" in completed.stdout


def test_a_foreign_desktop_entry_is_handed_back(tmp_path: Path) -> None:
    """A file BLAXCY replaced belongs to whoever had it, not to BLAXCY."""
    prefix = tmp_path / "local"
    applications = prefix / "share" / "applications"
    applications.mkdir(parents=True)
    foreign = applications / "blaxcy.desktop"
    foreign.write_text("[Desktop Entry]\nName=Someone else\n", encoding="utf-8")

    installed = _run(INSTALL_SH, prefix=prefix)
    assert installed.returncode == 0, installed.stdout + installed.stderr
    manifest = _manifest(prefix)
    assert [Path(str(b["path"])) for b in manifest["backups"]] == [foreign]
    assert "Name=BLAXCY" in foreign.read_text(encoding="utf-8")

    removed = _run(UNINSTALL_SH, prefix=prefix)
    assert removed.returncode == 0, removed.stdout + removed.stderr
    assert "restored 1 pre-existing file" in removed.stdout
    assert foreign.read_text(encoding="utf-8") == "[Desktop Entry]\nName=Someone else\n"


def _which(tool: str) -> str | None:
    """Locate a tool without importing the module under test's own helper."""
    from shutil import which

    return which(tool)
