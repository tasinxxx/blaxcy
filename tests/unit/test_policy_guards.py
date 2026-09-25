"""Policy guards (specification sections 28, 31, 42, 55, 58).

Each guard is tested for its positive path, its negative path and its
fail-closed behaviour -- a guard that cannot establish safety must refuse.
"""

from __future__ import annotations

import pytest

from core.calibration import Calibration, MonitorCalibration
from policy.guards import (
    check_blocked_application,
    check_calibration,
    check_capability,
    check_password_context,
    check_protected_application,
    check_visual_fallback,
    match_application,
)
from schemas.capability import Capability, CapabilityReport
from schemas.enums import CapabilityName, CapabilityStatus, ErrorCode, PolicyMode
from schemas.geometry import Affine2D, MonitorGeometry, MonitorLayout


def _layout() -> MonitorLayout:
    """A single-monitor layout for calibration fixtures."""
    return MonitorLayout(
        monitors=(MonitorGeometry(monitor_id=0, width=1920, height=1080, is_primary=True),)
    )


def _calibration(*, sample_count: int = 3, residual: float = 0.0) -> Calibration:
    """A calibration with a controllable validity."""
    return Calibration(
        layout=_layout(),
        monitors=(
            MonitorCalibration(
                monitor_id=0,
                transform=Affine2D.identity(),
                sample_count=sample_count,
                max_residual_px=residual,
                tolerance_px=1.0,
            ),
        ),
    )


def _report(name: CapabilityName, status: CapabilityStatus) -> CapabilityReport:
    """A capability report with exactly one entry."""
    return CapabilityReport(
        capabilities=(Capability(name=name, status=status, reason=None if status is CapabilityStatus.AVAILABLE else "nope"),),
        generated_at=0.0,
    )


# -- Application lists (sections 42, 58) --------------------------------------

def test_empty_deny_list_allows_everything() -> None:
    """No configured deny-list means no application is blocked."""
    assert check_blocked_application(("firefox",), ()).ok is True


def test_blocked_application_matches_by_substring_case_insensitively() -> None:
    """A deny-list entry catches the app regardless of case or decoration."""
    outcome = check_blocked_application(("KeePassXC", None), ("keepass",))
    assert outcome.ok is False
    assert outcome.code is ErrorCode.BLOCKED_APPLICATION


def test_blocked_application_matches_window_titles_too() -> None:
    """The match considers every supplied candidate string."""
    outcome = check_blocked_application((None, "Passwords - KeePassXC"), ("keepassxc",))
    assert outcome.ok is False


def test_unrelated_application_is_not_blocked() -> None:
    """A different application is unaffected by the deny-list."""
    assert check_blocked_application(("firefox", "Mozilla Firefox"), ("keepass",)).ok is True


def test_protected_application_is_a_privacy_verdict() -> None:
    """Section 42 refuses upload from a protected app, not interaction with it."""
    outcome = check_protected_application(("KeePassXC",), ("keepass",))
    assert outcome.ok is False
    assert outcome.code is ErrorCode.VISUAL_FALLBACK_DENIED


def test_match_application_returns_the_matching_pattern() -> None:
    """The matched pattern is reported so the reason can name it."""
    assert match_application(("FooBar",), ("foo", "bar")) == "foo"
    assert match_application(("FooBar",), ("baz",)) is None


# -- Credential context (section 55) -----------------------------------------

def test_typing_into_a_password_field_is_allowed_but_sensitive() -> None:
    """Typing a password is the intended interaction, and must be marked."""
    outcome = check_password_context(tool="type_text", is_password=True)
    assert outcome.ok is True
    assert outcome.sensitive is True


def test_reading_a_password_field_is_refused() -> None:
    """A content-reading tool on a credential field is denied outright."""
    for tool in ("describe_region", "find_element"):
        outcome = check_password_context(tool=tool, is_password=True)
        assert outcome.ok is False
        assert outcome.code is ErrorCode.PERMISSION_DENIED


def test_unknown_tool_on_a_password_field_fails_closed() -> None:
    """An interaction with no safe default is refused, not guessed."""
    outcome = check_password_context(tool="some_new_tool", is_password=True)
    assert outcome.ok is False


def test_non_password_context_is_never_sensitive() -> None:
    """Ordinary fields carry no sensitivity flag."""
    outcome = check_password_context(tool="type_text", is_password=False)
    assert outcome.ok is True
    assert outcome.sensitive is False


# -- Calibration (section 31) -------------------------------------------------

def test_no_calibration_requirement_is_not_a_failure() -> None:
    """No calibration object means none is required (the XTest identity case)."""
    assert check_calibration(None).ok is True


def test_complete_calibration_allows_input() -> None:
    """A full, in-tolerance calibration permits input."""
    assert check_calibration(_calibration()).ok is True


def test_disarmed_calibration_blocks_input() -> None:
    """Too few samples leaves input disarmed (section 31)."""
    outcome = check_calibration(_calibration(sample_count=1))
    assert outcome.ok is False
    assert outcome.code is ErrorCode.CALIBRATION_FAILED


def test_out_of_tolerance_calibration_blocks_input() -> None:
    """A fit beyond tolerance is not good enough to aim with."""
    outcome = check_calibration(_calibration(residual=5.0))
    assert outcome.ok is False


# -- Capability (section 28) --------------------------------------------------

def test_missing_report_abstains() -> None:
    """Without a report the guard abstains rather than inventing a verdict."""
    assert check_capability(None, CapabilityName.MOUSE).ok is True


def test_unavailable_capability_blocks() -> None:
    """An UNAVAILABLE capability is refused."""
    outcome = check_capability(_report(CapabilityName.MOUSE, CapabilityStatus.UNAVAILABLE), CapabilityName.MOUSE)
    assert outcome.ok is False
    assert outcome.code is ErrorCode.BACKEND_UNAVAILABLE


def test_degraded_capability_is_refused_unless_explicitly_allowed() -> None:
    """DEGRADED is not silently treated as AVAILABLE."""
    report = _report(CapabilityName.MOUSE, CapabilityStatus.DEGRADED)
    assert check_capability(report, CapabilityName.MOUSE).ok is False
    assert check_capability(report, CapabilityName.MOUSE, allow_degraded=True).ok is True


def test_available_capability_passes() -> None:
    """AVAILABLE is the only status that passes by default."""
    report = _report(CapabilityName.MOUSE, CapabilityStatus.AVAILABLE)
    assert check_capability(report, CapabilityName.MOUSE).ok is True


def test_unprobed_capability_is_refused() -> None:
    """A report that does not mention the capability cannot vouch for it."""
    report = _report(CapabilityName.MOUSE, CapabilityStatus.AVAILABLE)
    outcome = check_capability(report, CapabilityName.KEYBOARD)
    assert outcome.ok is False


# -- Visual fallback (sections 41, 42) ----------------------------------------


def test_visual_fallback_requires_assist_or_autonomous() -> None:
    """Section 41: OBSERVE may not upload anything."""
    outcome = check_visual_fallback(mode=PolicyMode.OBSERVE)
    assert outcome.ok is False
    assert outcome.code is ErrorCode.VISUAL_FALLBACK_DENIED
    assert outcome.details["mode"] == PolicyMode.OBSERVE.value


@pytest.mark.parametrize("mode", [PolicyMode.ASSIST, PolicyMode.AUTONOMOUS])
def test_visual_fallback_is_allowed_from_assist_upward(mode: PolicyMode) -> None:
    """ASSIST and AUTONOMOUS are the two permitted modes, and nothing else."""
    assert check_visual_fallback(mode=mode).ok is True


def test_visual_fallback_is_refused_when_disabled() -> None:
    """A disabled capability is refused with the taxonomy code for an absent backend."""
    outcome = check_visual_fallback(mode=PolicyMode.ASSIST, enabled=False)
    assert outcome.ok is False
    assert outcome.code is ErrorCode.BACKEND_UNAVAILABLE


def test_visual_fallback_never_uploads_a_protected_application() -> None:
    """Section 42: a protected application's content must not reach a visual model."""
    outcome = check_visual_fallback(
        mode=PolicyMode.ASSIST,
        application=("KeePassXC", "main window"),
        protected=("keepass",),
    )
    assert outcome.ok is False
    assert outcome.code is ErrorCode.VISUAL_FALLBACK_DENIED
    assert outcome.details["matched"] == "keepass"


def test_visual_fallback_never_uploads_a_blocked_application() -> None:
    """Section 58 outranks the privacy verdict: blocked means no upload at all."""
    outcome = check_visual_fallback(
        mode=PolicyMode.AUTONOMOUS,
        application=("1Password", None),
        protected=("password",),
        blocked=("1password",),
    )
    assert outcome.ok is False
    assert outcome.code is ErrorCode.BLOCKED_APPLICATION


def test_visual_fallback_refuses_even_when_a_blocked_list_does_not_match() -> None:
    """An unrelated application on the list does not silently change the verdict."""
    outcome = check_visual_fallback(
        mode=PolicyMode.ASSIST,
        application=("Firefox", "Mozilla Firefox"),
        blocked=("keepass",),
        protected=("wallet",),
    )
    assert outcome.ok is True
