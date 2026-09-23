"""Session and platform detection (specification sections 7, 29, 30).

Detects what the environment actually is -- X11, XWayland, native Wayland, the
desktop environment, display variables, portal availability and AT-SPI bus
availability -- by *observing* the system rather than by inferring support from
the mere presence of a ``DISPLAY`` variable (section 30 forbids faking X11
support through ``DISPLAY`` when the compositor does not expose the required
semantics).
"""

from __future__ import annotations

import os
import platform
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from schemas.enums import SessionType

_BUS_TIMEOUT_MS = 500


class SessionInfo(BaseModel):
    """A factual snapshot of the current display session environment.

    Fields that could not be determined are ``None`` -- never guessed. The
    ``notes`` list records anything the detection could not confirm.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_type: SessionType
    display: str | None = None
    wayland_display: str | None = None
    xdg_session_type: str | None = None
    desktop_environment: str | None = None
    session_desktop: str | None = None
    os_name: str = ""
    os_version: str = ""
    python_version: str = ""
    portal_available: bool | None = Field(
        default=None,
        description="True/False if the XDG RemoteDesktop/ScreenCast portal was checked; "
        "None if the check could not be performed at all.",
    )
    atspi_bus_available: bool | None = Field(
        default=None,
        description="True/False if the AT-SPI accessibility bus was checked; None if unknown.",
    )
    notes: tuple[str, ...] = ()

    @property
    def is_x11(self) -> bool:
        """True for a real X11 session (not merely an XWayland compatibility layer)."""
        return self.session_type is SessionType.X11

    @property
    def has_x_display(self) -> bool:
        """True when an X display is usable, including through XWayland."""
        return self.session_type in (SessionType.X11, SessionType.XWAYLAND)


def _dbus_name_has_owner(name: str) -> bool | None:
    """Query the session bus for a well-known name's owner.

    Returns ``True``/``False`` when the bus answered, or ``None`` when the bus
    or GI bindings are unavailable -- absence of evidence is recorded as
    unknown, never as ``False``.
    """
    try:
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib
    except Exception:
        return None

    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        if bus is None:
            return None
        reply = bus.call_sync(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "NameHasOwner",
            GLib.Variant("(s)", (name,)),
            GLib.VariantType.new("(b)"),
            Gio.DBusCallFlags.NONE,
            _BUS_TIMEOUT_MS,
            None,
        )
        if reply is None:
            return None
        unpacked: Any = reply.unpack()
        return bool(unpacked[0])
    except Exception:
        return None


def _detect_session_type(
    *,
    xdg_session_type: str | None,
    wayland_display: str | None,
    display: str | None,
) -> tuple[SessionType, tuple[str, ...]]:
    """Classify the session, preferring native Wayland over its X bridge."""
    notes: list[str] = []
    if xdg_session_type == "wayland" or wayland_display:
        if display:
            notes.append(
                "Wayland session with DISPLAY set: X is available only as XWayland "
                "compatibility; X11-only backends may not see real compositor state."
            )
            return SessionType.XWAYLAND, tuple(notes)
        return SessionType.WAYLAND, tuple(notes)
    if xdg_session_type == "x11" or display:
        return SessionType.X11, tuple(notes)
    notes.append("could not determine session type from environment variables")
    return SessionType.UNKNOWN, tuple(notes)


def detect_session() -> SessionInfo:
    """Observe and return the current session environment.

    This function performs no input and mutates nothing; it only reads
    environment variables, OS identity, and well-known D-Bus name ownership.
    """
    xdg_session_type = os.environ.get("XDG_SESSION_TYPE") or None
    display = os.environ.get("DISPLAY") or None
    wayland_display = os.environ.get("WAYLAND_DISPLAY") or None

    session_type, notes = _detect_session_type(
        xdg_session_type=xdg_session_type,
        wayland_display=wayland_display,
        display=display,
    )

    return SessionInfo(
        session_type=session_type,
        display=display,
        wayland_display=wayland_display,
        xdg_session_type=xdg_session_type,
        desktop_environment=os.environ.get("XDG_CURRENT_DESKTOP") or None,
        session_desktop=os.environ.get("DESKTOP_SESSION") or None,
        os_name=platform.system() or "",
        os_version=platform.release() or "",
        python_version=platform.python_version(),
        portal_available=_dbus_name_has_owner("org.freedesktop.portal.Desktop"),
        atspi_bus_available=_dbus_name_has_owner("org.a11y.Bus"),
        notes=notes,
    )
