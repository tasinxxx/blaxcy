"""Unit tests for the bridge task protocol (Phase 2A requirement 6/7/8/9/15).

Covers: strict parsing, version pinning, size and field guards, unsafe-input
rejection (coordinates, ids, leases, secrets), result serialization, and the
envelope invariants the boundary promises.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from bridge.task_protocol import (
    MAX_TASK_PAYLOAD_BYTES,
    MAX_TASK_STEPS,
    TASK_SCHEMA_VERSION,
    Task,
    TaskEnvelope,
    TaskResult,
    TaskStatus,
    TaskValidationError,
)
from schemas.sequences import SequenceStep


def _step(step_id: str = "s1", **overrides: object) -> dict[str, object]:
    step: dict[str, object] = {"step_id": step_id, "tool": "click", "target": "Send"}
    step.update(overrides)
    return step


def _task_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": TASK_SCHEMA_VERSION,
        "task_id": "task-1",
        "plan": [_step()],
    }
    payload.update(overrides)
    return payload


# -- Parsing --------------------------------------------------------------------


def test_a_well_formed_task_parses() -> None:
    task = Task.parse(_task_payload())
    assert task.task_id == "task-1"
    assert task.schema_version == TASK_SCHEMA_VERSION
    assert len(task.plan) == 1
    assert task.plan[0].tool == "click"
    assert task.plan[0].target == "Send"


def test_a_json_string_parses_like_a_dict() -> None:
    task = Task.parse(json.dumps(_task_payload()))
    assert task.task_id == "task-1"


def test_a_wrong_schema_version_is_rejected() -> None:
    with pytest.raises(TaskValidationError) as excinfo:
        Task.parse(_task_payload(schema_version=TASK_SCHEMA_VERSION + 1))
    assert excinfo.value.reason == "TASK_SCHEMA_INVALID"


def test_a_missing_schema_version_is_rejected() -> None:
    payload = _task_payload()
    del payload["schema_version"]
    with pytest.raises(TaskValidationError):
        Task.parse(payload)


def test_an_empty_plan_is_rejected() -> None:
    with pytest.raises(TaskValidationError):
        Task.parse(_task_payload(plan=[]))


def test_a_plan_over_the_step_cap_is_rejected() -> None:
    steps = [_step(f"s{i}") for i in range(MAX_TASK_STEPS + 1)]
    with pytest.raises(TaskValidationError):
        Task.parse(_task_payload(plan=steps))


def test_an_oversized_payload_is_rejected() -> None:
    payload = json.dumps(_task_payload()).replace(
        '"Send"', '"' + "x" * (MAX_TASK_PAYLOAD_BYTES + 10) + '"'
    )
    with pytest.raises(TaskValidationError) as excinfo:
        Task.parse(payload)
    assert excinfo.value.reason == "TASK_TOO_LARGE"


def test_a_non_object_payload_is_rejected() -> None:
    for payload in ("[]", '"hello"', "42", "null"):
        with pytest.raises(TaskValidationError) as excinfo:
            Task.parse(payload)
        assert excinfo.value.reason == "TASK_NOT_AN_OBJECT"


def test_a_malformed_json_payload_is_rejected() -> None:
    with pytest.raises(TaskValidationError) as excinfo:
        Task.parse('{"schema_version": 1, ')
    assert excinfo.value.reason == "TASK_NOT_JSON"


def test_unknown_top_level_fields_are_rejected() -> None:
    with pytest.raises(TaskValidationError) as excinfo:
        Task.parse(_task_payload(extra_field="nope"))
    assert excinfo.value.reason == "UNKNOWN_TASK_FIELDS"
    assert excinfo.value.details["unknown"] == ["extra_field"]


@pytest.mark.parametrize(
    "key",
    ["x", "y", "coordinates", "element_id", "elementid", "lease", "lease_id", "frame_id", "state_version", "generation"],
)
def test_pre_resolved_fields_are_rejected_at_the_top_level(key: str) -> None:
    with pytest.raises(TaskValidationError) as excinfo:
        Task.parse(_task_payload(**{key: 1}))
    assert excinfo.value.reason == "PRE_RESOLVED_TASK_FIELD"
    assert excinfo.value.details["key"] == key


@pytest.mark.parametrize(
    "key",
    ["password", "api_key", "secret", "credential", "auth_token", "authorization"],
)
def test_credential_shaped_fields_are_rejected_at_the_top_level(key: str) -> None:
    with pytest.raises(TaskValidationError) as excinfo:
        Task.parse(_task_payload(**{key: "hunter2"}))
    assert excinfo.value.reason == "FORBIDDEN_TASK_FIELD"


def test_a_smuggled_element_id_inside_a_step_is_rejected() -> None:
    payload = _task_payload(plan=[{**_step(), "element_id": "e1"}])
    with pytest.raises(TaskValidationError) as excinfo:
        Task.parse(payload)
    assert excinfo.value.reason == "PRE_RESOLVED_TASK_FIELD"
    assert excinfo.value.details["key"] == "element_id"


def test_a_smuggled_secret_inside_a_step_is_rejected() -> None:
    payload = _task_payload(plan=[{**_step(), "text": "ok", "password": "hunter2"}])
    with pytest.raises(TaskValidationError) as excinfo:
        Task.parse(payload)
    assert excinfo.value.reason == "FORBIDDEN_TASK_FIELD"


def test_a_nested_secret_inside_step_args_is_rejected() -> None:
    payload = _task_payload(plan=[{**_step(), "args": {"nested": {"api_key": "k"}}}])
    with pytest.raises(TaskValidationError) as excinfo:
        Task.parse(payload)
    assert excinfo.value.reason == "FORBIDDEN_TASK_FIELD"


def test_a_coordinate_shaped_target_is_rejected_by_the_sequence_schema() -> None:
    payload = _task_payload(plan=[{"step_id": "s1", "tool": "click", "target": "100, 200"}])
    with pytest.raises(TaskValidationError):
        Task.parse(payload)


def test_a_shell_tool_cannot_be_a_step() -> None:
    payload = _task_payload(plan=[{"step_id": "s1", "tool": "execute_shell", "command": "rm -rf /"}])
    with pytest.raises(TaskValidationError):
        Task.parse(payload)


def test_a_nesting_run_sequence_step_is_rejected() -> None:
    payload = _task_payload(plan=[{"step_id": "s1", "tool": "run_sequence", "steps": []}])
    with pytest.raises(TaskValidationError):
        Task.parse(payload)


def test_a_duplicate_step_id_is_rejected() -> None:
    payload = _task_payload(plan=[_step("s1"), _step("s1", target="Draft")])
    with pytest.raises(TaskValidationError):
        Task.parse(payload)


def test_a_bad_task_id_is_rejected() -> None:
    with pytest.raises(TaskValidationError):
        Task.parse(_task_payload(task_id="bad id with spaces"))


def test_a_halt_on_field_is_not_part_of_the_task_contract() -> None:
    """The bridge never lets a producer narrow the safety halt set (§66.1)."""
    with pytest.raises(TaskValidationError) as excinfo:
        Task.parse(_task_payload(halt_on=[]))
    assert excinfo.value.reason == "UNKNOWN_TASK_FIELDS"


# -- Result serialization (requirement 7/15) --------------------------------------


def _result(status: TaskStatus = TaskStatus.COMPLETED, **overrides: object) -> TaskResult:
    defaults: dict[str, object] = {
        "task_id": "task-1",
        "status": status,
        "sequence_id": "seq-1",
        "wall_clock_ms": 12.5,
        "completed_count": 1,
    }
    defaults.update(overrides)
    return TaskResult(**defaults)


def test_a_completed_result_serializes_round_trip() -> None:
    payload = _result().to_payload()
    assert json.loads(json.dumps(payload)) == payload
    assert payload["status"] == "COMPLETED"
    assert payload["halted"] is False


def test_a_halted_result_carries_the_halt_fields() -> None:
    payload = _result(
        status=TaskStatus.HALTED,
        halted=True,
        halt_reason="step s1 halted",
        halt_code="TARGET_AMBIGUOUS",
    ).to_payload()
    assert payload["halted"] is True
    assert payload["halt_code"] == "TARGET_AMBIGUOUS"


def test_a_rejected_result_requires_a_reason_and_has_no_steps() -> None:
    payload = _result(status=TaskStatus.REJECTED, sequence_id=None, rejection_reason="TASK_TOO_LARGE").to_payload()
    assert payload["rejection_reason"] == "TASK_TOO_LARGE"
    assert payload["steps"] == []
    with pytest.raises(ValidationError):
        TaskResult(task_id="t", status=TaskStatus.REJECTED)


def test_a_timed_out_result_requires_the_stop_evidence() -> None:
    payload = _result(
        status=TaskStatus.TIMED_OUT,
        sequence_id=None,
        halted=True,
        halt_reason="bridge timeout",
        timeout_elapsed_ms=1000.0,
        timeout_stop_latched=True,
    ).to_payload()
    assert payload["timeout_stop_latched"] is True
    with pytest.raises(ValidationError):
        TaskResult(task_id="t", status=TaskStatus.TIMED_OUT, timeout_elapsed_ms=1.0)


def test_a_completed_result_cannot_claim_a_halt() -> None:
    with pytest.raises(ValidationError):
        _result(halted=True, halt_reason="nope")


def test_an_envelope_serializes_round_trip_with_and_without_a_tool_envelope() -> None:
    result = _result()
    assert json.loads(json.dumps(TaskEnvelope(task_id="t", envelope=None, result=result).to_payload()))
    with_envelope = TaskEnvelope(task_id="t", envelope={"ok": True}, result=result)
    assert json.loads(json.dumps(with_envelope.to_payload()))["envelope"] == {"ok": True}


def test_a_step_result_keeps_the_run_sequence_shape() -> None:
    """A task's step results are the core's own — no translation layer."""
    ok = Task.parse(_task_payload())
    assert isinstance(ok.plan[0], SequenceStep)
