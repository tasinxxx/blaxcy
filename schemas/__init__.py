"""BLAXCY typed schemas.

Everything that crosses a module boundary in BLAXCY is a typed, validated
schema. Schemas are the contract between perception, policy, control and the
Brain; they are deliberately strict so that invalid state cannot propagate.

Phase 0 established the vocabulary (capabilities, errors, core enums).
Phase 1 adds the perception/action contract: coordinate geometry, screen
state, UI elements, element leases, the tool envelope and the ``run_sequence``
schemas.
"""

from schemas.actions import (
    REQUIRED_TOOLS,
    SEQUENCE_STEP_TOOLS,
    TOOL_ACTION_CLASS,
    PlannedAction,
    ToolEnvelope,
    ToolName,
    action_class_for,
    elapsed_ms_since,
    most_restrictive_action_class,
    requires_verification,
)
from schemas.capability import Capability, CapabilityReport
from schemas.elements import (
    ElementCandidate,
    ElementQuery,
    UIElement,
    identity_fingerprint,
    perception_confidence,
)
from schemas.enums import (
    ACTION_CLASS_SEVERITY,
    CHANGE_CLASS_SEVERITY,
    PASSWORD_ROLES,
    SOURCE_BASE_CONFIDENCE,
    TEXT_ENTRY_ROLES,
    ActionClass,
    CapabilityName,
    CapabilityStatus,
    ChangeClass,
    CoordinateSpace,
    ErrorCode,
    PerceptionSource,
    PhaseStatus,
    PolicyMode,
    UIRole,
    VerificationState,
)
from schemas.errors import BlaxcyError
from schemas.events import (
    EmergencyStopEvent,
    Event,
    EventType,
    HumanTakeoverEvent,
    ModeChangedEvent,
    SequenceCompletedEvent,
    SequenceEvent,
    SequenceHaltedEvent,
    SequenceStartedEvent,
    SequenceStepCompletedEvent,
)
from schemas.geometry import (
    Affine2D,
    GeometryMap,
    MonitorGeometry,
    MonitorInputTransform,
    MonitorLayout,
    Point,
    Rect,
    Size,
)
from schemas.leases import DEFAULT_LEASE_TTL_MS, ElementLease, issue_lease
from schemas.screen_state import (
    ChangeRegion,
    ScreenDelta,
    ScreenState,
    WindowStackEntry,
    classify_delta,
)
from schemas.sequences import (
    DEFAULT_HALT_CONDITIONS,
    FORBIDDEN_STEP_KEYS,
    HaltCondition,
    RunSequenceRequest,
    SequenceProgress,
    SequenceResult,
    SequenceStep,
    SequenceStepResult,
)

__all__ = [
    "ACTION_CLASS_SEVERITY",
    "CHANGE_CLASS_SEVERITY",
    "DEFAULT_HALT_CONDITIONS",
    "DEFAULT_LEASE_TTL_MS",
    "FORBIDDEN_STEP_KEYS",
    "PASSWORD_ROLES",
    "REQUIRED_TOOLS",
    "SEQUENCE_STEP_TOOLS",
    "SOURCE_BASE_CONFIDENCE",
    "TEXT_ENTRY_ROLES",
    "TOOL_ACTION_CLASS",
    "ActionClass",
    "Affine2D",
    "BlaxcyError",
    "Capability",
    "CapabilityName",
    "CapabilityReport",
    "CapabilityStatus",
    "ChangeClass",
    "ChangeRegion",
    "CoordinateSpace",
    "ElementCandidate",
    "ElementLease",
    "ElementQuery",
    "EmergencyStopEvent",
    "ErrorCode",
    "Event",
    "EventType",
    "GeometryMap",
    "HaltCondition",
    "HumanTakeoverEvent",
    "ModeChangedEvent",
    "MonitorGeometry",
    "MonitorInputTransform",
    "MonitorLayout",
    "PerceptionSource",
    "PhaseStatus",
    "PlannedAction",
    "Point",
    "PolicyMode",
    "Rect",
    "RunSequenceRequest",
    "ScreenDelta",
    "ScreenState",
    "SequenceCompletedEvent",
    "SequenceEvent",
    "SequenceHaltedEvent",
    "SequenceProgress",
    "SequenceResult",
    "SequenceStartedEvent",
    "SequenceStep",
    "SequenceStepCompletedEvent",
    "SequenceStepResult",
    "Size",
    "ToolEnvelope",
    "ToolName",
    "UIElement",
    "UIRole",
    "VerificationState",
    "WindowStackEntry",
    "action_class_for",
    "classify_delta",
    "elapsed_ms_since",
    "identity_fingerprint",
    "issue_lease",
    "most_restrictive_action_class",
    "perception_confidence",
    "requires_verification",
]
