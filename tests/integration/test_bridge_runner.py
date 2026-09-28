"""Integration tests for the local bridge over the Phase 8/10 fakes (Phase 2A).

These tests run a real :class:`~ai.tool_protocol.ToolDispatcher` wired to a real
:class:`~control.sequence_runner.SequenceRunner` over the project's fake input
backend and scripted perception — the same harness the batching-layer tests
use. Nothing here touches a real desktop, and every dispatch goes through the
**one** tool door, which is the property Phase 2A exists to demonstrate: the
bridge adds no execution path of its own, so every guarantee below (OBSERVE
refusal, ambiguous-target halt, halt conditions, emergency stop) is the core's
own behavior, reached through the bridge.
"""

from __future__ import annotations

import json
import time

import pytest

from bridge.auth import BridgeAuthenticator, canonical_task_document
from bridge.runner import DEFAULT_TASK_TIMEOUT_SECONDS, LocalBridge
from bridge.task_protocol import (
    TASK_SCHEMA_VERSION,
    TaskEnvelope,
    TaskStatus,
)
from config.settings import Settings
from schemas.enums import ErrorCode, PolicyMode
from schemas.sequences import SequenceStep
from tests.harness.phase101 import (
    WORKFLOW_PLAN,
    build_sequence_env,
    duplicate_controls,
    script_workflow_with_duplicates,
    sequence_settings,
    workflow_plan_with_duplicate_label,
    workflow_state,
)

#: A token valid for every test here (>= MIN_TOKEN_LENGTH).
TEST_TOKEN = "integration-test-token-0123456789abcdef"


def _signed_document(
    payload: dict[str, object], auth: BridgeAuthenticator, nonce: str = "n1"
) -> tuple[str, str, float]:
    """The canonical document plus its signature, as a producer would send them.

    The timestamp is the real current time, because the bridge verifies against
    its own clock and refuses a signature whose timestamp is outside the skew
    window — signing with a stale fixed timestamp is exactly the replay attempt
    the boundary exists to refuse.
    """
    document = canonical_task_document(payload)
    now = time.time()
    return document, auth.sign_request(document, nonce=nonce, timestamp=now), now


def _task_payload(
    plan: list[dict[str, object]] | None = None,
    task_id: str = "task-1",
    *,
    operator_approved: bool = False,
) -> dict[str, object]:
    return {
        "schema_version": TASK_SCHEMA_VERSION,
        "task_id": task_id,
        "plan": plan if plan is not None else [dict(step) for step in WORKFLOW_PLAN],
        "operator_approved": operator_approved,
    }


def _confirm(message: str, details: dict[str, object]) -> bool:
    """A human operator who approves terminal submissions (the happy path)."""
    return True


class _BridgeEnv:
    """The bridge over a fully wired fake desktop, plus the signature helper.

    ``with_capabilities=False`` mirrors what the batching-layer tests do: the
    fakes satisfy every *functional* requirement, and leaving the default probe
    report in place would gate mouse input on a capability the fake backend has
    no real probe for (the core's gate is correct; the harness just doesn't
    model that probe). A confirmation callback is wired because the §74
    workflow's step s3 presses Return — a terminal-submission-shaped key that
    section 54 correctly routes through a human decision; without an operator
    the sequence would (correctly) halt on ``CONFIRMATION_REQUIRED``.
    """

    def __init__(self, **kwargs: object) -> None:
        kwargs.setdefault("with_capabilities", False)
        kwargs.setdefault("confirmation", _confirm)
        self.env = build_sequence_env(**kwargs)  # type: ignore[arg-type]
        self.auth = BridgeAuthenticator(token=TEST_TOKEN)
        self.bridge = LocalBridge(
            self.env.dispatch_env.dispatcher,
            authenticator=self.auth,
            settings=self.env.settings,
            timeout_seconds=5.0,
        )

    def submit(
        self, payload: dict[str, object], *, nonce: str = "n1", timestamp: float | None = None
    ) -> TaskEnvelope:
        moment = time.time() if timestamp is None else timestamp
        document = canonical_task_document(payload)
        signature = self.auth.sign_request(document, nonce=nonce, timestamp=moment)
        return self.bridge.submit(document, signature=signature, nonce=nonce, timestamp=moment)


# -- Successful dispatch (requirement 4/15) ---------------------------------------

def test_explicit_task_authorization_reaches_dispatcher() -> None:
    class _CapturingDispatcher:
        sequence_runner = object()

        def __init__(self) -> None:
            self.confirmed: list[bool] = []

        def dispatch(self, call: object, **kwargs: object) -> object:
            self.confirmed.append(bool(kwargs["confirmed"]))
            from schemas.actions import ToolEnvelope

            return ToolEnvelope(ok=True, data={"halted": False, "steps": [], "sequence_id": "s1"})

    auth = BridgeAuthenticator(token=TEST_TOKEN)
    dispatcher = _CapturingDispatcher()
    bridge = LocalBridge(dispatcher, authenticator=auth, settings=Settings(), timeout_seconds=1.0)  # type: ignore[arg-type]
    response = bridge.submit(
        *_signed_document(_task_payload(plan=[{"step_id": "s1", "tool": "get_capabilities"}], operator_approved=True), auth)
    )
    assert response.result.status is TaskStatus.COMPLETED
    assert dispatcher.confirmed == [True]




def test_a_valid_task_executes_through_the_real_dispatcher() -> None:
    env = _BridgeEnv(script_workflow=True)
    response = env.submit(_task_payload())

    assert response.result.status is TaskStatus.COMPLETED
    assert response.result.halted is False
    assert response.envelope is not None and response.envelope["ok"] is True
    assert response.result.completed_count == 5
    assert len(response.result.steps) == 5
    assert all(step.error_code is None for step in response.result.steps)
    # The input really flowed through the fakes' backend — the bridge did not
    # simulate anything itself. Five steps, each a move+click or key sequence.
    assert len(env.env.input_events()) >= 5
    assert response.result.sequence_id is not None


def test_a_completed_task_serializes_fully() -> None:
    env = _BridgeEnv(script_workflow=True)
    payload = env.submit(_task_payload()).to_payload()
    json.dumps(payload)  # must not raise
    assert payload["result"]["status"] == "COMPLETED"
    assert payload["envelope"]["ok"] is True
    assert len(payload["result"]["steps"]) == 5


def test_the_bridge_status_reflects_the_outcome() -> None:
    env = _BridgeEnv(script_workflow=True)
    env.submit(_task_payload())
    status = env.bridge.status
    assert status["tasks_submitted"] == 1
    assert status["tasks_completed"] == 1
    assert status["authenticated"] is True


# -- Rejections (requirement 8/9/15) ------------------------------------------------


def test_a_bad_signature_is_rejected_and_nothing_dispatches() -> None:
    env = _BridgeEnv(script_workflow=True)
    document = canonical_task_document(_task_payload())
    response = env.bridge.submit(
        document, signature="0" * 64, nonce="n1", timestamp=time.time()
    )
    assert response.result.status is TaskStatus.REJECTED
    assert response.result.rejection_reason == "SIGNATURE_MISMATCH"
    assert response.envelope is None
    assert env.env.input_events() == []  # nothing reached the desktop path


def test_an_unsafe_task_is_rejected_before_any_dispatch() -> None:
    env = _BridgeEnv(script_workflow=True)
    payload = _task_payload(plan=[{"step_id": "s1", "tool": "click", "target": "Send", "x": 100}])
    response = env.submit(payload)
    assert response.result.status is TaskStatus.REJECTED
    assert response.result.rejection_reason == "PRE_RESOLVED_TASK_FIELD"
    assert env.env.input_events() == []


def test_a_secret_carrying_task_is_rejected() -> None:
    env = _BridgeEnv(script_workflow=True)
    payload = _task_payload(plan=[{"step_id": "s1", "tool": "type_text", "target": "f", "text": "x", "password": "hunter2"}])
    response = env.submit(payload)
    assert response.result.status is TaskStatus.REJECTED
    assert response.result.rejection_reason == "FORBIDDEN_TASK_FIELD"


def test_a_malformed_json_document_is_rejected() -> None:
    env = _BridgeEnv(script_workflow=True)
    document = "{not json"
    now = time.time()
    signature = env.auth.sign_request(document, nonce="n1", timestamp=now)
    response = env.bridge.submit(document, signature=signature, nonce="n1", timestamp=now)
    assert response.result.status is TaskStatus.REJECTED
    assert response.result.rejection_reason == "TASK_NOT_JSON"


def test_an_unknown_schema_version_is_rejected() -> None:
    env = _BridgeEnv(script_workflow=True)
    payload = _task_payload()
    payload["schema_version"] = TASK_SCHEMA_VERSION + 99
    response = env.submit(payload)
    assert response.result.status is TaskStatus.REJECTED


# -- Policy and halt inheritance (requirement 5/13) ----------------------------------


def test_observe_refuses_the_task_without_executing_any_step() -> None:
    env = _BridgeEnv(script_workflow=True, mode=PolicyMode.OBSERVE)
    response = env.submit(_task_payload())

    assert response.result.status is TaskStatus.HALTED
    assert response.result.halt_code == ErrorCode.PERMISSION_DENIED.value
    assert response.envelope is not None and response.envelope["ok"] is False
    assert len(response.result.steps) == 5
    assert all(step.error_code == "NOT_EXECUTED" for step in response.result.steps)
    assert env.env.input_events() == []


def test_an_ambiguous_target_halts_the_task_with_an_honest_tail() -> None:
    env = _BridgeEnv(
        script_workflow=False,
        state=workflow_state(1, extra=duplicate_controls()),
    )
    # The initial state itself carries two same-named controls, so step 1 is
    # ambiguous and the plan must halt before any input.
    ambiguous_plan: list[dict[str, object]] = [
        {"step_id": "s1", "tool": "click", "target": "Duplicate action", "role": "BUTTON"},
    ]
    response = env.submit(_task_payload(plan=ambiguous_plan))
    assert response.result.status is TaskStatus.HALTED
    assert response.result.halt_code == ErrorCode.TARGET_AMBIGUOUS.value
    assert response.result.steps[0].error_code == "TARGET_AMBIGUOUS"
    assert env.env.input_events() == []


def test_a_mid_sequence_halt_marks_the_remaining_steps_not_executed() -> None:
    env = _BridgeEnv(script_workflow=False)
    # The harness's duplicates choreography: two same-named controls appear from
    # frame 3 on, so steps s1/s2 complete and s3 is ambiguous mid-plan. The
    # harness's plan builder has duplicate s3 ids (it predates the bridge's
    # stricter parse), so the ids are corrected here.
    script_workflow_with_duplicates(env.env)
    plan = workflow_plan_with_duplicate_label()
    plan[3]["step_id"] = "s4"
    plan[4]["step_id"] = "s5"
    response = env.submit(_task_payload(plan=[dict(step) for step in plan]))
    steps = response.result.steps
    assert response.result.status is TaskStatus.HALTED
    assert response.result.halt_code == ErrorCode.TARGET_AMBIGUOUS.value
    # Plan order: s1 click Search, s2 type, s3 press Return, s4 click the
    # duplicated label (ambiguous from frame 3), s5 click Play.
    assert steps[0].error_code is None
    assert steps[1].error_code is None
    assert steps[2].error_code is None
    assert steps[3].error_code == "TARGET_AMBIGUOUS"
    assert steps[4].error_code == "NOT_EXECUTED"


def test_a_latched_stop_halts_the_task_before_any_input() -> None:
    # The harness takes an abort hook (what the composition root wires to the
    # real EmergencyStop); a latched stop must halt the task with its own code
    # before any step runs — the §63-first property, reached through the bridge.
    env = _BridgeEnv(
        script_workflow=True,
        abort_check=lambda: ErrorCode.EMERGENCY_STOP_ACTIVE,
    )
    response = env.submit(_task_payload())
    assert response.result.status is TaskStatus.HALTED
    assert response.result.halt_code == ErrorCode.EMERGENCY_STOP_ACTIVE.value
    assert env.env.input_events() == []


def test_a_sequence_refusal_is_reported_not_swallowed() -> None:
    """A dispatcher-level refusal (no runner wired) is a structured rejection."""
    env = _BridgeEnv(script_workflow=True)
    env.env.dispatch_env.dispatcher.attach_sequence_runner(None)
    response = env.submit(_task_payload())
    assert response.result.status is TaskStatus.REJECTED
    assert response.result.rejection_reason == "SEQUENCE_RUNNER_UNAVAILABLE"


# -- Timeout handling (requirement 12/13) ---------------------------------------------


class _HangingDispatcher:
    """A dispatcher whose ``run_sequence`` never returns — the pathological case."""

    def __init__(self) -> None:
        self.sequence_runner = object()  # passes the precondition check
        self.stop_calls: list[str] = []

    def dispatch(self, call: object, **kwargs: object) -> object:
        import time

        time.sleep(10)  # far beyond the test's timeout
        raise AssertionError("the test should have timed out long before")


def test_a_hung_dispatch_times_out_and_reports_the_stop_honestly() -> None:

    auth = BridgeAuthenticator(token=TEST_TOKEN)

    class _Stop:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def trigger(self, reason: str) -> object:
            self.calls.append(reason)

            class _R:
                latched = True

            return _R()

    stop = _Stop()
    dispatcher = _HangingDispatcher()
    bridge = LocalBridge(
        dispatcher,  # type: ignore[arg-type]
        authenticator=auth,
        settings=Settings(),
        emergency_stop=stop,
        timeout_seconds=0.2,
    )
    document, signature, timestamp = _signed_document(_task_payload(), auth)
    response = bridge.submit(document, signature=signature, nonce="n1", timestamp=timestamp)

    assert response.result.status is TaskStatus.TIMED_OUT
    assert response.result.timeout_stop_latched is True
    assert response.result.timeout_elapsed_ms is not None
    assert response.result.timeout_elapsed_ms >= 0
    assert stop.calls == ["bridge task timeout"]
    assert response.envelope is None


def test_a_failing_stop_on_timeout_is_reported_as_not_latched() -> None:
    auth = BridgeAuthenticator(token=TEST_TOKEN)

    class _BrokenStop:
        def trigger(self, reason: str) -> object:
            raise RuntimeError("stop path broken")

    dispatcher = _HangingDispatcher()
    bridge = LocalBridge(
        dispatcher,  # type: ignore[arg-type]
        authenticator=auth,
        settings=Settings(),
        emergency_stop=_BrokenStop(),
        timeout_seconds=0.2,
    )
    document, signature, timestamp = _signed_document(_task_payload(), auth)
    response = bridge.submit(document, signature=signature, nonce="n1", timestamp=timestamp)

    assert response.result.status is TaskStatus.TIMED_OUT
    assert response.result.timeout_stop_latched is False


def test_the_default_timeout_is_bounded() -> None:
    assert 0 < DEFAULT_TASK_TIMEOUT_SECONDS <= 600


def test_a_non_positive_timeout_is_a_configuration_error() -> None:
    env = _BridgeEnv(script_workflow=True)
    with pytest.raises(ValueError):
        LocalBridge(
            env.env.dispatch_env.dispatcher,
            authenticator=env.auth,
            settings=Settings(),
            timeout_seconds=0,
        )


# -- Unwired stop on timeout (honest absence) ----------------------------------------


def test_a_timeout_without_a_wired_stop_reports_not_latched() -> None:
    auth = BridgeAuthenticator(token=TEST_TOKEN)
    bridge = LocalBridge(
        _HangingDispatcher(),  # type: ignore[arg-type]
        authenticator=auth,
        settings=Settings(),
        emergency_stop=None,
        timeout_seconds=0.2,
    )
    document, signature, timestamp = _signed_document(_task_payload(), auth)
    response = bridge.submit(document, signature=signature, nonce="n1", timestamp=timestamp)
    assert response.result.status is TaskStatus.TIMED_OUT
    assert response.result.timeout_stop_latched is False


# -- Determinism helpers -------------------------------------------------------------


def test_sequence_step_round_trip_is_stable() -> None:
    step = SequenceStep(step_id="s1", tool="click", target="Send")
    assert step.to_dict()["target"] == "Send"


def test_sequence_settings_helper_disables_the_layer_when_asked() -> None:
    settings = sequence_settings(enabled=False)
    assert settings.sequence.enabled is False
