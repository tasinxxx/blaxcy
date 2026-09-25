"""Policy guards (specification sections 31, 42, 55, 58).

Each guard answers one narrow safety question and returns a
:class:`GuardOutcome`. Guards never perform input and never raise for an
expected denial -- a refusal is a value, so the caller can report the exact
taxonomy code and keep the pipeline's control flow explicit.

The guards are deliberately *fail-closed*: a guard that cannot establish that
proceeding is safe reports the failure rather than assuming permission. A guard
that could be talked out of its verdict would be a second, weaker safety
authority beside the executor, which is exactly what section 13 forbids.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from core.calibration import Calibration
from schemas.capability import CapabilityReport
from schemas.enums import CapabilityName, CapabilityStatus, ErrorCode, PolicyMode


@dataclass(frozen=True)
class GuardOutcome:
    """The verdict of one guard.

    ``sensitive`` marks a permitted action that involves credential-like context
    (typing into a password field): allowed, but its content must never be
    logged, cached, sent to the Brain or uploaded (sections 42, 55, 70).
    """

    ok: bool
    code: ErrorCode | None = None
    message: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    sensitive: bool = False

    @classmethod
    def allow(cls, *, sensitive: bool = False) -> GuardOutcome:
        """A passing verdict."""
        return cls(ok=True, sensitive=sensitive)

    @classmethod
    def deny(
        cls,
        code: ErrorCode,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> GuardOutcome:
        """A failing verdict carrying one taxonomy code."""
        return cls(ok=False, code=code, message=message, details=details or {})


def _matches(value: str | None, needles: Sequence[str]) -> str | None:
    """Return the first needle contained in ``value`` (case-folded), or ``None``."""
    if not value:
        return None
    folded = value.casefold()
    for needle in needles:
        if needle and needle.casefold() in folded:
            return needle
    return None


def match_application(
    candidates: Sequence[str | None],
    needles: Sequence[str],
) -> str | None:
    """Return the first matching pattern against any candidate string.

    Matching is a case-folded substring test against every supplied candidate
    (application name, window title, window class): a deny-list that a renamed
    window could walk around would not be a deny-list.
    """
    for candidate in candidates:
        hit = _matches(candidate, needles)
        if hit is not None:
            return hit
    return None


def check_blocked_application(
    candidates: Sequence[str | None],
    blocked: Sequence[str],
) -> GuardOutcome:
    """Section 58: refuse to act while a blocked application is in context.

    A blocked application means no input and no autonomous override. Visual
    upload is separately refused by :func:`check_protected_application`.
    """
    if not blocked:
        return GuardOutcome.allow()
    matched = match_application(candidates, blocked)
    if matched is None:
        return GuardOutcome.allow()
    return GuardOutcome.deny(
        ErrorCode.BLOCKED_APPLICATION,
        f"application matching {matched!r} is on the blocked list",
        details={"matched": matched},
    )


def check_protected_application(
    candidates: Sequence[str | None],
    protected: Sequence[str],
) -> GuardOutcome:
    """Section 42: never upload content from a protected application.

    This is a privacy verdict, not a permission one: a protected application may
    still be interacted with, but its content must not reach a visual model.
    """
    if not protected:
        return GuardOutcome.allow()
    matched = match_application(candidates, protected)
    if matched is None:
        return GuardOutcome.allow()
    return GuardOutcome.deny(
        ErrorCode.VISUAL_FALLBACK_DENIED,
        f"content from {matched!r} is protected and must not be uploaded",
        details={"matched": matched},
    )


#: Modes in which section 41 permits a visual-grounding upload.
VISUAL_FALLBACK_MODES: frozenset[PolicyMode] = frozenset(
    {PolicyMode.ASSIST, PolicyMode.AUTONOMOUS}
)


def check_visual_fallback(
    *,
    mode: PolicyMode,
    enabled: bool = True,
    application: Sequence[str | None] = (),
    protected: Sequence[str] = (),
    blocked: Sequence[str] = (),
) -> GuardOutcome:
    """Section 41/42: may current content be uploaded to a visual model?

    This is the one place the visual-grounding decision is composed, so the rule
    lives beside the matchers it uses rather than being re-implemented per caller.
    A visual upload is the only perception path that leaves the machine, so the
    order is fail-closed and every branch is a refusal:

    1. a blocked application (section 58) -- no visual upload at all;
    2. a protected application (section 42) -- content must never be uploaded;
    3. the capability disabled in configuration;
    4. a mode below ``ASSIST`` (section 56 starts every fresh run in OBSERVE, so
       this is the default state).

    The grounder enforces the mode gate again on its own path, and independently
    refuses any region overlapping credential geometry (sections 42, 55). A guard
    that could be bypassed by calling the grounder directly would not be a guard.
    """
    if blocked:
        matched = match_application(application, blocked)
        if matched is not None:
            return GuardOutcome.deny(
                ErrorCode.BLOCKED_APPLICATION,
                f"application matching {matched!r} is blocked, so nothing may be uploaded",
                details={"matched": matched},
            )
    if protected:
        matched = match_application(application, protected)
        if matched is not None:
            return GuardOutcome.deny(
                ErrorCode.VISUAL_FALLBACK_DENIED,
                f"content from {matched!r} is protected and must not be uploaded",
                details={"matched": matched},
            )
    if not enabled:
        return GuardOutcome.deny(
            ErrorCode.BACKEND_UNAVAILABLE,
            "visual grounding is disabled by configuration",
            details={"capability": CapabilityName.VISUAL_GROUNDING.value},
        )
    if mode not in VISUAL_FALLBACK_MODES:
        return GuardOutcome.deny(
            ErrorCode.VISUAL_FALLBACK_DENIED,
            f"visual grounding requires mode >= ASSIST (section 41), not {mode.value}",
            details={"mode": mode.value},
        )
    return GuardOutcome.allow()


#: Tools that read element *content* back out of the desktop. These must never be
#: pointed at a credential field: the content would then be a candidate for
#: model context or a log line (sections 42, 55).
_CONTENT_READING_TOOLS: frozenset[str] = frozenset({"describe_region", "find_element"})

#: Tools that legitimately interact with a credential field. Typing a password
#: into a password box is the intended use; it is permitted but marked
#: ``sensitive`` so its content is never recorded anywhere.
_INTERACTION_TOOLS: frozenset[str] = frozenset(
    {"type_text", "press_key", "hotkey", "click", "double_click", "right_click", "activate_element"}
)


def check_password_context(*, tool: str, is_password: bool) -> GuardOutcome:
    """Section 55: credential context is interacted with, never read.

    Args:
        tool: The tool being considered.
        is_password: Whether the resolved target (or the active context) is a
            credential field.

    Returns:
        A passing verdict marked ``sensitive`` for a permitted interaction, or a
        ``PERMISSION_DENIED`` verdict for a tool that would read the content
        back out.
    """
    if not is_password:
        return GuardOutcome.allow()
    if tool in _INTERACTION_TOOLS:
        return GuardOutcome.allow(sensitive=True)
    if tool in _CONTENT_READING_TOOLS:
        return GuardOutcome.deny(
            ErrorCode.PERMISSION_DENIED,
            f"{tool} must never be used on a credential field (section 55)",
            details={"tool": tool},
        )
    # Any other tool pointed at a credential field is refused rather than
    # guessed about: an unknown interaction with a password has no safe default.
    return GuardOutcome.deny(
        ErrorCode.PERMISSION_DENIED,
        f"{tool} is not permitted against a credential field",
        details={"tool": tool},
    )


def check_calibration(calibration: Calibration | None) -> GuardOutcome:
    """Section 31: input stays disarmed while calibration is incomplete.

    ``None`` means no calibration is required for this session (the deliberate
    identity mapping used on X11 with XTest), which is distinct from a
    calibration that exists but failed.
    """
    if calibration is None:
        return GuardOutcome.allow()
    if calibration.disarmed:
        return GuardOutcome.deny(
            ErrorCode.CALIBRATION_FAILED,
            calibration.failure_reason() or "calibration is incomplete; input remains disarmed",
            details={
                "missing_monitors": list(calibration.missing_monitors),
                "invalid_monitors": list(calibration.invalid_monitors),
            },
        )
    return GuardOutcome.allow()


def check_capability(
    report: CapabilityReport | None,
    name: CapabilityName,
    *,
    allow_degraded: bool = False,
) -> GuardOutcome:
    """Section 28: a capability must be honestly AVAILABLE before it is used.

    When no report is supplied the guard abstains (the input layer performs its
    own functional probe and fails honestly on its own); when a report *is*
    supplied, its verdict is binding.
    """
    if report is None:
        return GuardOutcome.allow()
    capability = report.get(name)
    if capability is None:
        return GuardOutcome.deny(
            ErrorCode.BACKEND_UNAVAILABLE,
            f"capability {name.value} was not probed",
            details={"capability": name.value},
        )
    if capability.status is CapabilityStatus.AVAILABLE:
        return GuardOutcome.allow()
    if capability.status is CapabilityStatus.DEGRADED and allow_degraded:
        return GuardOutcome.allow()
    return GuardOutcome.deny(
        ErrorCode.BACKEND_UNAVAILABLE,
        capability.reason or f"capability {name.value} is {capability.status.value}",
        details={"capability": name.value, "status": capability.status.value},
    )
