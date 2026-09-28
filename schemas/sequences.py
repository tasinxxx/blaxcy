"""``run_sequence`` schemas (specification section 66.1).

The batching layer lets the Brain submit an ordered plan in one call. These
schemas enforce the two rules that keep batching from becoming a shortcut:

1. **A step is a description, never a pre-resolved coordinate or lease.** A
   ``target`` that looks like a coordinate, or arguments carrying
   ``element_id``/``lease_id``/``frame_id``/etc., are rejected at parse time.
   A lease issued before its step begins is stale by construction (section 44),
   so accepting one would silently weaken revalidation (section 45).
2. **The halt set can only narrow, never widen past the default safety set.**
   ``halt_on`` may be a *subset* of :data:`DEFAULT_HALT_CONDITIONS`; it can
   never introduce a halt that removes a safety stop.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from schemas.actions import (
    SEQUENCE_STEP_TOOLS,
    ToolEnvelope,
    ToolName,
    action_class_for,
    most_restrictive_action_class,
    requires_verification,
)
from schemas.enums import ActionClass, ErrorCode, UIRole


class HaltCondition(StrEnum):
    """Conditions that make a ``run_sequence`` stop and report (section 66.1)."""

    TARGET_AMBIGUOUS = "AMBIGUOUS"
    VERIFICATION_CONTRADICTED = "CONTRADICTED"
    VERIFICATION_UNVERIFIED = "UNVERIFIED"
    CONFIRMATION_REQUIRED = "CONFIRMATION_REQUIRED"
    BLOCKED_APPLICATION = "BLOCKED_APPLICATION"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    PASSWORD_CONTEXT = "PASSWORD_CONTEXT"
    HUMAN_TAKEOVER = "HUMAN_TAKEOVER"
    EMERGENCY_STOP_ACTIVE = "EMERGENCY_STOP_ACTIVE"
    CALIBRATION_FAILED = "CALIBRATION_FAILED"
    TARGET_OCCLUDED = "TARGET_OCCLUDED"
    BACKEND_UNAVAILABLE = "BACKEND_UNAVAILABLE"
    CAPTURE_FAILED = "CAPTURE_FAILED"
    A11Y_TIMEOUT = "A11Y_TIMEOUT"
    TARGET_STALE = "TARGET_STALE"
    TARGET_NOT_FOUND = "TARGET_NOT_FOUND"


#: The complete default safety halt set (section 66.1). ``halt_on`` may only
#: ever be a subset of this.
DEFAULT_HALT_CONDITIONS: frozenset[HaltCondition] = frozenset(HaltCondition)

#: Argument keys that would mean a pre-resolved coordinate/identity/lease, and
#: are therefore forbidden inside a sequence step.
FORBIDDEN_STEP_KEYS: frozenset[str] = frozenset(
    {
        "x",
        "y",
        "px",
        "py",
        "coord",
        "coords",
        "coordinate",
        "coordinates",
        "point",
        "position",
        "screen_x",
        "screen_y",
        "desktop_x",
        "desktop_y",
        "input_x",
        "input_y",
        "element_id",
        "elementid",
        "lease",
        "lease_id",
        "leaseid",
        "element_lease",
        "frame_id",
        "state_version",
        "generation",
    }
)

_COORDINATE_TARGET_RE = re.compile(r"^\s*[-+]?\d+(?:\.\d+)?\s*[,;]\s*[-+]?\d+(?:\.\d+)?\s*$")
_COORDINATE_PREFIX_RE = re.compile(r"^\s*[xy]\s*=", re.IGNORECASE)


class SequenceStep(BaseModel):
    """One declared step in a sequence: a tool plus a target *description*.

    Tool-specific arguments (``text``, ``key``, ``amount``, ...) are captured in
    ``args`` so the on-the-wire JSON stays exactly the shape of the standalone
    tool call, while the schema still validates the safety-relevant parts.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    step_id: str = Field(min_length=1)
    tool: str
    target: str | None = None
    role: UIRole | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    require_verification: bool | None = Field(
        default=None,
        description="None means 'use the default for this step's action class'.",
    )

    @model_validator(mode="before")
    @classmethod
    def _collect_args(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        known = {"step_id", "tool", "target", "role", "args", "require_verification"}
        collected: dict[str, Any] = dict(data.get("args") or {})
        for key in list(data):
            if key not in known:
                collected[key] = data[key]
        normalized = {key: value for key, value in data.items() if key in known}
        normalized["args"] = collected
        return normalized

    @model_validator(mode="after")
    def _validate_step(self) -> SequenceStep:
        if self.tool not in SEQUENCE_STEP_TOOLS:
            raise ValueError(
                f"tool {self.tool!r} cannot be a sequence step "
                "(run_sequence does not nest; unknown tools are rejected)"
            )
        if self.target is not None and _looks_like_coordinate(self.target):
            raise ValueError(
                "sequence step 'target' must be a description, never a coordinate "
                "(section 66.1): a coordinate issued before the step begins is stale"
            )
        forbidden = sorted(FORBIDDEN_STEP_KEYS.intersection(self.args))
        if forbidden:
            raise ValueError(
                f"sequence step arguments may not carry pre-resolved identity/lease/coordinate "
                f"fields (section 66.1): {forbidden}"
            )
        return self

    @property
    def action_class(self) -> ActionClass:
        """The action class of this step's tool."""
        return action_class_for(self.tool)

    @property
    def requires_verification(self) -> bool:
        """Effective verification requirement (explicit override or class default)."""
        if self.require_verification is not None:
            return self.require_verification
        return requires_verification(self.action_class)

    def to_dict(self) -> dict[str, Any]:
        """Reconstruct the original step JSON (args flattened back out)."""
        payload: dict[str, Any] = {"step_id": self.step_id, "tool": self.tool}
        if self.target is not None:
            payload["target"] = self.target
        if self.role is not None:
            payload["role"] = self.role.value
        if self.require_verification is not None:
            payload["require_verification"] = self.require_verification
        payload.update(self.args)
        return payload


class RunSequenceRequest(BaseModel):
    """A validated ``run_sequence`` tool call (section 66.1)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    steps: tuple[SequenceStep, ...] = Field(min_length=1)
    halt_on: tuple[HaltCondition, ...] | None = Field(
        default=None,
        description="None means the full default safety set.",
    )

    @model_validator(mode="after")
    def _validate_request(self) -> RunSequenceRequest:
        ids = [step.step_id for step in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("sequence step_id values must be unique")
        if self.halt_on is not None:
            requested = set(self.halt_on)
            extra = requested - DEFAULT_HALT_CONDITIONS
            if extra:
                raise ValueError(
                    f"halt_on may only narrow within the default safety set; unsupported: {sorted(c.value for c in extra)}"
                )
        return self

    @property
    def effective_halt_on(self) -> frozenset[HaltCondition]:
        """The halt set actually applied (full default when unset)."""
        if self.halt_on is None:
            return DEFAULT_HALT_CONDITIONS
        return frozenset(self.halt_on)

    @property
    def action_class(self) -> ActionClass:
        """The most restrictive class among the declared steps (section 57)."""
        return most_restrictive_action_class(tuple(step.action_class for step in self.steps))


class SequenceStepResult(ToolEnvelope):
    """One step's result: a tool envelope plus its position and requirement."""

    step_id: str = Field(min_length=1)
    index: int = Field(ge=0)
    tool: str
    action_class: ActionClass
    require_verification: bool = False

    @classmethod
    def from_envelope(
        cls,
        envelope: ToolEnvelope,
        *,
        step: SequenceStep,
        index: int,
    ) -> SequenceStepResult:
        """Wrap a step's envelope with its identity and position."""
        return cls(
            step_id=step.step_id,
            index=index,
            tool=step.tool,
            action_class=step.action_class,
            require_verification=step.requires_verification,
            ok=envelope.ok,
            error_code=envelope.error_code,
            message=envelope.message,
            data=envelope.data,
            verification=envelope.verification,
            state_version=envelope.state_version,
            frame_id=envelope.frame_id,
            elapsed_ms=envelope.elapsed_ms,
        )

    @classmethod
    def not_executed_step(
        cls, *, step: SequenceStep, index: int, reason: str | None = None
    ) -> SequenceStepResult:
        """A ``NOT_EXECUTED`` result for a step the runner never reached."""
        envelope = ToolEnvelope.not_executed(reason or "sequence halted before this step")
        return cls.from_envelope(envelope, step=step, index=index)


class SequenceResult(BaseModel):
    """The full, ordered result array ``run_sequence`` returns (section 66)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sequence_id: str = Field(min_length=1)
    steps: tuple[SequenceStepResult, ...]
    halted: bool
    halt_reason: str | None = None
    halt_code: ErrorCode | None = Field(
        default=None,
        description=(
            "The structured code the halt maps to (section 77), so the Brain does "
            "not have to parse a message. Set only when the sequence halted."
        ),
    )
    wall_clock_ms: float = Field(ge=0.0)
    started_at: float

    @model_validator(mode="after")
    def _validate_result(self) -> SequenceResult:
        indices = [step.index for step in self.steps]
        if indices != list(range(len(self.steps))):
            raise ValueError("sequence results must be ordered, dense and zero-based")
        if self.halted and not self.halt_reason:
            raise ValueError("a halted sequence must record a halt_reason")
        if not self.halted and self.halt_reason is not None:
            raise ValueError("an unhalted sequence must not carry a halt_reason")
        if not self.halted and self.halt_code is not None:
            raise ValueError("an unhalted sequence must not carry a halt_code")
        return self

    @property
    def completed_count(self) -> int:
        """Number of steps that produced a result other than NOT_EXECUTED."""
        return sum(
            1 for step in self.steps if step.error_code is not ErrorCode.NOT_EXECUTED
        )

    @property
    def not_executed_count(self) -> int:
        """Number of steps the runner never reached."""
        return sum(1 for step in self.steps if step.error_code is ErrorCode.NOT_EXECUTED)

    def to_dict(self) -> dict[str, Any]:
        """JSON-shaped result: the per-step envelope array (section 66)."""
        return {
            "sequence_id": self.sequence_id,
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "halt_code": None if self.halt_code is None else self.halt_code.value,
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
        }


class SequenceProgress(BaseModel):
    """Live sequence progress recorded in the state cache (section 65)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sequence_id: str = Field(min_length=1)
    current_index: int = Field(ge=0)
    total_steps: int = Field(ge=1)
    completed_indices: tuple[int, ...] = ()
    halt_reason: str | None = None

    @model_validator(mode="after")
    def _validate_progress(self) -> SequenceProgress:
        if self.current_index >= self.total_steps and self.halt_reason is None and len(
            self.completed_indices
        ) < self.total_steps:
            raise ValueError("current_index cannot exceed the step count unless the sequence halted")
        if any(index >= self.total_steps or index < 0 for index in self.completed_indices):
            raise ValueError("completed_indices must be within [0, total_steps)")
        return self

    @property
    def is_complete(self) -> bool:
        """True when every step has completed."""
        return len(self.completed_indices) == self.total_steps

    def to_dict(self) -> dict[str, Any]:
        """JSON-shaped progress for the GUI progress indicator (section 71)."""
        return {
            "sequence_id": self.sequence_id,
            "current_index": self.current_index,
            "total_steps": self.total_steps,
            "completed_indices": list(self.completed_indices),
            "halt_reason": self.halt_reason,
        }


def _looks_like_coordinate(text: str) -> bool:
    """True when ``text`` is a bare coordinate rather than a description."""
    return bool(_COORDINATE_TARGET_RE.match(text) or _COORDINATE_PREFIX_RE.match(text))


# Re-exported for callers that only import from this module.
__all__ = [
    "DEFAULT_HALT_CONDITIONS",
    "FORBIDDEN_STEP_KEYS",
    "HaltCondition",
    "RunSequenceRequest",
    "SequenceProgress",
    "SequenceResult",
    "SequenceStep",
    "SequenceStepResult",
    "ToolName",
]
