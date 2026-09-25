"""Phase 9 safety gate (specification sections 21, 22, 61-64, 83).

This is the suite the specification's Phase 9 gate is about: emergency stop,
human takeover and bounded recovery, all wired into the executor's real abort
hook and real failure paths -- not tested in isolation from what actually
injects input.

Every negative case asserts that **nothing was injected**, because "the call
returned an error" is not a safety property on its own.
"""

from __future__ import annotations

import time

import pytest

from config.settings import RecoverySettings
from control.backends.base import PointerButton
from control.emergency_stop import EmergencyStop, combine_abort_checks
from control.executor import Executor
from control.recovery import FailureClass, RecoveryController
from control.takeover import TakeoverController
from schemas.actions import PlannedAction, ToolName
from schemas.elements import ElementQuery
from schemas.enums import ErrorCode, PolicyMode
from schemas.events import EventType
from tests.harness.phase8 import ExecutorEnv, make_delta, make_element, make_state

pytestmark = pytest.mark.safety

_QUERY = ElementQuery(text="Send")


def _input_events(env: ExecutorEnv) -> list[tuple[object, ...]]:
    """Real desktop-changing events (``flush`` is bookkeeping, not input)."""
    return [event for event in env.backend.events if event[0] != "flush"]


# -- Emergency stop stops the executor (section 63) ---------------------------

def test_a_latched_emergency_stop_prevents_input() -> None:
    """A stop latched before the action means the action never injects."""
    estop = EmergencyStop()
    env = ExecutorEnv(
        mode=PolicyMode.ASSIST,
        state=make_state(elements=(make_element(),)),
        abort_check=estop.abort_code,
    )
    estop.trigger("test")

    envelope = env.executor.execute(PlannedAction(tool=ToolName.CLICK, target=_QUERY))

    assert envelope.ok is False
    assert envelope.error_code is ErrorCode.EMERGENCY_STOP_ACTIVE
    assert _input_events(env) == []


def test_a_latched_stop_short_circuits_before_any_work() -> None:
    """A stop does not leave the body resolving against a reclaimed desktop."""
    estop = EmergencyStop()
    env = ExecutorEnv(
        mode=PolicyMode.ASSIST,
        state=make_state(elements=(make_element(),)),
        abort_check=estop.abort_code,
    )
    estop.trigger("test")

    envelope = env.executor.execute(PlannedAction(tool=ToolName.CLICK, target=_QUERY))

    assert envelope.error_code is ErrorCode.EMERGENCY_STOP_ACTIVE
    # No resolution, no lease, no verification: it never got that far.
    assert "resolution" not in envelope.data
    assert env.bus.history_of(EventType.LEASE_ISSUED) == ()
    assert _input_events(env) == []


def test_a_stop_latched_mid_action_still_prevents_input() -> None:
    """A stop arriving after resolution but before injection still wins."""
    estop = EmergencyStop()
    element = make_element()
    stale = make_state(elements=(element,), monotonic=time.monotonic() - 10.0)
    env = ExecutorEnv(mode=PolicyMode.ASSIST, state=stale)
    inner = env.perceiver

    def perceiver() -> object:
        # The stop arrives while the executor is re-perceiving, i.e. mid-action.
        estop.trigger("mid-action")
        return inner()

    executor = Executor(
        env.settings,
        permissions=env.permissions,
        mode_controller=env.modes,
        state_cache=env.cache,
        resolver=env.resolver,
        mouse=env.mouse,
        keyboard=env.keyboard,
        verifier=env.verifier,
        tracker=env.tracker,
        event_bus=env.bus,
        perceive=perceiver,  # type: ignore[arg-type]
        abort_check=estop.abort_code,
    )
    inner.script(make_state(frame_id=6, elements=(element,)))

    envelope = executor.execute(PlannedAction(tool=ToolName.CLICK, target=_QUERY))

    assert envelope.ok is False
    assert envelope.error_code is ErrorCode.EMERGENCY_STOP_ACTIVE
    assert _input_events(env) == []


def test_a_full_stop_drops_to_observe_so_the_next_action_is_denied_too() -> None:
    """Defence in depth: the stop both refuses the hook and changes the mode."""
    env = ExecutorEnv(mode=PolicyMode.ASSIST, state=make_state(elements=(make_element(),)))
    estop = EmergencyStop(mode_controller=env.modes)
    estop.register_input(env.mouse)
    estop.trigger("test")
    env.executor = _rewire(env, abort_check=estop.abort_code)

    envelope = env.executor.execute(PlannedAction(tool=ToolName.CLICK, target=_QUERY))

    assert env.modes.mode is PolicyMode.OBSERVE
    assert envelope.ok is False
    assert _input_events(env) == []


def test_a_stop_releases_input_the_executor_was_holding() -> None:
    """A held button from a drag comes off when the stop is pressed (§63)."""
    estop = EmergencyStop()
    env = ExecutorEnv(mode=PolicyMode.ASSIST, state=make_state(elements=(make_element(),)))
    estop.register_input(env.backend)
    env.backend.press_button(PointerButton.LEFT)
    assert env.backend.held_buttons != ()

    result = estop.trigger("test")

    assert env.backend.held_buttons == ()
    assert env.backend.held_keys == ()
    assert result.released_buttons == ("left",)


# -- Human takeover (section 62) ---------------------------------------------

def test_a_stop_wired_to_the_controllers_reports_what_it_released() -> None:
    """The realistic wiring names the held button; it is not silently lost."""
    estop = EmergencyStop()
    env = ExecutorEnv(mode=PolicyMode.ASSIST, state=make_state(elements=(make_element(),)))
    estop.register_input(env.mouse)
    estop.register_input(env.keyboard)
    env.backend.press_button(PointerButton.LEFT)
    held_before = env.mouse.held_buttons
    assert held_before == (PointerButton.LEFT,)

    result = estop.trigger("test")

    assert "left" in result.released_buttons
    assert env.backend.held_buttons == ()
    held_after = env.mouse.held_buttons
    assert held_after == ()


def test_takeover_stops_automation_and_never_injects() -> None:
    """A human with control means BLAXCY does not touch the desktop."""
    takeover = TakeoverController()
    env = ExecutorEnv(
        mode=PolicyMode.ASSIST,
        state=make_state(elements=(make_element(),)),
        abort_check=takeover.abort_code,
    )
    takeover.begin("user took over")

    envelope = env.executor.execute(PlannedAction(tool=ToolName.CLICK, target=_QUERY))

    assert envelope.ok is False
    assert envelope.error_code is ErrorCode.HUMAN_TAKEOVER
    assert _input_events(env) == []


def test_takeover_releases_held_input_and_then_allows_actions_after_resume() -> None:
    """Resume clears the latch; a later explicit action is permitted again."""
    takeover = TakeoverController()
    element = make_element()
    state = make_state(frame_id=5, elements=(element,))
    env = ExecutorEnv(
        mode=PolicyMode.ASSIST,
        state=state,
        abort_check=takeover.abort_code,
    )
    takeover.register_input(env.backend)
    env.backend.press_button(PointerButton.LEFT)
    takeover.begin("user took over")
    assert env.backend.held_buttons == ()

    takeover.resume()
    after = make_state(frame_id=6, elements=(element,))
    env.perceiver.script(after, make_delta(before=state, after=after))
    presses_before = len(env.backend.payloads("press"))

    envelope = env.executor.execute(PlannedAction(tool=ToolName.CLICK, target=_QUERY))

    assert envelope.ok is True
    # Exactly one new click: the setup's own held-button press is already counted.
    assert len(env.backend.payloads("press")) == presses_before + 1


def test_emergency_stop_outranks_takeover_when_both_are_latched() -> None:
    """The stronger statement is the one reported."""
    estop = EmergencyStop()
    takeover = TakeoverController()
    env = ExecutorEnv(
        mode=PolicyMode.ASSIST,
        state=make_state(elements=(make_element(),)),
        abort_check=combine_abort_checks(estop.abort_code, takeover.abort_code),
    )
    takeover.begin("takeover")
    assert env.executor.execute(PlannedAction(tool=ToolName.CLICK, target=_QUERY)).error_code is (
        ErrorCode.HUMAN_TAKEOVER
    )

    estop.trigger("stop")
    envelope = env.executor.execute(PlannedAction(tool=ToolName.CLICK, target=_QUERY))
    assert envelope.error_code is ErrorCode.EMERGENCY_STOP_ACTIVE
    assert _input_events(env) == []


# -- Bounded recovery wired into the executor (sections 21, 22, 61) -----------

def test_recovery_retries_a_stale_target_and_can_succeed() -> None:
    """One bounded retry against a freshly perceived state, injecting once."""
    element = make_element()
    stale = make_state(elements=(element,), monotonic=time.monotonic() - 10.0)
    recovery = RecoveryController(RecoverySettings())
    env = ExecutorEnv(mode=PolicyMode.ASSIST, state=stale, recovery=recovery)

    # 1. the first re-perception no longer finds the target (TARGET_STALE) ...
    env.perceiver.script(make_state(frame_id=6, elements=()))
    # 2. ... the recovery re-perception finds it again ...
    recovered = make_state(frame_id=7, elements=(element,))
    env.perceiver.script(recovered)
    # 3. ... and the retry's own observation shows the result.
    after = make_state(frame_id=8, elements=(element,))
    env.perceiver.script(after, make_delta(before=recovered, after=after))

    envelope = env.executor.execute(PlannedAction(tool=ToolName.CLICK, target=_QUERY))

    assert envelope.ok is True
    assert envelope.data["recovery_attempted"] is True
    assert len(env.backend.payloads("press")) == 1
    assert recovery.stats()["attempts_total"] == 1


def test_recovery_does_not_retry_a_destructive_action() -> None:
    """Section 4 rule 21, enforced through the executor rather than trusted."""
    recovery = RecoveryController(RecoverySettings())
    env = ExecutorEnv(mode=PolicyMode.ASSIST, state=make_state(elements=()), recovery=recovery)

    envelope = env.executor.execute(
        PlannedAction(
            tool=ToolName.DRAG,
            target=_QUERY,
            params={"destination": {"text": "Send"}},
        ),
        confirmed=True,
    )

    assert envelope.ok is False
    assert envelope.data["recovery"]["should_attempt"] is False
    assert "destructive" in envelope.data["recovery"]["reason"]
    assert _input_events(env) == []


def test_recovery_never_retries_after_input_was_injected() -> None:
    """An unverified click already happened; a retry would click twice."""
    element = make_element()
    recovery = RecoveryController(RecoverySettings())
    env = ExecutorEnv(
        mode=PolicyMode.ASSIST,
        state=make_state(elements=(element,)),
        recovery=recovery,
    )

    envelope = env.executor.execute(PlannedAction(tool=ToolName.CLICK, target=_QUERY))

    assert envelope.ok is False
    assert envelope.error_code is ErrorCode.VERIFICATION_UNVERIFIED
    assert envelope.data["recovery"]["should_attempt"] is False
    assert "already injected input" in envelope.data["recovery"]["reason"]
    assert len(env.backend.payloads("press")) == 1


def test_recovery_cannot_loop_however_often_it_is_called() -> None:
    """Repeated failures consume the budget instead of retrying forever (§22)."""
    recovery = RecoveryController(RecoverySettings())
    env = ExecutorEnv(mode=PolicyMode.ASSIST, state=make_state(elements=()), recovery=recovery)

    for _ in range(5):
        env.executor.execute(PlannedAction(tool=ToolName.CLICK, target=_QUERY))

    stats = env.executor.stats()
    assert stats["actions"] == 5
    assert stats["recovery_attempts"] == 2
    assert _input_events(env) == []


def test_a_safety_refusal_explains_that_it_is_not_recoverable() -> None:
    """A refusal is annotated, so the Brain sees why nothing will be retried."""
    recovery = RecoveryController(RecoverySettings())
    env = ExecutorEnv(mode=PolicyMode.OBSERVE, state=make_state(elements=(make_element(),)), recovery=recovery)

    envelope = env.executor.execute(PlannedAction(tool=ToolName.CLICK, target=_QUERY))

    assert envelope.ok is False
    assert envelope.data["recovery"]["failure_class"] == FailureClass.SAFETY.value
    assert envelope.data["recovery"]["should_attempt"] is False
    assert _input_events(env) == []


def test_disabling_recovery_leaves_the_original_behaviour() -> None:
    """Without a recovery controller, one attempt is one attempt."""
    env = ExecutorEnv(mode=PolicyMode.ASSIST, state=make_state(elements=()))
    envelope = env.executor.execute(PlannedAction(tool=ToolName.CLICK, target=_QUERY))
    assert envelope.ok is False
    assert "recovery_attempted" not in envelope.data
    assert env.executor.stats()["recovery_attempts"] == 0


# -- Helpers ------------------------------------------------------------------

def _rewire(env: ExecutorEnv, *, abort_check: object) -> Executor:
    """Rebuild the executor around the same components with a new abort hook."""
    return Executor(
        env.settings,
        permissions=env.permissions,
        mode_controller=env.modes,
        state_cache=env.cache,
        resolver=env.resolver,
        mouse=env.mouse,
        keyboard=env.keyboard,
        verifier=env.verifier,
        tracker=env.tracker,
        event_bus=env.bus,
        perceive=env.perceiver,
        abort_check=abort_check,  # type: ignore[arg-type]
    )
