"""``run_sequence`` at the dispatch boundary (specification sections 66, 66.1).

Phase 10 builds the *door*; Phase 10.1 builds the runner behind it. This file is
about the door, and keeps its own concern separate: that the dispatcher enforces
the section 66.1 schema rules, refuses a plan it cannot honestly execute, and
maps a runner's ordered result onto the uniform envelope without reshaping it.

The runner used here is a test double. It proves the dispatcher's contract
without depending on the real runner's internals; the real runner is covered by
``tests/unit/test_sequence_runner.py`` and the halting properties by
``tests/safety/test_sequence_halts.py``.
"""

from __future__ import annotations

from typing import Any

from ai.tool_protocol import ToolCall, ToolDispatcher
from config.settings import Settings
from schemas.actions import ToolEnvelope, ToolName
from schemas.enums import ErrorCode, VerificationState
from schemas.sequences import SequenceResult, SequenceStep, SequenceStepResult
from tests.harness.phase10 import build_dispatch_env


class FakeRunner:
    """A ``SequenceRunner`` double that records the request it was given."""

    def __init__(self, result: SequenceResult | None = None) -> None:
        """Create the runner, optionally with a scripted result."""
        self._result = result
        self.requests: list[Any] = []
        self.confirmed: list[bool] = []

    def run(self, request: Any, *, task_id: str | None = None, confirmed: bool = False) -> Any:
        """Record the request and return the scripted result."""
        self.requests.append(request)
        self.confirmed.append(confirmed)
        if self._result is None:
            raise AssertionError("the runner was called but no result was scripted")
        return self._result


def _result(*, halted: bool = False, reason: str | None = None) -> SequenceResult:
    """Build a two-step sequence result, halting after the first step if asked."""
    first = SequenceStep(step_id="s1", tool=ToolName.CLICK, target="Send")
    second = SequenceStep(step_id="s2", tool=ToolName.PRESS_KEY, args={"key": "Return"})
    completed = SequenceStepResult.from_envelope(
        ToolEnvelope.success(data={}, verification=VerificationState.VERIFIED),
        step=first,
        index=0,
    )
    tail = (
        SequenceStepResult.not_executed_step(step=second, index=1)
        if halted
        else SequenceStepResult.from_envelope(
            ToolEnvelope.success(data={}), step=second, index=1
        )
    )
    return SequenceResult(
        sequence_id="seq-1",
        steps=(completed, tail),
        halted=halted,
        halt_reason=reason,
        wall_clock_ms=12.5,
        started_at=0.0,
    )


def _enabled_settings(**overrides: Any) -> Settings:
    """Settings with sequence execution explicitly enabled, for delegation tests.

    A test double stands in for the Phase 10.1 runner here. The production default
    is now enabled too (the batching test list passed), so this helper exists to
    name the intent and to apply per-test limit overrides, not to turn the layer
    on.
    """
    settings = Settings()
    sequence = settings.sequence.model_copy(update={"enabled": True, **overrides})
    return settings.model_copy(update={"sequence": sequence})


def _disabled_settings() -> Settings:
    """Settings with sequence execution switched off, to test the refusal path."""
    settings = Settings()
    sequence = settings.sequence.model_copy(update={"enabled": False})
    return settings.model_copy(update={"sequence": sequence})


def _dispatcher(env: Any, *, runner: Any = None, settings: Settings | None = None) -> ToolDispatcher:
    """Build a dispatcher over the dispatch environment with an optional runner."""
    return ToolDispatcher(
        settings if settings is not None else env.settings,
        executor=env.env.executor,
        state_cache=env.cache,
        resolver=env.env.resolver,
        sequence_runner=runner,
        clock=env.clock,
        sleep=env.clock.sleep,
    )


PLAN: dict[str, Any] = {
    "steps": [
        {"step_id": "s1", "tool": "click", "target": "Send"},
        {"step_id": "s2", "tool": "press_key", "key": "Return"},
    ]
}


# -- Honest unavailability ----------------------------------------------------

def test_run_sequence_is_unavailable_when_no_runner_is_wired() -> None:
    """With no runner wired into this dispatcher, BLAXCY says so instead of pretending."""
    env = build_dispatch_env()
    dispatcher = _dispatcher(env)
    envelope = dispatcher.dispatch(ToolCall(name=ToolName.RUN_SEQUENCE, arguments=PLAN))
    assert envelope.ok is False
    assert envelope.error_code is ErrorCode.BACKEND_UNAVAILABLE
    assert "no sequence runner is wired" in (envelope.message or "")


def test_a_disabled_sequence_reports_disabled_not_missing() -> None:
    """A present-but-disabled runner is a different, honest reason."""
    env = build_dispatch_env()
    dispatcher = _dispatcher(env, runner=object(), settings=_disabled_settings())
    envelope = dispatcher.dispatch(ToolCall(name=ToolName.RUN_SEQUENCE, arguments=PLAN))
    assert envelope.ok is False
    assert "disabled by configuration" in (envelope.message or "")


def test_an_unavailable_sequence_executes_nothing() -> None:
    """The refusal must not have touched the desktop on the way out."""
    env = build_dispatch_env()
    dispatcher = _dispatcher(env)
    before = env.env.executor.stats()["actions"]
    dispatcher.dispatch(ToolCall(name=ToolName.RUN_SEQUENCE, arguments=PLAN))
    assert env.env.executor.stats()["actions"] == before
    assert env.env.backend.events == []


# -- Schema enforcement at the door ------------------------------------------

def test_the_step_limit_is_enforced() -> None:
    """``max_sequence_steps`` bounds the plan before anything else happens."""
    env = build_dispatch_env()
    dispatcher = _dispatcher(env, settings=_enabled_settings(max_sequence_steps=1))
    envelope = dispatcher.dispatch(ToolCall(name=ToolName.RUN_SEQUENCE, arguments=PLAN))
    assert envelope.ok is False
    assert envelope.error_code is ErrorCode.SEQUENCE_STEP_LIMIT_EXCEEDED
    assert envelope.data["steps"] == 2


def test_a_coordinate_target_is_refused_at_the_door() -> None:
    """A step target must be a description, never a pre-resolved coordinate."""
    env = build_dispatch_env()
    dispatcher = _dispatcher(env)
    envelope = dispatcher.dispatch(
        ToolCall(
            name=ToolName.RUN_SEQUENCE,
            arguments={"steps": [{"step_id": "s1", "tool": "click", "target": "120, 40"}]},
        )
    )
    assert envelope.ok is False
    assert envelope.error_code is ErrorCode.INTERNAL_ERROR


def test_a_step_carrying_a_lease_is_refused_at_the_door() -> None:
    """A lease issued before its step begins is stale by construction (section 44)."""
    env = build_dispatch_env()
    dispatcher = _dispatcher(env)
    envelope = dispatcher.dispatch(
        ToolCall(
            name=ToolName.RUN_SEQUENCE,
            arguments={
                "steps": [
                    {"step_id": "s1", "tool": "click", "target": "Send", "element_id": "e1"}
                ]
            },
        )
    )
    assert envelope.ok is False
    assert envelope.error_code is ErrorCode.INTERNAL_ERROR


def test_nested_and_unknown_step_tools_are_refused() -> None:
    """A sequence of sequences, or of a tool BLAXCY does not have, is rejected."""
    env = build_dispatch_env()
    dispatcher = _dispatcher(env)
    for tool in (ToolName.RUN_SEQUENCE, "summon_demon"):
        envelope = dispatcher.dispatch(
            ToolCall(
                name=ToolName.RUN_SEQUENCE,
                arguments={"steps": [{"step_id": "s1", "tool": tool, "target": "x"}]},
            )
        )
        assert envelope.ok is False
        assert envelope.error_code is ErrorCode.BACKEND_UNAVAILABLE


def test_a_malformed_plan_is_refused() -> None:
    """An empty plan, a non-object step and a widened halt set are all malformed."""
    env = build_dispatch_env()
    dispatcher = _dispatcher(env)
    plans: tuple[dict[str, Any], ...] = (
        {"steps": []},
        {"steps": ["click Send"]},
        {"steps": [{"step_id": "s1", "tool": "click", "target": "Send"}], "halt_on": ["NEVER"]},
    )
    for arguments in plans:
        envelope = dispatcher.dispatch(ToolCall(name=ToolName.RUN_SEQUENCE, arguments=arguments))
        assert envelope.ok is False
        assert envelope.error_code is ErrorCode.INTERNAL_ERROR


def test_halt_on_may_narrow_within_the_safety_set() -> None:
    """Narrowing is legal and reaches the runner; it is never widened."""
    env = build_dispatch_env()
    runner = FakeRunner(_result())
    dispatcher = _dispatcher(env, runner=runner, settings=_enabled_settings())
    arguments: dict[str, Any] = dict(PLAN, halt_on=["AMBIGUOUS"])
    envelope = dispatcher.dispatch(ToolCall(name=ToolName.RUN_SEQUENCE, arguments=arguments))
    assert envelope.ok is True
    request = runner.requests[0]
    assert {condition.value for condition in request.effective_halt_on} == {"AMBIGUOUS"}


# -- Runner delegation and envelope shape ------------------------------------

def test_a_completed_sequence_returns_the_per_step_envelope_array() -> None:
    """The result carries one uniform envelope per step, in order."""
    env = build_dispatch_env()
    runner = FakeRunner(_result())
    dispatcher = _dispatcher(env, runner=runner, settings=_enabled_settings())

    envelope = dispatcher.dispatch(ToolCall(name=ToolName.RUN_SEQUENCE, arguments=PLAN))

    assert envelope.ok is True
    steps = envelope.data["steps"]
    assert [step["step_id"] for step in steps] == ["s1", "s2"]
    assert [step["index"] for step in steps] == [0, 1]
    assert steps[0]["ok"] is True
    assert steps[0]["verification"] == VerificationState.VERIFIED.value
    assert envelope.data["halted"] is False


def test_a_halted_sequence_is_reported_as_a_failure_with_not_executed_tail() -> None:
    """A halt is a failed envelope carrying the honest partial result."""
    env = build_dispatch_env()
    runner = FakeRunner(_result(halted=True, reason="ambiguous target"))
    dispatcher = _dispatcher(env, runner=runner, settings=_enabled_settings())

    envelope = dispatcher.dispatch(ToolCall(name=ToolName.RUN_SEQUENCE, arguments=PLAN))

    assert envelope.ok is False
    assert envelope.error_code is ErrorCode.SEQUENCE_HALTED
    assert envelope.data["halt_reason"] == "ambiguous target"
    tail = envelope.data["steps"][1]
    assert tail["error_code"] == ErrorCode.NOT_EXECUTED.value
    assert tail["ok"] is False
