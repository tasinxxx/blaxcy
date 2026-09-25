"""``python3 -m installer`` -- the section 73 installer's command line.

``install.sh``, ``uninstall.sh`` and ``update.sh`` are each a few lines that exec
this module with one subcommand. Keeping the argument surface here (rather than
re-implemented in the shell) is what stops the documented scripts from drifting
away from the behaviour the test suite actually exercises.

Exit codes: ``0`` success, ``1`` a step failed (already rolled back), ``2`` usage.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Final

from installer.installer import Installer, InstallReport, default_prefix

#: The repository root: this file lives in ``<root>/installer/``.
DEFAULT_SOURCE: Final[Path] = Path(__file__).resolve().parent.parent

_STATUS_LABEL: Final[dict[str, str]] = {
    "ok": "ok  ",
    "skipped": "skip",
    "failed": "FAIL",
}


def build_parser() -> argparse.ArgumentParser:
    """The installer's argument parser."""
    parser = argparse.ArgumentParser(
        prog="installer",
        description="BLAXCY installer: preflight, install, update or uninstall (section 73).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    help_text = {
        "install": "install BLAXCY into the prefix (default: ~/.local)",
        "update": "re-install over an existing install, rolling back on failure",
        "uninstall": "remove exactly what the manifest lists",
        "preflight": "check whether an install is possible, changing nothing",
    }
    for name, description in help_text.items():
        command = sub.add_parser(name, help=description)
        command.add_argument(
            "--prefix",
            default=None,
            help="install prefix (default: $XDG_DATA_HOME/.. or ~/.local)",
        )
        command.add_argument(
            "--source",
            default=str(DEFAULT_SOURCE),
            help="the source tree to install from (default: this checkout)",
        )
        command.add_argument(
            "--python",
            default=None,
            help="reuse this interpreter instead of creating a virtualenv "
            "(implies --reuse-python)",
        )
        command.add_argument(
            "--reuse-python",
            action="store_true",
            help="do not create a virtualenv; use the running interpreter",
        )
        command.add_argument(
            "--system-deps",
            action="store_true",
            help="run the package manager for section 27's system dependencies "
            "(uses sudo; never implied)",
        )
        command.add_argument(
            "--system",
            action="store_true",
            help="permit a system prefix such as /usr (needs write access)",
        )
        command.add_argument(
            "--dry-run",
            action="store_true",
            help="report what would happen and change nothing",
        )
        command.add_argument(
            "--json", action="store_true", help="emit the report as JSON"
        )
    return parser


def print_report(report: InstallReport) -> None:
    """Render a report as a step list plus one honest summary line."""
    for step in report.steps:
        label = _STATUS_LABEL.get(step.status, step.status)
        detail = f" -- {step.detail}" if step.detail else ""
        print(f"[{label}] {step.name}{detail}")
    if report.rolled_back:
        print("[----] rolled back: every change this run made was undone")
    print()
    print(f"{report.command}: {'OK' if report.ok else 'FAILED'} - {report.message}")


def main(argv: list[str] | None = None) -> int:
    """Run one installer command. Returns a process exit code."""
    args = build_parser().parse_args(argv)
    prefix = Path(args.prefix).expanduser() if args.prefix else default_prefix()
    installer = Installer(
        source_root=Path(args.source),
        prefix=prefix,
        python=args.python,
        # ``--python`` is what a caller means by "reuse"; ``--reuse-python`` is how
        # they say it without naming one.
        reuse_python=bool(args.reuse_python or args.python),
        system_deps=bool(args.system_deps),
        dry_run=bool(args.dry_run),
        allow_system_prefix=bool(args.system),
    )

    if args.command == "preflight":
        steps = installer.preflight()
        report = InstallReport(
            command="preflight",
            prefix=str(prefix),
            ok=not any(step.status == "failed" for step in steps),
            steps=steps,
            message="preflight only; nothing on disk was changed",
        )
    elif args.command == "uninstall":
        report = installer.uninstall()
    elif args.command == "update":
        report = installer.install(require_existing=True)
    else:
        report = installer.install()

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print_report(report)
    return 0 if report.ok else 1


if __name__ == "__main__":  # pragma: no cover - the shell wrappers call main()
    raise SystemExit(main(sys.argv[1:]))
