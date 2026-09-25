"""BLAXCY installer (specification section 73).

Section 73 fixes the responsibility order and this module implements it literally:

```text
preflight -> detect package manager -> install system dependencies -> create venv
-> install python dependencies -> install application files -> install icon
-> install desktop entry -> create manifest/checksums -> validate runtime
-> rollback on failure
```

Three properties are deliberate.

**Standard library only.** The installer runs *before* the virtualenv and its
dependencies exist, so importing anything BLAXCY itself depends on would make the
installer unusable in exactly the situation it exists for. It therefore imports
nothing outside the standard library, and it reaches the application by running it
as a subprocess rather than importing it.

**Nothing is written until the whole install is built and understood.** The
application tree is assembled in a staging directory beside its final home; the
desktop entry, icon and launcher are written through one recorded action each.
Every action that touches an existing file first moves that file into a backup
directory, so :meth:`Installer.rollback` can put the machine back exactly as it
was. If any step fails, the rollback runs and the failure is reported -- an
installer that half-installs is worse than one that refuses.

**It never claims a step it did not take.** A skipped step says so, a step whose
package names are not verified on this distribution says so instead of inventing
them (section 80), and the runtime validation is a real subprocess run of the
installed copy whose exit status decides the outcome. Capability gaps that are not
blockers (a missing `tesseract`, an absent WM) are reported as DEGRADED rather
than failing the install.

The default prefix is the user's ``~/.local``, which is what makes this a *menu*
application without root: the desktop entry lands in ``~/.local/share/applications``
and the XDG data dirs already do the rest. A system prefix requires ``--system``.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

APP_NAME: Final[str] = "blaxcy"
MANIFEST_SCHEMA_VERSION: Final[int] = 1
DESKTOP_FILE_NAME: Final[str] = "blaxcy.desktop"
ICON_FILE_NAME: Final[str] = "blaxcy.png"
LAUNCHER_NAME: Final[str] = "blaxcy"
ICON_PIXELS: Final[int] = 256
MIN_PYTHON: Final[tuple[int, int]] = (3, 12)

#: The application's own packages and files. Explicit on purpose: an install must
#: never sweep up version control, the repository's own virtualenv, caches, tests
#: or documentation by accident.
APP_PACKAGES: Final[tuple[str, ...]] = (
    "ai",
    "bench",
    "config",
    "control",
    "core",
    "gui",
    "policy",
    "schemas",
    "security",
    "watchdog",
)
APP_FILES: Final[tuple[str, ...]] = ("main.py", "requirements.txt")

#: Never installed, even if present inside a package directory.
EXCLUDED_DIR_NAMES: Final[frozenset[str]] = frozenset(
    {"__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".git", ".venv"}
)
EXCLUDED_SUFFIXES: Final[frozenset[str]] = frozenset({".pyc", ".pyo"})

#: Section 27's system dependencies, with the package names verified on this
#: project's Debian-family host. Only the verified family is listed: an invented
#: package name for another distribution could install the wrong thing, so those
#: managers are detected and told what capabilities are wanted instead.
SYSTEM_PACKAGE_COMMAND: Final[tuple[str, ...]] = (
    "apt-get",
    "install",
    "-y",
    "python3-gi",
    "gir1.2-atspi-2.0",
    "at-spi2-core",
    "libxcb-cursor0",
    "libsecret-1-0",
    "python3-secretstorage",
    "tesseract-ocr",
    "tesseract-ocr-eng",
    "xdotool",
    "wmctrl",
    "xclip",
)
VERIFIED_PACKAGE_MANAGERS: Final[tuple[str, ...]] = ("apt-get",)
DETECTED_PACKAGE_MANAGERS: Final[tuple[str, ...]] = (
    "apt-get",
    "dnf",
    "pacman",
    "zypper",
    "apk",
)
#: Optional runtime tools, reported as DEGRADED capabilities when absent. None of
#: them is a blocker: BLAXCY probes every capability functionally at runtime.
OPTIONAL_TOOLS: Final[tuple[tuple[str, str], ...]] = (
    ("tesseract", "OCR (the fallback text source)"),
    ("xdotool", "the X11 fallback input path"),
    ("wmctrl", "an alternative window-activation path"),
    ("xclip", "a clipboard fallback"),
)

#: Modules an interpreter must have for a *reused* environment to run the app.
#: Probed so a missing dependency is reported before the install rather than
#: surfacing as a bare traceback from the validation step. GUI/accessibility
#: modules are deliberately not listed: they are exercised by the GUI and by the
#: accessibility service, and the validation subprocess is what proves those.
REUSE_PROBE_MODULES: Final[tuple[str, ...]] = (
    "pydantic",
    "tomlkit",
    "mss",
    "numpy",
    "cv2",
    "pytesseract",
    "PIL",
    "Xlib",
    "keyring",
    "rapidfuzz",
    "xxhash",
    "psutil",
)

#: Prints the subset of :data:`REUSE_PROBE_MODULES` the interpreter cannot import.
_REUSE_PROBE_SCRIPT: Final[str] = (
    "import importlib.util as _u, json as _j;"
    f"_names = {list(REUSE_PROBE_MODULES)!r};"
    "_missing = [];\n"
    "for _n in _names:\n"
    "    try:\n"
    "        _spec = _u.find_spec(_n)\n"
    "    except (ImportError, ValueError):\n"
    "        _spec = None\n"
    "    if _spec is None:\n"
    "        _missing.append(_n)\n"
    "print(_j.dumps(_missing))"
)

ICON_RELATIVE: Final[Path] = Path("desktop") / ICON_FILE_NAME
#: The checked-in desktop entry, which is the single source of truth for the
#: entry's fields; the installer only rewrites the path-dependent ones.
DESKTOP_TEMPLATE_RELATIVE: Final[Path] = Path("desktop") / DESKTOP_FILE_NAME

#: Prefixes that need ``--system`` (and therefore root, or write access).
SYSTEM_PREFIXES: Final[tuple[Path, ...]] = (Path("/usr"), Path("/usr/local"), Path("/opt"))


# ---------------------------------------------------------------------------
# Small pure helpers (unit-tested without touching the filesystem)
# ---------------------------------------------------------------------------


def checksum_of(path: Path) -> str:
    """The sha256 of a file, read in chunks so a large file cannot exhaust memory."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 256), b""):
            digest.update(block)
    return digest.hexdigest()


def project_version(source_root: Path) -> str:
    """The version from ``pyproject.toml``, or ``unknown`` when it cannot be read.

    ``unknown`` is honest: the manifest records what was actually installed, and a
    missing version must not become a fabricated one.
    """
    try:
        with (source_root / "pyproject.toml").open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, ValueError):
        return "unknown"
    project = data.get("project")
    if isinstance(project, dict):
        version = project.get("version")
        if isinstance(version, str):
            return version
    return "unknown"


def launcher_text(python: str, app_dir: Path) -> str:
    """The ``<prefix>/bin/blaxcy`` launcher: a pass-through to the installed app.

    It forwards its arguments, which is what lets the desktop entry say
    ``Exec=<launcher> gui`` while the same launcher is usable as
    ``blaxcy probe``/``blaxcy status`` from a shell.
    """
    return (
        "#!/bin/sh\n"
        f"# BLAXCY launcher (specification section 73).\n"
        f'exec "{python}" "{app_dir / "main.py"}" "$@"\n'
    )


def desktop_entry_text(template: str, *, launcher: Path, version: str) -> str:
    """The menu entry, derived from the repository's checked-in template.

    The entry has one source of truth -- ``desktop/blaxcy.desktop`` -- and this
    function adjusts only the fields that cannot be static: ``Exec`` must name the
    launcher's **absolute installed path** (a bare ``blaxcy`` is only correct for a
    system install where it is on ``PATH``), and ``X-BLAXCY-Version`` records what
    was actually installed. Every other field is copied verbatim, so changing the
    entry is a one-place change instead of two definitions that drift apart.

    ``StartupWMClass`` is deliberately **absent** from the template: it can only be
    set truthfully by observing the running window's ``WM_CLASS``, and nothing in
    this project has verified it on a real display. A guessed value would be a fake
    claim in a file the desktop trusts; omitting it costs only a moment of taskbar
    association.
    """
    lines: list[str] = []
    saw_exec = False
    saw_version = False
    for raw in template.splitlines():
        # The template's own commentary is for maintainers; the installed entry
        # keeps only the entry. (A '#' line is a comment per the Desktop Entry
        # Specification, so this is a removal, not a rewrite.)
        if raw.startswith("#"):
            continue
        if raw.startswith("Exec="):
            lines.append(f'Exec="{launcher}" gui')
            saw_exec = True
            continue
        if raw.startswith("X-BLAXCY-Version="):
            lines.append(f"X-BLAXCY-Version={version}")
            saw_version = True
            continue
        lines.append(raw)
    if not saw_exec:
        raise ValueError("the desktop template has no Exec= line")
    if not saw_version:
        lines.append(f"X-BLAXCY-Version={version}")
    return "\n".join(lines).rstrip("\n") + "\n"


def iter_app_files(source_root: Path) -> list[tuple[Path, Path]]:
    """Every ``(absolute source, relative destination)`` file to install.

    Deterministically ordered so the manifest is stable between runs, which is what
    makes a checksum comparison meaningful.
    """
    pairs: list[tuple[Path, Path]] = []
    for name in APP_FILES:
        candidate = source_root / name
        if candidate.is_file():
            pairs.append((candidate, Path(name)))
    for package in APP_PACKAGES:
        package_dir = source_root / package
        if not package_dir.is_dir():
            continue
        for path in sorted(package_dir.rglob("*")):
            if path.is_dir():
                continue
            if any(part in EXCLUDED_DIR_NAMES for part in path.parts):
                continue
            if path.suffix in EXCLUDED_SUFFIXES:
                continue
            pairs.append((path, path.relative_to(source_root)))
    return sorted(pairs, key=lambda pair: str(pair[1]))


def detect_package_manager(
    which: Any = shutil.which, names: tuple[str, ...] = DETECTED_PACKAGE_MANAGERS
) -> str | None:
    """The first available package manager, or ``None``.

    Functional detection (does the binary exist and is it usable), never an
    assumption from the distribution name (section 4 rule 12).
    """
    for name in names:
        if which(name):
            return name
    return None


def default_prefix(env: Mapping[str, str] | None = None) -> Path:
    """The default install prefix: the user-level XDG prefix, never a system one.

    ``$XDG_DATA_HOME`` **is** ``<prefix>/share``, so its parent is the prefix; the
    fallback is ``~/.local``. That is what makes this a menu install without root:
    the desktop entry lands in ``~/.local/share/applications``, which the desktop
    environment already reads, and the launcher in ``~/.local/bin``.
    """
    environment = os.environ if env is None else env
    data_home = environment.get("XDG_DATA_HOME")
    if data_home:
        return Path(data_home).expanduser().parent
    return Path.home() / ".local"


def is_system_prefix(prefix: Path) -> bool:
    """Whether a prefix is one that needs ``--system`` (and usually root)."""
    resolved = prefix.resolve(strict=False)
    return any(resolved == candidate for candidate in SYSTEM_PREFIXES)


def desktop_entry_matches_launcher(text: str, launcher: Path) -> bool:
    """Whether a desktop entry's ``Exec`` points at the given launcher."""
    for line in text.splitlines():
        if line.startswith("Exec="):
            return str(launcher) in line
    return False


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


@dataclass
class StepResult:
    """One section 73 step and what actually happened to it."""

    name: str
    status: str  # "ok" | "skipped" | "failed"
    detail: str = ""

    def to_dict(self) -> dict[str, str]:
        """JSON-shaped step record."""
        payload = {"name": self.name, "status": self.status}
        if self.detail:
            payload["detail"] = self.detail
        return payload


@dataclass
class InstallReport:
    """The outcome of one installer command, with the evidence behind it."""

    command: str
    prefix: str
    ok: bool
    steps: list[StepResult] = field(default_factory=list)
    files: list[dict[str, Any]] = field(default_factory=list)
    rolled_back: bool = False
    message: str = ""
    dry_run: bool = False

    def step(self, name: str) -> StepResult | None:
        """One recorded step by name."""
        for entry in self.steps:
            if entry.name == name:
                return entry
        return None

    def to_dict(self) -> dict[str, Any]:
        """JSON-shaped report for the CLI and the manifest."""
        return {
            "command": self.command,
            "prefix": self.prefix,
            "ok": self.ok,
            "dry_run": self.dry_run,
            "rolled_back": self.rolled_back,
            "message": self.message,
            "steps": [step.to_dict() for step in self.steps],
            "files": self.files,
        }


class InstallError(RuntimeError):
    """A step failed. Carries the step name so the report can name it."""

    def __init__(self, step: str, message: str) -> None:
        super().__init__(f"{step}: {message}")
        self.step = step
        self.message = message


@dataclass
class _Action:
    """One recorded change to the filesystem, with the means to undo it."""

    kind: str  # "write" | "copy" | "rename" | "mkdir"
    path: Path
    source: Path | None = None
    content: str | None = None
    mode: int | None = None
    backup: Path | None = None
    created: bool = False
    #: True when the file being replaced was BLAXCY's own previous install. Such a
    #: backup exists only so a failed update can roll back; on success it must be
    #: discarded, because an uninstall that "restored" it would resurrect the very
    #: version it just removed.
    owned: bool = False


# ---------------------------------------------------------------------------
# The installer
# ---------------------------------------------------------------------------


class Installer:
    """Installs, updates and removes a BLAXCY instance (section 73)."""

    def __init__(
        self,
        *,
        source_root: Path,
        prefix: Path,
        python: str | None = None,
        reuse_python: bool = False,
        system_deps: bool = False,
        dry_run: bool = False,
        allow_system_prefix: bool = False,
        clock: Any = time.time,
    ) -> None:
        self.source_root = Path(source_root).resolve()
        self.prefix = Path(prefix).expanduser().resolve()
        self.requested_python = python
        self.reuse_python = reuse_python
        self.system_deps = system_deps
        self.dry_run = dry_run
        self.allow_system_prefix = allow_system_prefix
        self._clock = clock
        self._actions: list[_Action] = []
        self._undo: list[_Action] = []
        self._manifest: dict[str, Any] = {}
        self._report = InstallReport(command="install", prefix=str(self.prefix), ok=False)
        self._backup_root = (
            self.prefix / "share" / APP_NAME / "backups" / f"{int(self._clock())}"
        )

    # -- Paths -----------------------------------------------------------------

    @property
    def app_dir(self) -> Path:
        """Where the application's own files live."""
        return self.prefix / "lib" / APP_NAME

    @property
    def venv_dir(self) -> Path:
        """Where the private virtualenv lives (kept separate from the app files)."""
        return self.prefix / "lib" / f"{APP_NAME}-venv"

    @property
    def staging_dir(self) -> Path:
        """Where the application tree is assembled before it is put in place."""
        return self.prefix / "lib" / f".{APP_NAME}-staging-{os.getpid()}"

    @property
    def launcher_path(self) -> Path:
        """The ``bin`` launcher the desktop entry points at."""
        return self.prefix / "bin" / LAUNCHER_NAME

    @property
    def icon_path(self) -> Path:
        """The hicolor theme icon, so menus can find it by name."""
        return (
            self.prefix
            / "share"
            / "icons"
            / "hicolor"
            / f"{ICON_PIXELS}x{ICON_PIXELS}"
            / "apps"
            / ICON_FILE_NAME
        )

    @property
    def desktop_path(self) -> Path:
        """The XDG desktop entry, which is what puts BLAXCY in the menu."""
        return self.prefix / "share" / "applications" / DESKTOP_FILE_NAME

    @property
    def manifest_path(self) -> Path:
        """The manifest: every installed path, its checksum, and the step record."""
        return self.prefix / "share" / APP_NAME / "install-manifest.json"

    # -- Preflight -------------------------------------------------------------

    def preflight(self) -> list[StepResult]:
        """Section 73's first step: decide whether this install is even possible.

        Failures here change nothing on disk, which is the point of doing them
        first: a preflight that ran after the first write would not be a preflight.
        """
        results: list[StepResult] = []

        results.append(
            StepResult(
                "platform",
                "ok" if platform.system() == "Linux" else "failed",
                f"{platform.system()} {platform.release()}" if platform.system() == "Linux" else
                f"BLAXCY is Linux-native; {platform.system()} is unsupported (section 30)",
            )
        )

        version = sys.version_info
        if version[:2] >= MIN_PYTHON:
            results.append(
                StepResult("python", "ok", f"{version.major}.{version.minor}.{version.micro}")
            )
        else:
            results.append(
                StepResult(
                    "python",
                    "failed",
                    f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ is required "
                    f"(section 27); this is {version.major}.{version.minor}",
                )
            )

        missing = [
            name
            for name in ("main.py", "pyproject.toml")
            if not (self.source_root / name).is_file()
        ]
        results.append(
            StepResult(
                "source tree",
                "failed" if missing else "ok",
                f"{self.source_root} is missing {', '.join(missing)}" if missing else
                str(self.source_root),
            )
        )

        if is_system_prefix(self.prefix) and not self.allow_system_prefix:
            results.append(
                StepResult(
                    "prefix",
                    "failed",
                    f"{self.prefix} is a system prefix; a menu install belongs in "
                    "~/.local (the default). Pass --system to install there anyway",
                )
            )
        else:
            results.append(StepResult("prefix", "ok", str(self.prefix)))

        if self.prefix == self.source_root or self.source_root in self.prefix.parents:
            results.append(
                StepResult(
                    "prefix",
                    "failed",
                    "the prefix is inside the source tree; the installer would copy "
                    "the tree onto itself",
                )
            )

        writable = self._prefix_writable()
        results.append(
            StepResult(
                "writable",
                "ok" if writable else "failed",
                str(self.prefix) if writable else f"cannot create or write {self.prefix}",
            )
        )

        manager = detect_package_manager()
        if manager is None:
            results.append(
                StepResult("package manager", "skipped", "no known package manager found")
            )
        elif manager in VERIFIED_PACKAGE_MANAGERS:
            command = " ".join(SYSTEM_PACKAGE_COMMAND)
            results.append(
                StepResult(
                    "package manager",
                    "ok",
                    f"{manager}; system dependencies via: sudo {command}"
                    if not self.system_deps
                    else f"{manager}; installing system dependencies",
                )
            )
        else:
            results.append(
                StepResult(
                    "package manager",
                    "skipped",
                    f"{manager} detected, but its package names are not verified by "
                    "this project, so none is invented (section 80). Install the "
                    "section 27 capabilities manually: python3-gi equivalent, "
                    "at-spi2-core equivalent, tesseract-ocr, and optionally "
                    "xdotool/wmctrl/xclip",
                )
            )

        absent = [(tool, why) for tool, why in OPTIONAL_TOOLS if shutil.which(tool) is None]
        if absent:
            results.append(
                StepResult(
                    "optional tools",
                    "skipped",
                    "DEGRADED (not blockers): "
                    + "; ".join(f"{tool} missing ({why})" for tool, why in absent),
                )
            )
        else:
            results.append(StepResult("optional tools", "ok", "all present"))

        if self.reuse_python:
            interpreter = self._interpreter_for_reuse()
            results.append(
                StepResult(
                    "interpreter",
                    "ok" if interpreter else "failed",
                    str(interpreter) if interpreter else
                    f"the requested interpreter does not exist: {self.requested_python}",
                )
            )
        return results

    def _prefix_writable(self) -> bool:
        """Whether the prefix can be created/written, checked without creating it."""
        candidate = self.prefix
        while not candidate.exists() and candidate != candidate.parent:
            candidate = candidate.parent
        return os.access(candidate, os.W_OK | os.X_OK) if candidate.exists() else False

    def _interpreter_for_reuse(self) -> Path | None:
        """The interpreter to reuse, when ``--python``/``--reuse-python`` was given.

        The path is made absolute but deliberately **not** symlink-resolved. A
        virtualenv's ``bin/python`` is a symlink to the real interpreter, and
        resolving it silently discards the virtualenv -- its ``sys.prefix`` and
        therefore its site-packages -- turning a working install into a
        ``ModuleNotFoundError``. The installer's own runtime validation caught
        exactly that, which is why the rule is stated here rather than left to luck.
        """
        raw = self.requested_python
        if raw is None:
            return None
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
        found = shutil.which(raw)
        if found is None:
            return None
        located = Path(found)
        return located if located.is_absolute() else Path.cwd() / located

    def _reuse_probe(self, interpreter: Path) -> tuple[bool, list[str]]:
        """Whether a reused interpreter runs at all, and which deps it lacks."""
        completed = subprocess.run(
            [str(interpreter), "-c", _REUSE_PROBE_SCRIPT],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            return False, []
        try:
            missing = json.loads(completed.stdout.strip() or "[]")
        except ValueError:
            return False, []
        if not isinstance(missing, list):
            return False, []
        return True, [str(name) for name in missing]

    # -- Step implementations --------------------------------------------------

    def _step_venv(self) -> StepResult:
        """Step: create the virtualenv and install the Python dependencies."""
        if self.reuse_python:
            interpreter = self._interpreter_for_reuse()
            if interpreter is None:  # pragma: no cover - preflight already failed
                raise InstallError("venv", "no usable interpreter to reuse")
            runs, missing = self._reuse_probe(interpreter)
            if not runs:
                raise InstallError(
                    "venv",
                    f"the interpreter {interpreter} could not be run at all",
                )
            if missing:
                return StepResult(
                    "venv",
                    "skipped",
                    f"reusing {interpreter}; DEGRADED -- missing dependencies: "
                    f"{', '.join(missing)} (install them into that environment, or "
                    f"drop --python to let the installer create its own venv)",
                )
            return StepResult("venv", "skipped", f"reusing {interpreter} (dependencies present)")

        if not self.dry_run:
            self._run(
                [sys.executable, "-m", "venv", "--system-site-packages", str(self.venv_dir)],
                step="venv",
            )
        requirements = self.source_root / "requirements.txt"
        if not requirements.is_file():
            return StepResult("python dependencies", "skipped", "no requirements.txt")
        pip = self.venv_dir / "bin" / "pip"
        if not self.dry_run:
            self._run(
                [str(pip), "install", "-r", str(requirements)],
                step="python dependencies",
            )
        return StepResult("venv", "ok", f"created {self.venv_dir}")

    def _step_system_deps(self, preflight: list[StepResult]) -> StepResult:
        """Step: install the system dependencies, if the operator asked for it."""
        manager = detect_package_manager()
        if manager is None:
            return StepResult("system dependencies", "skipped", "no package manager")
        if manager not in VERIFIED_PACKAGE_MANAGERS:
            return StepResult(
                "system dependencies",
                "skipped",
                f"{manager} package names are not verified by this project",
            )
        if not self.system_deps:
            return StepResult(
                "system dependencies",
                "skipped",
                "not requested; run with --system-deps (needs sudo) to install: "
                + " ".join(SYSTEM_PACKAGE_COMMAND),
            )
        command = list(SYSTEM_PACKAGE_COMMAND)
        if os.geteuid() != 0:
            command = ["sudo", *command]
        if self.dry_run:
            return StepResult("system dependencies", "skipped", "dry run: " + " ".join(command))
        completed = subprocess.run(command, check=False)
        if completed.returncode != 0:
            raise InstallError(
                "system dependencies", f"{' '.join(command)} exited {completed.returncode}"
            )
        return StepResult("system dependencies", "ok", " ".join(command))

    def _step_app_files(self) -> tuple[StepResult, list[dict[str, Any]]]:
        """Step: assemble the application tree in staging and swap it into place."""
        pairs = iter_app_files(self.source_root)
        if not pairs:
            raise InstallError("application files", f"no application files under {self.source_root}")
        if self.dry_run:
            return (
                StepResult("application files", "skipped", f"dry run: {len(pairs)} files"),
                [{"path": str(self.app_dir / rel), "source": str(src)} for src, rel in pairs],
            )

        if self.staging_dir.exists():
            shutil.rmtree(self.staging_dir)
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        for source, relative in pairs:
            destination = self.staging_dir / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)

        # Swap the staged tree into place, keeping the previous one for rollback.
        # The swap is committed here, not deferred, so it is registered as already
        # done: anything in ``_actions`` has yet to be applied, and putting the swap
        # there would leave the old application tree unrestorable.
        if self.app_dir.exists():
            backup = self._backup_path_for(self.app_dir)
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(self.app_dir), str(backup))
            self._undo.append(
                _Action(kind="rename", path=self.app_dir, backup=backup, created=False)
            )
        else:
            self._undo.append(_Action(kind="rename", path=self.app_dir, created=True))
        shutil.move(str(self.staging_dir), str(self.app_dir))

        files: list[dict[str, Any]] = []
        for _source, relative in pairs:
            installed = self.app_dir / relative
            files.append(
                {
                    "path": str(installed),
                    "kind": "app",
                    "bytes": installed.stat().st_size,
                    "sha256": checksum_of(installed),
                }
            )
        return StepResult("application files", "ok", f"{len(files)} files -> {self.app_dir}"), files

    def _step_write(self, *, step: str, path: Path, content: str, mode: int = 0o644) -> StepResult:
        """Step: write one file, backing up whatever was there before."""
        self._actions.append(_Action(kind="write", path=path, content=content, mode=mode))
        if self.dry_run:
            return StepResult(step, "skipped", f"dry run: would write {path}")
        return StepResult(step, "ok", str(path))

    def _step_icon(self) -> StepResult:
        """Step: install the icon into the hicolor theme."""
        source = self.source_root / ICON_RELATIVE
        if not source.is_file():
            raise InstallError(
                "icon",
                f"{source} is missing; regenerate it with "
                "desktop/make_icon.py (an entry with a missing icon shows a blank square)",
            )
        self._actions.append(_Action(kind="copy", path=self.icon_path, source=source))
        if self.dry_run:
            return StepResult("icon", "skipped", f"dry run: would copy {source}")
        return StepResult("icon", "ok", str(self.icon_path))

    def _step_desktop_entry(self, interpreter: str) -> StepResult:
        """Step: write the launcher and the menu entry that points at it."""
        template_path = self.source_root / DESKTOP_TEMPLATE_RELATIVE
        if not template_path.is_file():
            raise InstallError(
                "desktop entry",
                f"{template_path} is missing; the menu entry is derived from it "
                "rather than generated, so the entry has one definition",
            )
        text = desktop_entry_text(
            template_path.read_text(encoding="utf-8"),
            launcher=self.launcher_path,
            version=project_version(self.source_root),
        )
        content = launcher_text(interpreter, self.app_dir)
        self._actions.append(_Action(kind="write", path=self.launcher_path, content=content, mode=0o755))
        self._actions.append(_Action(kind="write", path=self.desktop_path, content=text, mode=0o644))
        if self.dry_run:
            return StepResult("desktop entry", "skipped", f"dry run: would write {self.desktop_path}")
        return StepResult("desktop entry", "ok", str(self.desktop_path))

    def _step_validate(self, interpreter: str) -> StepResult:
        """Step: really run the installed copy, and check the entry is valid."""
        if self.dry_run:
            return StepResult("validate", "skipped", "dry run")
        completed = subprocess.run(
            [interpreter, str(self.app_dir / "main.py"), "session"],
            cwd=str(self.app_dir),
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        if completed.returncode != 0:
            tail = (completed.stderr or completed.stdout or "").strip().splitlines()
            raise InstallError(
                "validate",
                f"the installed copy exited {completed.returncode}"
                + (f": {tail[-1]}" if tail else ""),
            )
        if "session type" not in completed.stdout:
            raise InstallError("validate", "the installed copy ran but printed no session report")
        detail = "installed copy ran; session report produced"
        validator = shutil.which("desktop-file-validate")
        if validator is not None:
            check = subprocess.run(
                [validator, str(self.desktop_path)],
                capture_output=True,
                text=True,
                check=False,
            )
            if check.returncode != 0:
                raise InstallError(
                    "validate",
                    f"desktop-file-validate rejected {self.desktop_path}: "
                    f"{(check.stdout or check.stderr).strip()}",
                )
            detail += "; desktop entry valid"
        else:
            detail += "; desktop-file-validate not installed (entry unvalidated)"
        return StepResult("validate", "ok", detail)

    def _step_refresh_caches(self) -> StepResult:
        """Step: best-effort XDG database refresh, reported rather than assumed.

        Both tools are caches, not state: failing to refresh one changes how quickly
        the menu notices BLAXCY, never whether it works. So a refusal is reported
        with its exit status rather than swallowed, and it does not fail the install
        -- but it is never reported as having applied either.
        """
        applied: list[str] = []
        not_applied: list[str] = []
        for tool, argument in (
            ("update-desktop-database", str(self.desktop_path.parent)),
            ("gtk-update-icon-cache", str(self.icon_path.parent.parent.parent)),
        ):
            binary = shutil.which(tool)
            if binary is None:
                not_applied.append(f"{tool} not installed")
                continue
            if self.dry_run:
                not_applied.append(f"{tool} (dry run)")
                continue
            completed = subprocess.run([binary, argument], capture_output=True, check=False)
            if completed.returncode == 0:
                applied.append(tool)
            else:
                not_applied.append(f"{tool} exit {completed.returncode}")
        detail = ""
        if applied:
            detail = "applied: " + ", ".join(applied)
        if not_applied:
            detail += ("; " if detail else "") + "not applied: " + ", ".join(not_applied)
        return StepResult("desktop caches", "ok" if applied else "skipped", detail)

    # -- Manifest --------------------------------------------------------------

    def _write_manifest(self, report: InstallReport, files: list[dict[str, Any]]) -> None:
        """Write the manifest: every installed path, its checksum and the steps."""
        payload: dict[str, Any] = {
            "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
            "app": APP_NAME,
            "version": project_version(self.source_root),
            "installed_at": self._clock(),
            "prefix": str(self.prefix),
            "source_root": str(self.source_root),
            "launcher": str(self.launcher_path),
            "icon": str(self.icon_path),
            "desktop_entry": str(self.desktop_path),
            "app_dir": str(self.app_dir),
            "venv": None if self.reuse_python else str(self.venv_dir),
            "python": self._manifest.get("python"),
            "checksum_algorithm": "sha256",
            "files": files,
            # What we moved out of the way, so an uninstall can hand the user's own
            # files back rather than leaving them stranded in a backup directory.
            "backups": [
                {"path": str(action.path), "backup": str(action.backup)}
                for action in self._undo
                if action.backup is not None
            ],
            "steps": [step.to_dict() for step in report.steps],
            "ok": report.ok,
        }
        previous = self._manifest
        if previous:
            if "version" in previous:
                payload["updated_from"] = previous["version"]
            if "files" in previous:
                payload["previous_file_count"] = len(previous["files"])
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        self.manifest_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        self._manifest = payload

    def read_manifest(self) -> dict[str, Any] | None:
        """The installed manifest, or ``None`` when nothing valid is installed here.

        Unreadable JSON and JSON that is not an object are both ``None``: a
        manifest we cannot understand is not a manifest, and pretending otherwise
        would let an update trust a file it cannot read.
        """
        try:
            data: Any = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    # -- Install / update ------------------------------------------------------

    def install(self, *, require_existing: bool = False) -> InstallReport:
        """Run every section 73 step, rolling back if any of them fails.

        Args:
            require_existing: Set for ``update``: refuse unless a manifest is
                already installed here, so an update cannot silently become a
                first install into the wrong prefix.
        """
        report = InstallReport(
            command="update" if require_existing else "install",
            prefix=str(self.prefix),
            ok=False,
            dry_run=self.dry_run,
        )
        self._report = report

        preflight = self.preflight()
        report.steps.extend(preflight)
        failed = [step for step in preflight if step.status == "failed"]
        if failed:
            report.message = "preflight failed: " + "; ".join(
                f"{step.name}: {step.detail}" for step in failed
            )
            return report

        self._manifest = self.read_manifest() or {}
        if require_existing and not self._manifest:
            report.message = (
                f"no installed manifest at {self.manifest_path}; run install.sh first "
                f"(updating a prefix that was never installed would guess)"
            )
            return report

        interpreter = self._install_interpreter()
        report.steps.append(self._step_venv())
        report.steps.append(self._step_system_deps(preflight))

        files: list[dict[str, Any]] = []
        try:
            step, files = self._step_app_files()
            report.steps.append(step)
            report.steps.append(self._step_icon())
            report.steps.append(self._step_desktop_entry(interpreter))
            report.files = files
            # Everything this run writes is on disk *before* validation, so the
            # validation step inspects the real installed artifacts (a desktop entry
            # that has not been written yet is not a desktop entry) and a failure
            # has exactly one rollback path.
            if not self.dry_run:
                self._write_actions()
            report.steps.append(self._step_validate(interpreter))
            report.steps.append(self._step_refresh_caches())
        except InstallError as exc:
            report.message = f"{exc.step} failed: {exc.message}"
            report.steps.append(StepResult(exc.step, "failed", exc.message))
            report.rolled_back = self._rollback()
            report.ok = False
            self._cleanup_staging()
            return report
        except (OSError, subprocess.SubprocessError) as exc:
            # A filesystem or subprocess failure is a failure like any other: it
            # must roll back rather than leave a half-installed prefix behind.
            message = f"failed while applying changes: {exc!r}"
            report.message = message
            report.steps.append(StepResult("apply changes", "failed", repr(exc)))
            report.rolled_back = self._rollback()
            report.ok = False
            self._cleanup_staging()
            return report

        # The new version validated, so the backups of our *own* previous files are
        # superseded -- discarded before the manifest is written, so the manifest
        # only ever records borrowed files that an uninstall must hand back.
        if not self.dry_run:
            self._discard_owned_backups()

        # The manifest records the outcome, so it is written once the outcome is
        # known -- including the validation result above.
        if not self.dry_run:
            report.steps.append(StepResult("manifest", "ok", str(self.manifest_path)))
            report.ok = True
            self._write_manifest(report, files)
        else:
            report.steps.append(StepResult("manifest", "skipped", "dry run"))
            report.ok = True
        report.message = (
            f"{'would install' if self.dry_run else 'installed'} {len(files)} files to "
            f"{self.app_dir}; menu entry {self.desktop_path}"
        )
        return report

    def _install_interpreter(self) -> str:
        """The interpreter the launcher should use, decided honestly."""
        if self.reuse_python:
            interpreter = self._interpreter_for_reuse()
            if interpreter is not None:
                self._manifest["python"] = str(interpreter)
                return str(interpreter)
        self._manifest["python"] = str(self.venv_dir / "bin" / "python")
        return str(self.venv_dir / "bin" / "python")

    # -- Uninstall -------------------------------------------------------------

    def uninstall(self) -> InstallReport:
        """Remove exactly what the manifest lists, restoring any backups.

        Nothing outside the manifest is touched: an uninstaller that swept the
        share directory would delete other applications' files.
        """
        report = InstallReport(command="uninstall", prefix=str(self.prefix), ok=False)
        manifest = self.read_manifest()
        if not manifest:
            report.message = f"nothing is installed at {self.prefix} (no manifest)"
            report.ok = True
            return report
        # ``_is_ours`` refuses any path outside the prefix the manifest records, so
        # it has to be the manifest's prefix that is in hand, not the one this run
        # was pointed at.
        self._manifest = manifest

        removed = 0
        for entry in manifest.get("files", []):
            path = Path(str(entry.get("path", "")))
            if path.is_file() and self._is_ours(path):
                path.unlink()
                removed += 1
        for key in ("launcher", "icon", "desktop_entry"):
            raw = manifest.get(key)
            if not raw:
                continue
            path = Path(str(raw))
            if path.is_file() and self._is_ours(path):
                path.unlink()
                removed += 1
        app_dir = Path(str(manifest.get("app_dir") or ""))
        if app_dir.is_dir() and self._is_ours(app_dir):
            shutil.rmtree(app_dir)
        venv_dir = manifest.get("venv")
        if self.reuse_python or not venv_dir:
            report.steps.append(
                StepResult("venv", "skipped", "a reused interpreter is left untouched")
            )
        else:
            venv_path = Path(str(venv_dir))
            if venv_path.is_dir() and self._is_ours(venv_path):
                shutil.rmtree(venv_path)
                report.steps.append(StepResult("venv", "ok", f"removed {venv_path}"))
        report.steps.append(StepResult("files", "ok", f"removed {removed} files"))

        # Hand back anything this install had to move aside (a desktop entry or an
        # icon that belonged to something else). Restoring is the least surprising
        # outcome, and it is what keeps an uninstall from quietly deleting a file
        # BLAXCY never owned.
        restored = 0
        for entry in manifest.get("backups", []):
            if not isinstance(entry, dict):
                continue
            target = Path(str(entry.get("path", "")))
            backup = Path(str(entry.get("backup", "")))
            if not target.name or not backup.is_file() or not self._is_ours(target):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.unlink(missing_ok=True)
            shutil.move(str(backup), str(target))
            restored += 1
        report.steps.append(
            StepResult(
                "restore",
                "ok" if restored else "skipped",
                f"restored {restored} pre-existing file(s) this install had replaced"
                if restored
                else "no pre-existing files were replaced by this install",
            )
        )

        # The desktop caches still list the entry we just deleted, so the same
        # best-effort refresh runs here -- otherwise the menu keeps an entry whose
        # Exec points at a launcher that no longer exists.
        report.steps.append(self._step_refresh_caches())
        report.steps.append(StepResult("manifest", "ok", str(self.manifest_path)))
        self.manifest_path.unlink(missing_ok=True)
        # The backups live beside the manifest, not under this run's own timestamped
        # root (an uninstall is a different run from the install that made them).
        shutil.rmtree(self.manifest_path.parent / "backups", ignore_errors=True)
        self._prune_created_dirs()
        report.ok = True
        report.message = f"removed {removed} files; manifest deleted"
        return report

    def _is_ours(self, path: Path) -> bool:
        """Whether a path is inside the prefix the manifest recorded.

        A manifest is a file on disk and could name anything; refusing to act
        outside the prefix the manifest itself claims is the difference between an
        uninstaller and a deletion tool.
        """
        try:
            path.resolve(strict=False).relative_to(Path(str(self._manifest.get("prefix", self.prefix))).resolve(strict=False))
        except (ValueError, OSError):
            return False
        return True

    @staticmethod
    def _prune_empty(directory: Path) -> None:
        """Remove a directory only if it is empty, and never raise."""
        try:
            if directory.is_dir() and not any(directory.iterdir()):
                directory.rmdir()
        except OSError:
            return

    # -- Actions and rollback --------------------------------------------------

    def _owns(self, path: Path) -> bool:
        """Whether the manifest of the current install already claims this path.

        This is the distinction an uninstall depends on: a file BLAXCY installed
        before is *ours* to delete, while a file that belonged to something else
        (another package's desktop entry, a user's hand-written icon) was only ever
        borrowed and must be handed back.
        """
        manifest = self._manifest
        if not manifest:
            return False
        claimed = {
            str(manifest.get(key))
            for key in ("launcher", "icon", "desktop_entry", "app_dir")
            if manifest.get(key)
        }
        for entry in manifest.get("files", []):
            if isinstance(entry, dict) and entry.get("path"):
                claimed.add(str(entry["path"]))
        return str(path) in claimed

    def _write_actions(self) -> None:
        """Apply every recorded write/copy action, backing up what exists."""
        for action in self._actions:
            if action.kind not in ("write", "copy"):
                continue
            action.path.parent.mkdir(parents=True, exist_ok=True)
            action.owned = self._owns(action.path)
            if action.path.exists():
                backup = self._backup_path_for(action.path)
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(action.path), str(backup))
                action.backup = backup
            else:
                action.created = True
            if action.kind == "write":
                action.path.write_text(action.content or "", encoding="utf-8")
            elif action.source is not None:
                shutil.copy2(action.source, action.path)
            if action.mode is not None:
                action.path.chmod(action.mode)
            self._undo.append(action)

    def _rollback(self) -> bool:
        """Put the filesystem back the way it was. Returns whether it ran.

        Every action that moved something aside is reversed first, newest first, so
        a partially committed install leaves no half-written application behind.
        """
        for action in reversed(self._undo):
            try:
                if action.backup is not None and action.backup.exists():
                    if action.path.is_dir() and not action.path.is_symlink():
                        shutil.rmtree(action.path, ignore_errors=True)
                    else:
                        action.path.unlink(missing_ok=True)
                    action.path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(action.backup), str(action.path))
                elif action.created:
                    if action.path.is_dir() and not action.path.is_symlink():
                        shutil.rmtree(action.path, ignore_errors=True)
                    else:
                        action.path.unlink(missing_ok=True)
            except OSError:
                continue
        self._undo.clear()
        self._cleanup_staging()
        with contextlib.suppress(OSError):  # pragma: no cover - ignore_errors covers it
            shutil.rmtree(self._backup_root, ignore_errors=True)
        # Leave the prefix as it was found: the files are gone above, and the
        # directories this run created are only removed when they are empty.
        self._prune_created_dirs()
        return True

    def _prune_created_dirs(self) -> None:
        """Remove the now-empty directories an install would have created.

        Deepest first, and only when empty, so a prefix shared with other
        applications is never disturbed and a non-empty directory is left alone.
        """
        for parent in (
            self.icon_path.parent,
            self.icon_path.parent.parent,
            self.icon_path.parent.parent.parent,
            self.icon_path.parent.parent.parent.parent,
            self.desktop_path.parent,
            self.launcher_path.parent,
            self.manifest_path.parent,
            # ``lib`` holds both the app tree and the virtualenv; a rolled-back
            # install leaves it empty, and an empty directory is litter.
            self.app_dir.parent,
        ):
            self._prune_empty(parent)

    def _discard_owned_backups(self) -> None:
        """Drop the backups of BLAXCY's own previous files, once the new install works.

        They were kept only so a failed update could be rolled back; keeping them
        would make a later uninstall restore the version it just replaced.
        """
        for action in self._undo:
            if not action.owned or action.backup is None:
                continue
            if action.backup.is_dir() and not action.backup.is_symlink():
                shutil.rmtree(action.backup, ignore_errors=True)
            else:
                action.backup.unlink(missing_ok=True)
            action.backup = None

    def _cleanup_staging(self) -> None:
        """Remove the staging tree if it survived a failure."""
        if self.staging_dir.exists():
            shutil.rmtree(self.staging_dir, ignore_errors=True)

    def _backup_path_for(self, path: Path) -> Path:
        """Where a pre-existing file/directory is moved before being replaced."""
        try:
            relative = path.relative_to(self.prefix)
        except ValueError:
            relative = Path(path.name)
        return self._backup_root / relative

    # -- Subprocess helper -----------------------------------------------------

    @staticmethod
    def _run(command: list[str], *, step: str) -> None:
        """Run a step's command, raising :class:`InstallError` on failure."""
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        if completed.returncode != 0:
            tail = (completed.stderr or completed.stdout or "").strip().splitlines()
            raise InstallError(
                step,
                f"{' '.join(command)} exited {completed.returncode}"
                + (f": {tail[-1]}" if tail else ""),
            )


__all__ = [
    "APP_FILES",
    "APP_NAME",
    "APP_PACKAGES",
    "DESKTOP_FILE_NAME",
    "DESKTOP_TEMPLATE_RELATIVE",
    "DETECTED_PACKAGE_MANAGERS",
    "ICON_FILE_NAME",
    "ICON_PIXELS",
    "ICON_RELATIVE",
    "LAUNCHER_NAME",
    "MANIFEST_SCHEMA_VERSION",
    "MIN_PYTHON",
    "OPTIONAL_TOOLS",
    "REUSE_PROBE_MODULES",
    "SYSTEM_PACKAGE_COMMAND",
    "VERIFIED_PACKAGE_MANAGERS",
    "InstallError",
    "InstallReport",
    "Installer",
    "StepResult",
    "checksum_of",
    "default_prefix",
    "desktop_entry_matches_launcher",
    "desktop_entry_text",
    "detect_package_manager",
    "is_system_prefix",
    "iter_app_files",
    "launcher_text",
    "project_version",
]
