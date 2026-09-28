"""Trusted producer for ChatGPT/Agent relay requests.

An Agent may commit only an unsigned Task document under relay/agent-requests.
This module is the trusted GitHub-hosted workflow step that validates that task
with the same Task parser used by BLAXCY, signs the exact canonical task bytes
with BLAXCY_BRIDGE_TOKEN, and records the normal Phase 2B envelope.

The module never executes a task and never talks to the desktop.
"""

from __future__ import annotations

import argparse
import json
import secrets
import time
from pathlib import Path
from typing import Any

from bridge.auth import BridgeAuthenticator, canonical_task_document
from bridge.task_protocol import Task
from relay.envelope import RELAY_ENVELOPE_VERSION, TaskEnvelopeDocument
from relay.store import TaskStore

DEFAULT_TTL_SECONDS = 3600.0
REQUEST_ROOT = Path("relay/agent-requests")


def build_signed_envelope(
    payload: dict[str, Any],
    *,
    authenticator: BridgeAuthenticator,
    now: float,
    ttl_seconds: float = DEFAULT_TTL_SECONDS,
) -> str:
    """Validate an Agent task and return the normal signed relay envelope."""
    task = Task.parse(payload)
    inner = task.model_dump(mode="json", exclude_none=True)
    inner_document = canonical_task_document(inner)
    nonce = f"agent:{task.task_id}:{secrets.token_hex(16)}"
    signature = authenticator.sign_request(inner_document, nonce=nonce, timestamp=now)
    envelope = {
        "relay_version": RELAY_ENVELOPE_VERSION,
        "task_id": task.task_id,
        "task": inner,
        "expires_at": now + ttl_seconds,
        "submission": {
            "signature": signature,
            "nonce": nonce,
            "timestamp": now,
        },
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "producer": "chatgpt-agent",
    }
    parsed = TaskEnvelopeDocument.parse(envelope, now=now)
    parsed.ensure_not_expired(now=now)
    return parsed.submission_bytes()


def process_request(
    request_path: Path,
    *,
    repo_root: Path,
    authenticator: BridgeAuthenticator,
    now: float | None = None,
) -> str:
    """Turn one Agent request into the normal relay task record."""
    payload = json.loads(request_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("agent request must be a JSON object")
    task_id = str(payload.get("task_id", ""))
    expected_root = (repo_root / REQUEST_ROOT).resolve()
    if request_path.resolve().parent != expected_root:
        raise ValueError("agent request is outside the allowed request directory")
    if not task_id:
        raise ValueError("agent request has no task_id")
    if request_path.name != f"{task_id}.json":
        raise ValueError("request filename must equal task_id")
    envelope = build_signed_envelope(
        payload,
        authenticator=authenticator,
        now=time.time() if now is None else now,
    )
    TaskStore(repo_root).write_task(task_id, envelope)
    return task_id


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sign one BLAXCY Agent relay request.")
    parser.add_argument("request", type=Path)
    parser.add_argument("--repo", type=Path, default=Path("."))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    auth = BridgeAuthenticator()
    if not auth.configured:
        raise SystemExit("BLAXCY_BRIDGE_TOKEN is not configured")
    task_id = process_request(
        args.request,
        repo_root=args.repo.resolve(),
        authenticator=auth,
    )
    print(task_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
