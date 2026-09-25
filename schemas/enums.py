"""Shared BLAXCY enums.

These mirror the specification's vocabulary exactly. Every value here is part
of the engineering contract and is referenced by section number where the
specification defines it.
"""

from __future__ import annotations

# ``enum.StrEnum`` (Python 3.11+) gives str-valued enums that serialise to
# JSON/TOML without a custom encoder, which matters because every BLAXCY
# boundary (tool envelope, logs, capability report) is JSON.
from enum import StrEnum


class CapabilityStatus(StrEnum):
    """Specification section 28: exactly one of three honest states."""

    AVAILABLE = "AVAILABLE"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"


class CapabilityName(StrEnum):
    """Specification section 28: the required capability set."""

    CAPTURE = "capture"
    ACCESSIBILITY = "accessibility"
    MOUSE = "mouse"
    KEYBOARD = "keyboard"
    POINTER_READBACK = "pointer_readback"
    OCR = "ocr"
    CLIPBOARD = "clipboard"
    WINDOW_INFO = "window_info"
    BROWSER_ACCESSIBILITY = "browser_accessibility"
    VISUAL_GROUNDING = "visual_grounding"
    BRAIN = "brain"
    SEQUENCE_EXECUTION = "sequence_execution"


class PolicyMode(StrEnum):
    """Specification section 56: policy modes."""

    OBSERVE = "OBSERVE"
    ASSIST = "ASSIST"
    AUTONOMOUS = "AUTONOMOUS"


class ActionClass(StrEnum):
    """Specification section 57: the four action classes.

    ``run_sequence`` (section 66.1) is deliberately *not* a member: it is a
    container whose gate is the most restrictive class among its steps.
    """

    READ_ONLY = "READ_ONLY"
    NAVIGATIONAL = "NAVIGATIONAL"
    MUTATING = "MUTATING"
    DESTRUCTIVE = "DESTRUCTIVE"


#: Ordering used to compute "most restrictive" class for a sequence gate.
ACTION_CLASS_SEVERITY: dict[ActionClass, int] = {
    ActionClass.READ_ONLY: 0,
    ActionClass.NAVIGATIONAL: 1,
    ActionClass.MUTATING: 2,
    ActionClass.DESTRUCTIVE: 3,
}


class ChangeClass(StrEnum):
    """Specification section 34: change classification."""

    NONE = "NONE"
    TRIVIAL = "TRIVIAL"
    ANIMATION = "ANIMATION"
    MEANINGFUL = "MEANINGFUL"
    MAJOR = "MAJOR"


#: Ordering used to compute the overall severity of a set of change regions.
CHANGE_CLASS_SEVERITY: dict[ChangeClass, int] = {
    ChangeClass.NONE: 0,
    ChangeClass.TRIVIAL: 1,
    ChangeClass.ANIMATION: 2,
    ChangeClass.MEANINGFUL: 3,
    ChangeClass.MAJOR: 4,
}

#: The change classes that count as *structural*: only these invalidate a
#: perception snapshot, a resolver-cache hint or a section 33.2 speculation,
#: and only these force a full re-validation of the region they cover. A
#: ``TRIVIAL``/``ANIMATION`` change deliberately invalidates nothing, so a
#: spinner elsewhere on screen cannot throw away a correct resolution.
SIGNIFICANT_CHANGE_CLASSES: frozenset[ChangeClass] = frozenset(
    {ChangeClass.MEANINGFUL, ChangeClass.MAJOR}
)


class UIRole(StrEnum):
    """Specification section 37: the perception element role vocabulary."""

    BUTTON = "BUTTON"
    TOGGLE = "TOGGLE"
    CHECKBOX = "CHECKBOX"
    RADIO = "RADIO"
    LINK = "LINK"
    TEXT_INPUT = "TEXT_INPUT"
    PASSWORD_INPUT = "PASSWORD_INPUT"
    COMBO_BOX = "COMBO_BOX"
    MENU = "MENU"
    MENU_ITEM = "MENU_ITEM"
    TAB = "TAB"
    LIST_ITEM = "LIST_ITEM"
    TREE_ITEM = "TREE_ITEM"
    SLIDER = "SLIDER"
    HEADING = "HEADING"
    LABEL = "LABEL"
    IMAGE = "IMAGE"
    VIDEO = "VIDEO"
    CANVAS = "CANVAS"
    DIALOG = "DIALOG"
    TOOLBAR = "TOOLBAR"
    DOCUMENT = "DOCUMENT"
    TERMINAL = "TERMINAL"
    TEXT_FRAGMENT = "TEXT_FRAGMENT"
    UNKNOWN = "UNKNOWN"


#: Roles whose contents are credential-like and must never be read, OCR'd,
#: cached or uploaded (sections 42, 55).
PASSWORD_ROLES: frozenset[UIRole] = frozenset({UIRole.PASSWORD_INPUT})

#: Roles that accept typed text (used by the focus guard, section 51).
TEXT_ENTRY_ROLES: frozenset[UIRole] = frozenset(
    {UIRole.TEXT_INPUT, UIRole.PASSWORD_INPUT, UIRole.TERMINAL, UIRole.DOCUMENT}
)


class PerceptionSource(StrEnum):
    """Specification sections 37/38/39: where an element observation came from.

    The order here is also the perception priority order (section 38):
    ``ATSPI`` then ``DOM`` then ``OCR`` then ``VISUAL``.
    """

    ATSPI = "ATSPI"
    DOM = "DOM"
    OCR = "OCR"
    VISUAL = "VISUAL"


#: Source base confidence rates (specification section 39), before the
#: ``geometry_sanity``, ``freshness`` and ``source_consistency`` multipliers.
SOURCE_BASE_CONFIDENCE: dict[PerceptionSource, float] = {
    PerceptionSource.ATSPI: 0.95,
    PerceptionSource.DOM: 0.92,
    PerceptionSource.OCR: 0.75,
    PerceptionSource.VISUAL: 0.80,
}


class CoordinateSpace(StrEnum):
    """Specification section 31: a coordinate never exists without its space."""

    FRAME = "FRAME"
    MONITOR = "MONITOR"
    DESKTOP = "DESKTOP"
    INPUT = "INPUT"


class VerificationState(StrEnum):
    """Specification section 60: verification semantics.

    ``VERIFIED`` and ``UNVERIFIED`` describe an executed action;
    ``CONTRADICTED`` describes evidence that it did not do what was intended;
    ``NOT_APPLICABLE`` is used for read-only work where no postcondition exists.
    """

    VERIFIED = "VERIFIED"
    UNVERIFIED = "UNVERIFIED"
    CONTRADICTED = "CONTRADICTED"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class PhaseStatus(StrEnum):
    """Specification section 20: implementation state vocabulary."""

    NOT_STARTED = "NOT_STARTED"
    IN_PROGRESS = "IN_PROGRESS"
    IMPLEMENTED = "IMPLEMENTED"
    VERIFIED = "VERIFIED"
    COMPLETE = "COMPLETE"
    BLOCKED = "BLOCKED"


class SessionType(StrEnum):
    """Specification section 29/30: detected display session flavour."""

    X11 = "x11"
    XWAYLAND = "xwayland"
    WAYLAND = "wayland"
    UNKNOWN = "unknown"


class ErrorCode(StrEnum):
    """Specification section 77: the complete structured error taxonomy.

    Every user-visible failure maps to one of these values. Values are policy
    identifiers, not free text; the human-readable message travels separately.
    """

    TARGET_NOT_FOUND = "TARGET_NOT_FOUND"
    TARGET_AMBIGUOUS = "TARGET_AMBIGUOUS"
    TARGET_STALE = "TARGET_STALE"
    TARGET_OCCLUDED = "TARGET_OCCLUDED"
    TARGET_OFFSCREEN = "TARGET_OFFSCREEN"
    FOCUS_MISMATCH = "FOCUS_MISMATCH"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    BLOCKED_APPLICATION = "BLOCKED_APPLICATION"
    CONFIRMATION_REQUIRED = "CONFIRMATION_REQUIRED"
    CONFIRMATION_DENIED = "CONFIRMATION_DENIED"
    BACKEND_UNAVAILABLE = "BACKEND_UNAVAILABLE"
    CAPTURE_FAILED = "CAPTURE_FAILED"
    A11Y_TIMEOUT = "A11Y_TIMEOUT"
    OCR_FAILED = "OCR_FAILED"
    VISUAL_FALLBACK_DENIED = "VISUAL_FALLBACK_DENIED"
    EMERGENCY_STOP_ACTIVE = "EMERGENCY_STOP_ACTIVE"
    HUMAN_TAKEOVER = "HUMAN_TAKEOVER"
    RATE_LIMITED = "RATE_LIMITED"
    MODEL_ERROR = "MODEL_ERROR"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    CALIBRATION_FAILED = "CALIBRATION_FAILED"
    NOT_EXECUTED = "NOT_EXECUTED"
    UNSUPPORTED_PLATFORM = "UNSUPPORTED_PLATFORM"
    UNICODE_UNSUPPORTED = "UNICODE_UNSUPPORTED"
    CLIPBOARD_FAILED = "CLIPBOARD_FAILED"
    VERIFICATION_UNVERIFIED = "VERIFICATION_UNVERIFIED"
    VERIFICATION_CONTRADICTED = "VERIFICATION_CONTRADICTED"
    SEQUENCE_HALTED = "SEQUENCE_HALTED"
    SEQUENCE_STEP_LIMIT_EXCEEDED = "SEQUENCE_STEP_LIMIT_EXCEEDED"
    SEQUENCE_WALL_CLOCK_EXCEEDED = "SEQUENCE_WALL_CLOCK_EXCEEDED"
