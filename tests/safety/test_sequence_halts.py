"""Batching safety properties (specification sections 66.1, 83, 85).

Everything in this file is a property that must hold *identically* whether a
triggering step arrived standalone or as part of a ``run_sequence``. That is the
whole point of the batching layer's existence: it removes Brain round-trips and
nothing else.

The three questions every test here is really asking:

1. Does a step that could not run safely stop the plan, rather than the plan
   carrying on over an unverified premise?
2. Does every step that did not run say so (``NOT_EXECUTED``), so a partial batch
   is never mistaken for a complete one?
3. Did any optimization -- a cache hint, a speculation, an up-front confirmation
   -- reach the desktop without the full pipeline behind it?
"""

from __future__ import annotations

from typing import Any

import pytest

from control.emergency_stop import EmergencyStop
from control.takeover import TakeoverController
from schemas.actions import ToolName
from schemas.enums import ErrorCode, UIRole
from tests.harness.phase8 import box_of
from tests.harness.phase101 import (
    WORKFLOW_PLAN,
    SequenceEnv,
    build_sequence_env,
    script_workflow_with_duplicates,
    sequence_settings,
    workflow_delta,
    workflow_plan_with_duplicate_label,
    workflow_state,
)

pytestmark = [pytest.mark.safety, pytest.mark.batching]


def _confirm(*_args: Any) -> bool:
    """A human who always says yes, so a test only exercises its own property."""
    return True


def _tolerant_plan() -> list[dict[str, Any]]:
    """The workflow, with the terminal submission explicitly confirmed-safe."""
    return [dict(step) for step in WORKFLOW_PLAN]


def _codes(envelope: Any) -> list[str | None]:
    """The per-step error codes, in order."""
    return [step["error_code"] for step in envelope.data["steps"]]


def _input_names(env: SequenceEnv) -> list[str]:
    """The ordered input event kinds the fake backend recorded."""
    return list(env.backend.event_names())


def _presses(env: SequenceEnv) -> int:
    """How many pointer-button presses were injected."""
    return _input_names(env).count("press")


# -- Ambiguity ----------------------------------------------------------------


def test_an_ambiguous_target_mid_sequence_halts_with_a_not_executed_tail() -> None:
    """Section 43's absolute ambiguity rule stops the batch and reports it."""
    env = build_sequence_env(confirmation=_confirm)
    script_workflow_with_duplicates(env)
    plan = workflow_plan_with_duplicate_label(step_index=2)

    envelope = env.run(plan)

    assert envelope.ok is False
    assert envelope.error_code is ErrorCode.TARGET_AMBIGUOUS
    assert envelope.data["halted"] is True
    assert envelope.data["halt_code"] == ErrorCode.TARGET_AMBIGUOUS.value
    steps = envelope.data["steps"]
    assert steps[0]["ok"] is True
    assert steps[1]["ok"] is True
    assert steps[2]["error_code"] == ErrorCode.TARGET_AMBIGUOUS.value
    assert [step["error_code"] for step in steps[3:]] == [ErrorCode.NOT_EXECUTED.value] * 2
    assert steps[2]["message"]
    # The duplicate was never clicked: only the one unambiguous click happened.
    assert _presses(env) == 1


def test_a_not_executed_step_says_why_it_was_not_executed() -> None:
    """A tail entry is evidence, not a filler row (section 66)."""
    env = build_sequence_env(confirmation=_confirm)
    script_workflow_with_duplicates(env)

    envelope = env.run(workflow_plan_with_duplicate_label(step_index=2))

    tail = envelope.data["steps"][3]
    assert tail["ok"] is False
    assert tail["error_code"] == ErrorCode.NOT_EXECUTED.value
    assert "halted" in tail["message"]
    assert tail["step_id"] == "s4"


# -- Emergency stop and human takeover ---------------------------------------


def _stop_on_nth_lease(env: SequenceEnv, *, nth: int) -> EmergencyStop:
    """Wire a real stop that latches when the *n*th step takes its lease.

    Latching between a step's lease and its injection is the sharpest form of
    "mid-step": the step has already resolved and leased, and the check that
    must still catch the stop is the one immediately before input.
    """
    from schemas.events import EventType

    stop = EmergencyStop(event_bus=env.dispatch_env.bus, mode_controller=env.dispatch_env.env.modes)
    env.dispatch_env.env.executor._abort_check = _combined(stop, None)
    seen: list[int] = []

    def on_event(event: Any) -> None:
        if event.event_type is EventType.LEASE_ISSUED:
            seen.append(1)
            if len(seen) == nth:
                stop.trigger("test: mid-sequence stop")

    env.dispatch_env.bus.subscribe(on_event)
    return stop


def _combined(stop: Any, takeover: Any) -> Any:
    """The executor's abort hook: stop outranks takeover (sections 20, 63)."""

    def check() -> ErrorCode | None:
        if stop is not None:
            code: ErrorCode | None = stop.abort_code()
            if code is not None:
                return code
        if takeover is not None:
            takeover_code: ErrorCode | None = takeover.abort_code()
            return takeover_code
        return None

    return check


def test_an_emergency_stop_mid_sequence_halts_and_injects_nothing_after_it() -> None:
    """Section 63: the stop wins mid-batch exactly as it does mid-action."""
    env = build_sequence_env(script_workflow=True, confirmation=_confirm)
    stop = _stop_on_nth_lease(env, nth=2)

    envelope = env.run(_tolerant_plan())

    assert stop.latched is True
    assert envelope.ok is False
    assert envelope.error_code is ErrorCode.EMERGENCY_STOP_ACTIVE
    assert envelope.data["halt_code"] == ErrorCode.EMERGENCY_STOP_ACTIVE.value
    steps = envelope.data["steps"]
    assert steps[0]["ok"] is True
    assert steps[1]["error_code"] == ErrorCode.EMERGENCY_STOP_ACTIVE.value
    assert [step["error_code"] for step in steps[2:]] == [ErrorCode.NOT_EXECUTED.value] * 3
    # Only the first step's click reached the desktop.
    assert _presses(env) == 1
    assert env.backend.held_buttons == ()
    assert env.backend.held_keys == ()


def test_a_human_takeover_mid_sequence_halts_and_is_never_replayed() -> None:
    """Section 62: takeover stops the batch; resuming does not resume the plan."""
    from schemas.events import EventType

    env = build_sequence_env(script_workflow=True, confirmation=_confirm)
    takeover = TakeoverController(
        event_bus=env.dispatch_env.bus, mode_controller=env.dispatch_env.env.modes
    )
    env.dispatch_env.env.executor._abort_check = _combined(None, takeover)
    seen: list[int] = []

    def on_event(event: Any) -> None:
        if event.event_type is EventType.LEASE_ISSUED:
            seen.append(1)
            if len(seen) == 2:
                takeover.begin("test: human took control")

    env.dispatch_env.bus.subscribe(on_event)

    envelope = env.run(_tolerant_plan())

    assert takeover.abort_code() is ErrorCode.HUMAN_TAKEOVER
    assert envelope.error_code is ErrorCode.HUMAN_TAKEOVER
    steps = envelope.data["steps"]
    assert steps[1]["error_code"] == ErrorCode.HUMAN_TAKEOVER.value
    assert [step["error_code"] for step in steps[2:]] == [ErrorCode.NOT_EXECUTED.value] * 3
    assert _presses(env) == 1

    # Resuming clears the latch; it does not replay the interrupted plan.
    before = _presses(env)
    takeover.resume()
    assert takeover.abort_code() is None
    assert _presses(env) == before
    assert env.cache.snapshot().sequence is not None  # the interrupted record remains


# -- Credential context -------------------------------------------------------


def test_a_credential_step_mid_sequence_is_never_batched() -> None:
    """Section 55: a password step halts the batch for per-step handling."""
    env = build_sequence_env(
        script_workflow=True,
        confirmation=_confirm,
        settings=sequence_settings(
            speculative_perception=True, resolver_cache=True, capture_boost=True
        ),
        speculative_perception=True,
        resolver_cache=True,
    )
    plan = [
        {"step_id": "s1", "tool": "click", "target": "Search", "role": "BUTTON"},
        {
            "step_id": "s2",
            "tool": "type_text",
            "target": "Password field",
            "role": "PASSWORD_INPUT",
            "text": "hunter2",
        },
        {"step_id": "s3", "tool": "click", "target": "Play", "role": "BUTTON"},
    ]

    envelope = env.run(plan)

    assert envelope.ok is False
    steps = envelope.data["steps"]
    assert steps[0]["ok"] is True
    # The credential step never ran and never will inside a batch.
    assert steps[1]["error_code"] == ErrorCode.NOT_EXECUTED.value
    assert steps[2]["error_code"] == ErrorCode.NOT_EXECUTED.value
    assert "credential" in steps[1]["message"].lower()
    # Nothing about it was cached, and nothing was speculated about it: the
    # only cache entry is the completed click, and the prefetcher refused to
    # schedule a hint for a description it must never resolve in advance.
    assert env.resolver_cache is not None
    assert env.resolver_cache.stats()["entries"] == 1
    assert env.speculative is not None
    assert env.speculative.stats()["requested"] == 0


def test_a_sensitive_result_halts_the_sequence() -> None:
    """A credential-context action's own result halts the batch (§42/§55)."""
    env = build_sequence_env()
    lander = workflow_state(4)
    search_box = lander.elements[1]
    password = search_box.model_copy(
        update={
            "role": UIRole.PASSWORD_INPUT,
            "password": True,
            "text": None,
            "accessible_name": "Password field",
        }
    )
    state = workflow_state(1, elements=(*lander.elements[:1], password, *lander.elements[2:]))
    env.cache.update_screen_state(state)
    plan = [
        {"step_id": "s1", "tool": "type_text", "target": "Password field", "text": "hunter2"},
        {"step_id": "s2", "tool": "click", "target": "Play", "role": "BUTTON"},
    ]

    envelope = env.run(plan)

    assert envelope.ok is False
    steps = envelope.data["steps"]
    assert steps[0]["ok"] is False
    # The reported evidence marks the credential context and never the value.
    assert "hunter2" not in str(steps[0])
    assert steps[1]["error_code"] == ErrorCode.NOT_EXECUTED.value


# -- Blocked applications -----------------------------------------------------


def test_a_plan_is_refused_while_a_blocked_application_is_active() -> None:
    """Section 58: a blocked app refuses the plan outright, in every mode."""
    settings = sequence_settings()
    settings = settings.model_copy(
        update={
            "safety": settings.safety.model_copy(update={"blocked_applications": ["fixture"]})
        }
    )
    env = build_sequence_env(settings=settings, script_workflow=True, confirmation=_confirm)

    envelope = env.run(_tolerant_plan())

    assert envelope.ok is False
    assert envelope.error_code is ErrorCode.BLOCKED_APPLICATION
    assert _codes(envelope) == [ErrorCode.NOT_EXECUTED.value] * 5
    assert _presses(env) == 0


def test_an_application_blocked_mid_sequence_halts_the_remainder() -> None:
    """Section 58: an in-flight sequence halts when a blocked app becomes active."""
    settings = sequence_settings()
    settings = settings.model_copy(
        update={
            "safety": settings.safety.model_copy(update={"blocked_applications": ["forbidden"]})
        }
    )
    env = build_sequence_env(settings=settings, confirmation=_confirm)
    previous = env.cache.current
    assert previous is not None
    first = env.cache.current.elements
    state_2 = workflow_state(2, active_app="forbidden")
    env.script(state_2, workflow_delta(previous, state_2, region=box_of(first[0])))
    plan = [
        {"step_id": "s1", "tool": "click", "target": "Search", "role": "BUTTON"},
        {"step_id": "s2", "tool": "click", "target": "Play", "role": "BUTTON"},
        {"step_id": "s3", "tool": "click", "target": "Go", "role": "BUTTON"},
    ]

    envelope = env.run(plan)

    assert envelope.ok is False
    assert envelope.error_code is ErrorCode.BLOCKED_APPLICATION
    assert envelope.data["steps"][0]["ok"] is True
    assert envelope.data["steps"][1]["error_code"] == ErrorCode.BLOCKED_APPLICATION.value
    assert envelope.data["steps"][2]["error_code"] == ErrorCode.NOT_EXECUTED.value
    assert _presses(env) == 1


# -- The halt set can only narrow within the safety set -----------------------


def test_halt_on_narrowing_tolerates_an_explicitly_named_condition() -> None:
    """Section 66.1: a narrowed halt set continues past the condition it omits.

    The same plan is run twice. With the default safety set, an unconfirmed
    terminal submission stops the sequence. With the halt set narrowed to a
    condition that does not match, the sequence continues past it -- the step's
    failure is still recorded per step, so a caller who narrows is told exactly
    what it tolerated rather than being handed a clean-looking batch.
    """
    plan = [
        {"step_id": "s1", "tool": "click", "target": "Search", "role": "BUTTON"},
        {"step_id": "s2", "tool": "press_key", "key": "Return"},
        {"step_id": "s3", "tool": "click", "target": "Play", "role": "BUTTON"},
    ]

    strict = _two_step_env()
    strict_envelope = strict.run(plan)
    assert strict_envelope.ok is False
    assert strict_envelope.data["halted"] is True
    assert strict_envelope.data["steps"][1]["error_code"] == ErrorCode.CONFIRMATION_REQUIRED.value
    assert strict_envelope.data["steps"][2]["error_code"] == ErrorCode.NOT_EXECUTED.value

    narrowed = _two_step_env()
    envelope = narrowed.run(plan, halt_on=["AMBIGUOUS"])
    assert envelope.ok is True
    assert envelope.data["halted"] is False
    assert envelope.data["steps"][1]["error_code"] == ErrorCode.CONFIRMATION_REQUIRED.value
    assert envelope.data["steps"][1]["ok"] is False
    assert envelope.data["steps"][2]["ok"] is True


def _two_step_env() -> SequenceEnv:
    """An environment scripted for the click / submission / click plan above.

    The submission step is denied before it observes anything, so it consumes no
    scripted observation; the two clicks consume one each.
    """
    env = build_sequence_env()
    previous = env.cache.current
    assert previous is not None
    second = workflow_state(2)
    third = workflow_state(3)
    env.script(second, workflow_delta(previous, second, region=box_of(previous.elements[0])))
    env.script(third, workflow_delta(second, third, region=box_of(second.elements[4])))
    return env


def test_an_unmapped_failure_always_halts() -> None:
    """An unrecognised failure is not evidence that continuing is safe.

    The halt set is narrowed to *nothing* first, which is the most permissive
    configuration the schema allows. A failure code outside the section 66.1
    halt table must still stop the plan: continuing on an unknown failure is
    precisely the inference batching is forbidden from making.
    """
    env = build_sequence_env(confirmation=_confirm)
    plan = [
        # An argument the click schema does not declare: the step is refused
        # with INTERNAL_ERROR, which is not in the halt table.
        {"step_id": "s1", "tool": "click", "target": "Play", "role": "BUTTON", "bogus": 1},
        {"step_id": "s2", "tool": "click", "target": "Play", "role": "BUTTON"},
    ]

    envelope = env.run(plan, halt_on=[])

    assert envelope.ok is False
    assert envelope.data["halted"] is True
    assert envelope.data["steps"][0]["error_code"] == ErrorCode.INTERNAL_ERROR.value
    assert envelope.data["steps"][1]["error_code"] == ErrorCode.NOT_EXECUTED.value
    assert _presses(env) == 0


# -- Section 32.1: read-only work never disturbs physical work ----------------


def test_concurrent_read_only_calls_never_reorder_or_replace_physical_steps() -> None:
    """Section 32.1: reads run on their own path and touch no action ordering.

    Two reader threads hammer ``get_screen_state`` for the whole duration of the
    sequence -- a barrier guarantees the reads are already in flight before the
    first step starts. The physical action sequence must be identical to a
    sequential run, every read must answer honestly, the read path must stay
    inside its cap, and every step's lease must still be stamped with the live
    frame of *its own* step rather than with whatever a read just returned.
    """
    import threading

    from ai.tool_protocol import ToolCall

    sequential = build_sequence_env(script_workflow=True, confirmation=_confirm)
    sequential.run(WORKFLOW_PLAN)
    expected_events = _input_names(sequential)

    env = build_sequence_env(settings=sequence_settings(), script_workflow=True, confirmation=_confirm)
    reads: list[Any] = []
    refusals: list[Any] = []
    start = threading.Barrier(3, timeout=10.0)
    stop = threading.Event()

    def reader() -> None:
        """Read the desktop continuously, on the bounded read path only."""
        first = env.dispatch_env.dispatcher.dispatch(
            ToolCall(name=ToolName.GET_SCREEN_STATE, arguments={})
        )
        reads.append(first)
        start.wait()  # both readers are now live, with a real read behind them
        while not stop.is_set():
            envelope = env.dispatch_env.dispatcher.dispatch(
                ToolCall(name=ToolName.GET_SCREEN_STATE, arguments={})
            )
            (reads if envelope.ok else refusals).append(envelope)

    workers = [threading.Thread(target=reader, daemon=True) for _ in range(2)]
    for worker in workers:
        worker.start()
    start.wait()  # the reads are in flight before the sequence begins
    try:
        envelope = env.run(WORKFLOW_PLAN)
    finally:
        stop.set()
        for worker in workers:
            worker.join(timeout=5.0)

    assert envelope.ok is True
    assert all(step["ok"] for step in envelope.data["steps"])
    # Byte-for-byte the same input sequence as the sequential run: a concurrent
    # read neither reordered, delayed into, nor inserted a physical action.
    assert _input_names(env) == expected_events
    assert len(reads) > 1, "the reads must actually have overlapped the sequence"
    # A saturated read path is refused honestly (RATE_LIMITED), never faked and
    # never allowed to delay an action.
    assert all(refusal.error_code is ErrorCode.RATE_LIMITED for refusal in refusals)
    stats = env.dispatch_env.dispatcher.stats
    assert stats.read_only_peak <= env.settings.performance.max_concurrent_readonly_dispatch
    # Revalidation used live state, not a snapshot a read happened to fetch.
    assert [record["frame_id"] for record in env.leases] == [1, 2, 4, 5]
