"""The error taxonomy must match specification section 77 exactly."""

from __future__ import annotations

from schemas.enums import ACTION_CLASS_SEVERITY, ActionClass, ErrorCode
from schemas.errors import BlaxcyError

#: The complete section 77 taxonomy, transcribed verbatim from the spec.
SPEC_ERROR_CODES = {
    "TARGET_NOT_FOUND",
    "TARGET_AMBIGUOUS",
    "TARGET_STALE",
    "TARGET_OCCLUDED",
    "TARGET_OFFSCREEN",
    "FOCUS_MISMATCH",
    "PERMISSION_DENIED",
    "BLOCKED_APPLICATION",
    "CONFIRMATION_REQUIRED",
    "CONFIRMATION_DENIED",
    "BACKEND_UNAVAILABLE",
    "CAPTURE_FAILED",
    "A11Y_TIMEOUT",
    "OCR_FAILED",
    "VISUAL_FALLBACK_DENIED",
    "EMERGENCY_STOP_ACTIVE",
    "HUMAN_TAKEOVER",
    "RATE_LIMITED",
    "MODEL_ERROR",
    "INTERNAL_ERROR",
    "CALIBRATION_FAILED",
    "NOT_EXECUTED",
    "UNSUPPORTED_PLATFORM",
    "UNICODE_UNSUPPORTED",
    "CLIPBOARD_FAILED",
    "VERIFICATION_UNVERIFIED",
    "VERIFICATION_CONTRADICTED",
    "SEQUENCE_HALTED",
    "SEQUENCE_STEP_LIMIT_EXCEEDED",
    "SEQUENCE_WALL_CLOCK_EXCEEDED",
}


def test_error_taxonomy_is_complete() -> None:
    """Every section 77 code exists, and no unexpected code was invented."""
    assert {code.value for code in ErrorCode} == SPEC_ERROR_CODES


def test_error_codes_are_unique() -> None:
    """Enum values must not collide."""
    values = [code.value for code in ErrorCode]
    assert len(values) == len(set(values))


def test_blaxcy_error_carries_structured_payload() -> None:
    """A failure must expose a code, message, details and retryability."""
    err = BlaxcyError(ErrorCode.TARGET_AMBIGUOUS, "two candidates matched", details={"n": 2})
    payload = err.to_dict()
    assert payload["error_code"] == "TARGET_AMBIGUOUS"
    assert payload["message"] == "two candidates matched"
    assert payload["details"] == {"n": 2}
    # Retry is never inferred from the code; it defaults to False.
    assert payload["retryable"] is False


def test_run_sequence_is_not_an_action_class() -> None:
    """Section 57: run_sequence is a container, not a fifth action class."""
    assert {cls.value for cls in ActionClass} == {
        "READ_ONLY",
        "NAVIGATIONAL",
        "MUTATING",
        "DESTRUCTIVE",
    }


def test_action_class_severity_is_ordered() -> None:
    """Most-restrictive-wins gating depends on this ordering."""
    assert ACTION_CLASS_SEVERITY[ActionClass.READ_ONLY] < ACTION_CLASS_SEVERITY[ActionClass.NAVIGATIONAL]
    assert ACTION_CLASS_SEVERITY[ActionClass.NAVIGATIONAL] < ACTION_CLASS_SEVERITY[ActionClass.MUTATING]
    assert ACTION_CLASS_SEVERITY[ActionClass.MUTATING] < ACTION_CLASS_SEVERITY[ActionClass.DESTRUCTIVE]
