"""Bounded recovery (specification sections 4 rule 21, 21, 61).

Recovery is where a body most easily turns a safety property into a bug, so the
rules are explicit and enforced here rather than left to a caller's judgement.

**A failure is classified before anything is retried.** Safety refusals
(``PERMISSION_DENIED``, ``BLOCKED_APPLICATION``, a latched stop) are never
retried: they are decisions, not accidents, and re-asking cannot change them.
An ambiguous target is never retried either -- the Brain has to disambiguate.
Only ``TARGET`` failures (stale/not-found/occluded, all of which genuinely mean
"look again") and ``TRANSIENT`` ones (a timeout, a rate limit) are candidates.

**Destructive actions are never automatically retried** (section 4 rule 21).
This holds even when the failure happened *before* any input, because "the drag
did not resolve its drop target" and "the drag half-happened" are not always
distinguishable from the failure code alone, and the cost of being wrong is
irreversible.

**Anything that already injected input is never retried.** A verification
failure means the action happened; re-running it would act on the desktop twice.

**Every budget is consumed, never reset by luck.** Attempts are counted per tool
and per *task step*, and a sequence step gets its own budget rather than sharing
one pool with the whole sequence (section 61): step 4 must not be able to spend
step 1's budget, and step 1 must not be able to starve step 4.

**A loop guard exists because retries are the classic infinite loop.** The same
tool with the same arguments three times is not a retry any more, it is a stuck
loop, and it stops.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from config.settings import RecoverySettings
from schemas.enums import ActionClass, ErrorCode

#: Failures that only ever mean "the world moved; look again". These are the
#: only codes a bounded automatic retry may act on.
_TARGET_CODES: frozenset[ErrorCode] = frozenset(
    {
        ErrorCode.TARGET_NOT_FOUND,
        ErrorCode.TARGET_STALE,
        ErrorCode.TARGET_OCCLUDED,
        ErrorCode.TARGET_OFFSCREEN,
    }
)

#: Transient infrastructure hiccups; a re-perception and a second attempt can
#: genuinely fix these.
_TRANSIENT_CODES: frozenset[ErrorCode] = frozenset(
    {
        ErrorCode.A11Y_TIMEOUT,
        ErrorCode.OCR_FAILED,
        ErrorCode.RATE_LIMITED,
    }
)

#: Decisions and latches. Retrying cannot change any of them, and several are
#: hard stops that must stay stopped.
_SAFETY_CODES: frozenset[ErrorCode] = frozenset(
    {
        ErrorCode.PERMISSION_DENIED,
        ErrorCode.BLOCKED_APPLICATION,
        ErrorCode.CONFIRMATION_REQUIRED,
        ErrorCode.CONFIRMATION_DENIED,
        ErrorCode.EMERGENCY_STOP_ACTIVE,
        ErrorCode.HUMAN_TAKEOVER,
        ErrorCode.VISUAL_FALLBACK_DENIED,
        ErrorCode.TARGET_AMBIGUOUS,
    }
)

#: Post-action outcomes. The action already ran, so nothing here is retried.
_VERIFICATION_CODES: frozenset[ErrorCode] = frozenset(
    {
        ErrorCode.VERIFICATION_UNVERIFIED,
        ErrorCode.VERIFICATION_CONTRADICTED,
    }
)

#: Infrastructure that will not come back because we asked again. The subsystems
#: behind these already performed their own bounded reinitialisation.
_BACKEND_CODES: frozenset[ErrorCode] = frozenset(
    {
        ErrorCode.BACKEND_UNAVAILABLE,
        ErrorCode.CAPTURE_FAILED,
        ErrorCode.CALIBRATION_FAILED,
        ErrorCode.CLIPBOARD_FAILED,
        ErrorCode.UNICODE_UNSUPPORTED,
        ErrorCode.UNSUPPORTED_PLATFORM,
    }
)

#: Programmer/contract errors. Retrying a bug just does the bug again.
_FATAL_CODES: frozenset[ErrorCode] = frozenset(
    {
        ErrorCode.INTERNAL_ERROR,
        ErrorCode.MODEL_ERROR,
        ErrorCode.SEQUENCE_HALTED,
        ErrorCode.SEQUENCE_STEP_LIMIT_EXCEEDED,
        ErrorCode.SEQUENCE_WALL_CLOCK_EXCEEDED,
        ErrorCode.NOT_EXECUTED,
    }
)


class FailureClass(StrEnum):
    """How a failure should be treated by recovery (section 21)."""

    SAFETY = "SAFETY"
    TARGET = "TARGET"
    TRANSIENT = "TRANSIENT"
    VERIFICATION = "VERIFICATION"
    BACKEND = "BACKEND"
    FATAL = "FATAL"
    UNKNOWN = "UNKNOWN"


#: The bounded recovery actions a class justifies (section 21's list).
RECOVERY_ACTIONS: dict[FailureClass, tuple[str, ...]] = {
    FailureClass.TARGET: ("re_perceive", "re_resolve", "revalidate"),
    FailureClass.TRANSIENT: ("re_perceive", "retry"),
}


def classify_failure(code: ErrorCode | None) -> FailureClass:
    """Classify a failure code (section 21).

    An unknown or ``None`` code is ``UNKNOWN`` and is not retried: a failure we
    cannot classify is not one we are entitled to retry.
    """
    if code is None:
        return FailureClass.UNKNOWN
    if code in _SAFETY_CODES:
        return FailureClass.SAFETY
    if code in _TARGET_CODES:
        return FailureClass.TARGET
    if code in _TRANSIENT_CODES:
        return FailureClass.TRANSIENT
    if code in _VERIFICATION_CODES:
        return FailureClass.VERIFICATION
    if code in _BACKEND_CODES:
        return FailureClass.BACKEND
    if code in _FATAL_CODES:
        return FailureClass.FATAL
    return FailureClass.UNKNOWN


@dataclass(frozen=True)
class RecoveryDecision:
    """Whether a bounded recovery attempt is allowed, and why."""

    should_attempt: bool
    failure_class: FailureClass
    actions: tuple[str, ...] = ()
    reason: str = ""
    tool_attempts_left: int = 0
    step_attempts_left: int = 0

    def to_dict(self) -> dict[str, Any]:
        """JSON-shaped decision for the envelope and logs."""
        return {
            "should_attempt": self.should_attempt,
            "failure_class": self.failure_class.value,
            "actions": list(self.actions),
            "reason": self.reason,
            "tool_attempts_left": self.tool_attempts_left,
            "step_attempts_left": self.step_attempts_left,
        }


def arguments_digest(tool: str, arguments: dict[str, Any] | None) -> str:
    """A stable digest of one call, for the loop guard.

    ``sort_keys`` makes argument order irrelevant; values are stringified so an
    unhashable argument cannot raise inside a safety mechanism.
    """
    payload = json.dumps(arguments or {}, sort_keys=True, default=str)
    return f"{tool}|{payload}"


class RecoveryController:
    """Owns the recovery budgets and the loop guard (section 61).

    Args:
        settings: The ``[recovery]`` configuration: automatic attempts per tool,
            attempts per task step, and the identical-call loop guard.
    """

    def __init__(self, settings: RecoverySettings | None = None) -> None:
        self._settings = settings if settings is not None else RecoverySettings()
        self._tool_attempts: dict[str, int] = {}
        self._call_counts: dict[str, int] = {}
        self._step_attempts = 0
        self._last_identity: str | None = None
        self._task_id: str | None = None
        self._step_id: str | None = None
        self._attempts_total = 0
        self._refusals = 0

    # -- Task / step scoping --------------------------------------------------

    def begin_task(self, task_id: str | None = None) -> None:
        """Start a task: per-tool budgets and the loop guard reset here.

        The loop guard is a property of a task, not of the process, so a new
        task is allowed to repeat a call the previous task already made.
        """
        self._task_id = task_id
        self._tool_attempts.clear()
        self._call_counts.clear()
        self._step_attempts = 0
        self._last_identity = None

    def begin_step(self, step_id: str | None = None) -> None:
        """Start a task step, giving it its **own** attempt budget (section 61).

        A ``run_sequence`` step calls this before it runs, so step 4 can never
        spend step 1's budget and vice versa.
        """
        self._step_id = step_id
        self._step_attempts = 0

    def end_step(self) -> None:
        """Finish the current step. The per-step budget is discarded."""
        self._step_id = None
        self._step_attempts = 0

    @property
    def task_id(self) -> str | None:
        """The active task identifier, or ``None``."""
        return self._task_id

    @property
    def step_id(self) -> str | None:
        """The active step identifier, or ``None``."""
        return self._step_id

    # -- Identity -------------------------------------------------------------

    def note_identity(self, identity: str | None) -> bool:
        """Record the target identity an attempt resolved to.

        Returns:
            ``True`` when the identity changed materially since the previous
            attempt, which is the signal to stop retrying (section 61).
        """
        previous = self._last_identity
        self._last_identity = identity
        if previous is None or identity is None:
            return False
        return previous != identity

    # -- Decisions ------------------------------------------------------------

    def classify(self, code: ErrorCode | None) -> FailureClass:
        """Classify a failure code."""
        return classify_failure(code)

    def should_attempt(
        self,
        *,
        code: ErrorCode | None,
        action_class: ActionClass,
        tool: str,
        arguments: dict[str, Any] | None = None,
        input_performed: bool = False,
        identity: str | None = None,
    ) -> RecoveryDecision:
        """Decide whether a bounded recovery attempt is permitted.

        Args:
            code: The failure the attempt would be recovering from.
            action_class: The action class, which vetoes retries for destructive
                work (section 4 rule 21).
            tool: The tool being retried.
            arguments: Its arguments, for the loop guard.
            input_performed: Whether the failed action already injected input.
                When it did, the failure is a verification outcome and the action
                is never re-run automatically.
            identity: The target identity the failed attempt resolved to, if any.
        """
        failure_class = classify_failure(code)
        digest = arguments_digest(tool, arguments)
        tool_left = max(0, self._settings.automatic_attempts_per_tool - self._tool_attempts.get(tool, 0))
        step_left = max(0, self._settings.attempts_per_task_step - self._step_attempts)

        def refuse(reason: str) -> RecoveryDecision:
            self._refusals += 1
            return RecoveryDecision(False, failure_class, (), reason, tool_left, step_left)

        if input_performed:
            return refuse("the action already injected input; it is never re-run automatically")
        if failure_class is FailureClass.SAFETY:
            return refuse("a safety refusal is a decision, not an accident (section 61)")
        if action_class is ActionClass.DESTRUCTIVE:
            return refuse("destructive actions are never blindly retried (section 4 rule 21)")
        if failure_class not in RECOVERY_ACTIONS:
            return refuse(f"{failure_class.value} failures are not retryable")
        if identity is not None and self._last_identity is not None and identity != self._last_identity:
            return refuse("the target identity changed materially; retrying would act on a different control")
        if self._call_counts.get(digest, 0) >= self._settings.identical_call_loop_guard:
            return refuse(
                f"the same call has been made {self._call_counts[digest]} times; loop guard"
            )
        if tool_left <= 0:
            return refuse(f"no automatic attempts remain for {tool}")
        if step_left <= 0:
            return refuse("no attempts remain for this task step")

        return RecoveryDecision(
            should_attempt=True,
            failure_class=failure_class,
            actions=RECOVERY_ACTIONS[failure_class],
            reason=f"bounded recovery for a {failure_class.value} failure",
            tool_attempts_left=tool_left,
            step_attempts_left=step_left,
        )

    # -- Accounting -----------------------------------------------------------

    def record_attempt(
        self,
        *,
        tool: str,
        arguments: dict[str, Any] | None = None,
        identity: str | None = None,
    ) -> None:
        """Consume one attempt against every applicable budget."""
        self._tool_attempts[tool] = self._tool_attempts.get(tool, 0) + 1
        self._step_attempts += 1
        digest = arguments_digest(tool, arguments)
        self._call_counts[digest] = self._call_counts.get(digest, 0) + 1
        self._attempts_total += 1
        if identity is not None:
            self._last_identity = identity

    def attempts_for(self, tool: str) -> int:
        """How many automatic attempts ``tool`` has consumed in this task."""
        return self._tool_attempts.get(tool, 0)

    def call_count(self, tool: str, arguments: dict[str, Any] | None = None) -> int:
        """How many times this exact call has been seen in this task."""
        return self._call_counts.get(arguments_digest(tool, arguments), 0)

    def stats(self) -> dict[str, Any]:
        """A JSON-shaped snapshot for logs and benchmarks."""
        return {
            "task_id": self._task_id,
            "step_id": self._step_id,
            "attempts_total": self._attempts_total,
            "refusals": self._refusals,
            "tool_attempts": dict(self._tool_attempts),
            "step_attempts": self._step_attempts,
            "watched_calls": len(self._call_counts),
            "automatic_attempts_per_tool": self._settings.automatic_attempts_per_tool,
            "attempts_per_task_step": self._settings.attempts_per_task_step,
            "identical_call_loop_guard": self._settings.identical_call_loop_guard,
        }


__all__ = [
    "RECOVERY_ACTIONS",
    "FailureClass",
    "RecoveryController",
    "RecoveryDecision",
    "arguments_digest",
    "classify_failure",
]
