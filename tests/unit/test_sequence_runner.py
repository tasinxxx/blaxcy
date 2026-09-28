"""``run_sequence`` execution (specification section 66.1).

These tests cover what batching *is*: one Brain round trip, five real steps, each
one resolved, leased, revalidated and verified on its own, with the limits and
the two optimizations behaving as scheduling changes rather than as shortcuts.
The properties that must hold no matter what -- halting, confirmations, stop,
takeover, credential context -- live in ``tests/safety/test_sequence_halts.py``.
"""

from __future__ import annotations

from typing import Any

import pytest

from control.recovery import RecoveryController
from schemas.actions import ToolName
from schemas.enums import ChangeClass, ErrorCode, PolicyMode, VerificationState
from schemas.events import EventType
from schemas.sequences import RunSequenceRequest
from tests.harness.phase8 import box_of
from tests.harness.phase101 import (
    WORKFLOW_PLAN,
    SequenceEnv,
    build_sequence_env,
    sequence_settings,
    workflow_delta,
    workflow_state,
)

pytestmark = pytest.mark.batching


def _confirm(*_args: Any) -> bool:
    """A human who always says yes (the terminal submission needs one, §54)."""
    return True


def _ready(**kwargs: Any) -> SequenceEnv:
    """A workflow environment whose every step can verify, with a willing human."""
    kwargs.setdefault("script_workflow", True)
    kwargs.setdefault("confirmation", _confirm)
    return build_sequence_env(**kwargs)


def _input_names(env: SequenceEnv) -> list[str]:
    """The ordered input event kinds the fake backend recorded."""
    return [str(name) for name in env.backend.event_names()]


def _presses(env: SequenceEnv) -> int:
    """How many pointer-button presses were injected."""
    return _input_names(env).count("press")


# -- The happy path -----------------------------------------------------------


def test_query_for_step_infers_text_input_role_for_address_bar() -> None:
    step = SequenceStep(
        step_id="s1",
        tool="type_text",
        target="address bar",
        args={"text": "https://example.com"},
    )
    query = query_for_step(step)
    assert query is not None
    assert query.role_hint is UIRole.TEXT_INPUT


def test_a_five_step_plan_runs_in_one_round_trip_with_every_step_verified() -> None:
    """The section 74 workflow: one submission, five fully verified steps."""
    env = _ready()

    envelope = env.run(WORKFLOW_PLAN)

    assert envelope.ok is True
    steps = env.step_envelopes(envelope)
    assert [step["step_id"] for step in steps] == ["s1", "s2", "s3", "s4", "s5"]
    assert [step["index"] for step in steps] == [0, 1, 2, 3, 4]
    assert all(step["ok"] for step in steps)
    assert [step["verification"] for step in steps] == [VerificationState.VERIFIED.value] * 5
    # One round trip: the Brain made a single tool call and never heard back
    # between steps.
    assert env.dispatch_env.dispatcher.stats.calls[ToolName.RUN_SEQUENCE] == 1
    assert env.executor.stats()["actions"] == 5
    # Three click steps, and every button they pressed was released again.
    names = _input_names(env)
    assert _presses(env) == 3
    assert names.count("release") == 3
    # The typed text plus Return, with every key released (nothing left held).
    assert names.count("key_press") == len("song name") + 1
    assert names.count("key_press") == names.count("key_release")


def test_every_step_issues_its_own_lease_bound_to_its_own_observation() -> None:
    """No lease, coordinate or element id is carried between steps (section 66.1)."""
    env = _ready()

    env.run(WORKFLOW_PLAN)

    frames = [record["frame_id"] for record in env.leases]
    # Steps 1, 2, 4 and 5 carry targets; each lease is stamped with the frame
    # that was current when that step resolved, so the second occurrence of a
    # control can never reuse the first step's lease.
    assert frames == [1, 2, 4, 5]
    assert len({record["lease_id"] for record in env.leases}) == 4


def test_a_step_that_resolves_the_same_control_twice_still_resolves_it_live() -> None:
    """A repeated description is re-resolved against the new observation."""
    env = _ready(script_workflow=False)
    previous = env.cache.current
    assert previous is not None
    plan = [
        {"step_id": "a", "tool": "click", "target": "Play", "role": "BUTTON"},
        {"step_id": "b", "tool": "click", "target": "Play", "role": "BUTTON"},
    ]
    state_2 = workflow_state(2)
    env.script(state_2, workflow_delta(previous, state_2, region=box_of(workflow_state(2).elements[4])))
    state_3 = workflow_state(3)
    env.script(state_3, workflow_delta(state_2, state_3, region=box_of(state_3.elements[4])))

    envelope = env.run(plan)

    assert envelope.ok is True
    assert [record["frame_id"] for record in env.leases] == [1, 2]


# -- Limits -------------------------------------------------------------------


def test_the_wall_clock_limit_halts_before_executing_anything() -> None:
    """``max_sequence_wall_clock_seconds`` is an operational ceiling, not advice."""
    ticks = iter([0.0, 100.0])

    def clock() -> float:
        """Report the start time, then a time well past the limit."""
        return next(ticks, 100.0)

    env = build_sequence_env(
        settings=sequence_settings(max_sequence_wall_clock_seconds=5.0), clock=clock
    )

    envelope = env.run(WORKFLOW_PLAN)

    assert envelope.ok is False
    assert envelope.error_code is ErrorCode.SEQUENCE_WALL_CLOCK_EXCEEDED
    steps = env.step_envelopes(envelope)
    assert [step["error_code"] for step in steps] == [ErrorCode.NOT_EXECUTED.value] * 5
    assert env.executor.stats()["actions"] == 0
    assert env.input_events() == []


def test_the_step_limit_is_reported_before_the_runner_is_reached() -> None:
    """A too-large plan is refused at the door, so no step can start."""
    env = build_sequence_env(settings=sequence_settings(max_sequence_steps=2))

    envelope = env.run(WORKFLOW_PLAN)

    assert envelope.ok is False
    assert envelope.error_code is ErrorCode.SEQUENCE_STEP_LIMIT_EXCEEDED
    assert env.executor.stats()["actions"] == 0


def test_a_plan_is_never_executed_in_observe_mode() -> None:
    """Section 56: OBSERVE may validate a plan but never runs it."""
    env = build_sequence_env(mode=PolicyMode.OBSERVE, script_workflow=True)

    envelope = env.run(WORKFLOW_PLAN)

    assert envelope.ok is False
    assert envelope.error_code is ErrorCode.PERMISSION_DENIED
    steps = env.step_envelopes(envelope)
    assert all(step["error_code"] == ErrorCode.NOT_EXECUTED.value for step in steps)
    assert env.executor.stats()["actions"] == 0
    assert env.input_events() == []


# -- Verification requirement (section 66.1) ---------------------------------


def test_an_unverified_mutating_step_halts_the_sequence_by_default() -> None:
    """A required-but-absent postcondition stops the plan (§60/§66.1)."""
    env = build_sequence_env()
    previous = env.cache.current
    assert previous is not None
    state_2 = workflow_state(2)
    # A TRIVIAL change cannot establish a postcondition.
    env.script(state_2, workflow_delta(previous, state_2, region=box_of(previous.elements[0]), change_class=ChangeClass.TRIVIAL))
    plan = [
        {"step_id": "s1", "tool": "click", "target": "Search", "role": "BUTTON"},
        {"step_id": "s2", "tool": "click", "target": "Play", "role": "BUTTON"},
    ]

    envelope = env.run(plan)

    assert envelope.ok is False
    assert envelope.error_code is ErrorCode.VERIFICATION_UNVERIFIED
    steps = env.step_envelopes(envelope)
    assert steps[0]["verification"] == VerificationState.UNVERIFIED.value
    assert steps[1]["error_code"] == ErrorCode.NOT_EXECUTED.value


def test_an_explicit_per_step_override_lets_an_unverified_step_continue() -> None:
    """``require_verification: false`` is the caller electing to proceed unverified."""
    env = build_sequence_env()
    previous = env.cache.current
    assert previous is not None
    state_2 = workflow_state(2)
    state_3 = workflow_state(3)
    env.script(state_2, workflow_delta(previous, state_2, region=box_of(previous.elements[0]), change_class=ChangeClass.TRIVIAL))
    env.script(state_3, workflow_delta(state_2, state_3, region=box_of(state_2.elements[4])))
    plan = [
        {"step_id": "s1", "tool": "click", "target": "Search", "role": "BUTTON", "require_verification": False},
        {"step_id": "s2", "tool": "click", "target": "Play", "role": "BUTTON"},
    ]

    envelope = env.run(plan)

    assert envelope.ok is True
    steps = env.step_envelopes(envelope)
    assert steps[0]["verification"] == VerificationState.UNVERIFIED.value
    assert steps[0]["ok"] is True
    assert steps[0]["require_verification"] is False
    assert steps[1]["ok"] is True


def test_a_contradicted_result_is_never_turned_into_a_success() -> None:
    """The override governs an *absent* postcondition, never a contradicted one."""
    env = build_sequence_env()
    previous = env.cache.current
    assert previous is not None
    # The field stays empty, so the typed text is provably absent (CONTRADICTED).
    state_2 = workflow_state(2, typed="")
    env.script(
        state_2,
        workflow_delta(previous, state_2, region=box_of(previous.elements[1])),
    )
    plan = [
        {
            "step_id": "s1",
            "tool": "type_text",
            "target": "Search field",
            "text": "song name",
            "require_verification": False,
        }
    ]

    envelope = env.run(plan)

    assert envelope.ok is False
    assert envelope.error_code is ErrorCode.VERIFICATION_CONTRADICTED


# -- Section 33.1 capture boost ----------------------------------------------


def test_the_capture_boost_wraps_the_whole_sequence_and_is_reverted() -> None:
    """Section 33.1: a scheduling change that must always be undone."""
    env = _ready(settings=sequence_settings(capture_boost=True), capture_boost=True)

    env.run(WORKFLOW_PLAN)

    assert env.boosts == ["enter", "exit"]


def test_the_capture_boost_is_off_by_default() -> None:
    """Section 4 rule 31: an unproven optimization ships disabled."""
    env = _ready()

    env.run(WORKFLOW_PLAN)

    assert env.boosts == []


# -- Section 33.2 speculative perception -------------------------------------


def test_speculation_is_scheduled_for_the_next_step_only() -> None:
    """The prefetcher is asked for step *N+1* while step *N* runs (section 33.2)."""
    env = _ready(
        settings=sequence_settings(speculative_perception=True, resolver_cache=True),
        speculative_perception=True,
        resolver_cache=True,
    )

    env.run(WORKFLOW_PLAN)

    assert env.speculative is not None
    stats = env.speculative.stats()
    # A hint is scheduled after each step that has a *targeted* successor, so
    # s3 (press_key, no description) is skipped rather than guessed at.
    assert stats["requested"] == 3
    assert stats["completed"] == 3
    assert stats["taken"] >= 2


def test_a_consumed_speculation_is_counted_useful() -> None:
    """Section 76: usefulness is measured, not a counter that never increments.

    A primed hint that the step's own live resolution then consumes (a resolver
    cache HIT) is what "the speculation shortened resolution" means here. Without
    this the reported ``usefulness_rate`` would be structurally ``0.0`` -- a
    number presented as a measurement while never being updated (section 4 rule
    8, section 79).
    """
    env = _ready(
        settings=sequence_settings(speculative_perception=True, resolver_cache=True),
        speculative_perception=True,
        resolver_cache=True,
    )

    envelope = env.run(WORKFLOW_PLAN)

    assert envelope.ok is True
    assert env.speculative is not None
    stats = env.speculative.stats()
    assert stats["taken"] >= 2
    assert stats["useful"] >= 1
    assert stats["usefulness_rate"] is not None and stats["usefulness_rate"] > 0.0


def test_a_speculation_is_discarded_when_its_region_structurally_changes() -> None:
    """Section 33.2: a MEANINGFUL/MAJOR change discards the hint for its region."""
    from config.settings import ResolverSettings
    from core.speculative_perceiver import SpeculativePerceiver
    from core.target_resolver import TargetResolver
    from schemas.elements import ElementQuery

    state = workflow_state(1)
    perceiver = SpeculativePerceiver(
        sequence_settings(speculative_perception=True).sequence,
        TargetResolver(ResolverSettings()),
        background=False,
    )
    query = ElementQuery(text="Play")

    perceiver.speculate("s5", query, state)
    assert perceiver.peek("s5") is not None

    # A MEANINGFUL change inside the hinted element's own box.
    discarded = perceiver.note_change(ChangeClass.MEANINGFUL, (box_of(state.elements[4]),))

    assert discarded == 1
    assert perceiver.take("s5", state=state) is None
    assert perceiver.stats()["discarded_change"] == 1


def test_a_speculation_survives_a_change_elsewhere() -> None:
    """Only a change to the region the hint depends on invalidates it."""
    from config.settings import ResolverSettings
    from core.speculative_perceiver import SpeculativePerceiver
    from core.target_resolver import TargetResolver
    from schemas.elements import ElementQuery

    state = workflow_state(1)
    perceiver = SpeculativePerceiver(
        sequence_settings(speculative_perception=True).sequence,
        TargetResolver(ResolverSettings()),
        background=False,
    )
    perceiver.speculate("s5", ElementQuery(text="Play"), state)

    assert perceiver.note_change(ChangeClass.MAJOR, (box_of(state.elements[0]),)) == 0
    hint = perceiver.take("s5", state=state)
    assert hint is not None and hint.element_id == "play"


def test_speculation_is_never_scheduled_for_a_credential_target() -> None:
    """Section 55: a password description is never speculated about."""
    from config.settings import ResolverSettings
    from core.speculative_perceiver import SpeculativePerceiver
    from core.target_resolver import TargetResolver
    from schemas.elements import ElementQuery
    from schemas.enums import UIRole

    perceiver = SpeculativePerceiver(
        sequence_settings(speculative_perception=True).sequence,
        TargetResolver(ResolverSettings()),
        background=False,
    )

    scheduled = perceiver.speculate(
        "s1", ElementQuery(text="Password", role_hint=UIRole.PASSWORD_INPUT), workflow_state(1)
    )

    assert scheduled is False
    assert perceiver.stats()["requested"] == 0


def test_a_disabled_prefetcher_does_nothing_at_all() -> None:
    """``speculative_perception = false`` keeps the runner from scheduling (§4 rule 31)."""
    env = _ready(settings=sequence_settings(speculative_perception=False))

    env.run(WORKFLOW_PLAN)

    assert env.speculative is None


# -- Progress, events and per-step recovery scoping --------------------------


def test_the_sequence_publishes_its_lifecycle_events_and_progress() -> None:
    """Sections 65 and 71: the GUI can see every step, not an opaque batch."""
    from core.event_bus import EventBus

    env = _ready()
    seen: list[EventType] = []
    bus: EventBus = env.dispatch_env.bus
    bus.subscribe(lambda event: seen.append(event.event_type))

    envelope = env.run(WORKFLOW_PLAN)

    assert seen[0] is EventType.SEQUENCE_STARTED
    assert seen.count(EventType.SEQUENCE_STEP_COMPLETED) == 5
    assert seen[-1] is EventType.SEQUENCE_COMPLETED
    progress = env.cache.snapshot().sequence
    assert envelope.data["halted"] is False
    assert progress is not None
    assert progress.total_steps == 5
    assert progress.completed_indices == (0, 1, 2, 3, 4)
    assert progress.is_complete is True


class RecordingRecovery(RecoveryController):
    """The real recovery policy, plus a record of the scoping calls it received."""

    def __init__(self) -> None:
        """Create the recorder over the production budgets."""
        super().__init__()
        self.calls: list[str] = []

    def begin_task(self, task_id: str | None = None) -> None:
        """Record and delegate."""
        self.calls.append("begin_task")
        super().begin_task(task_id)

    def begin_step(self, step_id: str | None = None) -> None:
        """Record and delegate."""
        self.calls.append(f"begin_step:{step_id}")
        super().begin_step(step_id)

    def end_step(self) -> None:
        """Record and delegate."""
        self.calls.append("end_step")
        super().end_step()


def test_each_step_gets_its_own_recovery_budget() -> None:
    """Section 61: step 4 cannot spend step 1's budget."""
    recovery = RecordingRecovery()
    env = _ready(recovery=recovery)

    env.run(WORKFLOW_PLAN[:2])

    assert recovery.calls == [
        "begin_task",
        "begin_step:s1",
        "end_step",
        "begin_step:s2",
        "end_step",
    ]


# -- The runner never pre-authorises a destructive step ----------------------


def test_a_confirmed_flag_never_pre_authorises_a_destructive_step() -> None:
    """Section 66.1: a destructive step's confirmation is always requested fresh."""
    said_no: list[str] = []

    def refuse(message: str, details: dict[str, Any]) -> bool:
        """Record the ask and refuse it."""
        said_no.append(message)
        return False

    env = build_sequence_env(confirmation=refuse)
    request = RunSequenceRequest.model_validate(
        {
            "steps": [
                {
                    "step_id": "s1",
                    "tool": "drag",
                    "target": "Search",
                    "destination": "Play",
                }
            ]
        }
    )

    result = env.runner.run(request, confirmed=True)

    assert result.halted is True
    # The halt reports the step's own refusal code: a human was asked and said
    # no, which is more precise than the generic "confirmation was required".
    assert result.halt_code is ErrorCode.CONFIRMATION_DENIED
    assert said_no, "the runner must have asked a human rather than trusting the flag"
    assert _presses(env) == 0


def test_a_destructive_step_proceeds_once_a_human_confirms() -> None:
    """A fresh confirmation is what authorises it -- nothing else."""
    asked: list[str] = []

    def confirm(message: str, details: dict[str, Any]) -> bool:
        """Record the ask and approve it."""
        asked.append(message)
        return True

    env = build_sequence_env(confirmation=confirm)
    previous = env.cache.current
    assert previous is not None
    plan = [{"step_id": "s1", "tool": "drag", "target": "Search", "destination": "Play"}]
    state_2 = workflow_state(2)
    env.script(state_2, workflow_delta(previous, state_2, region=box_of(previous.elements[0])))

    envelope = env.run(plan)

    assert envelope.ok is True
    assert asked, "the destructive step must have been confirmed"
    assert len(env.leases) == 1
