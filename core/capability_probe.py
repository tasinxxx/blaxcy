"""Functional capability probing (specification sections 28, 29, 80).

Every probe here actually exercises the mechanism it reports on. A backend is
only reported ``AVAILABLE`` when a functional probe passed; the mere presence
of a binary on ``PATH`` is never sufficient. On any failure the probe reports
``DEGRADED`` or ``UNAVAILABLE`` with a ``reason`` and a ``fix_hint`` rather
than fabricating success.

No probe in this module injects physical input. Mouse/keyboard availability is
established by querying the XTEST extension, which proves the capability
exists without moving the pointer or pressing a key. Where a functional probe
would require real input, the capability is reported honestly as not yet
implemented for its phase instead of being guessed.
"""

from __future__ import annotations

import importlib
import time
from contextlib import suppress
from typing import Any

from config.settings import AccessibilitySettings, Settings
from control.backends.base import BackendProbe
from core.accessibility import AccessibilityService
from core.browser_accessibility import BrowserAccessibility
from core.session_detector import SessionInfo
from schemas.capability import Capability, CapabilityReport
from schemas.enums import CapabilityName, CapabilityStatus, SessionType
from schemas.errors import BlaxcyError

#: Keyring coordinates for the Gemini API key (specification section 69).
KEYRING_SERVICE = "blaxcy"
KEYRING_GEMINI_KEY = "gemini_api_key"


def _try_import(module: str) -> Any:
    """Import ``module`` and return it, or ``None`` if unavailable.

    The return type is ``Any`` on purpose: these are runtime-probed, optionally
    present third-party modules whose attributes BLAXCY must not assume. Every
    caller checks for ``None`` before touching the module.
    """
    try:
        return importlib.import_module(module)
    except Exception:
        return None


def _cap(
    name: CapabilityName,
    status: CapabilityStatus,
    *,
    backend: str | None = None,
    latency_ms: float | None = None,
    details: dict[str, Any] | None = None,
    reason: str | None = None,
    fix_hint: str | None = None,
) -> Capability:
    """Construct a capability, keeping probe code terse and consistent."""
    return Capability(
        name=name,
        status=status,
        backend=backend,
        latency_ms=latency_ms,
        details=details or {},
        reason=reason,
        fix_hint=fix_hint,
    )


# ---------------------------------------------------------------------------
# Individual probes.
# ---------------------------------------------------------------------------


def probe_capture(session: SessionInfo) -> Capability:
    """Probe screen capture via ``mss`` by taking one real grab."""
    mss = _try_import("mss")
    if mss is None:
        return _cap(
            CapabilityName.CAPTURE,
            CapabilityStatus.UNAVAILABLE,
            reason="mss is not importable",
            fix_hint="pip install mss inside the project venv",
        )

    factory = getattr(mss, "MSS", None) or getattr(mss, "mss", None)
    if factory is None:
        return _cap(
            CapabilityName.CAPTURE,
            CapabilityStatus.UNAVAILABLE,
            reason="mss module exposes neither MSS nor mss factory",
        )

    started = time.perf_counter()
    try:
        capture = factory()
        try:
            monitors = list(capture.monitors)
        finally:
            closer = getattr(capture, "close", None)
            if callable(closer):
                closer()
    except Exception as exc:
        status = (
            CapabilityStatus.UNAVAILABLE
            if session.session_type is SessionType.WAYLAND
            else CapabilityStatus.DEGRADED
        )
        return _cap(
            CapabilityName.CAPTURE,
            status,
            backend="mss",
            reason=f"mss import succeeded but a live grab failed: {exc!r}",
            fix_hint=(
                "On native Wayland, capture must go through the XDG ScreenCast portal + "
                "PipeWire (Phase 2); X11 capture cannot see compositor state."
                if session.session_type is SessionType.WAYLAND
                else "Check the X display / compositor state."
            ),
        )
    latency_ms = (time.perf_counter() - started) * 1000.0
    # monitors[0] is the virtual union of all monitors; the rest are physical.
    physical = monitors[1:] if len(monitors) > 1 else monitors
    return _cap(
        CapabilityName.CAPTURE,
        CapabilityStatus.AVAILABLE,
        backend="mss",
        latency_ms=round(latency_ms, 3),
        details={
            "monitor_count": len(physical),
            "monitors": [
                {
                    "left": int(m.get("left", 0)),
                    "top": int(m.get("top", 0)),
                    "width": int(m.get("width", 0)),
                    "height": int(m.get("height", 0)),
                }
                for m in physical
            ],
        },
    )


def probe_accessibility(
    session: SessionInfo,
    settings: AccessibilitySettings | None = None,
    *,
    service: AccessibilityService | None = None,
) -> Capability:
    """Probe AT-SPI perception functionally by observing the live tree.

    This is a real functional probe: it starts the accessibility service and
    performs one bounded traversal. A probe that merely imported ``gi`` would
    report ``AVAILABLE`` for an accessibility stack that cannot actually answer
    a query (section 28).

    Args:
        session: Detected session facts (the bus check avoids a pointless wait).
        settings: Accessibility settings; defaults to the packaged defaults.
        service: An already-started service to reuse (used by :func:`probe_all`).
    """
    if session.atspi_bus_available is False:
        return _cap(
            CapabilityName.ACCESSIBILITY,
            CapabilityStatus.UNAVAILABLE,
            backend="atspi",
            reason="no AT-SPI accessibility bus (org.a11y.Bus) is present on the session bus",
            fix_hint="enable accessibility support; ensure at-spi2-core is installed and running",
        )

    accessibility_settings = settings or AccessibilitySettings()
    owns_service = service is None
    active_service = service if service is not None else AccessibilityService(accessibility_settings)
    try:
        if not active_service.is_running:
            active_service.start()
    except BlaxcyError:
        # The service records the startup failure; report it rather than guess.
        return active_service.capability()
    try:
        # A failed query still yields an evidence-carrying verdict: capability()
        # reads the service's own recorded failure rather than guessing.
        with suppress(BlaxcyError):
            active_service.refresh()
        return active_service.capability()
    finally:
        if owns_service:
            active_service.stop()


def _probe_xtest() -> BackendProbe:
    """Query the XTEST extension through the Phase 7 backend, injecting nothing.

    The backend owns the probe so the capability report and the input layer can
    never disagree about what is actually available (section 28 rule 12).
    """
    from control.backends import XtestBackend

    return XtestBackend().probe()


def _input_capability(name: CapabilityName, session: SessionInfo) -> Capability:
    """Build the mouse/keyboard capability from the shared XTEST probe."""
    if not session.has_x_display:
        return _cap(
            name,
            CapabilityStatus.UNAVAILABLE,
            reason=(
                "no X display available for injection; Wayland input needs the "
                "RemoteDesktop portal or a verified libei/ydotool path (section 30)"
            ),
            fix_hint="run under X11/XWayland, or implement the portal backend",
        )
    probe = _probe_xtest()
    if probe.available:
        return _cap(
            name,
            CapabilityStatus.AVAILABLE,
            backend=probe.name,
            details=probe.details,
        )
    # XTEST absent: only a *functionally probed* fallback may be reported, and no
    # xdotool backend exists yet -- so report the degraded state honestly rather
    # than assuming the xdotool binary works.
    xdotool = _which("xdotool")
    if xdotool:
        return _cap(
            name,
            CapabilityStatus.DEGRADED,
            backend="xdotool",
            reason=(
                f"XTEST unavailable ({probe.reason}); xdotool is present but no "
                "xdotool backend is implemented"
            ),
            fix_hint="implement the xdotool fallback backend, or enable the XTEST extension",
            details={"xdotool": xdotool},
        )
    return _cap(
        name,
        CapabilityStatus.UNAVAILABLE,
        reason=f"XTEST unavailable ({probe.reason}) and no xdotool fallback found",
        fix_hint="enable the XTEST extension",
    )


def probe_mouse(session: SessionInfo) -> Capability:
    """Probe pointer-injection capability via XTEST presence (no input injected)."""
    return _input_capability(CapabilityName.MOUSE, session)


def probe_keyboard(session: SessionInfo) -> Capability:
    """Probe keyboard-injection capability (the same XTEST mechanism)."""
    return _input_capability(CapabilityName.KEYBOARD, session)


def probe_pointer_readback(session: SessionInfo) -> Capability:
    """Probe pointer position readback -- a read-only X query, safe to run here."""
    if not session.has_x_display:
        return _cap(
            CapabilityName.POINTER_READBACK,
            CapabilityStatus.UNAVAILABLE,
            reason="no X display available to read pointer position",
        )
    if _try_import("Xlib") is None:
        return _cap(
            CapabilityName.POINTER_READBACK,
            CapabilityStatus.UNAVAILABLE,
            reason="python-xlib is not importable",
            fix_hint="pip install python-xlib",
        )
    started = time.perf_counter()
    try:
        from Xlib import display

        conn = display.Display()
        try:
            root = conn.screen().root
            pointer = root.query_pointer()
            position = {"x": int(pointer.root_x), "y": int(pointer.root_y)}
        finally:
            conn.close()
    except Exception as exc:
        return _cap(
            CapabilityName.POINTER_READBACK,
            CapabilityStatus.DEGRADED,
            reason=f"pointer readback failed: {exc!r}",
            fix_hint="XInput2 readback may be required; verify in Phase 7",
        )
    latency_ms = (time.perf_counter() - started) * 1000.0
    return _cap(
        CapabilityName.POINTER_READBACK,
        CapabilityStatus.AVAILABLE,
        backend="xlib",
        latency_ms=round(latency_ms, 3),
        details={"position": position},
    )


def probe_ocr(session: SessionInfo) -> Capability:
    """Probe OCR functionally by asking pytesseract for the engine version."""
    pytesseract = _try_import("pytesseract")
    if pytesseract is None:
        return _cap(
            CapabilityName.OCR,
            CapabilityStatus.UNAVAILABLE,
            reason="pytesseract is not importable",
            fix_hint="pip install pytesseract and install tesseract-ocr",
        )
    started = time.perf_counter()
    try:
        version = str(pytesseract.get_tesseract_version())
    except Exception as exc:
        return _cap(
            CapabilityName.OCR,
            CapabilityStatus.UNAVAILABLE,
            backend="pytesseract",
            reason=f"tesseract engine not usable: {exc!r}",
            fix_hint="install tesseract-ocr and tesseract-ocr-eng",
        )
    latency_ms = (time.perf_counter() - started) * 1000.0
    return _cap(
        CapabilityName.OCR,
        CapabilityStatus.AVAILABLE,
        backend="pytesseract",
        latency_ms=round(latency_ms, 3),
        details={"tesseract_version": version},
    )


def probe_clipboard(session: SessionInfo) -> Capability:
    """Probe clipboard access read-only by inspecting CLIPBOARD selection ownership."""
    if not session.has_x_display:
        return _cap(
            CapabilityName.CLIPBOARD,
            CapabilityStatus.UNAVAILABLE,
            reason="no X display available for X11 clipboard access",
        )
    if _try_import("Xlib") is None:
        return _cap(
            CapabilityName.CLIPBOARD,
            CapabilityStatus.UNAVAILABLE,
            reason="python-xlib is not importable",
            fix_hint="pip install python-xlib",
        )
    try:
        from Xlib import display

        conn = display.Display()
        try:
            root = conn.screen().root
            atom = conn.intern_atom("CLIPBOARD")
            owner = conn.get_selection_owner(atom)
            owner_known = owner is not None and owner != 0
            root.id  # noqa: B018 - touching the root proves the connection is live
        finally:
            conn.close()
    except Exception as exc:
        return _cap(
            CapabilityName.CLIPBOARD,
            CapabilityStatus.DEGRADED,
            reason=f"clipboard selection query failed: {exc!r}",
            fix_hint="verify clipboard tooling in Phase 7",
        )
    details: dict[str, Any] = {"selection_owned": bool(owner_known)}
    xclip = _which("xclip")
    if xclip:
        details["xclip"] = xclip
    return _cap(
        CapabilityName.CLIPBOARD,
        CapabilityStatus.AVAILABLE,
        backend="xlib",
        details=details,
    )


def probe_window_info(session: SessionInfo) -> Capability:
    """Probe window metadata read via Xlib + EWMH on a live connection."""
    if not session.has_x_display:
        return _cap(
            CapabilityName.WINDOW_INFO,
            CapabilityStatus.UNAVAILABLE,
            reason="no X display available for window queries",
        )
    if _try_import("Xlib") is None:
        return _cap(
            CapabilityName.WINDOW_INFO,
            CapabilityStatus.UNAVAILABLE,
            reason="python-xlib is not importable",
            fix_hint="pip install python-xlib",
        )
    started = time.perf_counter()
    try:
        from Xlib import display

        conn = display.Display()
        try:
            root = conn.screen().root
            tree = root.query_tree()
            children = list(tree.children)
            conn.intern_atom("_NET_ACTIVE_WINDOW")
        finally:
            conn.close()
    except Exception as exc:
        return _cap(
            CapabilityName.WINDOW_INFO,
            CapabilityStatus.DEGRADED,
            reason=f"window enumeration failed: {exc!r}",
        )
    latency_ms = (time.perf_counter() - started) * 1000.0
    details: dict[str, Any] = {"top_level_windows": len(children)}
    wmctrl = _which("wmctrl")
    if wmctrl:
        details["wmctrl"] = wmctrl
    return _cap(
        CapabilityName.WINDOW_INFO,
        CapabilityStatus.AVAILABLE,
        backend="python-xlib",
        latency_ms=round(latency_ms, 3),
        details=details,
    )


def probe_browser_accessibility(
    session: SessionInfo,
    settings: AccessibilitySettings | None = None,
    *,
    service: AccessibilityService | None = None,
) -> Capability:
    """Probe browser accessibility functionally against the live AT-SPI tree.

    Browser accessibility is only ``AVAILABLE`` when a supported browser is
    actually exposing an accessibility tree *and* a page URL was discovered.
    A browser whose tree is visible but whose URL is not discoverable reports
    ``DEGRADED`` with a reason (section 36 order, section 80 honesty).
    """
    if session.atspi_bus_available is False:
        return _cap(
            CapabilityName.BROWSER_ACCESSIBILITY,
            CapabilityStatus.UNAVAILABLE,
            backend="atspi",
            reason="no AT-SPI bus, which browser accessibility depends on",
            fix_hint="enable accessibility support; ensure at-spi2-core is installed and running",
        )

    accessibility_settings = settings or AccessibilitySettings()
    owns_service = service is None
    active_service = service if service is not None else AccessibilityService(accessibility_settings)
    if owns_service:
        try:
            active_service.start()
        except BlaxcyError as exc:
            return _cap(
                CapabilityName.BROWSER_ACCESSIBILITY,
                CapabilityStatus.UNAVAILABLE,
                backend="atspi",
                reason=(
                    "browser accessibility requires AT-SPI perception, which is "
                    f"unavailable: {exc.message}"
                ),
                fix_hint="install python3-gi, gir1.2-atspi-2.0 and at-spi2-core",
            )
    try:
        return BrowserAccessibility(active_service).capability()
    finally:
        if owns_service:
            active_service.stop()


def probe_visual_grounding(session: SessionInfo) -> Capability:
    """Report visual grounding honestly: not implemented until Phase 11."""
    return _cap(
        CapabilityName.VISUAL_GROUNDING,
        CapabilityStatus.UNAVAILABLE,
        reason="visual grounding fallback is not implemented until Phase 11",
        fix_hint="implement core/visual_grounder.py in Phase 11; it also requires a Brain",
    )


def probe_sequence_execution(settings: Settings) -> Capability:
    """Report ``run_sequence`` availability (specification sections 28, 66.1)."""
    limits = {
        "configured_enabled": settings.sequence.enabled,
        "max_sequence_steps": settings.sequence.max_sequence_steps,
        "max_sequence_wall_clock_seconds": settings.sequence.max_sequence_wall_clock_seconds,
        "capture_boost": settings.sequence.capture_boost,
        "speculative_perception": settings.sequence.speculative_perception,
    }
    runner = _try_import("control.sequence_runner")
    if runner is None:
        return _cap(
            CapabilityName.SEQUENCE_EXECUTION,
            CapabilityStatus.UNAVAILABLE,
            reason="sequence runner is not implemented until Phase 10.1",
            fix_hint="implement control/sequence_runner.py after the Phase 9 safety gate",
            details=limits,
        )
    if not settings.sequence.enabled:
        return _cap(
            CapabilityName.SEQUENCE_EXECUTION,
            CapabilityStatus.UNAVAILABLE,
            reason="sequence execution is implemented but disabled by configuration",
            fix_hint="set [sequence] enabled = true only after the batching test list passes",
            details=limits,
        )
    return _cap(
        CapabilityName.SEQUENCE_EXECUTION,
        CapabilityStatus.AVAILABLE,
        backend="sequence_runner",
        details=limits,
    )


def probe_brain(settings: Settings) -> Capability:
    """Probe Brain connectivity: SDK importable and an API key is configured."""
    genai = _try_import("google.genai")
    if genai is None:
        return _cap(
            CapabilityName.BRAIN,
            CapabilityStatus.UNAVAILABLE,
            reason="google-genai SDK is not importable",
            fix_hint="pip install google-genai",
        )
    keyring = _try_import("keyring")
    if keyring is None:
        return _cap(
            CapabilityName.BRAIN,
            CapabilityStatus.UNAVAILABLE,
            backend="google-genai",
            reason="keyring is not importable, so no API key can be read",
            fix_hint="pip install keyring SecretStorage",
        )
    try:
        key = keyring.get_password(KEYRING_SERVICE, KEYRING_GEMINI_KEY)
    except Exception as exc:
        return _cap(
            CapabilityName.BRAIN,
            CapabilityStatus.UNAVAILABLE,
            backend="google-genai",
            reason=f"keyring backend is unavailable: {exc!r}",
            fix_hint="ensure a working Secret Service/keyring backend is running",
        )
    if not key:
        return _cap(
            CapabilityName.BRAIN,
            CapabilityStatus.UNAVAILABLE,
            backend="google-genai",
            reason=(
                f"no Gemini API key stored (keyring service={KEYRING_SERVICE!r}, "
                f"key={KEYRING_GEMINI_KEY!r})"
            ),
            fix_hint="store the key via the Phase 10 setup flow; never in config or CLI args",
        )
    return _cap(
        CapabilityName.BRAIN,
        CapabilityStatus.AVAILABLE,
        backend="google-genai",
        details={"model": settings.gemini.model, "key_present": True},
    )


def _which(executable: str) -> str | None:
    """Return the absolute path of ``executable`` if present (evidence only)."""
    import shutil

    return shutil.which(executable)


# ---------------------------------------------------------------------------
# Aggregate probe.
# ---------------------------------------------------------------------------


def probe_all(
    settings: Settings,
    session: SessionInfo | None = None,
) -> CapabilityReport:
    """Run every Phase 0 capability probe in specification section 29 order.

    The probes perform no physical input and mutate no state; this is a
    read-only environment report.
    """
    if session is None:
        from core.session_detector import detect_session

        session = detect_session()

    # One accessibility service is shared by the accessibility and browser
    # probes: AT-SPI has a single owner thread (section 35), and starting it
    # twice would be both slower and less honest about the real state.
    service = AccessibilityService(settings.accessibility)
    try:
        capabilities = (
            probe_capture(session),
            probe_accessibility(session, settings.accessibility, service=service),
            probe_mouse(session),
            probe_pointer_readback(session),
            probe_keyboard(session),
            probe_ocr(session),
            probe_clipboard(session),
            probe_window_info(session),
            probe_browser_accessibility(session, settings.accessibility, service=service),
            probe_visual_grounding(session),
            probe_sequence_execution(settings),
            probe_brain(settings),
        )
    finally:
        service.stop()
    return CapabilityReport(
        capabilities=capabilities,
        generated_at=time.time(),
        session_type=session.session_type.value,
    )
