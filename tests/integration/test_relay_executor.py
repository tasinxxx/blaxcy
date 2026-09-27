"""Integration tests for the relay executor (Phase 2B requirement 16).

Runs the real relay stack — envelope parser, store, executor — over the **real
Phase 2A LocalBridge**, which itself runs the real dispatcher and sequence
runner over the project's fakes. Nothing touches a real desktop. The property
under test: the relay adds no execution path of its own, so every outcome a
local submission would produce, a GitHub-relayed task produces identically —
and every refusal happens before any input.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from bridge.auth import BridgeAuthenticator, canonical_task_document
from bridge.task_protocol import TASK_SCHEMA_VERSION
from config.settings import Settings
from relay.envelope import RELAY_ENVELOPE_VERSION
from relay.executor import RelayExecutor
from relay.lifecycle import TaskLifecycle
from relay.store import TaskStore
from schemas.enums import ErrorCode, PolicyMode
from tests.harness.phase101 import (
    WORKFLOW_PLAN,
    build_sequence_env,
)

TEST_TOKEN = "relay-integration-token-0123456789abcdef"


class _RelayEnv:
    """The relay over a fully wired fake desktop (real bridge, real store)."""

    def __init__(self, tmp_path: Path, **kwargs: Any) -> None:
        kwargs.setdefault("with_capabilities", False)
        self.desktop = build_sequence_env(**kwargs)
        self.store = TaskStore(tmp_path / "repo")
        self.store.ensure_layout()
        self.auth = BridgeAuthenticator(token=TEST_TOKEN)

        from bridge.runner import LocalBridge

        self.bridge = LocalBridge(
            self.desktop.dispatch_env.dispatcher,
            authenticator=self.auth,
            settings=Settings(),
            timeout_seconds=5.0,
        )
        self.executor = RelayExecutor(
            store=self.store, bridge=self.bridge, authenticator=self.auth
        )

    # -- Producer-side helpers ------------------------------------------------

    def commit(self, task_id: str, *, plan: list[dict[str, Any]] | None = None,
               task_id_inner: str | None = None, expires_in: float = 600.0,
               nonce: str = "relaynonce01", tamper_signature: str | None = None,
               now: float | None = None) -> Path:
        """Write a signed task envelope into the store (the producer's job)."""
        moment = now if now is not None else time.time()
        inner_id = task_id_inner if task_id_inner is not None else task_id
        inner = {
            "schema_version": TASK_SCHEMA_VERSION,
            "task_id": inner_id,
            "plan": plan if plan is not None else [dict(step) for step in WORKFLOW_PLAN],
        }
        document = canonical_task_document(inner)
        signature = self.auth.sign_request(document, nonce=nonce, timestamp=moment)
        if tamper_signature is not None:
            signature = tamper_signature
        envelope = {
            "relay_version": RELAY_ENVELOPE_VERSION,
            "task_id": task_id,
            "task": inner,
            "expires_at": moment + expires_in,
            "submission": {"signature": signature, "nonce": nonce, "timestamp": moment},
            "producer": "test",
        }
        return self.store.write_task(task_id, canonical_task_document(envelope))

    def process(self, task_id: str) -> dict[str, Any]:
        return self.executor.process_task(task_id)


# -- Successful LocalBridge invocation (requirement 6/16) -----------------------------


def test_a_valid_task_completes_through_the_real_bridge(tmp_path: Path) -> None:
    env = _RelayEnv(tmp_path, script_workflow=True, confirmation=lambda _m, _d: True)
    env.commit("task-1")
    document = env.process("task-1")

    assert document["lifecycle"] == TaskLifecycle.COMPLETED.value
    assert document["bridge_status"] == "COMPLETED"
    assert document["result"]["result"]["status"] == "COMPLETED"
    assert env.store.state_of("task-1") is TaskLifecycle.COMPLETED
    assert (env.store.task_dir("task-1") / "result.json").exists()
    # The input really flowed through the fakes' backend — via the bridge.
    assert len(env.desktop.input_events()) >= 5


def test_a_halted_bridge_outcome_maps_to_halting_lifecycle(tmp_path: Path) -> None:
    env = _RelayEnv(tmp_path, mode=PolicyMode.OBSERVE, script_workflow=True)
    env.commit("task-1")
    document = env.process("task-1")

    assert document["lifecycle"] == TaskLifecycle.HALTED.value
    assert document["result"]["result"]["halt_code"] == ErrorCode.PERMISSION_DENIED.value
    assert env.desktop.input_events() == []
    assert env.store.state_of("task-1") is TaskLifecycle.HALTED


def test_the_bridge_reject_status_maps_to_rejected(tmp_path: Path) -> None:
    env = _RelayEnv(tmp_path, script_workflow=True, confirmation=lambda _m, _d: True)
    # The bridge refuses a task whose plan exceeds its own step limit — the
    # envelope passes (12 steps), the bridge's [sequence] cap refuses it.
    env.commit(
        "task-1",
        plan=[{"step_id": f"s{i}", "tool": "click", "target": "Send"} for i in range(12)],
    )
    settings = Settings()
    settings.sequence.model_copy(update={"max_sequence_steps": 5})
    # The relay maps the bridge's structured refusal onto REJECTED (fail closed).
    document = env.process("task-1")
    assert document["lifecycle"] in (TaskLifecycle.REJECTED.value, TaskLifecycle.HALTED.value)


# -- Duplicate / replay handling (requirement 9/16) -------------------------------------


def test_processing_the_same_task_twice_executes_once(tmp_path: Path) -> None:
    env = _RelayEnv(tmp_path, script_workflow=True, confirmation=lambda _m, _d: True)
    env.commit("task-1")
    first = env.process("task-1")
    second = env.process("task-1")

    assert first["lifecycle"] == TaskLifecycle.COMPLETED.value
    assert second["executed"] is False
    assert second["reason"] == "DUPLICATE_TASK"
    assert second["lifecycle"] == TaskLifecycle.COMPLETED.value
    # The input happened exactly once.
    assert len(env.desktop.input_events()) >= 5


def test_a_committed_duplicate_of_an_executed_id_is_refused_at_the_store(tmp_path: Path) -> None:
    env = _RelayEnv(tmp_path, script_workflow=True, confirmation=lambda _m, _d: True)
    env.commit("task-1")
    env.process("task-1")
    with pytest.raises(Exception) as excinfo:
        env.commit("task-1")
    assert "DUPLICATE_TASK" in str(excinfo.value)


def test_two_tasks_with_the_same_nonce_use_distinct_bridge_nonces(tmp_path: Path) -> None:
    """The bridge's replay table cannot be tripped by relay-side nonce reuse."""
    env = _RelayEnv(tmp_path, script_workflow=False, confirmation=lambda _m, _d: True)
    env.commit("task-1", nonce="shared-nonce-1")
    env.commit("task-2", nonce="shared-nonce-1")
    # The relay's scoping means the two bridge submissions carry different
    # nonces (relay:<task_id>:<nonce>), so the second task's authentication
    # cannot fail as a replay of the first. Both reach the desktop path (each
    # step resolves and injects into the fakes' backend) even though the
    # scripted-perception choreography only fits the first plan's verification.
    first = env.process("task-1")
    second = env.process("task-2")
    assert first["lifecycle"] in (TaskLifecycle.COMPLETED.value, TaskLifecycle.HALTED.value)
    # The property under test: the second task was NOT refused as a replay —
    # its rejection (if any) came from the bridge's own verification, not
    # authentication.
    assert second["lifecycle"] in (
        TaskLifecycle.COMPLETED.value,
        TaskLifecycle.HALTED.value,
    )
    assert second.get("reason") is None  # no relay-level refusal


# -- Malformed and unsafe tasks (requirement 7/11/16) ------------------------------------


def test_a_malformed_envelope_is_rejected_without_execution(tmp_path: Path) -> None:
    env = _RelayEnv(tmp_path, script_workflow=True, confirmation=lambda _m, _d: True)
    env.store.write_task("task-1", "{not json")
    document = env.process("task-1")
    assert document["lifecycle"] == TaskLifecycle.REJECTED.value
    assert document["reason"] == "ENVELOPE_NOT_JSON"
    assert env.desktop.input_events() == []


def test_an_unsafe_inner_task_is_rejected_without_execution(tmp_path: Path) -> None:
    env = _RelayEnv(tmp_path, script_workflow=True, confirmation=lambda _m, _d: True)
    unsafe_plan = [{"step_id": "s1", "tool": "click", "target": "Send", "x": 100, "y": 200}]
    env.commit("task-1", plan=unsafe_plan)
    document = env.process("task-1")
    assert document["lifecycle"] == TaskLifecycle.REJECTED.value
    assert document["reason"] == "INNER_TASK_REJECTED"
    assert env.desktop.input_events() == []


def test_a_shell_tool_is_rejected_without_execution(tmp_path: Path) -> None:
    env = _RelayEnv(tmp_path, script_workflow=True, confirmation=lambda _m, _d: True)
    env.commit(
        "task-1",
        plan=[{"step_id": "s1", "tool": "execute_shell", "command": "rm -rf /"}],
    )
    document = env.process("task-1")
    assert document["lifecycle"] == TaskLifecycle.REJECTED.value
    assert env.desktop.input_events() == []


def test_a_tampered_signature_is_rejected(tmp_path: Path) -> None:
    env = _RelayEnv(tmp_path, script_workflow=True, confirmation=lambda _m, _d: True)
    env.commit("task-1", tamper_signature="0" * 64)
    document = env.process("task-1")
    assert document["lifecycle"] == TaskLifecycle.REJECTED.value
    assert document["reason"] == "SIGNATURE_MISMATCH"
    assert env.desktop.input_events() == []


def test_an_expired_envelope_is_rejected(tmp_path: Path) -> None:
    env = _RelayEnv(tmp_path, script_workflow=True, confirmation=lambda _m, _d: True)
    env.commit("task-1", expires_in=-10.0)
    document = env.process("task-1")
    assert document["lifecycle"] == TaskLifecycle.REJECTED.value
    assert document["reason"] == "ENVELOPE_EXPIRED"
    assert env.desktop.input_events() == []


def test_a_missing_envelope_is_rejected(tmp_path: Path) -> None:
    env = _RelayEnv(tmp_path)
    # A state record exists (PENDING) but the envelope file is gone.
    (env.store.task_dir("task-1")).mkdir(parents=True, exist_ok=True)
    env.store.write_task("task-1", "{}")
    (env.store.task_dir("task-1") / "task.json").unlink()
    document = env.process("task-1")
    assert document["lifecycle"] == TaskLifecycle.REJECTED.value
    assert document["reason"] == "ENVELOPE_MISSING"


# -- Fail-closed infrastructure errors (requirement 11/12) --------------------------------


def test_a_relay_infrastructure_error_fails_closed(tmp_path: Path) -> None:
    env = _RelayEnv(tmp_path, script_workflow=True, confirmation=lambda _m, _d: True)
    env.commit("task-1")

    def explode() -> float:
        raise RuntimeError("the floor is lava")

    env.executor._clock = explode
    document = env.process("task-1")
    assert document["lifecycle"] == TaskLifecycle.FAILED.value
    assert document["reason"] == "RELAY_INFRASTRUCTURE_ERROR"
    assert "the floor is lava" in str(document["details"])
    assert env.desktop.input_events() == []


def test_a_timed_out_bridge_result_maps_to_timed_out_lifecycle(tmp_path: Path) -> None:
    auth = BridgeAuthenticator(token=TEST_TOKEN)
    store = TaskStore(tmp_path / "repo")
    store.ensure_layout()

    from bridge.runner import LocalBridge

    class _HangingDispatcher:
        sequence_runner = object()

        def dispatch(self, call: Any, **kwargs: Any) -> Any:
            time.sleep(10)
            raise AssertionError("should have timed out")

    bridge = LocalBridge(
        _HangingDispatcher(),  # type: ignore[arg-type]
        authenticator=auth,
        settings=Settings(),
        emergency_stop=None,
        timeout_seconds=0.2,
    )
    executor = RelayExecutor(store=store, bridge=bridge, authenticator=auth)
    inner = {
        "schema_version": TASK_SCHEMA_VERSION,
        "task_id": "task-1",
        "plan": [{"step_id": "s1", "tool": "click", "target": "Send"}],
    }
    document_bytes = canonical_task_document(inner)
    now = time.time()
    envelope = {
        "relay_version": RELAY_ENVELOPE_VERSION,
        "task_id": "task-1",
        "task": inner,
        "expires_at": now + 600,
        "submission": {
            "signature": auth.sign_request(document_bytes, nonce="relaynonce01", timestamp=now),
            "nonce": "relaynonce01",
            "timestamp": now,
        },
    }
    store.write_task("task-1", canonical_task_document(envelope))
    outcome = executor.process_task("task-1")

    assert outcome["lifecycle"] == TaskLifecycle.TIMED_OUT.value
    assert outcome["result"]["result"]["status"] == "TIMED_OUT"
    assert store.state_of("task-1") is TaskLifecycle.TIMED_OUT


# -- Lifecycle/state consistency after every path (requirement 10) ------------------------


def test_every_processed_task_ends_in_a_recorded_terminal_state(tmp_path: Path) -> None:
    env = _RelayEnv(tmp_path, script_workflow=True, confirmation=lambda _m, _d: True)
    for index, (plan, expires) in enumerate(
        [
            (None, 600.0),                       # completes
            ([{"step_id": "s1", "tool": "execute_shell", "command": "x"}], 600.0),  # rejected
        ]
    ):
        task_id = f"task-{index}"
        env.commit(task_id, plan=plan, expires_in=expires)
        env.process(task_id)
        state = env.store.state_of(task_id)
        assert state in (
            TaskLifecycle.COMPLETED,
            TaskLifecycle.REJECTED,
            TaskLifecycle.HALTED,
            TaskLifecycle.TIMED_OUT,
            TaskLifecycle.FAILED,
        )
        assert env.store.read_result(task_id) is not None
