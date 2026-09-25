"""The executor: the one place physical input is authorised (sections 44, 45, 47, 59).

Every input action in BLAXCY runs the same state machine, in the same order,
with no shortcut:

``VALIDATE -> PERMISSION -> RESOLVE -> LEASE -> REVALIDATE -> ENSURE_VISIBLE ->
ACT -> OBSERVE -> VERIFY``

The executor is the **only** caller of the physical input layer
(:mod:`control.mouse`, :mod:`control.keyboard`). The input backends perform no
policy, lease or revalidation check (section 13), which is safe *precisely
because* nothing reaches them except through this pipeline.

Three invariants this module exists to enforce:

* **A fresh lease before every action** (section 44). The lease is issued here,
  from the exact observation the target was resolved against, and is never
  reused, extended or carried over.
* **Full revalidation before every input** (section 45). The lease is checked
  against live state: still valid, generation current, frame fresh, identity
  unchanged, still visible, still in bounds, not occluded, right window, right
  application, no credential context, transform valid.
* **Honest verification after every action** (section 60). The result reports
  ``VERIFIED`` / ``UNVERIFIED`` / ``CONTRADICTED`` from real evidence, and an
  action whose verification was required and did not succeed is reported as a
  failure -- never smoothed into a success.

Read-only tools never come here. They do not touch the desktop and are
dispatched on the read path (section 32.1); routing them through the
single-writer executor would serialise work that needs no serialisation. A tool
this phase cannot really perform (``activate_element``, whose AT-SPI action
invocation is not implemented) returns a structured ``BACKEND_UNAVAILABLE``
rather than a fabricated success.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from config.settings import Settings
from control.action_tracker import ActionTracker
from control.backends.base import PointerButton
from control.keyboard import KeyboardController
from control.mouse import MouseController
from control.recovery import RecoveryController
from control.verifier import VerificationOutcome, Verifier
from control.window_manager import WindowController
from core.calibration import Calibration
from core.event_bus import EventBus
from core.state_cache import StateCache
from core.target_resolver import (
    OcclusionAssessment,
    ResolutionResult,
    ResolutionStatus,
    TargetResolver,
    assess_occlusion,
    bind_lease,
    occluders_above,
)
from policy.modes import ModeController
from policy.permissions import PermissionEngine, PolicyDecision, is_terminal_submit
from schemas.actions import (
    ActivationOutcome,
    PlannedAction,
    ToolEnvelope,
    ToolName,
    elapsed_ms_since,
)
from schemas.capability import CapabilityReport
from schemas.elements import ElementQuery, UIElement, identity_fingerprint
from schemas.enums import (
    CapabilityName,
    CoordinateSpace,
    ErrorCode,
    UIRole,
    VerificationState,
)
from schemas.errors import BlaxcyError
from schemas.events import Event, EventType
from schemas.geometry import Point
from schemas.leases import ElementLease
from schemas.screen_state import ScreenDelta, ScreenState


@dataclass(frozen=True)
class AttemptOutcome:
    """One execution attempt's result, plus what recovery needs to judge it.

    ``input_performed`` is the flag that keeps recovery honest: an action that
    already injected input must never be automatically re-run, because the first
    attempt may well have worked and a second one would act on the desktop twice.
    """

    envelope: ToolEnvelope
    input_performed: bool = False
    identity: str | None = None


@dataclass(frozen=True)
class RevalidationResult:
    """The outcome of the section 45 revalidation checklist."""

    ok: bool
    code: ErrorCode | None = None
    message: str | None = None
    element: UIElement | None = None
    occlusion: OcclusionAssessment | None = None
    moved_px: float | None = None
    checks: dict[str, bool] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """JSON-shaped revalidation evidence for the envelope."""
        return {
            "ok": self.ok,
            "checks": self.checks,
            "moved_px": self.moved_px,
            "occlusion": self.occlusion.to_dict() if self.occlusion is not None else None,
        }


def revalidate_target(
    lease: ElementLease,
    state: ScreenState,
    *,
    max_state_age_ms: float,
    occlusion_threshold: float = 0.50,
    previous_element: UIElement | None = None,
    occluders: Sequence[UIElement] = (),
    expect_window_id: int | None = None,
    now_monotonic: float | None = None,
) -> RevalidationResult:
    """Run the full section 45 checklist for ``lease`` against ``state``.

    Args:
        lease: The lease issued for this action; it is never refreshed here.
        state: The live observation to revalidate against.
        max_state_age_ms: Maximum acceptable age of ``state`` (sections 45, 76).
        occlusion_threshold: Coverage above this blocks the action (section 46).
        previous_element: The element as resolved, used to measure how far the
            target has moved since (section 45's 4 px tolerance).
        occluders: Known objects above the target.
        expect_window_id: When set, the target must still belong to this window.
        now_monotonic: Injected clock for deterministic tests.

    Returns:
        A :class:`RevalidationResult`. ``ok=False`` means input must not happen.
    """
    checks: dict[str, bool] = {}
    now = time.monotonic() if now_monotonic is None else now_monotonic

    # 1. Lease is still live.
    checks["lease_valid"] = not lease.is_expired(now)
    if not checks["lease_valid"]:
        return RevalidationResult(
            False,
            ErrorCode.TARGET_STALE,
            f"element lease expired ({lease.age_ms(now):.1f} ms > {lease.ttl_ms} ms ttl)",
            checks=checks,
        )

    # 2. No newer perception generation has superseded the lease (section 32).
    checks["generation_current"] = lease.is_generation_current(state.generation)
    if not checks["generation_current"]:
        return RevalidationResult(
            False,
            ErrorCode.TARGET_STALE,
            "a newer perception generation invalidated this lease",
            checks=checks,
        )

    # 3. The observation itself is fresh enough to act on.
    checks["state_fresh"] = state.is_fresh(max_state_age_ms, now)
    if not checks["state_fresh"]:
        return RevalidationResult(
            False,
            ErrorCode.TARGET_STALE,
            f"state is {state.age_ms(now):.1f} ms old (max {max_state_age_ms} ms)",
            checks=checks,
        )

    # 4. The lease is not from the future relative to this state.
    checks["state_not_older_than_lease"] = state.state_version >= lease.state_version
    if not checks["state_not_older_than_lease"]:
        return RevalidationResult(
            False,
            ErrorCode.TARGET_STALE,
            "the current state predates the lease",
            checks=checks,
        )

    # 5/6. The target still exists and is still the same control.
    element = state.element_by_id(lease.element_id)
    checks["target_present"] = element is not None
    if element is None:
        return RevalidationResult(
            False,
            ErrorCode.TARGET_STALE,
            "the leased target is no longer present in the current state",
            checks=checks,
        )
    checks["identity_consistent"] = lease.matches_identity(element.identity)
    if not checks["identity_consistent"]:
        return RevalidationResult(
            False,
            ErrorCode.TARGET_STALE,
            "the element at this position is no longer the leased control",
            element=element,
            checks=checks,
        )

    # 7. Still visible / enabled.
    checks["visible"] = element.visible
    checks["enabled"] = element.enabled
    if not element.visible or not element.enabled:
        return RevalidationResult(
            False,
            ErrorCode.TARGET_STALE,
            "the leased target is no longer visible and enabled",
            element=element,
            checks=checks,
        )

    # 8. Geometry is still usable and on-screen.
    point = element.point_for_input()
    checks["has_geometry"] = point is not None
    if point is None:
        return RevalidationResult(
            False,
            ErrorCode.TARGET_STALE,
            "the leased target no longer has usable geometry",
            element=element,
            checks=checks,
        )
    inside_desktop = point.space is not CoordinateSpace.DESKTOP or state.layout.desktop_rect.contains(point)
    checks["inside_desktop"] = inside_desktop
    if not inside_desktop:
        return RevalidationResult(
            False,
            ErrorCode.TARGET_OFFSCREEN,
            "the leased target lies outside the desktop bounds",
            element=element,
            checks=checks,
        )

    # 9. Occlusion (section 46). Only objects stacked at or above the target's own
    # window can hide it; the caller hands over everything it perceived, which
    # includes the desktop background and the windows behind the target.
    occlusion = assess_occlusion(
        element,
        occluders_above(element, occluders, state.window_stack),
        threshold=occlusion_threshold,
    )
    checks["not_occluded"] = occlusion.is_actionable
    if not checks["not_occluded"]:
        return RevalidationResult(
            False,
            ErrorCode.TARGET_OCCLUDED,
            occlusion.reason,
            element=element,
            occlusion=occlusion,
            checks=checks,
        )

    # 10. Right window.
    if expect_window_id is not None:
        checks["window_matches"] = element.owner_window_id == expect_window_id
        if not checks["window_matches"]:
            return RevalidationResult(
                False,
                ErrorCode.TARGET_STALE,
                "the leased target belongs to a different window than expected",
                element=element,
                occlusion=occlusion,
                checks=checks,
            )

    moved_px: float | None = None
    if previous_element is not None:
        before_point = previous_element.point_for_input()
        if before_point is not None and point.space is before_point.space:
            moved_px = before_point.distance_to(point)

    return RevalidationResult(
        True, None, None, element=element, occlusion=occlusion, moved_px=moved_px, checks=checks
    )


#: Tools this executor can genuinely perform. ``activate_element`` is included,
#: but only when an activation hook is actually wired (``activate=`` below): the
#: AT-SPI action invocation lives at the accessibility layer, and without one the
#: honest answer remains an unavailable backend rather than a fabricated click.
EXECUTOR_TOOLS: frozenset[str] = frozenset(
    {
        ToolName.CLICK,
        ToolName.DOUBLE_CLICK,
        ToolName.RIGHT_CLICK,
        ToolName.TYPE_TEXT,
        ToolName.PRESS_KEY,
        ToolName.HOTKEY,
        ToolName.SCROLL,
        ToolName.DRAG,
        ToolName.ENSURE_WINDOW,
        ToolName.ACTIVATE_ELEMENT,
    }
)

#: Tools whose target is an element description that must be resolved live.
_TARGET_TOOLS: frozenset[str] = frozenset(
    {
        ToolName.CLICK,
        ToolName.DOUBLE_CLICK,
        ToolName.RIGHT_CLICK,
        ToolName.TYPE_TEXT,
        ToolName.SCROLL,
        ToolName.ACTIVATE_ELEMENT,
    }
)

#: Tools that depend on keyboard focus, so their window must really be active.
_FOCUS_TOOLS: frozenset[str] = frozenset({ToolName.TYPE_TEXT, ToolName.PRESS_KEY, ToolName.HOTKEY})

#: Tools that inject a key sequence and have **no** element target. There is
#: nothing to resolve, lease or revalidate for them -- revalidation is a check on
#: a specific element's liveness (section 45), and with no element the only
#: freshness requirement is the state itself, which :meth:`Executor._fresh_state`
#: already enforces. Their postcondition is the observed consequence (section 60).
_KEY_TOOLS: frozenset[str] = frozenset({ToolName.PRESS_KEY, ToolName.HOTKEY})


class Executor:
    """Runs the section 59 pipeline for one action at a time.

    Args:
        settings: Full configuration; ``[verification]``, ``[lease]`` and
            ``[safety]`` are the sections consulted here.
        permissions: The single permission policy (sections 56-58).
        mode_controller: The authoritative mode, including AUTONOMOUS expiry.
        state_cache: The current-state authority; leases are stored here.
        resolver: Deterministic target resolution (section 43).
        mouse: The section 48 mouse sequence.
        keyboard: The section 50 keyboard controller.
        verifier: The section 60 verifier.
        tracker: The section 78 ownership record.
        event_bus: Optional bus for lease/action/verification events.
        window_manager: Optional section 47 window control.
        perceive: Optional callable that re-observes the desktop and offers the
            result to the cache. Without it the executor works from the cache's
            current state and refuses to act on a state that is not fresh.
        abort_check: Optional callable returning a stop code (``EMERGENCY_STOP_ACTIVE``
            / ``HUMAN_TAKEOVER``) when automation must halt. Phase 9 wires the
            real emergency stop and takeover into this hook.
        confirmation: Optional ``(message, details) -> bool`` used to obtain a
            human confirmation for destructive actions (section 56).
        capabilities: Optional capability report used to gate input backends.
        calibration: Optional calibration; a disarmed calibration blocks input.
        recovery: Optional section 61 recovery policy. When supplied, a failure
            that happened *before* any input, in a recoverable class, is retried
            **once** here, against a freshly perceived state; every budget is
            enforced by the controller, so repeated calls cannot loop.
        resolve_fallback: Optional section 43 fallback cascade (OCR, then gated
            visual grounding). It is invoked **only** when the deterministic
            cascade fails to deliver an actionable target -- nothing found, or
            nothing that cleared ``act_threshold`` -- and **never** for an
            ambiguous result, which is a decision rather than a miss. It must
            accept an enriched observation into the cache; the resolution then
            runs again in full against that observation, so the same ambiguity
            rule, lease and revalidation still apply.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        permissions: PermissionEngine,
        mode_controller: ModeController,
        state_cache: StateCache,
        resolver: TargetResolver,
        mouse: MouseController,
        keyboard: KeyboardController,
        verifier: Verifier,
        tracker: ActionTracker,
        event_bus: EventBus | None = None,
        window_manager: WindowController | None = None,
        perceive: Callable[[], ScreenState | None] | None = None,
        abort_check: Callable[[], ErrorCode | None] | None = None,
        confirmation: Callable[[str, dict[str, Any]], bool] | None = None,
        capabilities: CapabilityReport | None = None,
        calibration: Calibration | None = None,
        recovery: RecoveryController | None = None,
        resolve_fallback: Callable[[ElementQuery], bool] | None = None,
        activate: Callable[[UIElement], ActivationOutcome] | None = None,
    ) -> None:
        self._settings = settings
        self._permissions = permissions
        self._mode = mode_controller
        self._cache = state_cache
        self._resolver = resolver
        self._mouse = mouse
        self._keyboard = keyboard
        self._verifier = verifier
        self._tracker = tracker
        self._bus = event_bus
        self._windows = window_manager
        self._perceive = perceive
        self._abort_check = abort_check
        self._confirmation = confirmation
        self._capabilities = capabilities
        self._calibration = calibration
        self._recovery = recovery
        self._resolve_fallback = resolve_fallback
        self._activate = activate
        self._actions = 0
        self._denied = 0
        self._recovery_attempts = 0

    # -- Public entry point ---------------------------------------------------

    def execute(
        self,
        planned: PlannedAction,
        *,
        task_id: str | None = None,
        step_id: str | None = None,
        sequence_id: str | None = None,
        confirmed: bool = False,
        require_verification: bool | None = None,
    ) -> ToolEnvelope:
        """Run one action through the full section 59 pipeline, with bounded recovery.

        A failure that happened **before** any input, in a class recovery is
        allowed to act on, is retried once against a freshly perceived state.
        The retry is a complete second pass -- policy, resolve, lease,
        revalidate, act, verify -- never a resumption of the first attempt. Every
        further retry has to pass the :class:`~control.recovery.RecoveryController`
        budgets again, so this can never become an unbounded loop (section 22).

        Args:
            require_verification: An explicit per-action override of the default
                requirement (section 66.1). ``None`` -- the only value a
                standalone call uses -- falls back to the class default, so this
                parameter changes nothing unless a sequence step asks for it.
                It can never turn a ``CONTRADICTED`` outcome into a success.
        """
        self._actions += 1
        started = time.monotonic()
        outcome = self._execute_once(
            planned,
            started=started,
            task_id=task_id,
            step_id=step_id,
            sequence_id=sequence_id,
            confirmed=confirmed,
            require_verification=require_verification,
        )
        envelope = outcome.envelope
        if self._recovery is None or envelope.ok:
            return envelope

        decision = self._recovery.should_attempt(
            code=envelope.error_code,
            action_class=planned.action_class,
            tool=planned.tool,
            arguments=planned.params,
            input_performed=outcome.input_performed,
            identity=outcome.identity,
        )
        if not decision.should_attempt:
            return self._annotate(envelope, {"recovery": decision.to_dict()}, started)

        self._recovery.record_attempt(
            tool=planned.tool, arguments=planned.params, identity=outcome.identity
        )
        self._recovery_attempts += 1
        # Re-perceive first: the whole point of a TARGET/TRANSIENT recovery is to
        # resolve against the world as it is now, not as it was when it failed.
        if self._perceive is not None:
            with contextlib.suppress(BlaxcyError):
                self._perceive()
        retry = self._execute_once(
            planned,
            started=started,
            task_id=task_id,
            step_id=step_id,
            sequence_id=sequence_id,
            confirmed=confirmed,
            require_verification=require_verification,
        )
        return self._annotate(
            retry.envelope,
            {"recovery": decision.to_dict(), "recovery_attempted": True},
            started,
        )

    def _execute_once(
        self,
        planned: PlannedAction,
        *,
        started: float,
        task_id: str | None,
        step_id: str | None,
        sequence_id: str | None,
        confirmed: bool,
        require_verification: bool | None = None,
    ) -> AttemptOutcome:
        """One pass of the section 59 pipeline."""
        tool = planned.tool

        # -- EARLY ABORT ------------------------------------------------------
        # A latched stop is checked before any work, not just before injection.
        # The check before ACT is the one that guarantees no input happens; this
        # one exists so a stop does not leave the body resolving, leasing and
        # revalidating against a desktop the user has already taken back. The
        # stop's own code is reported, which is the honest reason while latched.
        early_abort = self._abort_code()
        if early_abort is not None:
            return AttemptOutcome(
                self._deny(started, early_abort, "automation was stopped before input", planned), False
            )

        if tool not in EXECUTOR_TOOLS:
            return AttemptOutcome(
                self._deny(
                    started,
                    ErrorCode.BACKEND_UNAVAILABLE,
                    f"{tool!r} is not implemented by the executor "
                    "(read-only tools are dispatched on the read path; AT-SPI action "
                    "invocation is not implemented)",
                    planned,
                )
            )

        if tool is ToolName.ENSURE_WINDOW:
            # A focus change is not input injection, so it is not treated as
            # "performed" for recovery purposes.
            return AttemptOutcome(self._execute_ensure_window(planned, started))

        # -- VALIDATE ---------------------------------------------------------
        validation = self._validate(planned)
        if validation is not None:
            return AttemptOutcome(self._deny(started, validation[0], validation[1], planned))

        state = self._cache.current
        if state is None:
            return AttemptOutcome(
                self._deny(
                    started,
                    ErrorCode.CAPTURE_FAILED,
                    "no current perception state is available to act on",
                    planned,
                )
            )

        # -- PERMISSION (pre-resolution: mode, class, application, calibration) -
        decision = self._permissions.decide(
            tool=tool,
            mode=self._mode.mode,
            action_class=planned.action_class,
            application=self._application_candidates(state, None),
            calibration=self._calibration,
            capability_report=self._capabilities,
            terminal_submit=is_terminal_submit(tool, planned.params),
            terminal_command=_visible_command(planned),
            confirmed=confirmed,
        )
        granted = self._resolve_decision(decision, planned, started)
        if isinstance(granted, ToolEnvelope):
            return AttemptOutcome(granted)

        # -- Key actions: no target, so no resolution step (validated above) ---
        if tool in _KEY_TOOLS:
            context = self._permissions.decide(
                tool=tool,
                mode=self._mode.mode,
                action_class=planned.action_class,
                application=self._application_candidates(state, None),
                calibration=self._calibration,
                capability_report=self._capabilities,
                capability=_input_capability(tool),
                confirmed=confirmed,
            )
            granted_context = self._resolve_decision(context, planned, started)
            if isinstance(granted_context, ToolEnvelope):
                return AttemptOutcome(granted_context)
            with self._tracker.tracking(
                tool, task_id=task_id, step_id=step_id, sequence_id=sequence_id
            ):
                return self._act_and_verify(
                    planned=planned,
                    tool=tool,
                    element=None,
                    lease=None,
                    resolution_data=None,
                    started=started,
                    require_verification=require_verification,
                )

        # -- RESOLVE ----------------------------------------------------------
        query = self._require_query(planned)
        # ``state`` is passed so the section 43.1 cache can *record* this
        # successful resolution (and know the active window). It never feeds
        # scoring: the hint is re-verified live on the next lookup.
        resolution = self._resolver.resolve(
            query, state.elements, occluders=state.elements, state=state
        )
        fallback_data: dict[str, Any] | None = None
        if (
            not resolution.is_resolved
            and resolution.status is not ResolutionStatus.AMBIGUOUS
            and self._resolve_fallback is not None
        ):
            # Section 43's later cascade stages -- OCR over the relevant regions,
            # then visual grounding under the section 41/42 gate -- run here and
            # only here: after a deterministic *miss* (nothing found, or nothing
            # that cleared act_threshold), never for an ambiguous result. The
            # fallback accepts an enriched observation into the cache; the
            # resolution below then runs in full against it, so an ambiguity the
            # enrichment introduced is still an ambiguity, and the lease and
            # revalidation that follow are unchanged.
            try:
                enriched = self._resolve_fallback(query)
            except BlaxcyError as exc:  # a fallback failure is not an action failure
                enriched = False
                fallback_data = {"attempted": True, "error": exc.code.value}
            refreshed = self._cache.current
            if enriched and refreshed is not None:
                state = refreshed
                resolution = self._resolver.resolve(
                    query, state.elements, occluders=state.elements, state=state
                )
                fallback_data = {
                    "attempted": True,
                    "resolved": resolution.is_resolved,
                    "state_version": state.state_version,
                    "frame_id": state.frame_id,
                }
            elif fallback_data is None:
                fallback_data = {"attempted": True, "resolved": False}
        if not resolution.is_resolved or resolution.best is None:
            code, message = _resolution_failure(resolution)
            data: dict[str, Any] = {"resolution": resolution.to_dict()}
            if fallback_data is not None:
                data["resolution_fallback"] = fallback_data
            return AttemptOutcome(self._deny(started, code, message, planned, data=data))
        winner = resolution.best
        element = winner.element

        # -- PERMISSION (context-sensitive: credential field, terminal typing) --
        context = self._permissions.decide(
            tool=tool,
            mode=self._mode.mode,
            action_class=planned.action_class,
            application=self._application_candidates(state, element),
            password_context=element.is_password,
            calibration=self._calibration,
            capability_report=self._capabilities,
            capability=_input_capability(tool),
            terminal_command=_visible_command(planned) if element.role is UIRole.TERMINAL else None,
            confirmed=confirmed,
        )
        granted = self._resolve_decision(context, planned, started)
        if isinstance(granted, ToolEnvelope):
            return AttemptOutcome(granted)

        # A credential-context action keeps the text it must type, but its
        # content is never placed in the envelope, an event or a log line. The
        # ``sensitive`` flag travels with the result so no downstream consumer
        # has to guess (sections 42, 55, 70).

        # -- RE-OBSERVE -------------------------------------------------------
        # Section 54's gate can hold this thread for as long as a human takes, and
        # nothing BLAXCY controls bounds that wait. The lease is issued *after* the
        # wait, so binding it to the pre-confirmation observation would produce a
        # lease already older than its own TTL -- which then expires during the very
        # re-perception section 45 needs in order to revalidate it. The screen may
        # also genuinely have moved while the dialog was up, so the target is
        # re-resolved against the new observation rather than carried across.
        reobserved: dict[str, Any] | None = None
        max_state_age_ms = float(self._settings.verification.max_action_state_age_ms)
        if not state.is_fresh(max_state_age_ms):
            try:
                refreshed = self._fresh_state()
            except BlaxcyError as exc:
                return AttemptOutcome(
                    self._deny(started, exc.code, exc.message, planned, data=exc.details)
                )
            if refreshed is not None and refreshed.frame_id != state.frame_id:
                reobservation = self._resolver.resolve(
                    query, refreshed.elements, occluders=refreshed.elements, state=refreshed
                )
                # The approval was given for one specific control. If the target no
                # longer resolves, or resolves to something else, that approval does
                # not transfer: the honest outcome is a refusal, never a click on
                # whatever happens to be in the same place now.
                if not reobservation.is_resolved or reobservation.best is None:
                    code, message = _resolution_failure(reobservation)
                    data = {"resolution": reobservation.to_dict(), "reobservation": True}
                    return AttemptOutcome(self._deny(started, code, message, planned, data=data))
                if identity_fingerprint(reobservation.best.element) != identity_fingerprint(
                    element
                ):
                    data = {"resolution": reobservation.to_dict(), "reobservation": True}
                    return AttemptOutcome(
                        self._deny(
                            started,
                            ErrorCode.TARGET_STALE,
                            "the target changed while the confirmation was pending; the "
                            "approval does not transfer to a different control",
                            planned,
                            data=data,
                        )
                    )
                # Same control, new observation: the permission decision above still
                # describes it, and the lease is bound to what the executor will
                # actually act on.
                state = refreshed
                resolution = reobservation
                winner = reobservation.best
                element = winner.element
                reobserved = {
                    "frame_id": state.frame_id,
                    "state_version": state.state_version,
                    "waited_ms": round(elapsed_ms_since(started), 1),
                }

        # -- LEASE ------------------------------------------------------------
        lease = bind_lease(winner, state, ttl_ms=self._settings.lease.ttl_ms)
        self._cache.store_lease(lease)
        self._emit(
            EventType.LEASE_ISSUED,
            {"lease_id": lease.lease_id, "element_id": lease.element_id, "frame_id": lease.frame_id},
        )

        with self._tracker.tracking(
            tool, lease=lease, task_id=task_id, step_id=step_id, sequence_id=sequence_id
        ):
            return self._act_and_verify(
                planned=planned,
                tool=tool,
                element=element,
                lease=lease,
                resolution_data={
                    **resolution.to_dict(),
                    **({"reobservation": reobserved} if reobserved is not None else {}),
                },
                started=started,
                sensitive=granted.sensitive,
                require_verification=require_verification,
            )

    # -- Pipeline stages ------------------------------------------------------

    def _validate(self, planned: PlannedAction) -> tuple[ErrorCode, str] | None:
        """Cheap, pre-perception parameter validation."""
        if planned.tool in _TARGET_TOOLS and planned.target is None:
            return (ErrorCode.TARGET_NOT_FOUND, f"{planned.tool} requires a target description")
        if planned.tool is ToolName.TYPE_TEXT and not str(planned.params.get("text", "")):
            return (ErrorCode.PERMISSION_DENIED, "type_text requires non-empty text")
        if planned.tool is ToolName.PRESS_KEY and not planned.params.get("key"):
            return (ErrorCode.PERMISSION_DENIED, "press_key requires a key")
        if planned.tool is ToolName.HOTKEY and not planned.params.get("combo"):
            return (ErrorCode.PERMISSION_DENIED, "hotkey requires a combo")
        if planned.tool is ToolName.DRAG and not planned.params.get("destination"):
            return (ErrorCode.PERMISSION_DENIED, "drag requires a destination description")
        return None

    def _act_and_verify(
        self,
        *,
        planned: PlannedAction,
        tool: str,
        element: UIElement | None,
        lease: ElementLease | None,
        resolution_data: dict[str, Any] | None,
        started: float,
        sensitive: bool = False,
        require_verification: bool | None = None,
    ) -> AttemptOutcome:
        """REVALIDATE -> ENSURE_VISIBLE -> ACT -> OBSERVE -> VERIFY.

        A target-based action carries a lease and is revalidated fully against
        live state. A key action carries neither, because it has no element to
        revalidate; its only freshness guarantee is the state the executor
        already required (see ``_KEY_TOOLS``).

        Every failure before injection is returned with ``input_performed=False``
        so a bounded recovery may act on it; a failure at or after injection is
        returned with ``input_performed=True`` so it never can.
        """
        identity = element.identity if element is not None else None
        before = self._cache.current
        try:
            state = self._fresh_state()
        except BlaxcyError as exc:
            return AttemptOutcome(
                self._deny(started, exc.code, exc.message, planned, data=exc.details), False, identity
            )

        revalidation: RevalidationResult | None = None
        live_element = element
        if lease is not None:
            # -- REVALIDATE ---------------------------------------------------
            revalidation = revalidate_target(
                lease,
                state,
                max_state_age_ms=float(self._settings.verification.max_action_state_age_ms),
                previous_element=element,
                occluders=state.elements,
            )
            if not revalidation.ok:
                return AttemptOutcome(
                    self._deny(
                        started,
                        revalidation.code or ErrorCode.TARGET_STALE,
                        revalidation.message or "revalidation failed",
                        planned,
                        data={"revalidation": revalidation.to_dict()},
                        frame_id=state.frame_id,
                        state_version=state.state_version,
                    ),
                    False,
                    identity,
                )
            live_element = revalidation.element or element

            # -- ENSURE_VISIBLE (focus, when the action depends on it) --------
            if live_element is not None:
                focus_failure = self._ensure_visible(live_element, planned, state)
                if focus_failure is not None:
                    return AttemptOutcome(
                        self._deny(
                            started,
                            focus_failure[0],
                            focus_failure[1],
                            planned,
                            data={"revalidation": revalidation.to_dict()},
                        ),
                        False,
                        identity,
                    )

        # -- Abort point: nothing may yield between here and injection --------
        abort = self._abort_code()
        if abort is not None:
            return AttemptOutcome(
                self._deny(started, abort, "automation was stopped before input", planned),
                False,
                identity,
            )

        # -- ACT --------------------------------------------------------------
        label = live_element.element_id if live_element is not None else tool
        self._cache.set_active_action(f"{tool}:{label}")
        failure = self._perform(tool, planned, live_element, state, started)
        if failure is not None:
            # A failure inside the input layer is treated as possibly-partial: an
            # unexpected exception during typing or a drag may already have
            # injected something, so it is never automatically re-run.
            return AttemptOutcome(failure, True, identity)

        # -- OBSERVE ----------------------------------------------------------
        after = self._observe()
        delta = self._delta_for(after)

        # -- VERIFY -----------------------------------------------------------
        outcome = self._verify(tool, planned, live_element, before, after, delta)
        self._cache.set_verification(outcome.state)
        self._emit(
            EventType.VERIFICATION_RESULT,
            {
                "action": tool,
                "element_id": live_element.element_id if live_element is not None else None,
                "verification": outcome.state.value,
                "postcondition": outcome.postcondition.value,
                "reason": outcome.reason,
            },
        )
        data: dict[str, Any] = {"verification": outcome.to_dict()}
        if live_element is not None:
            data["target"] = _target_summary(live_element)
        if lease is not None:
            data["lease"] = lease.to_dict()
        if resolution_data is not None:
            data["resolution"] = resolution_data
        if revalidation is not None:
            data["revalidation"] = revalidation.to_dict()
        if sensitive:
            # The action touched credential context; its content was never read.
            data["sensitive"] = True
        if revalidation is not None and (revalidation.moved_px or 0.0) > 4.0:
            data["geometry_revalidated"] = True
            data["moved_px"] = revalidation.moved_px

        # An explicit per-step override (section 66.1) wins; otherwise the class
        # default applies, which is what every standalone call gets. The override
        # only decides whether an *absent* postcondition is tolerated -- a
        # CONTRADICTED result is still a failure below, always.
        required = (
            require_verification
            if require_verification is not None
            else (
                planned.require_verification
                or self._verifier.requires_verification(planned.action_class)
            )
        )
        frame_id = after.frame_id if after is not None else state.frame_id
        state_version = after.state_version if after is not None else state.state_version

        if outcome.state is VerificationState.CONTRADICTED:
            return AttemptOutcome(
                ToolEnvelope.failure(
                    ErrorCode.VERIFICATION_CONTRADICTED,
                    outcome.reason,
                    data=data,
                    verification=outcome.state,
                    state_version=state_version,
                    frame_id=frame_id,
                    elapsed_ms=elapsed_ms_since(started),
                ),
                True,
                identity,
            )
        if outcome.state is VerificationState.UNVERIFIED and required:
            return AttemptOutcome(
                ToolEnvelope.failure(
                    ErrorCode.VERIFICATION_UNVERIFIED,
                    outcome.reason,
                    data=data,
                    verification=outcome.state,
                    state_version=state_version,
                    frame_id=frame_id,
                    elapsed_ms=elapsed_ms_since(started),
                ),
                True,
                identity,
            )
        return AttemptOutcome(
            ToolEnvelope.success(
                data=data,
                verification=outcome.state,
                state_version=state_version,
                frame_id=frame_id,
                elapsed_ms=elapsed_ms_since(started),
            ),
            True,
            identity,
        )

    # -- Acting ---------------------------------------------------------------

    def _perform(
        self,
        tool: str,
        planned: PlannedAction,
        element: UIElement | None,
        state: ScreenState,
        started: float,
    ) -> ToolEnvelope | None:
        """Inject the physical input. Returns an envelope only on failure."""
        try:
            # Key actions have no element and are injected first: they need no
            # geometry, and requiring one would make a keyboard action fail for
            # a reason that does not apply to it.
            if tool is ToolName.PRESS_KEY:
                modifiers = tuple(str(m) for m in (planned.params.get("modifiers") or ()))
                self._keyboard.press_key(str(planned.params.get("key", "")), modifiers=modifiers)
                return None
            if tool is ToolName.HOTKEY:
                self._keyboard.hotkey(str(planned.params.get("combo", "")))
                return None
            # Section 66's ``activate_element``: the application performs its own
            # accessibility action instead of receiving a synthetic pointer event.
            # It is handled before the geometry requirement on purpose -- an
            # AT-SPI action needs no coordinates -- but it still arrives here only
            # after policy, a fresh lease and the full section 45 revalidation,
            # and it is verified exactly like any other MUTATING action.
            if tool is ToolName.ACTIVATE_ELEMENT:
                if self._activate is None:
                    return self._deny(
                        started,
                        ErrorCode.BACKEND_UNAVAILABLE,
                        "no accessibility action backend is wired for activate_element",
                        planned,
                    )
                if element is None:
                    return self._deny(started, ErrorCode.TARGET_STALE, "target vanished", planned)
                outcome = self._activate(element)
                if not outcome.ok:
                    return self._deny(
                        started,
                        ErrorCode.BACKEND_UNAVAILABLE,
                        outcome.reason or "the accessibility action was refused",
                        planned,
                    )
                return None

            point = element.point_for_input() if element is not None else None
            if point is None or element is None:
                return self._deny(started, ErrorCode.TARGET_STALE, "target has no geometry", planned)
            if tool is ToolName.CLICK:
                result = self._mouse.click(point)
                if not result.ok:
                    return self._deny(
                        started, ErrorCode.BACKEND_UNAVAILABLE, result.reason or "click failed", planned
                    )
                return None
            if tool is ToolName.DOUBLE_CLICK:
                result = self._mouse.double_click(point)
                if not result.ok:
                    return self._deny(
                        started, ErrorCode.BACKEND_UNAVAILABLE, result.reason or "double click failed", planned
                    )
                return None
            if tool is ToolName.RIGHT_CLICK:
                result = self._mouse.right_click(point)
                if not result.ok:
                    return self._deny(
                        started, ErrorCode.BACKEND_UNAVAILABLE, result.reason or "right click failed", planned
                    )
                return None
            if tool is ToolName.SCROLL:
                scroll = self._mouse.scroll(
                    point,
                    vertical=int(planned.params.get("vertical", 3)),
                    horizontal=int(planned.params.get("horizontal", 0)),
                )
                if not scroll.ok:
                    return self._deny(
                        started, ErrorCode.BACKEND_UNAVAILABLE, scroll.reason or "scroll failed", planned
                    )
                return None
            if tool is ToolName.TYPE_TEXT:
                # A credential field's content must not be placed on the clipboard:
                # the clipboard is shared, and a clipboard manager persists it in
                # history, which would leak the password out of the field it was
                # typed into (sections 42, 55). The controller refuses rather than
                # pasting a secret, and the refusal is reported honestly.
                sensitive = bool(element is not None and element.is_password)
                self._keyboard.type_text(
                    str(planned.params.get("text", "")),
                    expected_focus=element if element is not None and element.is_text_entry else None,
                    state=state,
                    sensitive=sensitive,
                )
                return None
            if tool is ToolName.DRAG:
                return self._perform_drag(planned, point, state, started)
        except BlaxcyError as exc:
            return self._deny(started, exc.code, exc.message, planned, data=exc.details)
        return self._deny(
            started, ErrorCode.BACKEND_UNAVAILABLE, f"{tool} has no executor implementation", planned
        )

    def _perform_drag(
        self,
        planned: PlannedAction,
        source_point: Point,
        state: ScreenState,
        started: float,
    ) -> ToolEnvelope | None:
        """Perform a drag with a *freshly resolved* destination (section 49).

        The destination is a live resolution, never a coordinate baked into the
        plan: a drag whose drop point was decided before the screen was observed
        would be exactly the pre-baked-coordinate shortcut section 66.1 forbids.
        """
        destination = _coerce_query(planned.params.get("destination"))
        if destination is None:
            return self._deny(
                started,
                ErrorCode.PERMISSION_DENIED,
                "drag destination must be a target description",
                planned,
            )
        resolved = self._resolver.resolve(
            destination, state.elements, occluders=state.elements, state=state
        )
        if not resolved.is_resolved or resolved.element is None:
            code, message = _resolution_failure(resolved)
            return self._deny(started, code, f"drag destination: {message}", planned)
        drop_point = resolved.element.point_for_input()
        if drop_point is None:
            return self._deny(
                started, ErrorCode.TARGET_STALE, "drag destination has no geometry", planned
            )
        result = self._mouse.drag(source_point, drop_point)
        if not result.ok:
            return self._deny(
                started, ErrorCode.BACKEND_UNAVAILABLE, result.reason or "drag failed", planned
            )
        return None

    def _execute_ensure_window(self, planned: PlannedAction, started: float) -> ToolEnvelope:
        """Section 47: activate a window and verify it actually became active."""
        decision = self._permissions.decide(
            tool=planned.tool,
            mode=self._mode.mode,
            action_class=planned.action_class,
            application=self._application_candidates(self._cache.current, None),
            calibration=self._calibration,
            capability_report=self._capabilities,
            capability=CapabilityName.WINDOW_INFO,
        )
        granted = self._resolve_decision(decision, planned, started)
        if isinstance(granted, ToolEnvelope):
            return granted
        if self._windows is None:
            return self._deny(
                started,
                ErrorCode.BACKEND_UNAVAILABLE,
                "no window manager is wired, so window activation is unavailable",
                planned,
            )
        window_id = int(planned.params.get("window_id", 0))
        with self._tracker.tracking(planned.tool, deadline_seconds=2.0):
            activated = self._windows.ensure_active(
                window_id, timeout_ms=int(planned.params.get("timeout_ms", 600))
            )
        after = self._observe()
        outcome = self._verifier.verify_window(after=after, window_id=window_id)
        self._cache.set_verification(outcome.state)
        data = {"window_id": window_id, "activated": activated, "verification": outcome.to_dict()}
        if not activated or outcome.state is VerificationState.CONTRADICTED:
            return ToolEnvelope.failure(
                ErrorCode.VERIFICATION_CONTRADICTED,
                "the requested window did not become active" if not activated else outcome.reason,
                data=data,
                verification=VerificationState.CONTRADICTED,
                elapsed_ms=elapsed_ms_since(started),
            )
        return ToolEnvelope.success(
            data=data, verification=outcome.state, elapsed_ms=elapsed_ms_since(started)
        )

    # -- Focus / visibility ---------------------------------------------------

    def _ensure_visible(
        self,
        element: UIElement,
        planned: PlannedAction,
        state: ScreenState,
    ) -> tuple[ErrorCode, str] | None:
        """Make sure the target's window is focused (sections 46, 47, 51).

        Only keyboard-input tools depend on focus, and only the window manager
        can change it. When no window manager is wired the check is skipped
        rather than assumed -- the keyboard focus guard (section 51) still fails
        closed on the typing path.
        """
        if planned.tool not in _FOCUS_TOOLS or self._windows is None or element.owner_window_id is None:
            return None
        if state.active_window_id == element.owner_window_id:
            return None
        if not self._windows.ensure_active(element.owner_window_id):
            return (
                ErrorCode.FOCUS_MISMATCH,
                f"could not make window {element.owner_window_id} active before input",
            )
        return None

    # -- Observation ----------------------------------------------------------

    def _fresh_state(self) -> ScreenState:
        """The state to revalidate against, re-perceiving when the cache is stale.

        Raises:
            BlaxcyError: ``CAPTURE_FAILED`` when a fresh observation is required
                but cannot be produced. Acting on a stale observation is never
                the fallback (sections 45, 76).
        """
        current = self._cache.current
        max_age = float(self._settings.verification.max_action_state_age_ms)
        if current is not None and current.is_fresh(max_age):
            return current
        refreshed = self._perceive() if self._perceive is not None else None
        if refreshed is not None:
            return refreshed
        if current is not None:
            return current
        raise BlaxcyError(
            ErrorCode.CAPTURE_FAILED,
            "no fresh perception state is available to revalidate against",
        )

    def _observe(self) -> ScreenState | None:
        """Re-observe the desktop after an action."""
        if self._perceive is not None:
            return self._perceive()
        return self._cache.current

    def _delta_for(self, after: ScreenState | None) -> ScreenDelta | None:
        """The classified delta describing the step to ``after``, when available."""
        delta = self._cache.delta
        if delta is None or after is None or delta.to_frame_id != after.frame_id:
            return None
        return delta

    # -- Verification ---------------------------------------------------------

    def _verify(
        self,
        tool: str,
        planned: PlannedAction,
        element: UIElement | None,
        before: ScreenState | None,
        after: ScreenState | None,
        delta: ScreenDelta | None,
    ) -> VerificationOutcome:
        """Choose the postcondition that matches the action (section 60).

        Typing into a credential field is deliberately unverifiable: the field's
        content is never read back (section 55), so the truthful outcome there is
        ``UNVERIFIED``. With the default ``require_verification_for_mutating``
        that makes a password step report a verification failure rather than a
        success it cannot prove -- and section 55 wants exactly that, because a
        password step in a sequence halts it and returns to per-step handling.
        """
        if tool is ToolName.TYPE_TEXT and element is not None and element.is_text_entry:
            return self._verifier.verify_text(
                after=after, target=element, text=str(planned.params.get("text", ""))
            )
        return self._verifier.verify_screen_change(
            before=before, after=after, delta=delta, target=element, reason_context=tool
        )

    # -- Policy plumbing ------------------------------------------------------

    def _resolve_decision(
        self,
        decision: PolicyDecision,
        planned: PlannedAction,
        started: float,
    ) -> PolicyDecision | ToolEnvelope:
        """Turn a policy decision into a permit, a confirmation, or an envelope."""
        if decision.allowed:
            return decision
        if decision.requires_confirmation:
            approved = False
            if self._confirmation is not None:
                approved = bool(
                    self._confirmation(
                        decision.message or "confirmation required",
                        {"tool": planned.tool, "action_class": planned.action_class.value},
                    )
                )
            if not approved:
                code = ErrorCode.CONFIRMATION_DENIED if self._confirmation is not None else decision.code
                return self._deny(
                    started,
                    code or ErrorCode.CONFIRMATION_REQUIRED,
                    decision.message or "confirmation required",
                    planned,
                )
            return decision
        return self._deny(
            started,
            decision.code or ErrorCode.PERMISSION_DENIED,
            decision.message or "denied by policy",
            planned,
        )

    def _application_candidates(
        self, state: ScreenState | None, element: UIElement | None
    ) -> tuple[str | None, ...]:
        """Application strings for the section 58/42 application lists."""
        candidates: list[str | None] = []
        if element is not None:
            candidates.append(element.owner_app)
        if state is not None:
            candidates.append(state.active_app)
        return tuple(candidates)

    def _abort_code(self) -> ErrorCode | None:
        """The stop code when automation must halt, else ``None``."""
        if self._abort_check is None:
            return None
        return self._abort_check()

    def _require_query(self, planned: PlannedAction) -> ElementQuery:
        """The target description, raising when it is missing."""
        if planned.target is None:
            raise BlaxcyError(
                ErrorCode.TARGET_NOT_FOUND, f"{planned.tool} requires a target description"
            )
        return planned.target

    def _emit(self, event_type: EventType, payload: dict[str, Any]) -> None:
        """Publish an event when a bus is wired."""
        if self._bus is not None:
            self._bus.publish(Event.create(event_type, payload))

    def _deny(
        self,
        started: float,
        code: ErrorCode,
        message: str,
        planned: PlannedAction,
        *,
        data: dict[str, Any] | None = None,
        frame_id: int | None = None,
        state_version: int | None = None,
    ) -> ToolEnvelope:
        """Build a failure envelope and count the refusal."""
        self._denied += 1
        return ToolEnvelope.failure(
            code,
            message,
            data=data or {"tool": planned.tool},
            state_version=state_version,
            frame_id=frame_id,
            elapsed_ms=elapsed_ms_since(started),
        )

    def _annotate(
        self,
        envelope: ToolEnvelope,
        extra: dict[str, Any],
        started: float,
    ) -> ToolEnvelope:
        """Attach recovery evidence and re-stamp the total elapsed time.

        Returns a copy rather than mutating: envelopes are frozen, and a failure
        envelope must keep its ``error_code`` (which is why this never rebuilds
        the envelope from scratch).
        """
        data = dict(envelope.data)
        data.update(extra)
        return envelope.model_copy(update={"data": data, "elapsed_ms": elapsed_ms_since(started)})

    def stats(self) -> dict[str, Any]:
        """A JSON-shaped health snapshot for logs and benchmarks."""
        return {
            "actions": self._actions,
            "denied": self._denied,
            "recovery_attempts": self._recovery_attempts,
            "mode": self._mode.mode.value,
            "tracker": self._tracker.snapshot().to_dict(),
            "recovery": None if self._recovery is None else self._recovery.stats(),
        }


def _resolution_failure(resolution: ResolutionResult) -> tuple[ErrorCode, str]:
    """Map a resolution outcome onto the section 77 taxonomy."""
    if resolution.status is ResolutionStatus.AMBIGUOUS:
        return ErrorCode.TARGET_AMBIGUOUS, resolution.reason
    return ErrorCode.TARGET_NOT_FOUND, resolution.reason


def _target_summary(element: UIElement) -> dict[str, Any]:
    """A safe element summary for the envelope -- never credential content."""
    return {
        "element_id": element.element_id,
        "role": element.role.value,
        "text": None if element.is_password else element.text,
        "accessible_name": element.accessible_name,
        "source": element.source.value,
        "confidence": element.confidence,
        "bbox": element.bbox.to_dict() if element.bbox is not None else None,
        "owner_window_id": element.owner_window_id,
        "password": element.is_password,
    }


def _visible_command(planned: PlannedAction) -> str | None:
    """The literal command text for terminal policy, when supplied.

    Section 54 requires the exact literal command to be visible before it is
    submitted. The executor cannot read a terminal's contents itself, so the
    caller supplies the visible line; without it, a submission is judged on the
    empty string, which the terminal guard treats as requiring confirmation.
    """
    value = planned.params.get("visible_command")
    return str(value) if value is not None else None


def _coerce_query(value: Any) -> ElementQuery | None:
    """Coerce a drag destination description into an :class:`ElementQuery`."""
    if isinstance(value, ElementQuery):
        return value
    if isinstance(value, dict):
        try:
            return ElementQuery.model_validate(value)
        except Exception:
            return None
    if isinstance(value, str):
        return ElementQuery(text=value)
    return None


def _input_capability(tool: str) -> CapabilityName | None:
    """The capability a tool's input depends on (section 28)."""
    if tool in _FOCUS_TOOLS:
        return CapabilityName.KEYBOARD
    if tool in (ToolName.CLICK, ToolName.DOUBLE_CLICK, ToolName.RIGHT_CLICK, ToolName.SCROLL, ToolName.DRAG):
        return CapabilityName.MOUSE
    return None


# ``PointerButton`` is imported for callers that name a button without importing
# the backend package directly.
__all__ = [
    "EXECUTOR_TOOLS",
    "Executor",
    "PointerButton",
    "RevalidationResult",
    "revalidate_target",
]
