"""Capability probing honesty (specification sections 28, 80)."""

from __future__ import annotations

from config.settings import Settings
from core.capability_probe import (
    probe_all,
    probe_browser_accessibility,
    probe_mouse,
    probe_sequence_execution,
    probe_visual_grounding,
)
from core.session_detector import SessionInfo, detect_session
from schemas.enums import CapabilityName, CapabilityStatus, SessionType


def _wayland_session() -> SessionInfo:
    """A synthetic native-Wayland session for deterministic probe testing."""
    return SessionInfo(session_type=SessionType.WAYLAND)


def test_probe_all_covers_every_required_capability() -> None:
    """Section 28's capability set is fully reported, in section 29 probe order."""
    report = probe_all(Settings(), detect_session())
    names = [c.name for c in report.capabilities]
    assert set(names) == set(CapabilityName)
    assert names == [
        CapabilityName.CAPTURE,
        CapabilityName.ACCESSIBILITY,
        CapabilityName.MOUSE,
        CapabilityName.POINTER_READBACK,
        CapabilityName.KEYBOARD,
        CapabilityName.OCR,
        CapabilityName.CLIPBOARD,
        CapabilityName.WINDOW_INFO,
        CapabilityName.BROWSER_ACCESSIBILITY,
        CapabilityName.VISUAL_GROUNDING,
        CapabilityName.SEQUENCE_EXECUTION,
        CapabilityName.BRAIN,
    ]


def test_unimplemented_capabilities_are_unavailable() -> None:
    """Not-yet-built features must report UNAVAILABLE with an honest reason."""
    session = detect_session()
    settings = Settings()

    browser = probe_browser_accessibility(session)
    visual = probe_visual_grounding(session)
    sequence = probe_sequence_execution(settings)

    for cap in (browser, visual, sequence):
        assert cap.status is CapabilityStatus.UNAVAILABLE
        assert cap.reason
    assert "Phase 3" in (browser.reason or "")
    assert "Phase 11" in (visual.reason or "")


def test_sequence_probe_reports_configured_limits() -> None:
    """Even while unavailable, the sequence capability must surface its limits."""
    settings = Settings()
    cap = probe_sequence_execution(settings)
    assert cap.details["configured_enabled"] is False
    assert cap.details["max_sequence_steps"] == settings.sequence.max_sequence_steps
    assert cap.details["max_sequence_wall_clock_seconds"] == (
        settings.sequence.max_sequence_wall_clock_seconds
    )


def test_mouse_probe_is_unavailable_without_an_x_display() -> None:
    """Native Wayland without a portal path cannot inject pointer input."""
    cap = probe_mouse(_wayland_session())
    assert cap.status is CapabilityStatus.UNAVAILABLE
    assert cap.backend is None
    assert cap.fix_hint
