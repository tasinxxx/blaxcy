"""The strict, versioned task and result schemas for the local bridge.

This module is the *external task boundary* — the seam where a high-level task
from a future local automation runner (or CI job) enters BLAXCY. It is **not**
the tool boundary (`ai.tool_protocol`), and it deliberately cannot express most
of what a tool call can:

* **A task is exactly one plan.** The only executable payload is ``plan``: an
  ordered :class:`~schemas.sequences.SequenceStep` list executed through the
  existing ``run_sequence`` tool. There is no free-form tool dispatch from a
  task, no single-action bypass, no ``halt_on`` override — the default safety
  halt set always applies (specification §66.1).
* **A task carries no pre-resolved values.** ``SequenceStep`` already rejects
  coordinates, ``element_id``/``lease_id``/``frame_id``/``state_version``/
  ``generation`` at parse time, and this schema adds a belt-and-braces scan of
  the whole raw payload so a hostile or buggy producer cannot smuggle one in
  anywhere (they are rejected before a model is ever built).
* **A task cannot carry secrets.** Password-shaped keys are refused outright;
  there is no tool on the task path that could use them, and a task that names
  one is rejected rather than accepted-and-ignored.
* **Every result is serializable.** :meth:`TaskEnvelope.to_payload` produces the
  JSON document the local boundary returns; nothing here holds a reference to
  BLAXCY internals (no leases, no screen states, no event objects).

The schema version exists so a runner can pin the contract it was built against
and refuse an incompatible bridge loudly instead of misinterpreting it.
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from schemas.sequences import (
    DEFAULT_HALT_CONDITIONS,
    FORBIDDEN_STEP_KEYS,
    SequenceStep,
    SequenceStepResult,
)

#: The task contract this module implements. Bump only with a migration: a
#: runner pinned to ``1`` must be able to tell that a bridge speaks a different
#: contract rather than guessing field semantics.
TASK_SCHEMA_VERSION: int = 1

#: Maximum serialized size of one raw task document, before parsing. A task is
#: a plan, not a bulk transfer; the ceiling exists so an oversized payload is a
#: clean rejection rather than a memory event on the receiving side.
MAX_TASK_PAYLOAD_BYTES: int = 256 * 1024

#: Maximum steps per task plan. The bridge inherits the sequence runner's own
#: runtime cap too (``[sequence] max_sequence_steps``); this is the earlier,
#: cheaper parse-time bound so an absurd plan is refused before validation.
MAX_TASK_STEPS: int = 48

#: Key fragments that mark a payload as carrying credential material. Matched
#: case-insensitively against every key anywhere in the raw task document.
#: ``token``/``secret``/``api_key`` style keys have no place in a task, and a
#: producer that includes one is either leaking a secret or testing the guard.
_PASSWORD_KEY_FRAGMENTS: tuple[str, ...] = (
    "password",
    "passwd",
    "credential",
    "api_key",
    "apikey",
    "secret",
    "token",
    "authorization",
)

#: Allowed top-level keys of a raw task document. Anything else is rejected:
#: an unknown field in a strict contract is a producer bug, and silently
#: ignoring it would mean the task runs with semantics the producer did not
#: intend.
_ALLOWED_TASK_KEYS: frozenset[str] = frozenset(
    {"schema_version", "task_id", "plan", "created_at_ms", "operator_approved"}
)


class TaskStatus(StrEnum):
    """The lifecycle state a bridge-reported task ended in."""

    #: The task ran through the sequence runner and every executed step
    #: completed without a halt (individual steps may still report a
    #: verification state; ``halted`` is the flag that matters).
    COMPLETED = "COMPLETED"
    #: The sequence runner halted on a safety/verification condition (§66.1).
    #: The structured reason travels in the envelope's halt fields.
    HALTED = "HALTED"
    #: The task was rejected before any dispatch: schema validation, an unsafe
    #: or oversized payload, or a bridge precondition (runner missing).
    REJECTED = "REJECTED"
    #: The bridge's own wall-clock ceiling elapsed before the task returned.
    #: BLAXCY's emergency stop is triggered as part of this outcome, so an
    #: abandoned task cannot keep controlling the desktop behind the caller's
    #: back.
    TIMED_OUT = "TIMED_OUT"


class TaskValidationError(Exception):
    """A task the bridge refused before any dispatch (``REJECTED``).

    Carries a machine-readable reason so the producing side can fix its payload
    without parsing prose.
    """

    def __init__(self, reason: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.details: dict[str, Any] = details or {}


class TaskTimeoutError(Exception):
    """The bridge's wall-clock ceiling elapsed (``TIMED_OUT``).

    ``stop_result`` carries the outcome of the emergency stop the bridge
    triggered on expiry (``None`` when no stop object was wired): the result
    must report whether the desktop was really taken back, never assume it.
    """

    def __init__(
        self,
        message: str,
        *,
        task_id: str | None,
        elapsed_ms: float,
        stop_result: Any = None,
    ) -> None:
        super().__init__(message)
        self.task_id = task_id
        self.elapsed_ms = elapsed_ms
        self.stop_result = stop_result


class Task(BaseModel):
    """One validated high-level task: a versioned, ordered plan.

    Construct via :meth:`Task.parse` for the boundary (raw dict in, strict model
    or a structured rejection out). Direct construction is possible but skips
    the raw-payload guards (size, unknown keys, forbidden keys), which is why
    the bridge only ever calls ``parse``.

    ``plan`` is the existing :class:`~schemas.sequences.SequenceStep`, unchanged:
    the bridge introduces **no** new step vocabulary, so every runner-side
    guarantee (unique ids, description-only targets, no nesting, no
    pre-resolved values) is inherited verbatim and test-pinned by the core.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Deliberately **required**, not defaulted: a versioned contract only works
    #: if the producer states which version it speaks. A missing version is a
    #: producer bug and is rejected, never silently filled in.
    schema_version: int
    task_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    plan: tuple[SequenceStep, ...] = Field(min_length=1, max_length=MAX_TASK_STEPS)
    created_at_ms: int | None = Field(default=None, ge=0)
    operator_approved: bool = Field(
        default=False,
        description="Explicit approval supplied by the trusted Agent producer for this task.",
    )

    @field_validator("schema_version")
    @classmethod
    def _version_must_match(cls, value: int) -> int:
        if value != TASK_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported task schema_version {value}; this bridge speaks {TASK_SCHEMA_VERSION}"
            )
        return value

    @model_validator(mode="after")
    def _step_ids_must_be_unique(self) -> Task:
        """Duplicate step ids are malformed (and the runner would refuse them later
        at dispatch); catching them at parse gives the producer the specific
        reason instead of a generic dispatch refusal."""
        ids = [step.step_id for step in self.plan]
        duplicates = sorted({step_id for step_id in ids if ids.count(step_id) > 1})
        if duplicates:
            raise ValueError(f"duplicate step_id values: {duplicates}")
        return self

    # -- The boundary entry point ---------------------------------------------

    @classmethod
    def parse(cls, payload: Any) -> Task:
        """Parse and validate one raw task document, or raise ``TaskValidationError``.

        The guards run in cheap-first order so the most common producer mistakes
        get the most specific reason:

        1. the payload must be a JSON object of bounded size;
        2. every top-level key must be part of the contract;
        3. no key anywhere may look like a credential field;
        4. no key anywhere may be a pre-resolved coordinate/identity/lease
           field (the same rule the sequence schema enforces per step, applied
           to the whole document);
        5. the document must satisfy the strict pydantic model.
        """
        if isinstance(payload, (str, bytes)):
            size = len(payload)
            if size > MAX_TASK_PAYLOAD_BYTES:
                raise TaskValidationError(
                    "TASK_TOO_LARGE",
                    {"size_bytes": size, "max_bytes": MAX_TASK_PAYLOAD_BYTES},
                )
            try:
                payload = json.loads(payload)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise TaskValidationError("TASK_NOT_JSON", {"error": str(exc)}) from exc
        if not isinstance(payload, dict):
            raise TaskValidationError("TASK_NOT_AN_OBJECT", {"type": type(payload).__name__})

        # The security scan runs *before* the contract check: a credential-shaped
        # or pre-resolved key must be named as such, not merely as "unknown" —
        # the producer needs to know the payload was refused for what it carried,
        # not just that a key was unexpected.
        offending = _find_forbidden_keys(payload)
        if offending is not None:
            key, kind = offending
            raise TaskValidationError(
                "FORBIDDEN_TASK_FIELD" if kind == "secret" else "PRE_RESOLVED_TASK_FIELD",
                {"key": key},
            )

        unknown = sorted(str(key) for key in payload if str(key) not in _ALLOWED_TASK_KEYS)
        if unknown:
            raise TaskValidationError("UNKNOWN_TASK_FIELDS", {"unknown": unknown})

        try:
            return cls.model_validate(payload)
        except Exception as exc:  # pydantic.ValidationError and friends
            raise TaskValidationError(
                "TASK_SCHEMA_INVALID", {"errors": _validation_errors(exc)}
            ) from exc


def _find_forbidden_keys(value: Any) -> tuple[str, str] | None:
    """The first credential-shaped or pre-resolved key anywhere in ``value``.

    Scans every mapping key at every depth (lists included) so a forbidden key
    cannot hide inside a nested structure. ``SequenceStep`` collects unknown
    step keys into ``args`` before validating, so a scan of only top-level keys
    would miss a smuggled ``element_id`` inside a step — the scan is the layer
    that makes the whole document safe, and the per-step validation is the
    second, independent net.
    """
    if isinstance(value, dict):
        for key, inner in value.items():
            lowered = str(key).casefold()
            if any(fragment in lowered for fragment in _PASSWORD_KEY_FRAGMENTS):
                return str(key), "secret"
            if lowered in FORBIDDEN_STEP_KEYS:
                return str(key), "preresolved"
            found = _find_forbidden_keys(inner)
            if found is not None:
                return found
        return None
    if isinstance(value, (list, tuple)):
        for item in value:
            found = _find_forbidden_keys(item)
            if found is not None:
                return found
    return None


def _validation_errors(exc: Exception) -> list[dict[str, Any]]:
    """A compact, serializable summary of a validation failure."""
    errors: list[dict[str, Any]] = getattr(exc, "errors", lambda **_: [])()
    return [
        {"path": ".".join(str(part) for part in error.get("loc", ())), "type": error.get("type")}
        for error in errors
    ]


class StepResult(SequenceStepResult):
    """One executed step's result — the core's own result, unchanged.

    Re-exported under a task-protocol name for readability; there is no new
    field and no translation, so a step's envelope is byte-compatible with the
    ``run_sequence`` tool's own per-step results.
    """


class TaskResult(BaseModel):
    """The strict result document for one submitted task.

    Fields map one-to-one onto the core's :class:`~schemas.sequences.SequenceResult`
    (the bridge adds nothing to a run) plus the task identity and the bridge's
    own lifecycle status. ``steps`` are ordered exactly like the plan; steps the
    runner never reached carry ``error_code == "NOT_EXECUTED"``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = TASK_SCHEMA_VERSION
    task_id: str
    status: TaskStatus
    #: ``None`` only for a pre-dispatch rejection (no sequence exists yet).
    sequence_id: str | None = None
    halted: bool = False
    halt_reason: str | None = None
    halt_code: str | None = None
    wall_clock_ms: float = Field(default=0.0, ge=0.0)
    completed_count: int = Field(default=0, ge=0)
    steps: tuple[StepResult, ...] = ()
    #: Present only when ``status`` is ``REJECTED``: the machine-readable
    #: validation reason and its details.
    rejection_reason: str | None = None
    rejection_details: dict[str, Any] | None = None
    #: Present only when ``status`` is ``TIMED_OUT``: how long the task had run
    #: when the bridge stopped waiting, and whether the emergency stop it then
    #: triggers actually latched (reported honestly, never assumed).
    timeout_elapsed_ms: float | None = Field(default=None, ge=0.0)
    timeout_stop_latched: bool | None = None

    @model_validator(mode="after")
    def _validate_status(self) -> TaskResult:
        if self.status is TaskStatus.REJECTED:
            if not self.rejection_reason:
                raise ValueError("a REJECTED result must carry rejection_reason")
            if self.steps:
                raise ValueError("a REJECTED result has no step results")
        if self.status is TaskStatus.TIMED_OUT:
            if self.timeout_elapsed_ms is None:
                raise ValueError("a TIMED_OUT result must carry timeout_elapsed_ms")
            if self.timeout_stop_latched is None:
                raise ValueError("a TIMED_OUT result must carry timeout_stop_latched")
        if self.status is TaskStatus.COMPLETED and (self.halted or self.halt_reason):
            raise ValueError("a COMPLETED result cannot carry a halt")
        return self

    def to_payload(self) -> dict[str, Any]:
        """The JSON document the local boundary returns for this task."""
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "status": self.status.value,
            "sequence_id": self.sequence_id,
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "halt_code": self.halt_code,
            "wall_clock_ms": self.wall_clock_ms,
            "completed_count": self.completed_count,
            "steps": [
                {
                    **step.to_dict(),
                    "step_id": step.step_id,
                    "index": step.index,
                    "tool": step.tool,
                    "action_class": step.action_class.value,
                    "require_verification": step.require_verification,
                }
                for step in self.steps
            ],
            "rejection_reason": self.rejection_reason,
            "rejection_details": self.rejection_details,
            "timeout_elapsed_ms": self.timeout_elapsed_ms,
            "timeout_stop_latched": self.timeout_stop_latched,
        }


class TaskEnvelope(BaseModel):
    """The complete local-boundary response for one task submission.

    ``envelope`` is the core ``run_sequence`` tool envelope (uniform §66 shape,
    including ``ok``/``error_code``/``verification``); ``result`` is the task-
    level view. A rejected or timed-out task carries ``envelope=None``: there
    is no tool envelope because no tool ran, and inventing one would fake an
    execution (§4 rule 8).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str
    envelope: dict[str, Any] | None = None
    result: TaskResult

    def to_payload(self) -> dict[str, Any]:
        """The JSON document the local boundary returns."""
        return {
            "task_id": self.task_id,
            "envelope": self.envelope,
            "result": self.result.to_payload(),
        }


__all__ = [
    "DEFAULT_HALT_CONDITIONS",
    "MAX_TASK_PAYLOAD_BYTES",
    "MAX_TASK_STEPS",
    "TASK_SCHEMA_VERSION",
    "SequenceStep",
    "StepResult",
    "Task",
    "TaskEnvelope",
    "TaskResult",
    "TaskStatus",
    "TaskTimeoutError",
    "TaskValidationError",
]
