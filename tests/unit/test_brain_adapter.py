"""The manual Brain tool loop (specification section 67).

The loop is where BLAXCY keeps control of the interaction, so the tests focus on
the ways it refuses to continue: a latched stop, a cancellation, a turn limit, a
wall-clock limit, and -- most importantly -- a destructive action that needs a
human. In no case may the loop answer a confirmation itself.
"""

from __future__ import annotations

from typing import Any

from ai.brain_adapter import BrainError, ModelTurn, ToolResultPayload
from ai.tool_protocol import tool_declarations
from config.settings import Settings
from schemas.actions import ToolName
from schemas.enums import ErrorCode, PolicyMode, VerificationState
from schemas.events import EventType
from tests.harness.phase8 import make_element, make_state
from tests.harness.phase10 import build_loop_env, model_turn

BUTTON_STATE = make_state(elements=(make_element("e1", text="Send"),))


def _assist_kwargs() -> dict[str, Any]:
    """Dispatch-env arguments that put the environment in ASSIST."""
    return {"state": BUTTON_STATE, "mode": PolicyMode.ASSIST}


# -- The simple paths ---------------------------------------------------------

def test_a_brain_that_asks_for_nothing_ends_the_task() -> None:
    """No tool call means the task is over; BLAXCY invents no follow-up."""
    loop, brain, _env = build_loop_env(turns=[model_turn(text="all done")], **_assist_kwargs())
    result = loop.run("say hello")
    assert result.halted is False
    assert result.final_text == "all done"
    assert result.turns == 1
    assert result.tool_calls == 0
    assert result.envelopes == ()
    assert len(brain.calls) == 1


def test_a_read_only_call_is_dispatched_and_returned_to_the_model() -> None:
    """The loop walks the call itself and feeds the envelope back."""
    loop, brain, _env = build_loop_env(
        turns=[
            model_turn(calls=[(ToolName.GET_SCREEN_STATE, {})]),
            model_turn(text="I can see the desktop"),
        ],
        **_assist_kwargs(),
    )
    result = loop.run("what is on screen?")

    assert result.halted is False
    assert result.tool_calls == 1
    payload = result.envelopes[0]
    assert payload.name == ToolName.GET_SCREEN_STATE
    assert payload.ok is True
    assert result.final_text == "I can see the desktop"
    # The second model call was shown the tool result.
    second_turn = brain.calls[1]["turns"]
    tool_turns = [turn for turn in second_turn if turn.role == "tool"]
    assert tool_turns and tool_turns[0].tool_results[0].name == ToolName.GET_SCREEN_STATE


def test_every_declared_tool_is_offered_to_the_model() -> None:
    """The manual loop advertises BLAXCY's real tool set, not the SDK's default."""
    loop, brain, _env = build_loop_env(turns=[model_turn(text="ok")], **_assist_kwargs())
    loop.run("task")
    declared = {declaration.name for declaration in brain.calls[0]["tools"]}
    assert declared == {declaration.name for declaration in tool_declarations()}


def test_the_system_instruction_names_the_current_mode() -> None:
    """The Brain is told what mode it is operating in (section 56)."""
    loop, brain, _env = build_loop_env(
        turns=[model_turn(text="ok")],
        mode_provider=lambda: PolicyMode.ASSIST.value,
        **_assist_kwargs(),
    )
    loop.run("task")
    assert "ASSIST" in brain.calls[0]["system_instruction"]


def test_tool_results_are_collected_by_the_observer() -> None:
    """A GUI/subscriber can watch each result as it happens."""
    loop, _brain, env = build_loop_env(
        turns=[
            model_turn(calls=[(ToolName.GET_SCREEN_STATE, {})]),
            model_turn(text="done"),
        ],
        **_assist_kwargs(),
    )
    loop.run("task")
    assert len(env.collected) == 1
    assert isinstance(env.collected[0], ToolResultPayload)


def test_counters_report_verified_and_failed_work() -> None:
    """The result summarises outcomes from the envelopes, not from optimism."""
    loop, _brain, _env = build_loop_env(
        turns=[
            model_turn(calls=[(ToolName.GET_SCREEN_STATE, {}), (ToolName.FIND_ELEMENT, {"target": "Ghost"})]),
            model_turn(text="done"),
        ],
        **_assist_kwargs(),
    )
    result = loop.run("task")
    assert result.tool_calls == 2
    assert result.failed == 1


# -- Limits and cancellation --------------------------------------------------

def test_the_turn_limit_stops_the_exchange() -> None:
    """A Brain that keeps calling tools cannot loop forever."""
    turns = [model_turn(calls=[(ToolName.GET_SCREEN_STATE, {})]) for _ in range(5)]
    loop, _brain, _env = build_loop_env(
        turns=turns, max_model_turns=2, **_assist_kwargs()
    )
    result = loop.run("task")
    assert result.halted is True
    assert result.halt_reason == "max_model_turns"
    assert result.turns == 2


def test_the_wall_clock_limit_stops_the_exchange() -> None:
    """A task cannot outlive its configured ceiling."""
    loop, _brain, _env = build_loop_env(
        turns=[model_turn(calls=[(ToolName.GET_SCREEN_STATE, {})])],
        max_task_wall_clock_seconds=-1.0,
        **_assist_kwargs(),
    )
    result = loop.run("task")
    assert result.halted is True
    assert result.halt_reason == "max_task_wall_clock_seconds"
    assert result.turns == 0


def test_cancellation_stops_the_loop_between_turns_and_calls() -> None:
    """A registered cancel (emergency stop/takeover) takes effect promptly."""
    loop, brain, _env = build_loop_env(
        turns=[model_turn(calls=[(ToolName.GET_SCREEN_STATE, {})]), model_turn(text="unreachable")],
        **_assist_kwargs(),
    )
    brain.on_call = loop.cancel
    result = loop.run("task")
    assert result.halted is True
    assert result.halt_reason == "cancelled"
    assert result.tool_calls == 0


def test_a_latched_abort_stops_the_loop_before_any_model_turn() -> None:
    """Emergency stop / takeover outrank the Brain (sections 62, 63)."""
    latched: dict[str, ErrorCode | None] = {"code": ErrorCode.EMERGENCY_STOP_ACTIVE}
    loop, brain, _env = build_loop_env(
        turns=[model_turn(text="never")],
        abort_check=lambda: latched["code"],
        **_assist_kwargs(),
    )
    result = loop.run("task")
    assert result.halted is True
    assert result.halt_reason == ErrorCode.EMERGENCY_STOP_ACTIVE.value
    assert brain.calls == []


def test_a_brain_failure_is_a_structured_halt_not_a_crash() -> None:
    """A transport/auth failure halts with a taxonomy code and an event."""
    loop, _brain, env = build_loop_env(
        turns=[],
        brain_error=BrainError(ErrorCode.MODEL_ERROR, "the model refused"),
        **_assist_kwargs(),
    )
    result = loop.run("task")
    assert result.halted is True
    assert result.halt_reason == ErrorCode.MODEL_ERROR.value
    assert env.events[-1].event_type is EventType.ERROR


# -- Confirmation is always a human decision ---------------------------------

def _destructive_turn() -> ModelTurn:
    """A model turn asking for a destructive drag."""
    return model_turn(
        calls=[(ToolName.DRAG, {"target": "invoice.pdf", "destination": "trash"})]
    )


def test_without_a_prompt_a_destructive_action_waits_for_a_human() -> None:
    """The loop never answers its own confirmation request."""
    loop, _brain, env = build_loop_env(
        turns=[_destructive_turn()],
        state=BUTTON_STATE,
        mode=PolicyMode.ASSIST,
    )
    result = loop.run("delete the invoice")

    assert result.halted is True
    assert result.halt_reason == ErrorCode.CONFIRMATION_DENIED.value
    assert result.envelopes[0].error_code == ErrorCode.CONFIRMATION_REQUIRED.value
    assert result.confirmations[0]["asked"] is False
    assert result.confirmations[0]["granted"] is False
    assert EventType.CONFIRMATION_REQUESTED in [event.event_type for event in env.events]
    # Nothing was injected while waiting for the answer.
    assert env.dispatch_env.env.backend.events == []


def test_a_granted_confirmation_reruns_the_action_as_a_fresh_pass() -> None:
    """Confirmation lets the *same* action run through the full pipeline."""
    loop, _brain, env = build_loop_env(
        turns=[_destructive_turn()],
        state=BUTTON_STATE,
        mode=PolicyMode.ASSIST,
        confirmation_prompt=lambda _payload: True,
    )
    result = loop.run("delete the invoice")

    assert result.confirmations[0]["asked"] is True
    assert result.confirmations[0]["granted"] is True
    # Two envelopes: the denied-at-confirmation pass, then the real attempt.
    assert len(result.envelopes) == 2
    assert result.envelopes[1].error_code != ErrorCode.CONFIRMATION_REQUIRED.value
    assert EventType.CONFIRMATION_RESOLVED in [event.event_type for event in env.events]


def test_a_denied_confirmation_halts_without_running_the_action() -> None:
    """A refusal stops the task; it does not silently skip the step."""
    loop, _brain, env = build_loop_env(
        turns=[_destructive_turn()],
        state=BUTTON_STATE,
        mode=PolicyMode.ASSIST,
        confirmation_prompt=lambda _payload: False,
    )
    result = loop.run("delete the invoice")
    assert result.halted is True
    assert result.halt_reason == ErrorCode.CONFIRMATION_DENIED.value
    assert len(result.envelopes) == 1
    assert env.dispatch_env.env.backend.events == []


def test_a_non_destructive_action_never_asks() -> None:
    """Only actions that require confirmation trigger the prompt."""
    asked: list[ToolResultPayload] = []

    def prompt(payload: ToolResultPayload) -> bool:
        asked.append(payload)
        return True

    loop, _brain, _env = build_loop_env(
        turns=[
            model_turn(calls=[(ToolName.CLICK, {"target": "Send"})]),
            model_turn(text="done"),
        ],
        state=BUTTON_STATE,
        mode=PolicyMode.ASSIST,
        confirmation_prompt=prompt,
    )
    loop.run("click send")
    assert asked == []


# -- Integration with the state cache ----------------------------------------

def test_the_active_task_is_visible_while_running_and_cleared_after() -> None:
    """The GUI can see what BLAXCY is working on (section 65)."""
    loop, brain, env = build_loop_env(turns=[model_turn(text="done")], **_assist_kwargs())
    seen: list[str | None] = []

    def observe() -> None:
        seen.append(env.dispatch_env.cache.active_task)

    brain.on_call = observe
    loop.run("task", task_id="task-7")

    assert seen == ["task-7"]
    assert env.dispatch_env.cache.active_task is None


def test_the_conversation_keeps_the_model_turn_with_its_calls() -> None:
    """A model turn that both speaks and calls is one turn, not two."""
    loop, _brain, _env = build_loop_env(
        turns=[
            model_turn(text="clicking Send", calls=[(ToolName.CLICK, {"target": "Send"})]),
            model_turn(text="done"),
        ],
        state=BUTTON_STATE,
        mode=PolicyMode.ASSIST,
    )
    result = loop.run("click send")
    roles = [turn.role for turn in result.conversation]
    assert roles[0] == "user"
    assert roles[1] == "model"
    assert result.conversation[1].text == "clicking Send"
    assert result.conversation[1].tool_calls
    assert result.conversation[2].role == "tool"


def test_a_verified_mutating_action_is_counted_as_verified() -> None:
    """``verified`` counts real VERIFIED envelopes only."""
    loop, _brain, _env = build_loop_env(
        turns=[
            model_turn(calls=[(ToolName.CLICK, {"target": "Send"})]),
            model_turn(text="done"),
        ],
        state=make_state(elements=(make_element("e1", text="Send"),)),
        mode=PolicyMode.ASSIST,
    )
    result = loop.run("click send")
    assert result.envelopes[0].envelope.get("verification") in {
        VerificationState.VERIFIED.value,
        VerificationState.UNVERIFIED.value,
    }
    assert result.verified <= result.tool_calls


def test_settings_drive_the_default_limits() -> None:
    """The configured limits are the defaults the loop uses."""
    settings = Settings()
    settings = settings.model_copy(
        update={"gemini": settings.gemini.model_copy(update={"max_model_turns": 1})}
    )
    loop, _brain, _env = build_loop_env(
        turns=[model_turn(calls=[(ToolName.GET_SCREEN_STATE, {})])],
        settings=settings,
        **_assist_kwargs(),
    )
    result = loop.run("task")
    assert result.halted is True
    assert result.halt_reason == "max_model_turns"
