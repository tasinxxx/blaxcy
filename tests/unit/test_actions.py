"""Tool vocabulary, action classes and the tool envelope (sections 57, 66)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from schemas.actions import (
    REQUIRED_TOOLS,
    SEQUENCE_STEP_TOOLS,
    TOOL_ACTION_CLASS,
    PlannedAction,
    ToolEnvelope,
    ToolName,
    action_class_for,
    most_restrictive_action_class,
    requires_verification,
)
from schemas.elements import ElementQuery
from schemas.enums import ActionClass, ErrorCode, VerificationState

#: The section 66 tool list, transcribed from the specification.
SPEC_TOOLS = {
    "get_screen_state",
    "find_element",
    "click",
    "double_click",
    "right_click",
    "type_text",
    "press_key",
    "hotkey",
    "scroll",
    "drag",
    "wait_for_change",
    "wait_for_element",
    "get_active_window",
    "describe_region",
    "activate_element",
    "ensure_window",
    "get_capabilities",
    "emergency_stop",
    "run_sequence",
}


def test_tool_set_matches_the_specification() -> None:
    """Every section 66 tool exists; nothing extra was invented."""
    assert REQUIRED_TOOLS == SPEC_TOOLS


def test_run_sequence_is_not_a_step_tool() -> None:
    """Sequences do not nest."""
    assert ToolName.RUN_SEQUENCE not in SEQUENCE_STEP_TOOLS
    assert SPEC_TOOLS - {ToolName.RUN_SEQUENCE} == SEQUENCE_STEP_TOOLS


def test_action_class_mapping_is_complete_for_step_tools() -> None:
    """Every step-capable tool has a consciously assigned action class."""
    assert set(TOOL_ACTION_CLASS) == SEQUENCE_STEP_TOOLS


def test_non_obvious_action_classes() -> None:
    """The safety-relevant class assignments are exactly as specified."""
    assert action_class_for(ToolName.GET_SCREEN_STATE) is ActionClass.READ_ONLY
    assert action_class_for(ToolName.SCROLL) is ActionClass.NAVIGATIONAL
    assert action_class_for(ToolName.ENSURE_WINDOW) is ActionClass.NAVIGATIONAL
    assert action_class_for(ToolName.CLICK) is ActionClass.MUTATING
    assert action_class_for(ToolName.TYPE_TEXT) is ActionClass.MUTATING
    # Section 49: an unresolved drop target defaults to destructive.
    assert action_class_for(ToolName.DRAG) is ActionClass.DESTRUCTIVE


def test_run_sequence_has_no_fixed_class() -> None:
    """Section 57: run_sequence's class depends on its steps."""
    with pytest.raises(ValueError):
        action_class_for(ToolName.RUN_SEQUENCE)


def test_unknown_tool_has_no_default_class() -> None:
    """A new tool cannot silently inherit a class."""
    with pytest.raises(ValueError):
        action_class_for("teleport")


def test_most_restrictive_class_wins() -> None:
    """A sequence containing one destructive step is gated as destructive."""
    assert (
        most_restrictive_action_class(
            (ActionClass.NAVIGATIONAL, ActionClass.DESTRUCTIVE, ActionClass.MUTATING)
        )
        is ActionClass.DESTRUCTIVE
    )
    assert most_restrictive_action_class(()) is ActionClass.READ_ONLY


def test_verification_required_for_mutating_and_destructive() -> None:
    """Section 60: reads/navigation need no postcondition; writes do."""
    assert requires_verification(ActionClass.MUTATING) is True
    assert requires_verification(ActionClass.DESTRUCTIVE) is True
    assert requires_verification(ActionClass.READ_ONLY) is False
    assert requires_verification(ActionClass.NAVIGATIONAL) is False


def test_planned_action_derives_class_and_verification() -> None:
    """The class and verification requirement come from the tool."""
    action = PlannedAction(tool=ToolName.CLICK, target=ElementQuery(text="OK"))
    assert action.action_class is ActionClass.MUTATING
    assert action.require_verification is True
    read = PlannedAction(tool=ToolName.GET_SCREEN_STATE)
    assert read.action_class is ActionClass.READ_ONLY
    assert read.require_verification is False


def test_planned_action_rejects_unknown_tool() -> None:
    """An unresolvable tool is a validation error."""
    with pytest.raises(ValidationError):
        PlannedAction(tool=ToolName.RUN_SEQUENCE)


def test_envelope_rejects_success_with_error_code() -> None:
    """ok=True and an error_code contradict each other."""
    with pytest.raises(ValidationError):
        ToolEnvelope(ok=True, error_code=ErrorCode.INTERNAL_ERROR)


def test_envelope_rejects_failure_without_error_code() -> None:
    """A failure must name one taxonomy code."""
    with pytest.raises(ValidationError):
        ToolEnvelope(ok=False)


def test_envelope_rejects_contradicted_success() -> None:
    """A CONTRADICTED result is never reported as ok."""
    with pytest.raises(ValidationError):
        ToolEnvelope(ok=True, verification=VerificationState.CONTRADICTED)


def test_envelope_success_and_failure_helpers() -> None:
    """The helpers produce consistent envelopes."""
    ok = ToolEnvelope.success(data={"n": 1}, verification=VerificationState.VERIFIED)
    assert ok.ok is True and ok.error_code is None
    assert ok.to_dict()["verification"] == "VERIFIED"
    bad = ToolEnvelope.failure(ErrorCode.TARGET_NOT_FOUND, "no match")
    assert bad.ok is False and bad.error_code is ErrorCode.TARGET_NOT_FOUND
    skipped = ToolEnvelope.not_executed()
    assert skipped.error_code is ErrorCode.NOT_EXECUTED
    assert skipped.verification is VerificationState.NOT_APPLICABLE


def test_envelope_to_dict_has_exactly_the_spec_fields() -> None:
    """Section 66 fixes the envelope field set."""
    payload = ToolEnvelope.success().to_dict()
    assert set(payload) == {
        "ok",
        "error_code",
        "message",
        "data",
        "verification",
        "state_version",
        "frame_id",
        "elapsed_ms",
    }
