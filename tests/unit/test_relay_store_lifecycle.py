"""Unit tests for the relay lifecycle and store (Phase 2B requirement 9/10/11)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from relay.lifecycle import (
    LEGAL_TRANSITIONS,
    TERMINAL_STATES,
    LifecycleError,
    TaskLifecycle,
    can_transition,
    require_transition,
)
from relay.store import StoreError, TaskStore


def _store(tmp_path: Path) -> TaskStore:
    store = TaskStore(tmp_path / "repo")
    store.ensure_layout()
    return store


def _task(store: TaskStore, task_id: str = "task-1", document: str = '{"task": true}') -> str:
    store.write_task(task_id, document)
    return task_id


# -- Lifecycle state machine (requirement 10) ------------------------------------------


def test_the_happy_path_is_legal() -> None:
    require_transition(TaskLifecycle.PENDING, TaskLifecycle.RUNNING, task_id="t")
    for terminal in TERMINAL_STATES:
        require_transition(TaskLifecycle.RUNNING, terminal, task_id="t")


def test_skipping_running_is_illegal() -> None:
    with pytest.raises(LifecycleError):
        require_transition(TaskLifecycle.PENDING, TaskLifecycle.COMPLETED, task_id="t")


def test_backwards_transitions_are_illegal() -> None:
    with pytest.raises(LifecycleError):
        require_transition(TaskLifecycle.RUNNING, TaskLifecycle.PENDING, task_id="t")


def test_terminal_states_transition_to_nothing() -> None:
    for terminal in TERMINAL_STATES:
        for attempted in TaskLifecycle:
            if attempted is terminal:
                continue
            assert not can_transition(terminal, attempted), (terminal, attempted)


def test_a_redundant_transition_is_refused() -> None:
    with pytest.raises(LifecycleError):
        require_transition(TaskLifecycle.RUNNING, TaskLifecycle.RUNNING, task_id="t")


def test_every_state_appears_in_the_transition_table() -> None:
    assert set(LEGAL_TRANSITIONS) == set(TaskLifecycle)


# -- Store: writing and claiming (requirement 3/9) ---------------------------------------


def test_a_written_task_starts_pending(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task_id = _task(store)
    assert store.state_of(task_id) is TaskLifecycle.PENDING
    assert (store.task_dir(task_id) / "task.json").exists()


def test_an_invalid_task_id_is_refused(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(StoreError) as excinfo:
        store.write_task("../escape", "{}")
    assert excinfo.value.reason == "INVALID_TASK_ID"


def test_resubmitting_a_terminal_task_is_a_duplicate(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task_id = _task(store)
    store.claim(task_id)
    with pytest.raises(StoreError) as excinfo:
        store.write_task(task_id, '{"task": true}')
    assert excinfo.value.reason == "DUPLICATE_TASK"


def test_resubmitting_a_pending_task_with_a_different_document_conflicts(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task_id = _task(store)
    with pytest.raises(StoreError) as excinfo:
        store.write_task(task_id, '{"task": false}')
    assert excinfo.value.reason == "TASK_CONFLICT"


def test_resubmitting_a_pending_task_with_the_same_document_is_a_no_op(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task_id = _task(store, document='{"task": true}')
    store.write_task(task_id, '{"task": true}')  # producer retry of its own write
    assert store.state_of(task_id) is TaskLifecycle.PENDING


def test_a_claim_transitions_to_running(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task_id = _task(store)
    store.claim(task_id)
    assert store.state_of(task_id) is TaskLifecycle.RUNNING
    assert (store.claims_dir / f"{task_id}.claim").exists()


def test_a_second_claim_is_a_duplicate(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task_id = _task(store)
    store.claim(task_id)
    with pytest.raises(StoreError) as excinfo:
        store.claim(task_id)
    assert excinfo.value.reason == "DUPLICATE_TASK"
    assert store.state_of(task_id) is TaskLifecycle.RUNNING


def test_a_claim_on_an_unknown_task_is_refused_and_rolled_back(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(StoreError):
        store.claim("never-written")
    assert not (store.claims_dir / "never-written.claim").exists()


def test_a_stale_claim_is_reset_only_explicitly(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task_id = _task(store)
    store.claim(task_id)
    assert store.reset_claims(task_id) is True
    assert not (store.claims_dir / f"{task_id}.claim").exists()
    assert store.reset_claims(task_id) is False


# -- Store: transitions and history (requirement 10) --------------------------------------


def test_transitions_are_recorded_with_history(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task_id = _task(store)
    store.claim(task_id)
    record = store.transition(task_id, TaskLifecycle.COMPLETED, note="bridge outcome")
    assert record["state"] == "COMPLETED"
    assert len(record["history"]) == 2
    assert record["history"][0]["to"] == "RUNNING"
    assert record["history"][1]["to"] == "COMPLETED"


def test_an_illegal_transition_leaves_the_record_untouched(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task_id = _task(store)
    with pytest.raises(StoreError) as excinfo:
        store.transition(task_id, TaskLifecycle.COMPLETED)
    assert excinfo.value.reason == "ILLEGAL_TRANSITION"
    assert store.state_of(task_id) is TaskLifecycle.PENDING


def test_a_completed_task_cannot_be_re_transitioned(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task_id = _task(store)
    store.claim(task_id)
    store.transition(task_id, TaskLifecycle.COMPLETED)
    with pytest.raises(StoreError) as excinfo:
        store.transition(task_id, TaskLifecycle.RUNNING)
    assert excinfo.value.reason == "ILLEGAL_TRANSITION"


def test_an_unknown_task_cannot_transition(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(StoreError) as excinfo:
        store.transition("ghost", TaskLifecycle.RUNNING)
    assert excinfo.value.reason == "TASK_UNKNOWN"


# -- Store: results (requirement 3) ---------------------------------------------------------


def test_results_are_written_once_and_atomically(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task_id = _task(store)
    first, flat = store.write_result(task_id, {"ok": True})
    assert first.exists() and flat.exists()
    assert json.loads(first.read_text()) == {"ok": True}
    with pytest.raises(StoreError) as excinfo:
        store.write_result(task_id, {"ok": False})
    assert excinfo.value.reason == "RESULT_EXISTS"
    assert json.loads(first.read_text()) == {"ok": True}  # never overwritten


def test_reading_a_missing_result_returns_none(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.read_result("nope") is None


def test_state_of_an_unknown_task_is_pending(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.state_of("ghost") is TaskLifecycle.PENDING
