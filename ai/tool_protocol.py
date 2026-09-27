"""The Brain's tool protocol and BLAXCY's dispatcher (specification section 66).

This module is the *only* door between a Brain tool call and BLAXCY. It has two
halves, and the split matters:

* **Declaration** -- the exact tool schemas advertised to the model
  (:func:`tool_declarations`). They are derived from the same constants the
  dispatcher validates against, so a tool cannot be advertised with one shape
  and accepted with another.
* **Dispatch** -- :class:`ToolDispatcher` maps a validated call onto either the
  *read path* (section 32.1) or the executor (sections 44/45/59).

Three rules make this the safety boundary rather than a convenience layer:

1. **Arguments are strict.** An unknown argument key is a rejection, not an
   ignore. A Brain that sends ``{"target": "search", "x": 90, "y": 40}`` must not
   have its coordinate silently dropped and a *different* element clicked: it is
   refused, so the Brain learns its request was not honoured as written.
2. **A target is always a description.** Coordinates, ``element_id``, leases,
   ``frame_id``, ``state_version`` and ``generation`` are never accepted for an
   action tool -- those are exactly the pre-resolved values that would bypass
   lease issuance and revalidation (sections 44, 45, 66.1).
3. **Read-only tools never enter the executor.** They do not touch the desktop,
   so they run on the bounded read path (section 32.1) and can never be
   serialised behind, or delay, a pending physical action. They are still fully
   preemptable by emergency stop and human takeover, and a snapshot they return
   is timestamped like any other state -- a later action must revalidate against
   live state regardless of what a read call just reported.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from config.settings import Settings
from control.executor import Executor
from control.window_manager import WindowController
from core.event_bus import EventBus
from core.logging_setup import get_logger
from core.state_cache import StateCache
from core.target_resolver import ResolutionStatus, TargetResolver
from schemas.actions import (
    SEQUENCE_STEP_TOOLS,
    ToolEnvelope,
    ToolName,
    elapsed_ms_since,
)
from schemas.capability import CapabilityReport
from schemas.elements import ElementQuery, UIElement
from schemas.enums import (
    CHANGE_CLASS_SEVERITY,
    CapabilityName,
    CapabilityStatus,
    ChangeClass,
    CoordinateSpace,
    ErrorCode,
    UIRole,
)
from schemas.errors import BlaxcyError
from schemas.geometry import Rect
from schemas.screen_state import ScreenState
from schemas.sequences import RunSequenceRequest, SequenceResult

_log = get_logger(__name__)

#: How long a read-only call waits for a dispatch slot before giving up. The
#: bound exists so a stalled read handler cannot hold every slot forever and
#: starve the other read calls (section 32.1's "bounded" requirement).
READ_SLOT_TIMEOUT_SECONDS: float = 5.0

#: Argument keys that would mean a pre-resolved coordinate, identity or lease.
#: Never accepted by any action tool, standalone or inside a sequence.
FORBIDDEN_ARGUMENT_KEYS: frozenset[str] = frozenset(
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

#: Tools handled on the section 32.1 read path (they inject no input).
READ_ONLY_TOOLS: frozenset[str] = frozenset(
    {
        ToolName.GET_SCREEN_STATE,
        ToolName.FIND_ELEMENT,
        ToolName.GET_ACTIVE_WINDOW,
        ToolName.DESCRIBE_REGION,
        ToolName.GET_CAPABILITIES,
        ToolName.WAIT_FOR_CHANGE,
        ToolName.WAIT_FOR_ELEMENT,
        ToolName.EMERGENCY_STOP,
    }
)

#: Maximum elements a single read-only response may carry, so a read call can
#: never blow the Brain's context budget on one turn (sections 68, 68.1).
MAX_CONTEXT_ELEMENTS: int = 60


class ToolProtocolError(BlaxcyError):
    """A tool call BLAXCY could not accept as written (section 66).

    Carries the taxonomy code chosen at the raise site: ``BACKEND_UNAVAILABLE``
    for a tool BLAXCY does not implement, ``INTERNAL_ERROR`` for a malformed
    argument set.
    """


# ---------------------------------------------------------------------------
# Declaration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolDeclaration:
    """One tool as advertised to the model."""

    name: str
    description: str
    parameters: dict[str, Any]

    def to_function_declaration(self) -> dict[str, Any]:
        """A provider-neutral function-declaration dict (JSON schema form)."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters_json_schema": self.parameters,
        }


def _arg(description: str, json_type: str, **extra: Any) -> dict[str, Any]:
    """Build one JSON-schema property."""
    return {"type": json_type, "description": description, **extra}


_TARGET_DESCRIPTION = (
    "A plain-language DESCRIPTION of the target control (its visible text, "
    "accessible name, or role). Never a coordinate, element id or lease: a "
    "pre-resolved target would bypass live resolution and revalidation."
)
_ROLE_ARG = _arg(
    "Optional UI role hint, to disambiguate targets that share a label.",
    "string",
    enum=[role.value for role in UIRole],
)
_CONTEXT_ARG = _arg(
    "Optional context (owning window/app/dialog) used to disambiguate the target.",
    "string",
)
_VISIBLE_COMMAND_ARG = _arg(
    "The literal command line currently visible in the terminal, for section 54 "
    "policy. Required when the action would submit a terminal line.",
    "string",
)


def _object_schema(
    properties: dict[str, Any],
    *,
    required: Sequence[str] = (),
) -> dict[str, Any]:
    """A JSON-schema object with strict additional-properties rejection."""
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = list(required)
    return schema


#: The declared parameter schema per tool. This is the single source of truth:
#: the dispatcher reads the same keys from it when validating a call.
TOOL_PARAMETER_SCHEMAS: dict[str, dict[str, Any]] = {
    ToolName.GET_SCREEN_STATE: _object_schema(
        {
            "max_elements": _arg(
                f"How many elements to include (default {MAX_CONTEXT_ELEMENTS}, "
                f"hard cap {MAX_CONTEXT_ELEMENTS}).",
                "integer",
            ),
            "include_elements": _arg("Whether to include element summaries.", "boolean"),
        }
    ),
    ToolName.FIND_ELEMENT: _object_schema(
        {
            "target": _arg("The text/name of the control to find.", "string"),
            "role": _ROLE_ARG,
            "context": _CONTEXT_ARG,
            "window_id": _arg("Restrict the search to one window id.", "integer"),
        },
        required=("target",),
    ),
    ToolName.CLICK: _object_schema(
        {
            "target": _arg(_TARGET_DESCRIPTION, "string"),
            "role": _ROLE_ARG,
            "context": _CONTEXT_ARG,
            "visible_command": _VISIBLE_COMMAND_ARG,
        },
        required=("target",),
    ),
    ToolName.DOUBLE_CLICK: _object_schema(
        {"target": _arg(_TARGET_DESCRIPTION, "string"), "role": _ROLE_ARG, "context": _CONTEXT_ARG},
        required=("target",),
    ),
    ToolName.RIGHT_CLICK: _object_schema(
        {"target": _arg(_TARGET_DESCRIPTION, "string"), "role": _ROLE_ARG, "context": _CONTEXT_ARG},
        required=("target",),
    ),
    ToolName.TYPE_TEXT: _object_schema(
        {
            "target": _arg(
                "Description of the text field to type into. It is focused and "
                "verified before a single key is injected.",
                "string",
            ),
            "text": _arg("The literal text to type.", "string"),
            "role": _ROLE_ARG,
            "context": _CONTEXT_ARG,
            "visible_command": _VISIBLE_COMMAND_ARG,
        },
        required=("target", "text"),
    ),
    ToolName.PRESS_KEY: _object_schema(
        {
            "key": _arg("Key name, e.g. 'Return', 'Escape', 'a', 'F5'.", "string"),
            "modifiers": _arg("Modifier key names, e.g. ['ctrl', 'shift'].", "array"),
            "visible_command": _VISIBLE_COMMAND_ARG,
        },
        required=("key",),
    ),
    ToolName.HOTKEY: _object_schema(
        {
            "combo": _arg("A combo string ('ctrl+l') or a list of keys.", "string"),
            "visible_command": _VISIBLE_COMMAND_ARG,
        },
        required=("combo",),
    ),
    ToolName.SCROLL: _object_schema(
        {
            "target": _arg(_TARGET_DESCRIPTION, "string"),
            "vertical": _arg("Wheel steps; positive scrolls down (default 3).", "integer"),
            "horizontal": _arg("Horizontal wheel steps (default 0).", "integer"),
            "role": _ROLE_ARG,
            "context": _CONTEXT_ARG,
        },
        required=("target",),
    ),
    ToolName.DRAG: _object_schema(
        {
            "target": _arg(_TARGET_DESCRIPTION + " (the source control)", "string"),
            "destination": _arg(
                "Description of the drop target; it is resolved live at drop time, "
                "never taken from the plan.", "string",
            ),
            "role": _ROLE_ARG,
            "context": _CONTEXT_ARG,
        },
        required=("target", "destination"),
    ),
    ToolName.WAIT_FOR_CHANGE: _object_schema(
        {
            "timeout_ms": _arg("How long to wait (default 2000).", "integer"),
            "min_change_class": _arg(
                "The smallest change that counts: TRIVIAL, ANIMATION, MEANINGFUL, MAJOR.",
                "string",
                enum=[klass.value for klass in ChangeClass if klass is not ChangeClass.NONE],
            ),
        }
    ),
    ToolName.WAIT_FOR_ELEMENT: _object_schema(
        {
            "target": _arg("The text/name of the control to wait for.", "string"),
            "role": _ROLE_ARG,
            "context": _CONTEXT_ARG,
            "timeout_ms": _arg("How long to wait (default 5000).", "integer"),
            "poll_ms": _arg("Poll interval (default 100).", "integer"),
        },
        required=("target",),
    ),
    ToolName.GET_ACTIVE_WINDOW: _object_schema({}),
    ToolName.DESCRIBE_REGION: _object_schema(
        {
            "x": _arg("Region left edge in DESKTOP coordinates.", "integer"),
            "y": _arg("Region top edge in DESKTOP coordinates.", "integer"),
            "width": _arg("Region width in pixels.", "integer"),
            "height": _arg("Region height in pixels.", "integer"),
        },
        required=("x", "y", "width", "height"),
    ),
    ToolName.ACTIVATE_ELEMENT: _object_schema(
        {"target": _arg(_TARGET_DESCRIPTION, "string"), "role": _ROLE_ARG, "context": _CONTEXT_ARG},
        required=("target",),
    ),
    ToolName.ENSURE_WINDOW: _object_schema(
        {
            "window_id": _arg("The window id to activate.", "integer"),
            "timeout_ms": _arg("Activation wait (default 600).", "integer"),
        },
        required=("window_id",),
    ),
    ToolName.GET_CAPABILITIES: _object_schema({}),
    ToolName.EMERGENCY_STOP: _object_schema(
        {"reason": _arg("Why the stop was triggered (for the log).", "string")}
    ),
    ToolName.RUN_SEQUENCE: _object_schema(
        {
            "steps": _arg(
                "Ordered plan. Each step is the argument shape of a standalone "
                "tool plus a step_id; targets are always descriptions.", "array",
            ),
            "halt_on": _arg(
                "Optional halt conditions. May only NARROW the default safety set; "
                "it can never disable a safety halt.", "array",
            ),
        },
        required=("steps",),
    ),
}

#: The declared description per tool, i.e. the guidance the model actually reads.
TOOL_DESCRIPTIONS: dict[str, str] = {
    ToolName.GET_SCREEN_STATE: (
        "Observe the desktop: current state stamps, active window/app and, by "
        "default, up to 60 element summaries. Read-only."
    ),
    ToolName.FIND_ELEMENT: (
        "Resolve a target description to candidate elements and report their "
        "scores, WITHOUT acting. Read-only; use it to check ambiguity first."
    ),
    ToolName.CLICK: "Click a control identified by description. Mutating.",
    ToolName.DOUBLE_CLICK: "Double-click a control identified by description. Mutating.",
    ToolName.RIGHT_CLICK: "Right-click a control identified by description. Mutating.",
    ToolName.TYPE_TEXT: (
        "Focus a text field (verified) and type literal text. Mutating. Never "
        "used for credentials: pass the field description, and BLAXCY types "
        "without ever reading the value back."
    ),
    ToolName.PRESS_KEY: (
        "Press one key, optionally with modifiers. Mutating. A key that submits "
        "a terminal line is a separate, separately authorised decision."
    ),
    ToolName.HOTKEY: "Press a key combination, e.g. 'ctrl+l'. Mutating.",
    ToolName.SCROLL: "Scroll at a target element. Navigational.",
    ToolName.DRAG: (
        "Drag from a source description to a destination description. "
        "DESTRUCTIVE: an unresolved drop target is treated as destructive and "
        "requires explicit confirmation."
    ),
    ToolName.WAIT_FOR_CHANGE: "Wait until the screen changes at least as much as asked. Read-only.",
    ToolName.WAIT_FOR_ELEMENT: "Wait until a target description resolves. Read-only.",
    ToolName.GET_ACTIVE_WINDOW: "Report the active window and its metadata. Read-only.",
    ToolName.DESCRIBE_REGION: (
        "Report what BLAXCY actually perceives inside a desktop region "
        "(accessibility elements and their text). Read-only. Model-based visual "
        "description is a separate, later capability and is reported as unavailable."
    ),
    ToolName.ACTIVATE_ELEMENT: (
        "Invoke a control's own accessibility action (e.g. a button's default "
        "action) instead of injecting a pointer event. Mutating. A target with "
        "no accessibility path reports BACKEND_UNAVAILABLE rather than "
        "substituting a click, and BLAXCY never fakes it."
    ),
    ToolName.ENSURE_WINDOW: (
        "Activate a window and verify it really became active (bounded wait). "
        "Navigational."
    ),
    ToolName.GET_CAPABILITIES: "Report every capability with its honest status and evidence. Read-only.",
    ToolName.EMERGENCY_STOP: (
        "Stop everything immediately: latch the stop, release tracked input, "
        "force OBSERVE. Always available."
    ),
    ToolName.RUN_SEQUENCE: (
        "Submit an ordered plan of steps and let BLAXCY execute them back to "
        "back. Every step still runs the full policy/resolve/lease/revalidate/"
        "execute/verify pipeline; the sequence halts rather than guessing "
        "forward, and unreached steps are reported NOT_EXECUTED."
    ),
}


def tool_declarations() -> tuple[ToolDeclaration, ...]:
    """Every section 66 tool, in a stable order, with its exact schema."""
    ordered = (
        ToolName.GET_SCREEN_STATE,
        ToolName.FIND_ELEMENT,
        ToolName.GET_ACTIVE_WINDOW,
        ToolName.DESCRIBE_REGION,
        ToolName.WAIT_FOR_CHANGE,
        ToolName.WAIT_FOR_ELEMENT,
        ToolName.GET_CAPABILITIES,
        ToolName.EMERGENCY_STOP,
        ToolName.CLICK,
        ToolName.DOUBLE_CLICK,
        ToolName.RIGHT_CLICK,
        ToolName.TYPE_TEXT,
        ToolName.PRESS_KEY,
        ToolName.HOTKEY,
        ToolName.SCROLL,
        ToolName.DRAG,
        ToolName.ACTIVATE_ELEMENT,
        ToolName.ENSURE_WINDOW,
        ToolName.RUN_SEQUENCE,
    )
    return tuple(
        ToolDeclaration(name=name, description=TOOL_DESCRIPTIONS[name], parameters=TOOL_PARAMETER_SCHEMAS[name])
        for name in ordered
    )


# ---------------------------------------------------------------------------
# Argument validation and parsing
# ---------------------------------------------------------------------------


def _validate_keys(tool: str, arguments: dict[str, Any], *, extra_allowed: Sequence[str] = ()) -> None:
    """Reject unknown and forbidden argument keys (section 66).

    A key is **allowed only if the tool's own declared schema allows it**. That
    single rule resolves the one place the two concerns overlap: a coordinate
    argument is forbidden for an action tool (it would be a pre-resolved target
    that bypasses resolution, lease and revalidation), but ``describe_region``
    genuinely takes a region rectangle, so its schema declares ``x``/``y`` and
    they are accepted there. The declaration is the contract, and nothing may
    arrive outside it.

    Keys that are not allowed are reported as *forbidden* (a recognisable
    coordinate/lease shape, which deserves an unmistakable message) or merely
    *unknown* (anything else).
    """
    if not isinstance(arguments, dict):
        raise ToolProtocolError(
            ErrorCode.INTERNAL_ERROR, f"{tool} arguments must be an object", details={"tool": tool}
        )
    keys = set(arguments)
    allowed = set(TOOL_PARAMETER_SCHEMAS[tool]["properties"]) | set(extra_allowed)
    not_allowed = keys - allowed
    forbidden = sorted(not_allowed.intersection(FORBIDDEN_ARGUMENT_KEYS))
    if forbidden:
        raise ToolProtocolError(
            ErrorCode.INTERNAL_ERROR,
            f"{tool} arguments may not carry pre-resolved coordinate/identity/lease fields "
            f"(section 66): {forbidden}",
            details={"tool": tool, "forbidden": forbidden},
        )
    unknown = sorted(not_allowed)
    if unknown:
        raise ToolProtocolError(
            ErrorCode.INTERNAL_ERROR,
            f"{tool} received unsupported argument(s) {unknown}; they were not applied "
            "(a partially honoured request would be worse than a refusal)",
            details={"tool": tool, "unknown": unknown, "allowed": sorted(allowed)},
        )


def _require(tool: str, arguments: dict[str, Any], key: str) -> Any:
    """Return a required argument, or raise a structured protocol error."""
    value = arguments.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ToolProtocolError(
            ErrorCode.INTERNAL_ERROR,
            f"{tool} requires the '{key}' argument",
            details={"tool": tool, "missing": key},
        )
    return value


def _optional_str(arguments: dict[str, Any], key: str) -> str | None:
    value = arguments.get(key)
    return None if value is None else str(value)


def _role_hint(tool: str, arguments: dict[str, Any]) -> UIRole | None:
    """Parse an optional role hint, rejecting an unknown role name."""
    raw = arguments.get("role")
    if raw is None:
        return None
    try:
        return UIRole(str(raw))
    except ValueError as exc:
        raise ToolProtocolError(
            ErrorCode.INTERNAL_ERROR,
            f"{tool} received an unknown role {raw!r}",
            details={"tool": tool, "role": str(raw)},
        ) from exc


def _int_arg(tool: str, arguments: dict[str, Any], key: str, *, default: int | None = None) -> int:
    """Parse an integer argument, rejecting a non-integer value."""
    if key not in arguments or arguments[key] is None:
        if default is None:
            raise ToolProtocolError(
                ErrorCode.INTERNAL_ERROR, f"{tool} requires the '{key}' argument", details={"tool": tool}
            )
        return default
    raw = arguments[key]
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ToolProtocolError(
            ErrorCode.INTERNAL_ERROR,
            f"{tool} '{key}' must be a number",
            details={"tool": tool, "key": key},
        )
    return int(raw)


def element_query(tool: str, arguments: dict[str, Any], *, target_key: str = "target") -> ElementQuery:
    """Build an :class:`ElementQuery` from a tool call's target description."""
    text = _require(tool, arguments, target_key)
    return ElementQuery(
        text=str(text),
        role_hint=_role_hint(tool, arguments),
        context=_optional_str(arguments, "context"),
    )


def build_planned_action(tool: str, arguments: dict[str, Any]) -> Any:
    """Map a validated action tool call onto a :class:`PlannedAction`.

    Read-only tools have no ``PlannedAction`` -- they never reach the executor --
    so this raises for them rather than inventing a fake one.

    Raises:
        ToolProtocolError: For an unknown tool, a read-only tool, or arguments
            that do not match the declared schema.
    """
    from schemas.actions import PlannedAction  # local import keeps the module import-light

    if tool not in TOOL_PARAMETER_SCHEMAS:
        raise ToolProtocolError(
            ErrorCode.BACKEND_UNAVAILABLE, f"unknown tool {tool!r}", details={"tool": tool}
        )
    if tool in READ_ONLY_TOOLS:
        raise ToolProtocolError(
            ErrorCode.INTERNAL_ERROR,
            f"{tool} is a read-only tool and has no PlannedAction",
            details={"tool": tool},
        )
    if tool == ToolName.RUN_SEQUENCE:
        raise ToolProtocolError(
            ErrorCode.INTERNAL_ERROR,
            "run_sequence is a container; parse it with parse_sequence_request()",
            details={"tool": tool},
        )

    _validate_keys(tool, arguments)

    if tool in (ToolName.CLICK, ToolName.DOUBLE_CLICK, ToolName.RIGHT_CLICK, ToolName.ACTIVATE_ELEMENT):
        return PlannedAction(tool=tool, target=element_query(tool, arguments))

    if tool == ToolName.TYPE_TEXT:
        return PlannedAction(
            tool=tool,
            target=element_query(tool, arguments),
            params={
                "text": str(_require(tool, arguments, "text")),
                **(
                    {"visible_command": str(arguments["visible_command"])}
                    if arguments.get("visible_command") is not None
                    else {}
                ),
            },
        )

    if tool == ToolName.PRESS_KEY:
        modifiers = arguments.get("modifiers") or ()
        if isinstance(modifiers, str):
            modifiers = [part.strip() for part in modifiers.split("+") if part.strip()]
        if not isinstance(modifiers, (list, tuple)):
            raise ToolProtocolError(
                ErrorCode.INTERNAL_ERROR,
                "press_key 'modifiers' must be a list of key names",
                details={"tool": tool},
            )
        return PlannedAction(
            tool=tool,
            params={
                "key": str(_require(tool, arguments, "key")),
                "modifiers": [str(value) for value in modifiers],
                **(
                    {"visible_command": str(arguments["visible_command"])}
                    if arguments.get("visible_command") is not None
                    else {}
                ),
            },
        )

    if tool == ToolName.HOTKEY:
        combo = _require(tool, arguments, "combo")
        if isinstance(combo, (list, tuple)):
            joined = "+".join(str(part) for part in combo)
        else:
            joined = str(combo)
        return PlannedAction(
            tool=tool,
            params={
                "combo": joined,
                **(
                    {"visible_command": str(arguments["visible_command"])}
                    if arguments.get("visible_command") is not None
                    else {}
                ),
            },
        )

    if tool == ToolName.SCROLL:
        return PlannedAction(
            tool=tool,
            target=element_query(tool, arguments),
            params={
                "vertical": _int_arg(tool, arguments, "vertical", default=3),
                "horizontal": _int_arg(tool, arguments, "horizontal", default=0),
            },
        )

    if tool == ToolName.DRAG:
        return PlannedAction(
            tool=tool,
            target=element_query(tool, arguments),
            params={"destination": str(_require(tool, arguments, "destination"))},
        )

    if tool == ToolName.ENSURE_WINDOW:
        return PlannedAction(
            tool=tool,
            params={
                "window_id": _int_arg(tool, arguments, "window_id"),
                **(
                    {"timeout_ms": _int_arg(tool, arguments, "timeout_ms")}
                    if arguments.get("timeout_ms") is not None
                    else {}
                ),
            },
        )

    # A tool in the schema table that is neither read-only nor handled above is a
    # programming error, not a runtime condition: refuse loudly rather than
    # silently producing an action with no arguments.
    raise ToolProtocolError(
        ErrorCode.BACKEND_UNAVAILABLE,
        f"{tool} has no dispatcher implementation",
        details={"tool": tool},
    )


def parse_tool_arguments(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Validate a read-only tool's arguments and return the normalized mapping."""
    if tool not in TOOL_PARAMETER_SCHEMAS:
        raise ToolProtocolError(
            ErrorCode.BACKEND_UNAVAILABLE, f"unknown tool {tool!r}", details={"tool": tool}
        )
    _validate_keys(tool, arguments)
    return dict(arguments)


class ToolCall(BaseModel):
    """One incoming Brain tool call, before validation against the schema."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)
    call_id: str | None = None


# ---------------------------------------------------------------------------
# The dispatcher
# ---------------------------------------------------------------------------


@runtime_checkable
class SequenceRunner(Protocol):
    """``run_sequence`` execution (specification section 66.1).

    Defined here so the dispatcher is complete now and Phase 10.1's runner plugs
    in without changing this module. Until a runner is supplied, ``run_sequence``
    reports an honest ``UNAVAILABLE`` -- it never pretends to have executed a
    plan.
    """

    def run(
        self,
        request: RunSequenceRequest,
        *,
        task_id: str | None = ...,
        confirmed: bool = ...,
    ) -> SequenceResult:
        """Execute the plan and return the ordered per-step result."""


@dataclass
class DispatchStats:
    """Observability for the read path (section 32.1) and the tools in use."""

    calls: dict[str, int] = field(default_factory=dict)
    read_only_calls: int = 0
    read_only_rejections: int = 0
    read_only_peak: int = 0
    refused: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """JSON-shaped stats for the GUI and the report."""
        return {
            "calls": dict(self.calls),
            "read_only_calls": self.read_only_calls,
            "read_only_rejections": self.read_only_rejections,
            "read_only_peak": self.read_only_peak,
            "refused": dict(self.refused),
        }


class ToolDispatcher:
    """The single door from a Brain tool call to BLAXCY (section 66).

    Args:
        settings: Full configuration (``[performance]`` bounds the read path).
        executor: The section 59 pipeline; the only route to physical input.
        state_cache: The current-state authority read by read-only tools.
        resolver: Deterministic target resolution (section 43).
        event_bus: Optional bus for dispatch events.
        window_manager: Optional section 47 window control.
        capabilities: Callable returning the current capability report.
        perceive: Optional callable that refreshes perception (used by the wait
            tools, which must observe real change rather than sleep blindly).
        emergency_stop: The section 63 stop, so the stop tool is reachable from
            the Brain. A stop request is a stop *request*: it runs the real,
            latched stop.
        abort_check: The same combined abort hook the executor uses; a read-only
            call is preempted by a latched stop exactly like any other work.
        sequence_runner: Optional section 66.1 runner. Absent means
            ``run_sequence`` is honestly unavailable.
        target_fallback: Optional section 43 fallback cascade (OCR, then gated
            visual grounding), used by ``find_element`` when the deterministic
            cascade delivers no actionable candidate (nothing found, or nothing
            above ``act_threshold``). It accepts an enriched observation into the
            cache and the query is then resolved again in full, so the same
            ambiguity rule applies. It is never consulted for an ambiguous result,
            and it never injects input.
        clock: Monotonic clock, injectable for deterministic waits.
        sleep: Sleep function, injectable for deterministic waits.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        executor: Executor,
        state_cache: StateCache,
        resolver: TargetResolver,
        event_bus: EventBus | None = None,
        window_manager: WindowController | None = None,
        capabilities: Callable[[], CapabilityReport | None] | None = None,
        perceive: Callable[[], ScreenState | None] | None = None,
        emergency_stop: Any = None,
        abort_check: Callable[[], ErrorCode | None] | None = None,
        sequence_runner: SequenceRunner | None = None,
        target_fallback: Callable[[ElementQuery], bool] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._settings = settings
        self._executor = executor
        self._cache = state_cache
        self._resolver = resolver
        self._bus = event_bus
        self._windows = window_manager
        self._capabilities = capabilities
        self._perceive = perceive
        self._stop = emergency_stop
        self._abort_check = abort_check
        self._sequences = sequence_runner
        self._target_fallback = target_fallback
        self._clock = clock
        self._sleep = sleep
        self._read_slots = threading.BoundedSemaphore(
            max(1, settings.performance.max_concurrent_readonly_dispatch)
        )
        self._read_lock = threading.Lock()
        self._read_in_flight = 0
        self.stats = DispatchStats()

    # -- Wiring ---------------------------------------------------------------

    def attach_sequence_runner(self, runner: SequenceRunner | None) -> None:
        """Wire (or unwire) the section 66.1 runner after construction.

        The runner needs the dispatcher -- every step goes through it -- and the
        dispatcher needs the runner, so one of the two must be attached after
        the other exists. A documented attach point is preferable to setting a
        private attribute from outside, and without a runner ``run_sequence``
        keeps reporting an honest ``UNAVAILABLE``.
        """
        self._sequences = runner

    @property
    def sequence_runner(self) -> SequenceRunner | None:
        """The wired sequence runner, or ``None``."""
        return self._sequences

    # -- Public entry point ---------------------------------------------------

    def dispatch(
        self,
        call: ToolCall,
        *,
        task_id: str | None = None,
        step_id: str | None = None,
        sequence_id: str | None = None,
        confirmed: bool = False,
        require_verification: bool | None = None,
    ) -> ToolEnvelope:
        """Validate and execute one tool call, returning one uniform envelope.

        Args:
            call: The tool name plus its raw arguments.
            task_id: The owning task, for the tracker and recovery budgets.
            step_id: The owning step, when this call is a sequence step.
            sequence_id: The owning sequence, when there is one.
            confirmed: Whether a human has already confirmed a destructive
                action. BLAXCY never sets this on its own (section 56), and the
                sequence runner never forwards one (section 66.1).
            require_verification: Per-step verification requirement (section
                66.1). ``None`` means "use the class default", which is what
                every standalone call does.
        """
        envelope = self._dispatch_inner(
            call,
            task_id=task_id,
            step_id=step_id,
            sequence_id=sequence_id,
            confirmed=confirmed,
            require_verification=require_verification,
        )
        self._log_dispatch(
            call, envelope, task_id=task_id, step_id=step_id, sequence_id=sequence_id
        )
        return envelope

    def _log_dispatch(
        self,
        call: ToolCall,
        envelope: ToolEnvelope,
        *,
        task_id: str | None,
        step_id: str | None,
        sequence_id: str | None,
    ) -> None:
        """Write one structured line per call (section 70).

        Only metadata section 70 permits is attached: the tool, the outcome, the
        error code, the verification state, the state version and frame the
        decision was taken against, the latency, and the batching identity when
        there is one. The call's *arguments* and the resolved target are never
        logged -- a target may describe a credential field (sections 42/55), and
        the arguments are exactly where an operator would put a password.
        """
        _log.info(
            "tool call",
            extra={
                "action_type": call.name,
                "ok": envelope.ok,
                "verification": envelope.verification.value,
                "error_code": envelope.error_code.value if envelope.error_code else None,
                "latency_ms": round(envelope.elapsed_ms, 3),
                "state_version": envelope.state_version,
                "frame_id": envelope.frame_id,
                "task_id": task_id,
                "step_id": step_id,
                "sequence_id": sequence_id,
            },
        )

    def _dispatch_inner(
        self,
        call: ToolCall,
        *,
        task_id: str | None = None,
        step_id: str | None = None,
        sequence_id: str | None = None,
        confirmed: bool = False,
        require_verification: bool | None = None,
    ) -> ToolEnvelope:
        started = self._clock()
        self.stats.calls[call.name] = self.stats.calls.get(call.name, 0) + 1
        try:
            if call.name == ToolName.RUN_SEQUENCE:
                return self._dispatch_sequence(call, started, task_id=task_id, confirmed=confirmed)
            if call.name in READ_ONLY_TOOLS:
                return self._dispatch_read_only(call, started)
            planned = build_planned_action(call.name, call.arguments)
        except ToolProtocolError as exc:
            self._refuse(call.name)
            return ToolEnvelope.failure(
                exc.code,
                exc.message,
                data={"details": exc.details},
                elapsed_ms=elapsed_ms_since(started),
            )
        except ValidationError as exc:
            self._refuse(call.name)
            return ToolEnvelope.failure(
                ErrorCode.INTERNAL_ERROR,
                f"{call.name} arguments failed schema validation",
                data={"errors": exc.error_count()},
                elapsed_ms=elapsed_ms_since(started),
            )

        return self._executor.execute(
            planned,
            task_id=task_id,
            step_id=step_id,
            sequence_id=sequence_id,
            confirmed=confirmed,
            require_verification=require_verification,
        )

    # -- Read path (section 32.1) --------------------------------------------

    def _dispatch_read_only(self, call: ToolCall, started: float) -> ToolEnvelope:
        """Run a read-only tool on the bounded, non-serialising read path."""
        acquired = self._read_slots.acquire(timeout=READ_SLOT_TIMEOUT_SECONDS)
        if not acquired:
            self.stats.read_only_rejections += 1
            return ToolEnvelope.failure(
                ErrorCode.RATE_LIMITED,
                "too many concurrent read-only calls "
                f"(max {self._settings.performance.max_concurrent_readonly_dispatch})",
                elapsed_ms=elapsed_ms_since(started),
            )
        try:
            with self._read_lock:
                self._read_in_flight += 1
                self.stats.read_only_peak = max(self.stats.read_only_peak, self._read_in_flight)
                self.stats.read_only_calls += 1
            try:
                # A latched stop preempts read-only work too (section 32.1): the
                # user has taken the desktop back, so BLAXCY stops reporting on it.
                abort = self._abort_code()
                if abort is not None:
                    self._refuse(call.name)
                    return ToolEnvelope.failure(
                        abort, "automation was stopped before the read-only call ran"
                    )
                return self._read_only(call, started)
            finally:
                with self._read_lock:
                    self._read_in_flight -= 1
        finally:
            self._read_slots.release()

    def _read_only(self, call: ToolCall, started: float) -> ToolEnvelope:
        """Dispatch to the specific read-only tool implementation."""
        try:
            arguments = parse_tool_arguments(call.name, call.arguments)
        except ToolProtocolError as exc:
            self._refuse(call.name)
            return ToolEnvelope.failure(
                exc.code, exc.message, data={"details": exc.details}, elapsed_ms=elapsed_ms_since(started)
            )

        if call.name == ToolName.EMERGENCY_STOP:
            return self._tool_emergency_stop(arguments, started)
        if call.name == ToolName.GET_CAPABILITIES:
            return self._tool_get_capabilities(started)
        if call.name == ToolName.GET_ACTIVE_WINDOW:
            return self._tool_get_active_window(started)
        if call.name == ToolName.GET_SCREEN_STATE:
            return self._tool_get_screen_state(arguments, started)
        if call.name == ToolName.FIND_ELEMENT:
            return self._tool_find_element(arguments, started)
        if call.name == ToolName.DESCRIBE_REGION:
            return self._tool_describe_region(arguments, started)
        if call.name == ToolName.WAIT_FOR_CHANGE:
            return self._tool_wait_for_change(arguments, started)
        if call.name == ToolName.WAIT_FOR_ELEMENT:
            return self._tool_wait_for_element(arguments, started)
        self._refuse(call.name)
        return ToolEnvelope.failure(
            ErrorCode.BACKEND_UNAVAILABLE,
            f"{call.name} has no read-path implementation",
            elapsed_ms=elapsed_ms_since(started),
        )

    def _tool_get_capabilities(self, started: float) -> ToolEnvelope:
        """Report every capability with its evidence (section 28)."""
        report = self._capabilities() if self._capabilities is not None else None
        if report is None:
            return ToolEnvelope.failure(
                ErrorCode.BACKEND_UNAVAILABLE,
                "no capability report is available; run the capability probe first",
                elapsed_ms=elapsed_ms_since(started),
            )
        return ToolEnvelope.success(data={"capabilities": report.to_dict()}, elapsed_ms=elapsed_ms_since(started))

    def _tool_get_active_window(self, started: float) -> ToolEnvelope:
        """Report the active window and its metadata (section 47), read-only."""
        if self._windows is None:
            return ToolEnvelope.failure(
                ErrorCode.BACKEND_UNAVAILABLE,
                "window information is unavailable (no window manager wired)",
                elapsed_ms=elapsed_ms_since(started),
            )
        try:
            window_id = self._windows.active_window()
            info = None
            if window_id is not None:
                for candidate in self._windows.list_windows():
                    if candidate.window_id == window_id:
                        info = candidate
                        break
        except BlaxcyError as exc:
            return ToolEnvelope.failure(
                exc.code, exc.message, data=exc.details, elapsed_ms=elapsed_ms_since(started)
            )
        state = self._cache.current
        return ToolEnvelope.success(
            data={
                "window_id": window_id,
                "window": info.to_dict() if info is not None else None,
                "state_active_window_id": None if state is None else state.active_window_id,
                "active_app": None if state is None else state.active_app,
            },
            state_version=None if state is None else state.state_version,
            frame_id=None if state is None else state.frame_id,
            elapsed_ms=elapsed_ms_since(started),
        )

    def _tool_get_screen_state(self, arguments: dict[str, Any], started: float) -> ToolEnvelope:
        """Report the current observation, with its stamps and freshness."""
        state = self._cache.current
        if state is None:
            return ToolEnvelope.failure(
                ErrorCode.CAPTURE_FAILED,
                "no screen state has been observed yet",
                elapsed_ms=elapsed_ms_since(started),
            )
        include_elements = bool(arguments.get("include_elements", True))
        limit = _clamp(int(arguments.get("max_elements", MAX_CONTEXT_ELEMENTS)), 0, MAX_CONTEXT_ELEMENTS)
        age_ms = state.age_ms(self._clock())
        stale = age_ms > float(self._settings.verification.max_action_state_age_ms)
        data: dict[str, Any] = {
            "state": state.to_model_context(),
            "age_ms": age_ms,
            "stale": stale,
            "max_action_state_age_ms": self._settings.verification.max_action_state_age_ms,
        }
        if include_elements:
            elements = [element_summary(element) for element in state.elements[:limit]]
            data["elements"] = elements
            data["elements_truncated"] = len(state.elements) > limit
        data["monitors"] = [
            {
                "monitor_id": monitor.monitor_id,
                "width": monitor.width,
                "height": monitor.height,
                "is_primary": monitor.is_primary,
            }
            for monitor in state.layout.monitors
        ]
        return ToolEnvelope.success(
            data=data,
            state_version=state.state_version,
            frame_id=state.frame_id,
            elapsed_ms=elapsed_ms_since(started),
        )

    def _tool_find_element(self, arguments: dict[str, Any], started: float) -> ToolEnvelope:
        """Resolve a description to candidates without acting (section 43)."""
        state = self._cache.current
        if state is None:
            return ToolEnvelope.failure(
                ErrorCode.CAPTURE_FAILED,
                "no screen state has been observed yet",
                elapsed_ms=elapsed_ms_since(started),
            )
        try:
            query = ElementQuery(
                text=str(arguments["target"]),
                role_hint=_role_hint(ToolName.FIND_ELEMENT, arguments),
                context=_optional_str(arguments, "context"),
                window_id=arguments.get("window_id"),
            )
        except (ToolProtocolError, ValidationError) as exc:
            self._refuse(ToolName.FIND_ELEMENT)
            return ToolEnvelope.failure(
                ErrorCode.INTERNAL_ERROR,
                f"find_element query is invalid: {exc}",
                elapsed_ms=elapsed_ms_since(started),
            )
        resolution = self._resolver.resolve(
            query, state.elements, occluders=state.elements, state=state
        )
        fallback_data: dict[str, Any] | None = None
        if (
            not resolution.is_resolved
            and resolution.status is not ResolutionStatus.AMBIGUOUS
            and self._target_fallback is not None
        ):
            # A deterministic miss may still be resolvable by the later section 43
            # stages (OCR over the relevant regions, then gated visual grounding).
            # The fallback accepts the enriched observation into the cache and the
            # query is resolved again in full -- ambiguity is still ambiguity.
            try:
                enriched = self._target_fallback(query)
            except BlaxcyError as exc:
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
                }
            elif fallback_data is None:
                fallback_data = {"attempted": True, "resolved": False}
        payload = resolution.to_dict()
        if fallback_data is not None:
            payload["resolution_fallback"] = fallback_data
        # Ambiguity is reported as a failure so the Brain cannot read a candidate
        # list as "here is the one to click" (section 43's absolute ambiguity rule).
        if resolution.status is ResolutionStatus.AMBIGUOUS:
            return ToolEnvelope.failure(
                ErrorCode.TARGET_AMBIGUOUS,
                resolution.reason,
                data={"resolution": payload},
                state_version=state.state_version,
                frame_id=state.frame_id,
                elapsed_ms=elapsed_ms_since(started),
            )
        if resolution.status is not ResolutionStatus.RESOLVED:
            return ToolEnvelope.failure(
                ErrorCode.TARGET_NOT_FOUND,
                resolution.reason,
                data={"resolution": payload},
                state_version=state.state_version,
                frame_id=state.frame_id,
                elapsed_ms=elapsed_ms_since(started),
            )
        return ToolEnvelope.success(
            data={"resolution": payload},
            state_version=state.state_version,
            frame_id=state.frame_id,
            elapsed_ms=elapsed_ms_since(started),
        )

    def _tool_describe_region(self, arguments: dict[str, Any], started: float) -> ToolEnvelope:
        """Report what BLAXCY really perceives inside a desktop region.

        This is accessibility perception only, and it never uploads pixels: a
        read-only query must not spend the user's screen on a model call without
        an explicit ask. The visual-grounding *availability* is reported from the
        capability probe, so the field is truthful instead of a stale "not
        implemented" claim (sections 8, 41, 80).
        """
        state = self._cache.current
        if state is None:
            return ToolEnvelope.failure(
                ErrorCode.CAPTURE_FAILED,
                "no screen state has been observed yet",
                elapsed_ms=elapsed_ms_since(started),
            )
        region = Rect(
            x=float(arguments["x"]),
            y=float(arguments["y"]),
            width=float(arguments["width"]),
            height=float(arguments["height"]),
            space=CoordinateSpace.DESKTOP,
        )
        if region.width <= 0 or region.height <= 0:
            return ToolEnvelope.failure(
                ErrorCode.INTERNAL_ERROR,
                "describe_region requires a positive width and height",
                elapsed_ms=elapsed_ms_since(started),
            )
        report = self._capabilities() if self._capabilities is not None else None
        visual = (
            report.get(CapabilityName.VISUAL_GROUNDING) if report is not None else None
        )
        visual_available = visual is not None and visual.status is CapabilityStatus.AVAILABLE
        if visual_available:
            visual_reason: str | None = None
        elif visual is None:
            visual_reason = "no capability report is available"
        else:
            visual_reason = visual.reason or visual.status.value
        inside: list[UIElement] = []
        for element in state.elements:
            box = element.bbox
            if box is None or box.space is not CoordinateSpace.DESKTOP:
                continue
            if box.intersects(region):
                inside.append(element)
        texts = [
            element.text
            for element in inside
            if element.text and not element.is_password
        ]
        return ToolEnvelope.success(
            data={
                "region": region.to_dict(),
                "elements": [element_summary(element) for element in inside[:MAX_CONTEXT_ELEMENTS]],
                "element_count": len(inside),
                "text_fragments": texts[:MAX_CONTEXT_ELEMENTS],
                "visual_description": {
                    "available": visual_available,
                    "reason": visual_reason,
                    "note": "this call performs no model upload; ask find_element for "
                    "the section 43 fallback cascade",
                },
            },
            state_version=state.state_version,
            frame_id=state.frame_id,
            elapsed_ms=elapsed_ms_since(started),
        )

    def _tool_wait_for_change(self, arguments: dict[str, Any], started: float) -> ToolEnvelope:
        """Wait for real observed change, with a hard timeout (section 34)."""
        timeout_ms = max(0, int(arguments.get("timeout_ms", 2000)))
        requested = str(arguments.get("min_change_class", ChangeClass.MEANINGFUL.value))
        try:
            min_class = ChangeClass(requested)
        except ValueError:
            self._refuse(ToolName.WAIT_FOR_CHANGE)
            return ToolEnvelope.failure(
                ErrorCode.INTERNAL_ERROR,
                f"unknown min_change_class {requested!r}",
                elapsed_ms=elapsed_ms_since(started),
            )
        deadline = self._clock() + timeout_ms / 1000.0
        before = self._cache.current
        observed: ScreenState | None = before
        while True:
            abort = self._abort_code()
            if abort is not None:
                return ToolEnvelope.failure(abort, "automation was stopped while waiting")
            observed = self._refresh()
            delta = self._cache.delta
            if (
                observed is not None
                and before is not None
                and (observed.state_version != before.state_version or observed.frame_id != before.frame_id)
                and delta is not None
                and _at_least(delta.change_class, min_class)
            ):
                return ToolEnvelope.success(
                    data={
                        "changed": True,
                        "change_class": delta.change_class.value,
                        "regions": [region.rect.to_dict() for region in delta.regions],
                        "state": observed.to_model_context(),
                    },
                    state_version=observed.state_version,
                    frame_id=observed.frame_id,
                    elapsed_ms=elapsed_ms_since(started),
                )
            if self._clock() >= deadline:
                return ToolEnvelope.success(
                    data={
                        "changed": False,
                        "change_class": None,
                        "timeout_ms": timeout_ms,
                        "state": None if observed is None else observed.to_model_context(),
                    },
                    state_version=None if observed is None else observed.state_version,
                    frame_id=None if observed is None else observed.frame_id,
                    elapsed_ms=elapsed_ms_since(started),
                )
            self._sleep(min(0.05, max(0.0, deadline - self._clock())))

    def _tool_wait_for_element(self, arguments: dict[str, Any], started: float) -> ToolEnvelope:
        """Wait until a target description resolves, with a hard timeout."""
        timeout_ms = max(0, int(arguments.get("timeout_ms", 5000)))
        poll_ms = max(10, int(arguments.get("poll_ms", 100)))
        try:
            query = ElementQuery(
                text=str(arguments["target"]),
                role_hint=_role_hint(ToolName.WAIT_FOR_ELEMENT, arguments),
                context=_optional_str(arguments, "context"),
            )
        except (ToolProtocolError, ValidationError) as exc:
            self._refuse(ToolName.WAIT_FOR_ELEMENT)
            return ToolEnvelope.failure(
                ErrorCode.INTERNAL_ERROR,
                f"wait_for_element query is invalid: {exc}",
                elapsed_ms=elapsed_ms_since(started),
            )
        deadline = self._clock() + timeout_ms / 1000.0
        state: ScreenState | None = None
        while True:
            abort = self._abort_code()
            if abort is not None:
                return ToolEnvelope.failure(abort, "automation was stopped while waiting")
            state = self._refresh()
            if state is not None:
                resolution = self._resolver.resolve(
                    query, state.elements, occluders=state.elements, state=state
                )
                if resolution.status is ResolutionStatus.RESOLVED:
                    return ToolEnvelope.success(
                        data={"found": True, "resolution": resolution.to_dict()},
                        state_version=state.state_version,
                        frame_id=state.frame_id,
                        elapsed_ms=elapsed_ms_since(started),
                    )
                if resolution.status is ResolutionStatus.AMBIGUOUS:
                    return ToolEnvelope.failure(
                        ErrorCode.TARGET_AMBIGUOUS,
                        resolution.reason,
                        data={"found": False, "resolution": resolution.to_dict()},
                        state_version=state.state_version,
                        frame_id=state.frame_id,
                        elapsed_ms=elapsed_ms_since(started),
                    )
            if self._clock() >= deadline:
                return ToolEnvelope.success(
                    data={
                        "found": False,
                        "timeout_ms": timeout_ms,
                        "state": None if state is None else state.to_model_context(),
                    },
                    state_version=None if state is None else state.state_version,
                    frame_id=None if state is None else state.frame_id,
                    elapsed_ms=elapsed_ms_since(started),
                )
            self._sleep(poll_ms / 1000.0)

    def _tool_emergency_stop(self, arguments: dict[str, Any], started: float) -> ToolEnvelope:
        """Trigger the real, latched stop (section 63)."""
        if self._stop is None:
            return ToolEnvelope.failure(
                ErrorCode.BACKEND_UNAVAILABLE,
                "the emergency stop is not wired; the Brain must not assume a stop happened",
                elapsed_ms=elapsed_ms_since(started),
            )
        reason = _optional_str(arguments, "reason") or "requested by the Brain"
        result = self._stop.trigger(reason=reason)
        return ToolEnvelope.success(
            data={"stop": result.to_dict(), "latched": True},
            elapsed_ms=elapsed_ms_since(started),
        )

    # -- Sequences (section 66.1) --------------------------------------------

    def _dispatch_sequence(
        self,
        call: ToolCall,
        started: float,
        *,
        task_id: str | None,
        confirmed: bool,
    ) -> ToolEnvelope:
        """Parse and (when a runner exists) execute a ``run_sequence`` plan."""
        try:
            request = _parse_sequence_request(call.arguments)
        except ToolProtocolError as exc:
            self._refuse(call.name)
            return ToolEnvelope.failure(
                exc.code, exc.message, data={"details": exc.details}, elapsed_ms=elapsed_ms_since(started)
            )
        except ValidationError as exc:
            self._refuse(call.name)
            return ToolEnvelope.failure(
                ErrorCode.INTERNAL_ERROR,
                "run_sequence arguments failed schema validation",
                data={"errors": exc.error_count()},
                elapsed_ms=elapsed_ms_since(started),
            )

        limit = self._settings.sequence.max_sequence_steps
        if len(request.steps) > limit:
            self._refuse(call.name)
            return ToolEnvelope.failure(
                ErrorCode.SEQUENCE_STEP_LIMIT_EXCEEDED,
                f"the plan has {len(request.steps)} steps; the limit is {limit}",
                data={"steps": len(request.steps), "max_sequence_steps": limit},
                elapsed_ms=elapsed_ms_since(started),
            )

        if self._sequences is None or not self._settings.sequence.enabled:
            self._refuse(call.name)
            reason = (
                "no sequence runner is wired into this dispatcher"
                if self._sequences is None
                else "sequence execution is disabled by configuration"
            )
            return ToolEnvelope.failure(
                ErrorCode.BACKEND_UNAVAILABLE,
                f"run_sequence is unavailable: {reason}",
                elapsed_ms=elapsed_ms_since(started),
            )

        result = self._sequences.run(request, task_id=task_id, confirmed=confirmed)
        data = result.to_dict()
        if result.halted:
            # The specific code (SEQUENCE_WALL_CLOCK_EXCEEDED, a step's own
            # refusal, ...) is more useful to the Brain than a generic halt.
            return ToolEnvelope.failure(
                result.halt_code or ErrorCode.SEQUENCE_HALTED,
                result.halt_reason or "the sequence halted",
                data=data,
                elapsed_ms=elapsed_ms_since(started),
            )
        return ToolEnvelope.success(data=data, elapsed_ms=elapsed_ms_since(started))

    # -- Internals ------------------------------------------------------------

    def _refresh(self) -> ScreenState | None:
        """Re-perceive through the injected perceiver, or read the cache as-is."""
        if self._perceive is None:
            return self._cache.current
        try:
            observed = self._perceive()
        except BlaxcyError:
            return self._cache.current
        return observed if observed is not None else self._cache.current

    def _abort_code(self) -> ErrorCode | None:
        """The latched stop/takeover code, if any."""
        return None if self._abort_check is None else self._abort_check()

    def _refuse(self, tool: str) -> None:
        """Record a refusal in the dispatch stats."""
        self.stats.refused[tool] = self.stats.refused.get(tool, 0) + 1


def _parse_sequence_request(arguments: dict[str, Any]) -> RunSequenceRequest:
    """Build a :class:`RunSequenceRequest` from raw ``run_sequence`` arguments."""
    _validate_keys(ToolName.RUN_SEQUENCE, arguments)
    steps = arguments.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ToolProtocolError(
            ErrorCode.INTERNAL_ERROR,
            "run_sequence requires a non-empty 'steps' list",
            details={"tool": ToolName.RUN_SEQUENCE},
        )
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            raise ToolProtocolError(
                ErrorCode.INTERNAL_ERROR,
                f"sequence step {index} must be an object",
                details={"index": index},
            )
        if str(step.get("tool", "")) not in SEQUENCE_STEP_TOOLS:
            raise ToolProtocolError(
                ErrorCode.BACKEND_UNAVAILABLE,
                f"sequence step {index} names a tool that cannot be a step: {step.get('tool')!r}",
                details={"index": index, "tool": step.get("tool")},
            )
    payload: dict[str, Any] = {"steps": steps}
    if arguments.get("halt_on") is not None:
        payload["halt_on"] = arguments["halt_on"]
    return RunSequenceRequest.model_validate(payload)


def _at_least(observed: ChangeClass, minimum: ChangeClass) -> bool:
    """True when ``observed`` is at least as severe as ``minimum``."""
    return CHANGE_CLASS_SEVERITY[observed] >= CHANGE_CLASS_SEVERITY[minimum]


def _clamp(value: int, low: int, high: int) -> int:
    """Clamp ``value`` into ``[low, high]``."""
    return max(low, min(high, value))


def element_summary(element: UIElement, *, include_geometry: bool = True) -> dict[str, Any]:
    """A safe element summary for the Brain (sections 43, 68).

    Credential content is structurally impossible here: a password element's
    ``text`` is reported as ``None`` and its label is taken from
    ``accessible_name`` (the field's *name*, never its value), because a
    password value must never reach model context (sections 42, 55).
    """
    summary: dict[str, Any] = {
        "element_id": element.element_id,
        "role": element.role.value,
        "text": None if element.is_password else element.text,
        "accessible_name": element.accessible_name,
        "source": element.source.value,
        "confidence": element.confidence,
        "clickable": element.effective_clickable or element.clickable,
        "enabled": element.enabled,
        "focused": element.focused,
        "occluded": element.occluded,
        "password": element.is_password,
        "owner_window_id": element.owner_window_id,
    }
    if include_geometry and element.bbox is not None:
        summary["bbox"] = element.bbox.to_dict()
    return summary


def protocol_snapshot() -> dict[str, Any]:
    """A loggable summary of the advertised protocol (no secrets, no state)."""
    return {
        "tools": [declaration.name for declaration in tool_declarations()],
        "read_only_tools": sorted(READ_ONLY_TOOLS),
        "max_context_elements": MAX_CONTEXT_ELEMENTS,
    }


__all__ = [
    "FORBIDDEN_ARGUMENT_KEYS",
    "MAX_CONTEXT_ELEMENTS",
    "READ_ONLY_TOOLS",
    "READ_SLOT_TIMEOUT_SECONDS",
    "TOOL_DESCRIPTIONS",
    "TOOL_PARAMETER_SCHEMAS",
    "DispatchStats",
    "SequenceRunner",
    "ToolCall",
    "ToolDeclaration",
    "ToolDispatcher",
    "ToolProtocolError",
    "build_planned_action",
    "element_query",
    "element_summary",
    "parse_tool_arguments",
    "protocol_snapshot",
    "tool_declarations",
]
