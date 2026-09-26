"""Capability probing honesty (specification sections 28, 80)."""

from __future__ import annotations

import pytest

from config.settings import Settings
from control.backends.base import BackendProbe
from control.backends.portal import PortalRemoteDesktopBackend
from core.capability_probe import (
    probe_all,
    probe_browser_accessibility,
    probe_keyboard,
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


def test_visual_grounding_reports_its_real_availability_with_evidence() -> None:
    """Phase 11 is implemented, so the verdict is about *availability*, not build state.

    The probe shares ``VisualGrounder.capability()`` rather than guessing, so on
    any host the status follows the switch, the SDK and the stored credential --
    and an UNAVAILABLE verdict always names a reason and a fix (section 80).
    """
    settings = Settings()
    visual = probe_visual_grounding(detect_session(), settings)

    assert visual.details["max_image_side"] == 1024
    assert visual.details["max_confidence"] == 0.80
    assert "Phase 11" not in (visual.reason or "")
    if visual.status is CapabilityStatus.AVAILABLE:
        assert visual.backend == "google-genai"
        assert visual.details["key_present"] is True
    else:
        assert visual.status is CapabilityStatus.UNAVAILABLE
        assert visual.reason
        assert visual.fix_hint


def test_visual_grounding_follows_the_configuration_switch() -> None:
    """Turning the fallback off is reported as UNAVAILABLE, never hidden."""
    base = Settings()
    disabled = base.model_copy(
        update={"visual": base.visual.model_copy(update={"enabled": False})}
    )
    visual = probe_visual_grounding(detect_session(), disabled)
    assert visual.status is CapabilityStatus.UNAVAILABLE
    assert "disabled by configuration" in (visual.reason or "")
    assert visual.fix_hint


def test_browser_accessibility_reports_unavailable_without_a_bus() -> None:
    """Browser accessibility depends on AT-SPI; without a bus it is honestly UNAVAILABLE.

    A synthetic session with ``atspi_bus_available=False`` keeps this deterministic
    regardless of which browsers happen to be open on the host -- the point is the
    *dependency* is reported, not a fabricated result.
    """
    session = SessionInfo(session_type=SessionType.X11, atspi_bus_available=False)
    cap = probe_browser_accessibility(session)
    assert cap.status is CapabilityStatus.UNAVAILABLE
    assert cap.backend == "atspi"
    assert "AT-SPI bus" in (cap.reason or "")
    assert cap.fix_hint


def test_sequence_execution_is_available_and_reports_its_limits() -> None:
    """Sections 83/84: the runner exists and the batching layer ships enabled."""
    settings = Settings()
    cap = probe_sequence_execution(settings)
    assert cap.status is CapabilityStatus.AVAILABLE
    assert cap.backend == "sequence_runner"
    assert cap.details["configured_enabled"] is True
    assert cap.details["max_sequence_steps"] == settings.sequence.max_sequence_steps
    assert cap.details["max_sequence_wall_clock_seconds"] == (
        settings.sequence.max_sequence_wall_clock_seconds
    )


def test_sequence_execution_reports_disabled_when_configured_off() -> None:
    """The capability follows the configuration switch, so the probe stays honest."""
    base = Settings()
    disabled = base.model_copy(
        update={"sequence": base.sequence.model_copy(update={"enabled": False})}
    )
    cap = probe_sequence_execution(disabled)
    assert cap.status is CapabilityStatus.UNAVAILABLE
    assert cap.details["configured_enabled"] is False


def test_mouse_probe_is_unavailable_without_an_x_display_or_portal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Native Wayland with no RemoteDesktop portal cannot inject pointer input.

    The portal probe is forced down so the verdict is deterministic regardless of
    what the host's session bus happens to advertise.
    """
    monkeypatch.setattr(
        PortalRemoteDesktopBackend,
        "probe",
        lambda _self: BackendProbe("portal-remotedesktop", False, "interface absent"),
    )
    cap = probe_mouse(_wayland_session())
    assert cap.status is CapabilityStatus.UNAVAILABLE
    assert cap.backend is None
    assert cap.fix_hint


def test_mouse_and_keyboard_use_the_portal_when_it_is_the_only_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On Wayland the portal is the path; the mouse is honestly DEGRADED.

    Absolute pointer motion needs a ScreenCast stream node, which this backend
    does not have by default, so the mouse is reported DEGRADED while the keyboard
    (which needs no stream) is AVAILABLE.
    """
    probe = BackendProbe(
        "portal-remotedesktop",
        True,
        None,
        {"interface_version": 2, "absolute_pointer": False, "session_started": False},
    )
    monkeypatch.setattr(PortalRemoteDesktopBackend, "probe", lambda _self: probe)

    keyboard = probe_keyboard(_wayland_session())
    mouse = probe_mouse(_wayland_session())

    assert keyboard.status is CapabilityStatus.AVAILABLE
    assert keyboard.backend == "portal-remotedesktop"
    assert mouse.status is CapabilityStatus.DEGRADED
    assert mouse.backend == "portal-remotedesktop"
    assert "stream node" in (mouse.reason or "")
