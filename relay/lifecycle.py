"""The task lifecycle state machine for the GitHub relay (Phase 2B).

The relay hands each task a single, strictly ordered lifecycle, and every
transition through it is explicit, validated, and recorded. A task may never
skip a stage, never go backwards, and never leave a terminal state — a claim of
``PENDING → COMPLETED`` with no ``RUNNING`` record in between is refused, not
smoothed over, because an unexplained transition is exactly what a replay, a
racing worker, or a tampered result looks like.

States (requirement 10):

``PENDING → RUNNING → { COMPLETED | HALTED | REJECTED | TIMED_OUT | FAILED }``

``FAILED`` is the relay's own state for *infrastructure* failure — the task
may have been perfectly valid and still never ran (the Body could not be
reached, the process died). ``REJECTED``/``HALTED``/``TIMED_OUT`` are the
bridge's own outcomes; ``FAILED`` is what the relay reports when *it* failed,
so a producer can tell "BLAXCY refused this" from "the relay never got to ask".

Transitions are the security mechanism here, not just bookkeeping: the
duplicate/replay guard (requirement 9) and the fail-closed rule (requirement 11)
are both enforced by refusing transitions, so the state machine and the record
store in :mod:`relay.store` are checked against each other on every operation.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class TaskLifecycle(StrEnum):
    """Every state a relay-tracked task can occupy."""

    #: Recorded in the store, never claimed, never executed.
    PENDING = "PENDING"
    #: Claimed by exactly one worker (a claim is the only path into RUNNING).
    RUNNING = "RUNNING"
    #: The LocalBridge reported every executed step completed without a halt.
    COMPLETED = "COMPLETED"
    #: The LocalBridge reported a section 66.1 halt (ambiguity, confirmation,
    #: stop, takeover, blocked app, ...). Unreached steps are NOT_EXECUTED.
    HALTED = "HALTED"
    #: Refused before any dispatch: malformed, unsafe, unauthenticated,
    #: duplicate, or the runner was unavailable.
    REJECTED = "REJECTED"
    #: The bridge wall-clock ceiling elapsed; the emergency stop ran.
    TIMED_OUT = "TIMED_OUT"
    #: The relay itself failed: the Body could not start, the process died,
    #: the result could not be produced. Never a desktop outcome.
    FAILED = "FAILED"


#: The five terminal states. Once a task is in one of these, every further
#: transition is refused: a completed task cannot be re-run, a rejected task
#: cannot be re-validated, and a FAILED task cannot be silently promoted.
TERMINAL_STATES: Final[frozenset[TaskLifecycle]] = frozenset(
    {
        TaskLifecycle.COMPLETED,
        TaskLifecycle.HALTED,
        TaskLifecycle.REJECTED,
        TaskLifecycle.TIMED_OUT,
        TaskLifecycle.FAILED,
    }
)

PENDING: Final[TaskLifecycle] = TaskLifecycle.PENDING
RUNNING: Final[TaskLifecycle] = TaskLifecycle.RUNNING

#: The complete legal transition table. Anything not listed here is refused
#: (fail closed, requirement 11) — there is no "unknown transition" fallback
#: that quietly accepts.
LEGAL_TRANSITIONS: Final[dict[TaskLifecycle, frozenset[TaskLifecycle]]] = {
    TaskLifecycle.PENDING: frozenset({TaskLifecycle.RUNNING}),
    # A RUNNING task ends in exactly one terminal state. FAILED is reachable
    # from RUNNING because "the relay died mid-claim" is a real outcome; the
    # other terminals come from the bridge's own result statuses.
    TaskLifecycle.RUNNING: frozenset(
        {
            TaskLifecycle.COMPLETED,
            TaskLifecycle.HALTED,
            TaskLifecycle.REJECTED,
            TaskLifecycle.TIMED_OUT,
            TaskLifecycle.FAILED,
        }
    ),
    # Terminal states transition to nothing.
    TaskLifecycle.COMPLETED: frozenset(),
    TaskLifecycle.HALTED: frozenset(),
    TaskLifecycle.REJECTED: frozenset(),
    TaskLifecycle.TIMED_OUT: frozenset(),
    TaskLifecycle.FAILED: frozenset(),
}

#: Every non-terminal state.
NON_TERMINAL_STATES: Final[frozenset[TaskLifecycle]] = frozenset(
    {TaskLifecycle.PENDING, TaskLifecycle.RUNNING}
)


class LifecycleError(Exception):
    """A transition the state machine refuses (fail closed, requirement 11)."""

    def __init__(self, message: str, *, task_id: str, current: TaskLifecycle, attempted: TaskLifecycle) -> None:
        super().__init__(message)
        self.task_id = task_id
        self.current = current
        self.attempted = attempted


def can_transition(current: TaskLifecycle, attempted: TaskLifecycle) -> bool:
    """Whether ``current → attempted`` is a legal transition."""
    return attempted in LEGAL_TRANSITIONS.get(current, frozenset())


def require_transition(
    current: TaskLifecycle, attempted: TaskLifecycle, *, task_id: str
) -> None:
    """Enforce one transition, raising :class:`LifecycleError` when illegal.

    Every caller treats a refusal as *final*: the task keeps its current state,
    and the refused transition is reported — never partially applied, never
    retried automatically (requirement 12).
    """
    if not isinstance(current, TaskLifecycle) or not isinstance(attempted, TaskLifecycle):
        raise LifecycleError(
            "lifecycle states must be TaskLifecycle values",
            task_id=task_id,
            current=current,
            attempted=attempted,
        )
    if current is attempted:
        raise LifecycleError(
            f"task is already {current.value}; a redundant transition is refused",
            task_id=task_id,
            current=current,
            attempted=attempted,
        )
    if not can_transition(current, attempted):
        raise LifecycleError(
            f"illegal transition {current.value} -> {attempted.value}",
            task_id=task_id,
            current=current,
            attempted=attempted,
        )


__all__ = [
    "LEGAL_TRANSITIONS",
    "NON_TERMINAL_STATES",
    "PENDING",
    "RUNNING",
    "TERMINAL_STATES",
    "LifecycleError",
    "TaskLifecycle",
    "can_transition",
    "require_transition",
]
