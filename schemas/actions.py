"""Tool vocabulary, action classes and the uniform tool envelope.

Specification section 66 requires every tool result -- success or failure -- to
use one envelope shape, so the Brain never has to special-case a response. This
module also fixes the action class of each tool (section 57), which is what the
policy layer (section 56) and the sequence gate (section 66.1) consult.

``run_sequence`` is deliberately absent from :data:`TOOL_ACTION_CLASS`: it is a
container, and its gate is the most restrictive class among its declared steps.
"""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from schemas.elements import ElementQuery
from schemas.enums import ACTION_CLASS_SEVERITY, ActionClass, ErrorCode, VerificationState


class ToolName:
    """Section 66 tool names, held as plain string constants.

    Kept as constants rather than an enum member set so that the *action class
    mapping* below stays the single source of truth for tool semantics, and a
    tool can never be added without consciously assigning it a class.
    """

    GET_SCREEN_STATE = "get_screen_state"
    FIND_ELEMENT = "find_element"
    CLICK = "click"
    DOUBLE_CLICK = "double_click"
    RIGHT_CLICK = "right_click"
    TYPE_TEXT = "type_text"
    PRESS_KEY = "press_key"
    HOTKEY = "hotkey"
    SCROLL = "scroll"
    DRAG = "drag"
    WAIT_FOR_CHANGE = "wait_for_change"
    WAIT_FOR_ELEMENT = "wait_for_element"
    GET_ACTIVE_WINDOW = "get_active_window"
    DESCRIBE_REGION = "describe_region"
    ACTIVATE_ELEMENT = "activate_element"
    ENSURE_WINDOW = "ensure_window"
    GET_CAPABILITIES = "get_capabilities"
    EMERGENCY_STOP = "emergency_stop"
    RUN_SEQUENCE = "run_sequence"


#: The complete section 66 tool set (including ``run_sequence``).
REQUIRED_TOOLS: frozenset[str] = frozenset(
    {
        ToolName.GET_SCREEN_STATE,
        ToolName.FIND_ELEMENT,
        ToolName.CLICK,
        ToolName.DOUBLE_CLICK,
        ToolName.RIGHT_CLICK,
        ToolName.TYPE_TEXT,
        ToolName.PRESS_KEY,
        ToolName.HOTKEY,
        ToolName.SCROLL,
        ToolName.DRAG,
        ToolName.WAIT_FOR_CHANGE,
        ToolName.WAIT_FOR_ELEMENT,
        ToolName.GET_ACTIVE_WINDOW,
        ToolName.DESCRIBE_REGION,
        ToolName.ACTIVATE_ELEMENT,
        ToolName.ENSURE_WINDOW,
        ToolName.GET_CAPABILITIES,
        ToolName.EMERGENCY_STOP,
        ToolName.RUN_SEQUENCE,
    }
)

#: Action class per tool (section 57). ``run_sequence`` is intentionally absent.
#:
#: Rationale for the non-obvious entries:
#: ``activate_element`` invokes an accessibility action (it mutates the app),
#: ``ensure_window`` only changes focus (navigational), ``scroll`` is
#: navigational, and ``drag`` is ``DESTRUCTIVE`` because an unresolved drop
#: target defaults to destructive (section 49).
TOOL_ACTION_CLASS: dict[str, ActionClass] = {
    ToolName.GET_SCREEN_STATE: ActionClass.READ_ONLY,
    ToolName.FIND_ELEMENT: ActionClass.READ_ONLY,
    ToolName.GET_ACTIVE_WINDOW: ActionClass.READ_ONLY,
    ToolName.DESCRIBE_REGION: ActionClass.READ_ONLY,
    ToolName.GET_CAPABILITIES: ActionClass.READ_ONLY,
    ToolName.WAIT_FOR_CHANGE: ActionClass.READ_ONLY,
    ToolName.WAIT_FOR_ELEMENT: ActionClass.READ_ONLY,
    ToolName.EMERGENCY_STOP: ActionClass.READ_ONLY,
    ToolName.SCROLL: ActionClass.NAVIGATIONAL,
    ToolName.ENSURE_WINDOW: ActionClass.NAVIGATIONAL,
    ToolName.CLICK: ActionClass.MUTATING,
    ToolName.DOUBLE_CLICK: ActionClass.MUTATING,
    ToolName.RIGHT_CLICK: ActionClass.MUTATING,
    ToolName.TYPE_TEXT: ActionClass.MUTATING,
    ToolName.PRESS_KEY: ActionClass.MUTATING,
    ToolName.HOTKEY: ActionClass.MUTATING,
    ToolName.ACTIVATE_ELEMENT: ActionClass.MUTATING,
    ToolName.DRAG: ActionClass.DESTRUCTIVE,
}

#: Tools that may appear as a step inside a ``run_sequence`` (no nesting).
SEQUENCE_STEP_TOOLS: frozenset[str] = REQUIRED_TOOLS - {ToolName.RUN_SEQUENCE}


def action_class_for(tool: str) -> ActionClass:
    """Return the action class for ``tool``.

    Raises:
        ValueError: For ``run_sequence`` (its class depends on its steps) or any
            tool with no assigned class -- a new tool must consciously declare
            its class rather than inherit a default.
    """
    if tool == ToolName.RUN_SEQUENCE:
        raise ValueError("run_sequence has no fixed class; use most_restrictive_action_class()")
    try:
        return TOOL_ACTION_CLASS[tool]
    except KeyError as exc:
        raise ValueError(f"no action class is defined for tool {tool!r}") from exc


def most_restrictive_action_class(classes: tuple[ActionClass, ...]) -> ActionClass:
    """Return the most restrictive class in ``classes`` (section 57).

    An empty sequence is ``READ_ONLY``; the sequence gate is the returned class,
    so a sequence containing one destructive step is gated as destructive.
    """
    if not classes:
        return ActionClass.READ_ONLY
    return max(classes, key=lambda c: ACTION_CLASS_SEVERITY[c])


def requires_verification(action_class: ActionClass) -> bool:
    """Default verification requirement for a class (sections 60, 66.1)."""
    return action_class in (ActionClass.MUTATING, ActionClass.DESTRUCTIVE)


class PlannedAction(BaseModel):
    """A validated request to perform one action, class already resolved."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: str
    target: ElementQuery | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    action_class: ActionClass = ActionClass.READ_ONLY
    require_verification: bool = False

    @model_validator(mode="before")
    @classmethod
    def _derive_from_tool(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        tool = data.get("tool")
        if tool is None:
            return data
        derived = dict(data)
        if derived.get("action_class") is None:
            derived["action_class"] = action_class_for(str(tool))
        if derived.get("require_verification") is None:
            derived["require_verification"] = requires_verification(derived["action_class"])
        return derived

    @model_validator(mode="after")
    def _validate_tool(self) -> PlannedAction:
        if self.tool not in TOOL_ACTION_CLASS:
            raise ValueError(f"unknown or unresolvable tool {self.tool!r}")
        return self


class ToolEnvelope(BaseModel):
    """The uniform result envelope for every tool call (section 66)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    error_code: ErrorCode | None = None
    message: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    verification: VerificationState = VerificationState.NOT_APPLICABLE
    state_version: int | None = None
    frame_id: int | None = None
    elapsed_ms: float = Field(default=0.0, ge=0.0)

    @model_validator(mode="after")
    def _validate_consistency(self) -> ToolEnvelope:
        if self.ok and self.error_code is not None:
            raise ValueError("a successful envelope must not carry an error_code")
        if not self.ok and self.error_code is None:
            raise ValueError("a failed envelope must carry an error_code")
        if self.verification is VerificationState.CONTRADICTED and self.ok:
            raise ValueError("a CONTRADICTED result cannot be reported as ok=True")
        return self

    @classmethod
    def success(
        cls,
        *,
        data: dict[str, Any] | None = None,
        verification: VerificationState = VerificationState.NOT_APPLICABLE,
        state_version: int | None = None,
        frame_id: int | None = None,
        elapsed_ms: float = 0.0,
        message: str | None = None,
    ) -> ToolEnvelope:
        """Build a successful envelope."""
        return cls(
            ok=True,
            error_code=None,
            message=message,
            data=data or {},
            verification=verification,
            state_version=state_version,
            frame_id=frame_id,
            elapsed_ms=elapsed_ms,
        )

    @classmethod
    def failure(
        cls,
        code: ErrorCode,
        message: str,
        *,
        data: dict[str, Any] | None = None,
        verification: VerificationState = VerificationState.NOT_APPLICABLE,
        state_version: int | None = None,
        frame_id: int | None = None,
        elapsed_ms: float = 0.0,
    ) -> ToolEnvelope:
        """Build a failed envelope carrying one taxonomy code."""
        return cls(
            ok=False,
            error_code=code,
            message=message,
            data=data or {},
            verification=verification,
            state_version=state_version,
            frame_id=frame_id,
            elapsed_ms=elapsed_ms,
        )

    @classmethod
    def not_executed(cls, message: str = "step was not executed") -> ToolEnvelope:
        """A section 66 ``NOT_EXECUTED`` result for an unreached step."""
        return cls.failure(ErrorCode.NOT_EXECUTED, message)

    def to_dict(self) -> dict[str, Any]:
        """JSON-shaped envelope, exactly the section 66 field set."""
        return {
            "ok": self.ok,
            "error_code": self.error_code.value if self.error_code is not None else None,
            "message": self.message,
            "data": self.data,
            "verification": self.verification.value,
            "state_version": self.state_version,
            "frame_id": self.frame_id,
            "elapsed_ms": self.elapsed_ms,
        }


def elapsed_ms_since(start_monotonic: float) -> float:
    """Milliseconds elapsed since ``start_monotonic`` (a ``time.monotonic()``)."""
    return max(0.0, (time.monotonic() - start_monotonic) * 1000.0)
