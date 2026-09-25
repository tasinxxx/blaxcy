"""``run_sequence`` execution -- batched multi-step control (specification section 66.1).

This module makes batching real, so it is also the one that carries the most
risk. The rule it enforces is unambiguous:

    **Every step runs through the exact same pipeline as a standalone call to
    that tool.**

The implementation takes that literally instead of re-implementing the pipeline:
a step is dispatched through the *same* :class:`~ai.tool_protocol.ToolDispatcher`
a standalone call would use, so a step inherits policy, fresh resolution
(section 43), a fresh lease (section 44), the full revalidation checklist
(section 45), occlusion and visibility checks (sections 46-47), the physical
input layer (Part VI) and verification (section 60) by construction. There is no
second pipeline to drift out of sync, and no coordinate, ``element_id`` or lease
is ever carried from one step to the next -- only the Brain's *description* of
the next step travels forward.

What batching removes is the Brain round-trip. What it does not remove, and can
never remove:

* the sequence-level gate (section 56/57) -- the plan is refused outright in
  OBSERVE, and gated as its most restrictive declared step class;
* per-step confirmation -- a destructive step asks a human *fresh* (§66.1); the
  runner never forwards a pre-given confirmation into a step, because
  pre-authorising a destructive step is exactly what batching must not do;
* per-step recovery budgets (section 61) -- step 4 cannot spend step 1's budget;
* the halt conditions below -- BLAXCY never guesses forward past a failure;
* an honest trailing ``NOT_EXECUTED`` for every step it did not reach (section 66).
"""

from __future__ import annotations

import contextlib
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from typing import Any, Protocol, runtime_checkable

from ai.tool_protocol import ToolCall
from config.settings import Settings
from core.event_bus import EventBus
from core.logging_setup import get_logger
from core.resolver_cache import CacheOutcome, ResolverCache
from core.speculative_perceiver import SpeculativePerceiver
from core.state_cache import StateCache
from policy.modes import ModeController
from policy.permissions import PermissionEngine
from schemas.actions import ToolEnvelope, ToolName, elapsed_ms_since
from schemas.elements import ElementQuery
from schemas.enums import ChangeClass, ErrorCode, PolicyMode, UIRole
from schemas.events import Event, EventType
from schemas.sequences import (
    HaltCondition,
    RunSequenceRequest,
    SequenceProgress,
    SequenceResult,
    SequenceStep,
    SequenceStepResult,
)

_log = get_logger(__name__)


@runtime_checkable
class StepDispatcher(Protocol):
    """The one door a sequence step goes through (satisfied by ``ToolDispatcher``).

    Declared as a protocol rather than imported concretely so this module stays
    testable with a double, while the production wiring still uses the single
    real dispatcher -- the point is that there is exactly one pipeline, not that
    there is exactly one class.
    """

    def dispatch(
        self,
        call: Any,
        *,
        task_id: str | None = ...,
        step_id: str | None = ...,
        sequence_id: str | None = ...,
        confirmed: bool = ...,
        require_verification: bool | None = ...,
    ) -> ToolEnvelope:
        """Execute one tool call and return its uniform envelope."""


#: Every failure code's halt condition (section 66.1). A code that is *not* in
#: this table still halts: an unrecognised failure is not evidence that it is
#: safe to continue, and continuing on an unverified premise is the one thing
#: batching must never do.
_HALT_FOR_CODE: dict[ErrorCode, HaltCondition] = {
    ErrorCode.TARGET_AMBIGUOUS: HaltCondition.TARGET_AMBIGUOUS,
    ErrorCode.TARGET_STALE: HaltCondition.TARGET_STALE,
    ErrorCode.TARGET_NOT_FOUND: HaltCondition.TARGET_NOT_FOUND,
    ErrorCode.TARGET_OCCLUDED: HaltCondition.TARGET_OCCLUDED,
    ErrorCode.VERIFICATION_CONTRADICTED: HaltCondition.VERIFICATION_CONTRADICTED,
    ErrorCode.VERIFICATION_UNVERIFIED: HaltCondition.VERIFICATION_UNVERIFIED,
    ErrorCode.CONFIRMATION_REQUIRED: HaltCondition.CONFIRMATION_REQUIRED,
    ErrorCode.CONFIRMATION_DENIED: HaltCondition.CONFIRMATION_REQUIRED,
    ErrorCode.BLOCKED_APPLICATION: HaltCondition.BLOCKED_APPLICATION,
    ErrorCode.PERMISSION_DENIED: HaltCondition.PERMISSION_DENIED,
    ErrorCode.HUMAN_TAKEOVER: HaltCondition.HUMAN_TAKEOVER,
    ErrorCode.EMERGENCY_STOP_ACTIVE: HaltCondition.EMERGENCY_STOP_ACTIVE,
    ErrorCode.CALIBRATION_FAILED: HaltCondition.CALIBRATION_FAILED,
    ErrorCode.BACKEND_UNAVAILABLE: HaltCondition.BACKEND_UNAVAILABLE,
    ErrorCode.CAPTURE_FAILED: HaltCondition.CAPTURE_FAILED,
    ErrorCode.A11Y_TIMEOUT: HaltCondition.A11Y_TIMEOUT,
}

#: Tools a step may carry that have no target description to speculate about.
_TARGETLESS_TOOLS: frozenset[str] = frozenset(
    {ToolName.PRESS_KEY, ToolName.HOTKEY, ToolName.ENSURE_WINDOW,
     ToolName.GET_SCREEN_STATE, ToolName.GET_CAPABILITIES, ToolName.GET_ACTIVE_WINDOW}
)


class SequenceRunner:
    """Executes a validated ``run_sequence`` plan (section 66.1).

    Args:
        settings: Full configuration; ``[sequence]`` supplies the limits and the
            two batching feature flags.
        dispatcher: The single tool door. Steps are dispatched through it, so a
            sequence step is a standalone tool call with a step id attached.
        state_cache: The current-state authority; also holds the live
            :class:`~schemas.sequences.SequenceProgress` the GUI reads (§65/§71).
        permissions: The one permission policy (sections 56-58).
        mode_controller: The authoritative mode, including AUTONOMOUS expiry.
        event_bus: Optional bus for the four sequence lifecycle events (§65).
        recovery: Optional section 61 recovery controller. When supplied, each
            step gets its own budget via ``begin_step``.
        cache: Optional section 43.1 resolver cache, invalidated on generation,
            window and structural change.
        speculative: Optional section 33.2 prefetcher.
        capture_boost: Optional ``() -> context manager`` raising the frame
            engine's capture profile for the duration of the sequence (§33.1).
        abort_check: The combined stop/takeover hook, checked before every step.
        clock: Monotonic clock, injectable for deterministic tests.
        sequence_id_factory: Injectable id source, for deterministic tests.
        calibration: Optional calibration, for the sequence-level gate.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        dispatcher: StepDispatcher,
        state_cache: StateCache,
        permissions: PermissionEngine,
        mode_controller: ModeController,
        event_bus: EventBus | None = None,
        recovery: Any = None,
        cache: ResolverCache | None = None,
        speculative: SpeculativePerceiver | None = None,
        capture_boost: Callable[[], Any] | None = None,
        abort_check: Callable[[], ErrorCode | None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sequence_id_factory: Callable[[], str] | None = None,
        calibration: Any = None,
    ) -> None:
        self._settings = settings
        self._dispatcher = dispatcher
        self._cache = state_cache
        self._permissions = permissions
        self._mode = mode_controller
        self._bus = event_bus
        self._recovery = recovery
        self._resolver_cache = cache
        self._speculative = speculative
        self._capture_boost = capture_boost
        self._abort_check = abort_check
        self._clock = clock
        self._new_id = sequence_id_factory or (lambda: f"seq-{uuid.uuid4().hex}")
        self._calibration = calibration
        self.sequences = 0
        self.steps_executed = 0
        self.halts = 0
        self.refused = 0

    # -- Public entry point ---------------------------------------------------

    def run(
        self,
        request: RunSequenceRequest,
        *,
        task_id: str | None = None,
        confirmed: bool = False,
    ) -> SequenceResult:
        """Execute ``request`` step by step, halting honestly (section 66.1).

        ``confirmed`` is accepted for protocol compatibility and deliberately
        **not forwarded** into a destructive step: section 66.1 requires a
        destructive step's confirmation to be requested fresh, so a sequence can
        never arrive pre-authorised. The executor's own confirmation callback is
        what asks; a human's "no" halts the sequence.
        """
        del confirmed  # never forwarded into a step -- see the docstring
        self.sequences += 1
        started = self._clock()
        sequence_id = self._new_id()
        total = len(request.steps)
        halt_on = request.effective_halt_on
        gate_class = request.action_class

        self._publish(
            EventType.SEQUENCE_STARTED,
            {
                "sequence_id": sequence_id,
                "step_index": 0,
                "total_steps": total,
                "action_class": gate_class.value,
                "task_id": task_id,
            },
        )
        self._set_progress(sequence_id, 0, total, (), None)
        if self._recovery is not None:
            self._recovery.begin_task(task_id)

        refusal = self._sequence_gate(request, gate_class)
        if refusal is not None:
            self.refused += 1
            return self._halt(
                sequence_id=sequence_id,
                request=request,
                results=[],
                start_index=0,
                code=refusal[0],
                reason=refusal[1],
                started=started,
            )

        results: list[SequenceStepResult] = []
        with self._boost():
            for index, step in enumerate(request.steps):
                step_result, halt = self._run_step(
                    step,
                    index=index,
                    sequence_id=sequence_id,
                    task_id=task_id,
                    request=request,
                    halt_on=halt_on,
                    elapsed_total=self._clock() - started,
                )
                results.append(step_result)
                self._publish(
                    EventType.SEQUENCE_STEP_COMPLETED,
                    {
                        "sequence_id": sequence_id,
                        "step_index": index,
                        "total_steps": total,
                        "step_id": step.step_id,
                        "tool": step.tool,
                        "ok": step_result.ok,
                        "verification": step_result.verification.value,
                        "error_code": (
                            None if step_result.error_code is None else step_result.error_code.value
                        ),
                    },
                )
                self._set_progress(
                    sequence_id,
                    index,
                    total,
                    tuple(range(index + 1)),
                    None,
                )
                self._note_change()
                if halt is not None:
                    return self._halt(
                        sequence_id=sequence_id,
                        request=request,
                        results=results,
                        start_index=index + 1,
                        code=halt[0],
                        reason=halt[1],
                        started=started,
                    )
                self._speculate_next(request.steps, index)

        self._set_progress(sequence_id, total - 1, total, tuple(range(total)), None)
        wall_clock_ms = elapsed_ms_since(started)
        self._publish(
            EventType.SEQUENCE_COMPLETED,
            {
                "sequence_id": sequence_id,
                "step_index": total - 1,
                "total_steps": total,
                "completed_count": total,
            },
        )
        return SequenceResult(
            sequence_id=sequence_id,
            steps=tuple(results),
            halted=False,
            halt_reason=None,
            wall_clock_ms=wall_clock_ms,
            started_at=started,
        )

    # -- Sequence-level gate (sections 56, 57) --------------------------------

    def _sequence_gate(
        self, request: RunSequenceRequest, gate_class: Any
    ) -> tuple[ErrorCode, str] | None:
        """Refuse the whole plan when policy does not permit it at all.

        Section 56: OBSERVE never executes a ``run_sequence`` -- it may be
        validated, never run. Section 57: an ASSIST/AUTONOMOUS plan is gated as
        its most restrictive declared step class.

        The container-level decision is taken with ``confirmed=True`` on
        purpose: the container's own "confirmation" is not a gate, because the
        real confirmation happens fresh on the destructive *step* (§66.1). Only
        a genuine refusal stops the plan here.
        """
        mode = self._mode.mode
        if mode is PolicyMode.OBSERVE:
            return (
                ErrorCode.PERMISSION_DENIED,
                "OBSERVE does not execute a run_sequence (section 56); "
                "the plan was accepted but no step was run",
            )
        state = self._cache.current
        decision = self._permissions.decide(
            tool=ToolName.RUN_SEQUENCE,
            mode=mode,
            action_class=gate_class,
            application=(None if state is None else state.active_app,),
            calibration=self._calibration,
            confirmed=True,
        )
        if decision.allowed:
            return None
        return (
            decision.code or ErrorCode.PERMISSION_DENIED,
            decision.message or "the sequence was refused by policy",
        )

    # -- One step -------------------------------------------------------------

    def _run_step(
        self,
        step: SequenceStep,
        *,
        index: int,
        sequence_id: str,
        task_id: str | None,
        request: RunSequenceRequest,
        halt_on: frozenset[HaltCondition],
        elapsed_total: float,
    ) -> tuple[SequenceStepResult, tuple[ErrorCode, str] | None]:
        """Run one step and decide whether it halts the sequence.

        Returns the step's result (always) and the halt reason (when the step
        halted the sequence). The result is built even on a halt, so the Brain
        always sees what actually happened rather than only that it stopped.
        """
        abort = self._abort_code()
        if abort is not None:
            return (
                SequenceStepResult.not_executed_step(
                    step=step, index=index, reason="automation was stopped before this step"
                ),
                (abort, "automation was stopped before the step ran"),
            )

        limit = self._settings.sequence.max_sequence_wall_clock_seconds
        if elapsed_total >= limit:
            return (
                SequenceStepResult.not_executed_step(
                    step=step,
                    index=index,
                    reason=f"sequence wall-clock limit of {limit} s was reached",
                ),
                (
                    ErrorCode.SEQUENCE_WALL_CLOCK_EXCEEDED,
                    f"the sequence exceeded max_sequence_wall_clock_seconds={limit}",
                ),
            )

        # A step that declares a credential field halts before it runs: section
        # 55 keeps password handling on the per-step path, never batched, never
        # speculated and never cached (see also section 33.2 and section 43.1).
        if step.role is UIRole.PASSWORD_INPUT:
            return (
                SequenceStepResult.not_executed_step(
                    step=step,
                    index=index,
                    reason="credential-context steps are not batched (section 55)",
                ),
                (
                    ErrorCode.PERMISSION_DENIED,
                    "a PASSWORD_INPUT step halts the sequence for per-step handling (section 55)",
                ),
            )

        if self._recovery is not None:
            self._recovery.begin_step(step.step_id)

        hint_note = self._take_speculation(step, index=index, sequence_id=sequence_id)
        try:
            envelope = self._dispatcher.dispatch(
                ToolCall(name=step.tool, arguments=step_arguments(step)),
                task_id=task_id,
                step_id=step.step_id,
                sequence_id=sequence_id,
                # Never a pre-supplied confirmation (section 66.1): the
                # executor's own callback asks the human, freshly.
                confirmed=False,
                require_verification=step.require_verification,
            )
        finally:
            if self._recovery is not None:
                self._recovery.end_step()
        self.steps_executed += 1

        if hint_note is not None:
            envelope = _with_note(envelope, hint_note)
            self._note_speculation_use(step, hint_note, envelope)

        result = SequenceStepResult.from_envelope(envelope, step=step, index=index)
        if envelope.ok:
            return result, None
        return result, self._halt_decision(envelope, step, halt_on)

    def _halt_decision(
        self,
        envelope: ToolEnvelope,
        step: SequenceStep,
        halt_on: frozenset[HaltCondition],
    ) -> tuple[ErrorCode, str] | None:
        """Decide whether a failed step halts the sequence (section 66.1)."""
        code = envelope.error_code or ErrorCode.INTERNAL_ERROR
        # A credential-context action reports ``sensitive`` rather than a
        # dedicated code, so the flag is checked explicitly; section 55 wants
        # that step handled on its own, never batched forward.
        if envelope.data.get("sensitive") is True:
            condition: HaltCondition | None = HaltCondition.PASSWORD_CONTEXT
        else:
            condition = _HALT_FOR_CODE.get(code)
        if condition is None:
            # Unmapped failure: halt. An unrecognised failure is not evidence
            # that continuing is safe.
            self.halts += 1
            return (code, f"step {step.step_id} failed with {code.value}: {envelope.message}")
        if condition not in halt_on:
            # The caller narrowed the halt set within the safety set (section
            # 66.1), so this particular condition is tolerated and the sequence
            # continues on an explicitly recorded failure.
            return None
        self.halts += 1
        return (
            code,
            f"step {step.step_id} ({step.tool}) halted the sequence on "
            f"{condition.value}: {envelope.message or code.value}",
        )

    # -- Section 33.1 / 33.2 integration --------------------------------------

    @contextlib.contextmanager
    def _boost(self) -> Iterator[None]:
        """Raise the capture profile for the duration of the sequence (§33.1).

        A scheduling change only: it shortens how long a real change takes to be
        observed, which shortens the settle/verify wait, and it changes no check.
        """
        factory = self._capture_boost
        if not self._settings.sequence.capture_boost or factory is None:
            yield
            return
        with contextlib.ExitStack() as stack:
            stack.enter_context(factory())
            yield

    def _speculate_next(self, steps: Sequence[StepLike], index: int) -> None:
        """Prefetch the next step's declared description (§33.2)."""
        speculative = self._speculative
        if speculative is None or not self._settings.sequence.speculative_perception:
            return
        if index + 1 >= len(steps):
            return
        following = steps[index + 1]
        query = query_for_step(following)
        if query is None:
            return
        state = self._cache.current
        if state is None:
            return
        speculative.speculate(following.step_id, query, state)

    def _take_speculation(
        self, step: SequenceStep, *, index: int, sequence_id: str
    ) -> dict[str, Any] | None:
        """Use the speculative hint for this step, as a hint only (§33.2).

        The hint never replaces resolution: the step's dispatch runs the full
        cascade anyway. What the hint can do is seed the section 43.1 cache with
        the identity path it observed, so the live lookup starts from a known
        identity instead of the top of the tree -- and the hint is reported, so
        its real usefulness is measurable rather than assumed.
        """
        speculative = self._speculative
        if speculative is None or not self._settings.sequence.speculative_perception:
            return None
        state = self._cache.current
        delta = self._cache.delta
        regions = () if delta is None else tuple(region.rect for region in delta.regions)
        hint = speculative.take(
            step.step_id,
            state=state,
            change_class=None if delta is None else delta.change_class,
            regions=regions,
        )
        if hint is None:
            return None
        note: dict[str, Any] = {"speculative": True, "hint": hint.to_dict()}
        if not hint.is_resolved:
            return note
        query = query_for_step(step)
        if query is None or self._resolver_cache is None:
            return note
        self._resolver_cache.prime(query, _as_identity_hint(hint))
        return note

    def _note_speculation_use(
        self, step: SequenceStep, hint_note: dict[str, Any], envelope: ToolEnvelope
    ) -> None:
        """Record a taken hint the step's own resolution actually consumed (§76).

        ``useful`` counts a hint that reached the live resolution, not merely one
        that was requested: the step's envelope must carry a resolver-cache HIT,
        which is what a primed hint becomes when the resolver consumes it. A
        hint that was taken but not consumed stays uncounted, so the section 76
        usefulness rate cannot flatter the optimization. This is observability
        only -- it increments a counter and changes no decision, position or
        safety check.
        """
        speculative = self._speculative
        if speculative is None:
            return
        hint = hint_note.get("hint")
        if not isinstance(hint, dict) or hint.get("identity_path") is None:
            return
        resolution = envelope.data.get("resolution")
        if not isinstance(resolution, dict):
            return
        lookup = resolution.get("cache")
        if not isinstance(lookup, dict) or lookup.get("outcome") != CacheOutcome.HIT.value:
            return
        self.mark_speculation_useful(step.step_id)

    def mark_speculation_useful(self, key: str) -> None:
        """Record that the hint for ``key`` really shortened live resolution."""
        if self._speculative is None:
            return
        self._speculative.mark_useful(key)

    # -- Cache maintenance (section 43.1) ------------------------------------

    def _note_change(self) -> None:
        """Invalidate hints the last step's change made unsafe (§34/§43.1).

        Called between steps, never inside one: invalidating mid-step would be a
        second, hidden input path.
        """
        delta = self._cache.delta
        state = self._cache.current
        regions = () if delta is None else tuple(region.rect for region in delta.regions)
        if self._resolver_cache is not None:
            if state is not None:
                self._resolver_cache.invalidate_generation(state.generation)
            # Section 43.1 invalidates on a *MAJOR* change, not on any
            # meaningful one. A MEANINGFUL change is usually the very repaint
            # the step just caused (a pressed button, a focused field), and
            # dropping the hint for that would make the cache useless in the
            # one workflow it exists to accelerate.
            if delta is not None and delta.change_class is ChangeClass.MAJOR:
                self._resolver_cache.invalidate_regions(regions)
        if self._speculative is not None and delta is not None:
            # A speculation is cheaper to recompute than a hint is to lose, so
            # section 33.2's wider MEANINGFUL-or-MAJOR rule applies here.
            self._speculative.note_change(delta.change_class, regions)

    # -- Halting --------------------------------------------------------------

    def _halt(
        self,
        *,
        sequence_id: str,
        request: RunSequenceRequest,
        results: Sequence[SequenceStepResult],
        start_index: int,
        code: ErrorCode,
        reason: str,
        started: float,
    ) -> SequenceResult:
        """Build the honest partial result: completed steps + NOT_EXECUTED tail."""
        tail = tuple(
            SequenceStepResult.not_executed_step(
                step=step, index=index, reason=f"sequence halted: {reason}"
            )
            for index, step in enumerate(request.steps)
            if index >= start_index
        )
        steps = tuple(results) + tail
        self.halts += 1
        self._set_progress(
            sequence_id,
            min(start_index, len(request.steps) - 1),
            len(request.steps),
            tuple(index for index in range(start_index)),
            reason,
        )
        self._publish(
            EventType.SEQUENCE_HALTED,
            {
                "sequence_id": sequence_id,
                "step_index": min(start_index, len(request.steps) - 1),
                "total_steps": len(request.steps),
                "halt_reason": reason,
                "error_code": code.value,
                "completed_count": len(results),
            },
        )
        # Section 70: a halted sequence is exactly where a human needs to be able
        # to see *why* from the log alone, with the step index and the reason.
        _log.warning(
            "sequence halted",
            extra={
                "action_type": "run_sequence",
                "ok": False,
                "sequence_id": sequence_id,
                "step_index": min(start_index, len(request.steps) - 1),
                "total_steps": len(request.steps),
                "sequence_halt_reason": reason,
                "error_code": code.value,
                "completed_count": len(results),
            },
        )
        return SequenceResult(
            sequence_id=sequence_id,
            steps=steps,
            halted=True,
            halt_reason=reason,
            halt_code=code,
            wall_clock_ms=elapsed_ms_since(started),
            started_at=started,
        )

    # -- Progress / events ----------------------------------------------------

    def _set_progress(
        self,
        sequence_id: str,
        current_index: int,
        total: int,
        completed: tuple[int, ...],
        halt_reason: str | None,
    ) -> None:
        """Publish the live progress record the GUI reads (sections 65, 71)."""
        self._cache.set_sequence_progress(
            SequenceProgress(
                sequence_id=sequence_id,
                current_index=min(current_index, total - 1),
                total_steps=total,
                completed_indices=completed,
                halt_reason=halt_reason,
            )
        )

    def _publish(self, event_type: EventType, payload: dict[str, Any]) -> None:
        """Publish one sequence lifecycle event (section 65)."""
        if self._bus is not None:
            self._bus.publish(Event.create(event_type, payload))

    def _abort_code(self) -> ErrorCode | None:
        """The latched stop/takeover code, if any."""
        return None if self._abort_check is None else self._abort_check()

    def stats(self) -> dict[str, Any]:
        """A JSON-shaped health snapshot for logs and the benchmark report."""
        return {
            "sequences": self.sequences,
            "steps_executed": self.steps_executed,
            "halts": self.halts,
            "refused": self.refused,
            "settings": {
                "enabled": self._settings.sequence.enabled,
                "max_sequence_steps": self._settings.sequence.max_sequence_steps,
                "max_sequence_wall_clock_seconds": (
                    self._settings.sequence.max_sequence_wall_clock_seconds
                ),
                "capture_boost": self._settings.sequence.capture_boost,
                "speculative_perception": self._settings.sequence.speculative_perception,
            },
            "resolver_cache": None if self._resolver_cache is None else self._resolver_cache.stats(),
            "speculative": None if self._speculative is None else self._speculative.stats(),
        }


#: A step-like object: anything the runner can read a description from.
StepLike = SequenceStep


def step_arguments(step: SequenceStep) -> dict[str, Any]:
    """The standalone tool arguments for ``step``.

    Exactly the argument shape the corresponding standalone tool takes, with the
    runner's own bookkeeping (``step_id``) removed. ``target``/``role`` stay
    because they *are* tool arguments -- descriptions, never resolved values.
    """
    arguments = step.to_dict()
    arguments.pop("step_id", None)
    arguments.pop("tool", None)
    return arguments


def query_for_step(step: SequenceStep) -> ElementQuery | None:
    """The target description of a step, or ``None`` when it has no target."""
    if step.tool in _TARGETLESS_TOOLS or step.target is None:
        return None
    return ElementQuery(text=step.target, role_hint=step.role)


def _as_identity_hint(hint: Any) -> Any:
    """Convert a speculation into the cache's ``IdentityHint`` shape.

    ``recorded_at`` is left at ``0.0`` on purpose: the section 43.1 cache stamps
    the real time when it inserts a primed hint (see ``ResolverCache.prime``),
    because the cache -- not this adapter -- owns the clock its TTL is measured
    against. Building a timestamp here would be a claim about a clock this code
    cannot see.
    """
    from core.resolver_cache import IdentityHint

    return IdentityHint(
        identity_path=hint.identity_path,
        element_id=hint.element_id or "",
        role=hint.role or "UNKNOWN",
        owner_window_id=hint.owner_window_id,
        frame_id=hint.frame_id,
        state_version=hint.state_version,
        generation=hint.generation,
        patch_hash=None,
        bbox=hint.bbox,
        recorded_at=0.0,
        cache_window_id=hint.owner_window_id,
    )


def _with_note(envelope: ToolEnvelope, note: dict[str, Any]) -> ToolEnvelope:
    """Attach the section 33.2 hint record to a step's envelope."""
    data = dict(envelope.data)
    data["speculative_hint"] = note
    return envelope.model_copy(update={"data": data})


__all__ = [
    "SequenceRunner",
    "StepDispatcher",
    "StepLike",
    "query_for_step",
    "step_arguments",
]
