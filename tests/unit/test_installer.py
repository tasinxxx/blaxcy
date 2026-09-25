"""Installer helpers (specification section 73).

The installer's *effects* are covered by the integration test that runs the real
scripts against a throwaway prefix. This file covers the parts that benefit from
being pinned down precisely and cheaply: what gets installed, what the generated
launcher and desktop entry contain, how the version is read, and how a package
manager is detected.

The desktop entry is deliberately the interesting one: it is the file that decides
whether BLAXCY appears in the menu, and its ``Exec`` line is the only part the
installer rewrites, so that rewrite is asserted rather than assumed.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from installer.installer import (
    APP_FILES,
    APP_PACKAGES,
    DESKTOP_FILE_NAME,
    ICON_PIXELS,
    Installer,
    checksum_of,
    default_prefix,
    desktop_entry_matches_launcher,
    desktop_entry_text,
    detect_package_manager,
    is_system_prefix,
    iter_app_files,
    launcher_text,
    project_version,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


# -- Version and checksums -----------------------------------------------------


def test_the_project_version_is_read_from_pyproject() -> None:
    """The manifest records the real version, not a literal."""
    assert project_version(REPO_ROOT) == "0.1.0"


def test_a_missing_or_broken_pyproject_reports_unknown(tmp_path: Path) -> None:
    """An unreadable version is ``unknown`` -- never an invented one."""
    assert project_version(tmp_path) == "unknown"
    (tmp_path / "pyproject.toml").write_text("this is not toml [[[", encoding="utf-8")
    assert project_version(tmp_path) == "unknown"


def test_checksum_matches_hashlib(tmp_path: Path) -> None:
    payload = tmp_path / "file.bin"
    payload.write_bytes(bytes(range(256)) * 4096)
    expected = hashlib.sha256(payload.read_bytes()).hexdigest()
    assert checksum_of(payload) == expected


# -- What gets installed -------------------------------------------------------


def test_the_application_file_list_is_explicit_and_excludes_build_junk() -> None:
    pairs = iter_app_files(REPO_ROOT)
    relatives = {str(relative) for _source, relative in pairs}
    for name in APP_FILES:
        assert name in relatives, f"{name} should be installed"
    for package in APP_PACKAGES:
        assert any(rel.startswith(f"{package}/") for rel in relatives), package
    assert not any("__pycache__" in rel for rel in relatives)
    assert not any(rel.endswith((".pyc", ".pyo")) for rel in relatives)
    # Version control, the repository's own venv, tests and docs are not the app.
    assert not any(rel.startswith(("tests/", ".git/", ".venv/", "docs/")) for rel in relatives)
    assert not any(rel.endswith(".desktop") for rel in relatives), (
        "the desktop entry belongs in the XDG applications dir, not the app tree"
    )


def test_the_file_list_is_deterministic() -> None:
    """A stable order is what makes the manifest's checksums comparable."""
    first = [str(relative) for _source, relative in iter_app_files(REPO_ROOT)]
    second = [str(relative) for _source, relative in iter_app_files(REPO_ROOT)]
    assert first == second
    assert first == sorted(first)


def test_a_package_with_caches_is_pruned(tmp_path: Path) -> None:
    """The filter is exercised on a synthetic tree, not only on this repository.

    The package has to be one of :data:`APP_PACKAGES` to be walked at all -- the
    list is explicit, so a stray directory is never installed by accident.
    """
    package = tmp_path / APP_PACKAGES[0]
    (package / "__pycache__").mkdir(parents=True)
    (package / "__pycache__" / "cached.cpython-314.pyc").write_bytes(b"\x00")
    (package / "module.py").write_text("x = 1\n", encoding="utf-8")
    (package / "stale.pyc").write_bytes(b"\x00")
    pairs = iter_app_files(tmp_path)
    relatives = {str(relative) for _source, relative in pairs}
    assert f"{APP_PACKAGES[0]}/module.py" in relatives
    assert not any("__pycache__" in rel or rel.endswith(".pyc") for rel in relatives)


# -- Generated files -----------------------------------------------------------


def test_the_launcher_forwards_its_arguments() -> None:
    text = launcher_text("/opt/python", Path("/p/lib/blaxcy"))
    assert text.startswith("#!/bin/sh\n")
    assert '"/opt/python"' in text
    assert '"/p/lib/blaxcy/main.py"' in text
    assert '"$@"' in text, "the desktop entry relies on forwarding `gui`"


def test_the_desktop_entry_rewrites_only_what_depends_on_the_prefix() -> None:
    template = (REPO_ROOT / "desktop" / DESKTOP_FILE_NAME).read_text(encoding="utf-8")
    launcher = Path("/home/someone/.local/bin/blaxcy")
    text = desktop_entry_text(template, launcher=launcher, version="9.9.9")
    assert text.startswith("[Desktop Entry]")
    assert f'Exec="{launcher}" gui' in text
    assert "X-BLAXCY-Version=9.9.9" in text
    assert "Name=BLAXCY" in text
    assert "Categories=Utility;Accessibility;" in text
    assert desktop_entry_matches_launcher(text, launcher)


def test_the_installed_entry_drops_the_maintainer_commentary() -> None:
    """The comments are documentation for the repository, not for the menu."""
    template = (REPO_ROOT / "desktop" / DESKTOP_FILE_NAME).read_text(encoding="utf-8")
    assert template.startswith("#"), "the template is expected to carry its own notes"
    text = desktop_entry_text(template, launcher=Path("/p/bin/blaxcy"), version="1.0")
    assert not any(line.startswith("#") for line in text.splitlines())
    assert "[Desktop Entry]" in text


def test_the_entry_is_set_for_desktop_file_validate() -> None:
    """The exact rules the desktop's own validator enforces."""
    template = (REPO_ROOT / "desktop" / DESKTOP_FILE_NAME).read_text(encoding="utf-8")
    text = desktop_entry_text(template, launcher=Path("/p/bin/blaxcy"), version="1.0")
    values = {
        key: value
        for line in text.splitlines()
        if line and not line.startswith("[") and "=" in line
        for key, value in [line.split("=", 1)]
    }
    assert values["Type"] == "Application"
    assert values["Icon"], "an entry with no Icon shows a blank square"
    assert values["Name"]
    assert values["Exec"].endswith(" gui")
    for key in ("Categories", "Keywords"):
        assert values[key].endswith(";"), f"{key} must be a semicolon-terminated list"


def test_a_template_without_exec_is_a_hard_error() -> None:
    """Renaming the Exec line must fail loudly rather than install a broken entry."""
    with pytest.raises(ValueError, match="no Exec= line"):
        desktop_entry_text("[Desktop Entry]\nName=X\n", launcher=Path("/b"), version="1")


def test_a_template_without_a_version_gets_one_appended() -> None:
    text = desktop_entry_text(
        "[Desktop Entry]\nExec=blaxcy gui\nName=BLAXCY\n", launcher=Path("/b"), version="2.0"
    )
    assert text.endswith("X-BLAXCY-Version=2.0\n")


# -- Environment detection -----------------------------------------------------


def test_package_manager_detection_is_functional() -> None:
    """Detection asks whether a binary exists, never what the distro claims."""
    assert detect_package_manager(which=lambda name: f"/usr/bin/{name}") == "apt-get"
    assert detect_package_manager(which=lambda name: None) is None
    seen: list[str] = []

    def only(name: str) -> str | None:
        seen.append(name)
        return "/usr/bin/pacman" if name == "pacman" else None

    assert detect_package_manager(which=only) == "pacman"
    assert seen[:2] == ["apt-get", "dnf"], "detection should stop at the first hit"


def test_system_prefixes_are_recognised(tmp_path: Path) -> None:
    assert is_system_prefix(Path("/usr")) is True
    assert is_system_prefix(Path("/usr/local")) is True
    assert is_system_prefix(tmp_path / "local") is False


def test_the_default_prefix_is_the_user_xdg_prefix() -> None:
    assert default_prefix({"XDG_DATA_HOME": "/home/x/.local/share"}) == Path("/home/x/.local")
    assert default_prefix({}) == Path.home() / ".local"


def test_paths_land_where_the_desktop_looks_for_them(tmp_path: Path) -> None:
    """The four install locations, asserted so a layout change is a visible diff."""
    installer = Installer(source_root=REPO_ROOT, prefix=tmp_path / "local")
    prefix = tmp_path / "local"
    assert installer.app_dir == prefix / "lib" / "blaxcy"
    assert installer.launcher_path == prefix / "bin" / "blaxcy"
    assert installer.icon_path == (
        prefix / "share" / "icons" / "hicolor" / f"{ICON_PIXELS}x{ICON_PIXELS}" / "apps" / "blaxcy.png"
    )
    assert installer.desktop_path == prefix / "share" / "applications" / DESKTOP_FILE_NAME
    assert installer.manifest_path == prefix / "share" / "blaxcy" / "install-manifest.json"
    # The venv is deliberately separate from the app tree, so an update replaces
    # the application without discarding the environment it runs in.
    assert installer.venv_dir != installer.app_dir
    assert installer.venv_dir.parent == installer.app_dir.parent


# -- Preflight ----------------------------------------------------------------


def test_preflight_refuses_a_prefix_inside_the_source_tree(tmp_path: Path) -> None:
    installer = Installer(source_root=REPO_ROOT, prefix=REPO_ROOT / "install-here")
    steps = installer.preflight()
    assert any(
        step.status == "failed" and "inside the source tree" in step.detail for step in steps
    )


def test_preflight_refuses_a_system_prefix_without_the_flag(tmp_path: Path) -> None:
    """The gate is ``--system``, and allowing it removes exactly that refusal.

    Writability is checked separately and honestly: ``/usr`` fails that check for
    an unprivileged user whether or not ``--system`` was passed, so the assertion
    is about the *gate* rather than about an install nobody could complete here.
    """
    refused = Installer(source_root=REPO_ROOT, prefix=Path("/usr")).preflight()
    assert any(step.status == "failed" and "--system" in step.detail for step in refused)

    allowed = Installer(source_root=REPO_ROOT, prefix=Path("/usr"), allow_system_prefix=True)
    steps = allowed.preflight()
    assert not any("--system" in step.detail for step in steps)
    writable = [step for step in steps if step.name == "writable"]
    assert writable, "writability must be probed, not assumed from the prefix"
    if os.geteuid() != 0:
        assert writable[0].status == "failed", "/usr is genuinely unwritable without root"

    # A prefix we can actually write must preflight clean: no gate applies to it.
    here = Installer(source_root=REPO_ROOT, prefix=tmp_path / "local")
    assert not any(step.status == "failed" for step in here.preflight())


def test_preflight_refuses_a_missing_source_tree(tmp_path: Path) -> None:
    installer = Installer(source_root=tmp_path / "nope", prefix=tmp_path / "local")
    steps = installer.preflight()
    assert any(step.status == "failed" and "missing" in step.detail for step in steps)


def test_preflight_never_fails_on_optional_tools(tmp_path: Path) -> None:
    """A missing tesseract is a DEGRADED capability, not a reason to refuse."""
    installer = Installer(source_root=REPO_ROOT, prefix=tmp_path / "local")
    steps = installer.preflight()
    failed = {step.name for step in steps if step.status == "failed"}
    assert "optional tools" not in failed
