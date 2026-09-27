"""The tool protocol's readiness for an external adapter (integration phase).

A future MCP adapter will consume exactly three surfaces: ``tool_declarations()``
(the advertised schemas), ``ToolCall`` (the request) and ``ToolEnvelope`` (the
result). These tests pin the properties that surface must keep so the adapter
phase needs no core rework:

* the declarations are **deterministic** (same input, same bytes) and **JSON-
  serializable** as-is — an adapter must not have to translate them;
* every schema is a strict object (``additionalProperties: false``) whose
  property types are real JSON types;
* the dispatcher rejects every class of pre-resolved/overspecified argument
  (coordinates, element ids, leases, unknown keys, missing keys, bad enums) —
  the boundary is strict, so the adapter never has to re-validate;
* result envelopes — single, failed, and the ordered ``run_sequence`` array
  with its ``NOT_EXECUTED`` tail — serialize to JSON unchanged.
"""

from __future__ import annotations

import json

import pytest

from ai.tool_protocol import (
    ToolCall,
    ToolProtocolError,
    build_planned_action,
    tool_declarations,
)
from schemas.actions import ToolEnvelope, ToolName
from schemas.enums import ErrorCode, VerificationState
from schemas.sequences import SequenceResult, SequenceStep, SequenceStepResult

_VALID_TYPES = {"object", "string", "integer", "boolean", "array", "number"}


def test_the_declarations_are_deterministic_across_calls() -> None:
    """A stable surface: two builds of the declarations must be identical."""
    first = [declaration.to_function_declaration() for declaration in tool_declarations()]
    second = [declaration.to_function_declaration() for declaration in tool_declarations()]
    assert first == second
    assert len(first) == 19  # the complete section 66 tool set


def test_every_declaration_is_a_json_serializable_strict_object_schema() -> None:
    for declaration in tool_declarations():
        payload = declaration.to_function_declaration()
        json.dumps(payload)  # must not raise
        schema = declaration.parameters
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        for key, value in schema.get("properties", {}).items():
            assert value.get("type") in _VALID_TYPES, (declaration.name, key)
            json.dumps(value)


def test_every_declaration_names_a_unique_tool() -> None:
    names = [declaration.name for declaration in tool_declarations()]
    assert len(names) == len(set(names))


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        (ToolName.CLICK, {"target": "Send", "x": 100, "y": 200}),
        (ToolName.CLICK, {"target": "Send", "element_id": "e1"}),
        (ToolName.CLICK, {"target": "Send", "lease_id": "l1"}),
        (ToolName.CLICK, {"target": "Send", "frame_id": 7}),
        (ToolName.CLICK, {"target": "Send", "bogus": 1}),
        (ToolName.TYPE_TEXT, {"target": "field"}),
        (ToolName.CLICK, {"target": "Send", "role": "WIZARD"}),
    ],
)
def test_the_dispatcher_rejects_every_pre_resolved_or_invalid_argument(
    tool: str, arguments: dict[str, object]
) -> None:
    """The boundary is strict, so an adapter never has to re-validate."""
    with pytest.raises(ToolProtocolError):
        build_planned_action(tool, arguments)


def test_unknown_tool_is_a_structured_refusal() -> None:
    with pytest.raises(ToolProtocolError) as excinfo:
        build_planned_action("not_a_tool", {})
    assert excinfo.value.code is ErrorCode.BACKEND_UNAVAILABLE


def test_a_tool_call_round_trips_through_json() -> None:
    call = ToolCall(name=ToolName.CLICK, arguments={"target": "Send"}, call_id="c1")
    payload = {"name": call.name, "arguments": call.arguments, "call_id": call.call_id}
    assert json.loads(json.dumps(payload)) == payload


def _sequence_result() -> SequenceResult:
    ok = ToolEnvelope.success(
        data={"target": {"element_id": "e1"}},
        verification=VerificationState.VERIFIED,
        state_version=3,
        frame_id=7,
        elapsed_ms=1.5,
    )
    step1 = SequenceStep(step_id="s1", tool=ToolName.CLICK, target="Send")
    executed = SequenceStepResult.from_envelope(ok, step=step1, index=0)
    step2 = SequenceStep(step_id="s2", tool=ToolName.CLICK, target="Draft")
    tail = SequenceStepResult.not_executed_step(step=step2, index=1, reason="halted")
    return SequenceResult(
        sequence_id="seq-1",
        steps=(executed, tail),
        halted=True,
        halt_reason="halt",
        halt_code=ErrorCode.TARGET_AMBIGUOUS,
        wall_clock_ms=12.0,
        started_at=0.0,
    )


@pytest.mark.parametrize(
    "envelope",
    [
        ToolEnvelope.success(data={"ok": 1}, verification=VerificationState.VERIFIED),
        ToolEnvelope.failure(ErrorCode.TARGET_AMBIGUOUS, "two", data={"resolution": {}}),
    ],
)
def test_tool_envelopes_serialize_to_json_unchanged(envelope: ToolEnvelope) -> None:
    assert json.loads(json.dumps(envelope.to_dict())) == envelope.to_dict()


def test_a_sequence_result_with_a_not_executed_tail_is_json_serializable() -> None:
    payload = _sequence_result().to_dict()
    assert json.loads(json.dumps(payload)) == payload
    assert payload["steps"][1]["error_code"] == "NOT_EXECUTED"
    assert payload["halted"] is True
    assert payload["halt_code"] == "TARGET_AMBIGUOUS"
