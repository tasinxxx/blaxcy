"""Bounded recovery (specification sections 4 rule 21, 21, 61).

Recovery is the subsystem most able to turn a safety property into a bug, so
these tests are mostly about what recovery *refuses* to do.
"""

from __future__ import annotations

import pytest

from config.settings import RecoverySettings
from control.recovery import (
    FailureClass,
    RecoveryController,
    RecoveryDecision,
    arguments_digest,
    classify_failure,
)
from schemas.enums import ActionClass, ErrorCode


def _recovery(**overrides: object) -> RecoveryController:
    """A recovery controller over default settings with optional overrides."""
    return RecoveryController(RecoverySettings.model_validate(overrides))


def _attempt(
    controller: RecoveryController,
    tool: str = "click",
    code: ErrorCode = ErrorCode.TARGET_STALE,
) -> RecoveryDecision:
    """Ask for a retry for a plain mutating click."""
    return controller.should_attempt(
        code=code,
        action_class=ActionClass.MUTATING,
        tool=tool,
        arguments={"target": {"text": "Send"}},
    )


# -- Classification -----------------------------------------------------------

@pytest.mark.parametrize(
    "code",
    [
        ErrorCode.TARGET_NOT_FOUND,
        ErrorCode.TARGET_STALE,
        ErrorCode.TARGET_OCCLUDED,
        ErrorCode.TARGET_OFFSCREEN,
    ],
)
def test_target_failures_are_classified_target(code: ErrorCode) -> None:
    """Only "the world moved" failures are candidates for an automatic retry."""
    assert classify_failure(code) is FailureClass.TARGET


@pytest.mark.parametrize(
    "code",
    [
        ErrorCode.PERMISSION_DENIED,
        ErrorCode.BLOCKED_APPLICATION,
        ErrorCode.CONFIRMATION_REQUIRED,
        ErrorCode.CONFIRMATION_DENIED,
        ErrorCode.EMERGENCY_STOP_ACTIVE,
        ErrorCode.HUMAN_TAKEOVER,
        ErrorCode.TARGET_AMBIGUOUS,
    ],
)
def test_decisions_and_latches_are_classified_safety(code: ErrorCode) -> None:
    """A refusal is a decision; re-asking cannot change it."""
    assert classify_failure(code) is FailureClass.SAFETY


@pytest.mark.parametrize(
    "code",
    [ErrorCode.VERIFICATION_UNVERIFIED, ErrorCode.VERIFICATION_CONTRADICTED],
)
def test_verification_outcomes_are_their_own_class(code: ErrorCode) -> None:
    """The action already ran, so these are never re-run automatically."""
    assert classify_failure(code) is FailureClass.VERIFICATION


@pytest.mark.parametrize(
    "code",
    [ErrorCode.BACKEND_UNAVAILABLE, ErrorCode.CAPTURE_FAILED, ErrorCode.CALIBRATION_FAILED],
)
def test_infrastructure_failures_are_not_retryable(code: ErrorCode) -> None:
    """A backend that is gone does not come back because we asked twice."""
    assert classify_failure(code) is FailureClass.BACKEND


def test_an_unclassifiable_failure_is_not_retried() -> None:
    """A failure we cannot classify is not one we are entitled to retry."""
    assert classify_failure(None) is FailureClass.UNKNOWN
    controller = _recovery()
    decision = controller.should_attempt(
        code=None, action_class=ActionClass.MUTATING, tool="click"
    )
    assert decision.should_attempt is False


# -- What recovery must refuse -------------------------------------------------

def test_a_safety_refusal_is_never_retried() -> None:
    """Permission and latch codes are decisions (section 61)."""
    controller = _recovery()
    decision = _attempt(controller, code=ErrorCode.PERMISSION_DENIED)
    assert decision.should_attempt is False
    assert "safety" in decision.reason


def test_an_ambiguous_target_is_never_retried() -> None:
    """Ambiguity needs the Brain to disambiguate, not a second guess (§43)."""
    controller = _recovery()
    decision = _attempt(controller, code=ErrorCode.TARGET_AMBIGUOUS)
    assert decision.should_attempt is False


def test_a_destructive_action_is_never_automatically_retried() -> None:
    """Section 4 rule 21: no blind retries, even for a pre-input failure."""
    controller = _recovery()
    decision = controller.should_attempt(
        code=ErrorCode.TARGET_STALE,
        action_class=ActionClass.DESTRUCTIVE,
        tool="drag",
        arguments={"destination": {"text": "Trash"}},
    )
    assert decision.should_attempt is False
    assert "destructive" in decision.reason


def test_an_action_that_already_injected_input_is_never_retried() -> None:
    """A verification failure means the action happened; re-running doubles it."""
    controller = _recovery()
    decision = controller.should_attempt(
        code=ErrorCode.VERIFICATION_UNVERIFIED,
        action_class=ActionClass.MUTATING,
        tool="click",
        input_performed=True,
    )
    assert decision.should_attempt is False


def test_a_material_identity_change_stops_the_retry() -> None:
    """Retrying against a different control is not recovering (§61)."""
    controller = _recovery()
    controller.note_identity("atspi:/p/old")
    decision = controller.should_attempt(
        code=ErrorCode.TARGET_STALE,
        action_class=ActionClass.MUTATING,
        tool="click",
        identity="atspi:/p/other",
    )
    assert decision.should_attempt is False
    assert "identity" in decision.reason


def test_the_same_identity_does_not_stop_the_retry() -> None:
    """An unchanged identity is exactly when a retry is meaningful."""
    controller = _recovery()
    controller.note_identity("atspi:/p/field")
    decision = controller.should_attempt(
        code=ErrorCode.TARGET_STALE,
        action_class=ActionClass.MUTATING,
        tool="click",
        identity="atspi:/p/field",
    )
    assert decision.should_attempt is True


# -- Budgets ------------------------------------------------------------------

def test_a_stale_target_gets_a_recovery_action_list() -> None:
    """A recoverable failure names the recovery it justifies (section 21)."""
    controller = _recovery()
    decision = _attempt(controller)
    assert decision.should_attempt is True
    assert decision.actions == ("re_perceive", "re_resolve", "revalidate")


def test_the_per_tool_budget_is_finite() -> None:
    """Two automatic attempts per tool, then no more."""
    controller = _recovery()
    assert _attempt(controller).should_attempt is True
    controller.record_attempt(tool="click", arguments={"target": "a"})
    assert _attempt(controller).should_attempt is True
    controller.record_attempt(tool="click", arguments={"target": "b"})
    decision = _attempt(controller)
    assert decision.should_attempt is False
    assert "no automatic attempts remain" in decision.reason


def test_the_loop_guard_stops_repeating_the_same_call() -> None:
    """Same tool, same arguments, three times is a loop, not a retry."""
    controller = _recovery()
    arguments = {"target": {"text": "Send"}}
    for _ in range(3):
        controller.record_attempt(tool="click", arguments=arguments)
    decision = controller.should_attempt(
        code=ErrorCode.TARGET_STALE,
        action_class=ActionClass.MUTATING,
        tool="click",
        arguments=arguments,
    )
    assert decision.should_attempt is False
    assert "loop guard" in decision.reason


def test_different_arguments_are_not_mistaken_for_a_loop() -> None:
    """The loop guard is per call, not per tool name."""
    controller = _recovery(automatic_attempts_per_tool=10)
    for index in range(3):
        controller.record_attempt(tool="click", arguments={"target": f"item-{index}"})
    decision = controller.should_attempt(
        code=ErrorCode.TARGET_STALE,
        action_class=ActionClass.MUTATING,
        tool="click",
        arguments={"target": "a new target"},
    )
    assert decision.should_attempt is True


def test_step_budgets_are_not_shared_across_a_sequence() -> None:
    """Step 4 cannot spend step 1's budget (section 61)."""
    controller = _recovery(attempts_per_task_step=2, automatic_attempts_per_tool=10)
    controller.begin_task("task")
    controller.begin_step("s1")
    for index in range(2):
        controller.record_attempt(tool="click", arguments={"target": f"step1-{index}"})

    # Step 1's own budget is exhausted while it is the current step ...
    assert _attempt(controller).should_attempt is False

    # ... but the next step of the same sequence gets a fresh one.
    controller.begin_step("s4")
    assert _attempt(controller).should_attempt is True


def test_the_step_budget_is_enforced_within_one_step() -> None:
    """A single step has a hard ceiling too."""
    controller = _recovery(attempts_per_task_step=2, automatic_attempts_per_tool=10)
    controller.begin_step("s1")
    controller.record_attempt(tool="click", arguments={"target": "a"})
    controller.record_attempt(tool="click", arguments={"target": "b"})
    decision = _attempt(controller)
    assert decision.should_attempt is False
    assert "task step" in decision.reason


def test_begin_task_resets_the_budgets() -> None:
    """A new task starts clean."""
    controller = _recovery()
    controller.record_attempt(tool="click", arguments={"target": "a"})
    controller.record_attempt(tool="click", arguments={"target": "b"})
    assert _attempt(controller).should_attempt is False
    controller.begin_task("fresh")
    assert _attempt(controller).should_attempt is True


def test_every_refusal_is_counted() -> None:
    """Refusals are visible in the stats rather than silently dropped."""
    controller = _recovery()
    controller.should_attempt(code=ErrorCode.PERMISSION_DENIED, action_class=ActionClass.MUTATING, tool="click")
    stats = controller.stats()
    assert stats["refusals"] == 1
    assert stats["automatic_attempts_per_tool"] == 2
    assert stats["attempts_per_task_step"] == 6
    assert stats["identical_call_loop_guard"] == 3


def test_argument_digest_ignores_ordering() -> None:
    """The digest is about the call, not about dict insertion order."""
    assert arguments_digest("click", {"a": 1, "b": 2}) == arguments_digest("click", {"b": 2, "a": 1})
    assert arguments_digest("click", {"a": 1}) != arguments_digest("press_key", {"a": 1})


def test_an_unhashable_argument_does_not_break_the_loop_guard() -> None:
    """A weird argument value must not raise inside a safety mechanism."""
    digest = arguments_digest("click", {"target": object()})
    assert digest.startswith("click|")
