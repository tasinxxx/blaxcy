"""``run_sequence`` schemas (specification section 66.1)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from schemas.actions import ToolEnvelope, ToolName
from schemas.enums import ActionClass, ErrorCode, UIRole, VerificationState
from schemas.sequences import (
    DEFAULT_HALT_CONDITIONS,
    FORBIDDEN_STEP_KEYS,
    HaltCondition,
    RunSequenceRequest,
    SequenceProgress,
    SequenceResult,
    SequenceStep,
    SequenceStepResult,
)


def _step(step_id: str = "s1", **overrides: object) -> SequenceStep:
    """Build a click step, overriding fields as needed."""
    base: dict[str, object] = {"step_id": step_id, "tool": ToolName.CLICK, "target": "search icon", "role": UIRole.BUTTON}
    base.update(overrides)
    return SequenceStep.model_validate(base)


def test_step_collects_tool_specific_args() -> None:
    """The wire shape of a step matches the standalone tool call."""
    step = SequenceStep.model_validate(
        {"step_id": "s2", "tool": ToolName.TYPE_TEXT, "target": "search box", "text": "song name"}
    )
    assert step.args == {"text": "song name"}
    assert step.target == "search box"
    assert step.to_dict() == {
        "step_id": "s2",
        "tool": ToolName.TYPE_TEXT,
        "target": "search box",
        "text": "song name",
    }


def test_step_rejects_coordinate_target() -> None:
    """A pre-resolved coordinate is stale by construction (section 66.1)."""
    with pytest.raises(ValidationError):
        _step(target="640, 480")
    with pytest.raises(ValidationError):
        _step(target="x=100, y=200")


def test_step_rejects_identity_and_lease_arguments() -> None:
    """A step may not carry a pre-resolved element or lease."""
    with pytest.raises(ValidationError):
        SequenceStep.model_validate(
            {"step_id": "s1", "tool": ToolName.CLICK, "target": "ok", "element_id": "e1"}
        )
    with pytest.raises(ValidationError):
        SequenceStep.model_validate(
            {"step_id": "s1", "tool": ToolName.CLICK, "target": "ok", "lease_id": "L1"}
        )
    assert "lease_id" in FORBIDDEN_STEP_KEYS
    assert "element_id" in FORBIDDEN_STEP_KEYS


def test_step_rejects_run_sequence_nesting() -> None:
    """Sequences do not nest."""
    with pytest.raises(ValidationError):
        _step(tool=ToolName.RUN_SEQUENCE)


def test_step_default_verification_by_class() -> None:
    """Verification defaults to the action class, but can be overridden."""
    click = _step(tool=ToolName.CLICK)
    assert click.action_class is ActionClass.MUTATING
    assert click.requires_verification is True
    read = _step(tool=ToolName.GET_SCREEN_STATE)
    assert read.requires_verification is False
    overridden = _step(tool=ToolName.CLICK, require_verification=False)
    assert overridden.requires_verification is False


def _request(steps: tuple[SequenceStep, ...] | None = None, **overrides: object) -> RunSequenceRequest:
    """Build a valid 3-step request (search -> type -> submit)."""
    default_steps = (
        _step("s1"),
        SequenceStep.model_validate(
            {"step_id": "s2", "tool": ToolName.TYPE_TEXT, "target": "search box", "text": "song"}
        ),
        SequenceStep.model_validate({"step_id": "s3", "tool": ToolName.PRESS_KEY, "key": "ENTER"}),
    )
    payload: dict[str, object] = {"steps": steps if steps is not None else default_steps}
    payload.update(overrides)
    return RunSequenceRequest.model_validate(payload)


def test_request_requires_unique_step_ids() -> None:
    """Duplicate step ids make results ambiguous."""
    with pytest.raises(ValidationError):
        _request(steps=(_step("s1"), _step("s1")))


def test_request_default_halt_set_is_the_full_safety_set() -> None:
    """Omitting halt_on means every default safety halt applies."""
    request = _request()
    assert request.effective_halt_on == DEFAULT_HALT_CONDITIONS
    assert HaltCondition.TARGET_AMBIGUOUS in request.effective_halt_on
    assert HaltCondition.PASSWORD_CONTEXT in request.effective_halt_on


def test_halt_on_can_narrow_but_not_widen() -> None:
    """Section 66.1: halt_on may only narrow within the default safety set."""
    narrowed = _request(halt_on=(HaltCondition.TARGET_AMBIGUOUS, HaltCondition.BLOCKED_APPLICATION))
    assert narrowed.effective_halt_on == frozenset(
        {HaltCondition.TARGET_AMBIGUOUS, HaltCondition.BLOCKED_APPLICATION}
    )
    with pytest.raises(ValidationError):
        RunSequenceRequest.model_validate(
            {
                "steps": [_step("s1").to_dict()],
                "halt_on": ["NOT_A_REAL_CONDITION"],
            }
        )


def test_request_action_class_is_most_restrictive() -> None:
    """A sequence gate is its most restrictive step."""
    steps = (
        _step("s1"),
        SequenceStep.model_validate({"step_id": "s4", "tool": ToolName.DRAG, "target": "file"}),
    )
    assert _request(steps=steps).action_class is ActionClass.DESTRUCTIVE
    assert _request().action_class is ActionClass.MUTATING


def test_step_result_from_envelope_preserves_fields() -> None:
    """A wrapped step result keeps the envelope's outcome."""
    envelope = ToolEnvelope.success(verification=VerificationState.VERIFIED, frame_id=7)
    step = _step("s1")
    result = SequenceStepResult.from_envelope(envelope, step=step, index=0)
    assert result.step_id == "s1"
    assert result.index == 0
    assert result.tool == ToolName.CLICK
    assert result.verification is VerificationState.VERIFIED
    assert result.frame_id == 7


def test_step_result_not_executed() -> None:
    """An unreached step reports NOT_EXECUTED."""
    result = SequenceStepResult.not_executed_step(step=_step("s3"), index=2, reason="sequence halted")
    assert result.error_code is ErrorCode.NOT_EXECUTED
    assert result.ok is False
    assert result.index == 2


def test_result_requires_dense_ordering() -> None:
    """Results are one entry per declared step, in order."""
    steps = (_step("s1"), _step("s2"))
    good = (
        SequenceStepResult.not_executed_step(step=steps[0], index=0),
        SequenceStepResult.not_executed_step(step=steps[1], index=1),
    )
    result = SequenceResult(
        sequence_id="seq-1",
        steps=good,
        halted=False,
        wall_clock_ms=10.0,
        started_at=0.0,
    )
    assert result.completed_count == 0
    assert result.not_executed_count == 2

    with pytest.raises(ValidationError):
        SequenceResult(
            sequence_id="seq-1",
            steps=(good[1],),
            halted=False,
            wall_clock_ms=1.0,
            started_at=0.0,
        )


def test_result_halt_reason_consistency() -> None:
    """A halted sequence records why; an unhalted one does not."""
    step = _step("s1")
    done = SequenceStepResult.not_executed_step(step=step, index=0)
    with pytest.raises(ValidationError):
        SequenceResult(
            sequence_id="q",
            steps=(done,),
            halted=False,
            halt_reason="AMBIGUOUS",
            wall_clock_ms=1.0,
            started_at=0.0,
        )
    with pytest.raises(ValidationError):
        SequenceResult(sequence_id="q", steps=(done,), halted=True, wall_clock_ms=1.0, started_at=0.0)
    halted = SequenceResult(
        sequence_id="q",
        steps=(done,),
        halted=True,
        halt_reason="AMBIGUOUS",
        wall_clock_ms=1.0,
        started_at=0.0,
    )
    assert halted.halt_reason == "AMBIGUOUS"


def test_result_to_dict_is_a_per_step_envelope_array() -> None:
    """Section 66: run_sequence returns an array of envelopes."""
    step = _step("s1")
    verified = SequenceStepResult.from_envelope(
        ToolEnvelope.success(verification=VerificationState.VERIFIED), step=step, index=0
    )
    result = SequenceResult(
        sequence_id="seq-9",
        steps=(verified,),
        halted=False,
        wall_clock_ms=42.0,
        started_at=0.0,
    )
    payload = result.to_dict()
    assert payload["sequence_id"] == "seq-9"
    assert payload["completed_count"] == 1
    entry = payload["steps"][0]
    assert entry["step_id"] == "s1"
    assert entry["tool"] == "click"
    assert entry["action_class"] == "MUTATING"
    assert entry["require_verification"] is True


def test_sequence_progress_validates_indices() -> None:
    """Progress indices stay within the plan."""
    progress = SequenceProgress(
        sequence_id="seq-1", current_index=1, total_steps=3, completed_indices=(0,)
    )
    assert progress.is_complete is False
    assert progress.to_dict()["completed_indices"] == [0]
    with pytest.raises(ValidationError):
        SequenceProgress(
            sequence_id="seq-1", current_index=0, total_steps=3, completed_indices=(5,)
        )
