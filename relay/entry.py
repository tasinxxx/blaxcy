"""The self-hosted runner CLI (Phase 2B, requirement 5/6).

This is what the GitHub Actions workflow invokes on the self-hosted runner. It
is **orchestration only**: it discovers tasks in the relay repository, hands
them to :class:`~relay.executor.RelayExecutor` (which goes through the Phase 2A
LocalBridge), and writes the results back. It never talks to the desktop
itself, never starts a BLAXCY Body, and never interprets task contents.

Commands:

* ``process`` — claim and execute every PENDING task (or one named task).
  Exits ``0`` when every processed task reached a terminal state; ``1`` when
  any task ended REJECTED/FAILED (so the workflow can report honestly).
  A HALTED or TIMED_OUT task is a *successful relay run* — BLAXCY did exactly
  what its safety design demanded — and exits ``0``.
* ``status`` — print the relay's task table as JSON. Read-only.

The Body itself is expected to be running (``python main.py gui`` or an
operator-supervised composition); this CLI deliberately does not start one,
because a desktop that begins acting because a CI queue was non-empty is
exactly the unattended-execution behavior the safety model forbids.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from bridge.auth import BridgeAuthenticator
from bridge.runner import LocalBridge
from config.settings import load_settings
from core.logging_setup import configure_logging
from relay.executor import RelayExecutor
from relay.lifecycle import TERMINAL_STATES, TaskLifecycle
from relay.store import TaskStore


def _cmd_process(args: argparse.Namespace) -> int:
    """Claim and execute every PENDING task (or one named task)."""
    store = TaskStore(Path(args.repo).expanduser())
    settings = load_settings(Path(args.config) if args.config else None)
    auth = BridgeAuthenticator()  # token from BLAXCY_BRIDGE_TOKEN; fails closed
    if not auth.configured:
        print(
            "error: no bridge token configured (set BLAXCY_BRIDGE_TOKEN); refusing to run",
            file=sys.stderr,
        )
        return 2
    # The Body's bridge and dispatcher are provided by the operator's running
    # composition; the relay connects to them through the injection points the
    # composition root already exposes (see docs/relay.md "Wiring").
    if args.wiring == "inprocess":
        from core.application import BlaxcyApplication

        app = BlaxcyApplication(settings)
        app.start()
        try:
            app.set_mode(args.mode, reason="operator-approved relay execution")
            bridge = LocalBridge(
                app.dispatcher,
                authenticator=auth,
                settings=settings,
                emergency_stop=app.stop,
            )
            executor = RelayExecutor(store=store, bridge=bridge, authenticator=auth)
            return _process_all(executor, store, task_id=args.task_id)
        finally:
            app.shutdown()
    raise ValueError(f"unknown wiring {args.wiring!r}")


def _process_all(executor: RelayExecutor, store: TaskStore, *, task_id: str | None) -> int:
    outcomes: list[dict[str, object]] = []
    targets = [task_id] if task_id else _pending_task_ids(store)
    for candidate in targets:
        document = executor.process_task(candidate)
        outcomes.append(document)
    print(json.dumps(outcomes, indent=2))
    failed = [
        document
        for document in outcomes
        if document.get("lifecycle") in (TaskLifecycle.REJECTED.value, TaskLifecycle.FAILED.value)
    ]
    return 1 if failed else 0


def _pending_task_ids(store: TaskStore) -> list[str]:
    """Every task directory whose recorded state is still PENDING."""
    if not store.tasks_dir.exists():
        return []
    pending: list[str] = []
    for directory in sorted(store.tasks_dir.iterdir()):
        if not directory.is_dir():
            continue
        if store.state_of(directory.name) is TaskLifecycle.PENDING:
            pending.append(directory.name)
    return pending


def _cmd_status(args: argparse.Namespace) -> int:
    """Print the relay's task table as JSON (read-only)."""
    store = TaskStore(Path(args.repo).expanduser())
    rows: list[dict[str, object]] = []
    if store.tasks_dir.exists():
        for directory in sorted(store.tasks_dir.iterdir()):
            if not directory.is_dir():
                continue
            record: dict[str, object] = {"task_id": directory.name, "state": store.state_of(directory.name).value}
            result = store.read_result(directory.name)
            if result is not None:
                record["bridge_status"] = result.get("bridge_status")
                record["lifecycle"] = result.get("lifecycle")
            rows.append(record)
    print(json.dumps(rows, indent=2))
    terminal = sum(1 for row in rows if row["state"] in {state.value for state in TERMINAL_STATES})
    print(f"# {len(rows)} task(s), {terminal} terminal", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="relay",
        description="BLAXCY GitHub relay runner (orchestration only; desktop control stays inside BLAXCY).",
    )
    parser.add_argument("--repo", default=".", help="relay repository root (default: cwd)")
    parser.add_argument("--config", default=None, help="explicit BLAXCY TOML config path")
    sub = parser.add_subparsers(dest="command", required=True)

    process = sub.add_parser("process", help="claim and execute pending tasks through the LocalBridge")
    process.add_argument("--task-id", default=None, help="process only this task id")
    process.add_argument(
        "--mode",
        type=PolicyMode,
        choices=tuple(PolicyMode),
        default=PolicyMode.OBSERVE,
        help="explicit execution mode for this relay run (default: OBSERVE)",
    )
    process.add_argument(
        "--wiring",
        default="inprocess",
        choices=("inprocess",),
        help="how the runner obtains the Body (in-process composition root)",
    )
    process.set_defaults(func=_cmd_process)

    status = sub.add_parser("status", help="print the relay task table as JSON")
    status.set_defaults(func=_cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(load_settings(Path(args.config) if args.config else None))
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
