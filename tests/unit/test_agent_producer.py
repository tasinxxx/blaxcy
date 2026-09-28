"""Tests for the trusted Agent relay producer."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from bridge.auth import BridgeAuthenticator
from relay.agent_producer import build_signed_envelope, process_request
from relay.envelope import TaskEnvelopeDocument
from relay.lifecycle import TaskLifecycle

TOKEN = "agent-producer-token-0123456789abcdef"


def _task(task_id: str = "agent-1") -> dict[str, object]:
    return {
        "schema_version": 1,
        "task_id": task_id,
        "plan": [
            {"step_id": "s1", "tool": "hotkey", "args": {"keys": ["CTRL", "L"]}},
            {"step_id": "s2", "tool": "type_text", "args": {"text": "https://www.youtube.com"}},
            {"step_id": "s3", "tool": "press_key", "args": {"key": "ENTER"}},
        ],
    }


def test_agent_producer_creates_a_normal_signed_envelope() -> None:
    auth = BridgeAuthenticator(token=TOKEN)
    now = time.time()
    raw = build_signed_envelope(_task(), authenticator=auth, now=now)
    parsed = TaskEnvelopeDocument.parse(raw, now=now)
    assert parsed.task.task_id == "agent-1"
    assert parsed.producer == "chatgpt-agent"
    assert parsed.expires_at > now


def test_agent_request_uses_the_normal_task_guards() -> None:
    auth = BridgeAuthenticator(token=TOKEN)
    unsafe = _task()
    unsafe["plan"] = [
        {"step_id": "s1", "tool": "click", "target": "Send", "element_id": "pre-resolved"}
    ]
    with pytest.raises(ValueError):
        build_signed_envelope(unsafe, authenticator=auth, now=time.time())


def test_process_request_writes_the_normal_store_record(tmp_path: Path) -> None:
    request_dir = tmp_path / "relay" / "agent-requests"
    request_dir.mkdir(parents=True)
    request = request_dir / "agent-1.json"
    request.write_text(json.dumps(_task()), encoding="utf-8")
    task_id = process_request(
        request,
        repo_root=tmp_path,
        authenticator=BridgeAuthenticator(token=TOKEN),
        now=time.time(),
    )
    assert task_id == "agent-1"
    state = json.loads((tmp_path / "relay" / "tasks" / "agent-1" / "state.json").read_text())
    assert state["state"] == TaskLifecycle.PENDING.value
    assert (tmp_path / "relay" / "tasks" / "agent-1" / "task.json").exists()


def test_request_filename_must_match_task_id(tmp_path: Path) -> None:
    request_dir = tmp_path / "relay" / "agent-requests"
    request_dir.mkdir(parents=True)
    request = request_dir / "wrong.json"
    request.write_text(json.dumps(_task("agent-1")), encoding="utf-8")
    with pytest.raises(ValueError, match="filename"):
        process_request(
            request,
            repo_root=tmp_path,
            authenticator=BridgeAuthenticator(token=TOKEN),
            now=time.time(),
        )
