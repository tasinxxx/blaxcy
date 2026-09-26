"""Wayland input through the XDG RemoteDesktop portal (specification section 30).

On a native Wayland session there is no X server for XTEST to inject into, and a
compositor does not accept synthetic input from an arbitrary client. The
sanctioned path is the desktop portal: BLAXCY asks
``org.freedesktop.portal.RemoteDesktop`` for a session, the user consents once,
and the portal then forwards pointer and keyboard events into the compositor.

This module implements that path as an
:class:`~control.backends.base.InputBackend` so it is interchangeable with XTEST:
the executor, lease and revalidation rules are identical and only the *how* of
injection changes (section 30). Like every backend it performs no policy, lease
or target check.

Two properties are deliberate:

* **The probe is functional and non-invasive.** It talks to the live session bus
  and asks whether the portal really advertises the RemoteDesktop interface (and
  at which version). It never creates a session and never injects anything, so a
  probe can run at startup without grabbing the user's input or raising a consent
  dialog. A probe that merely imported ``dbus`` would prove nothing (section 28
  rule 12).
* **Absolute pointer motion is honest.** The portal injects *relative* pointer
  motion unless a ScreenCast stream node is supplied for
  ``NotifyPointerMotionAbsolute``. This backend therefore tracks the last injected
  position and moves by delta; when the position is unknown and no stream node is
  configured it refuses (``BACKEND_UNAVAILABLE``) rather than guessing a delta.

``dbus`` (dbus-python) is imported lazily inside the transport, so importing this
module never requires it: the probe reports the capability honestly instead of
failing the process (section 28).
"""

from __future__ import annotations

import os
import threading
from contextlib import suppress
from typing import Any, Protocol

from control.backends.base import BackendProbe, InputBackend, KeyResolution, PointerButton
from schemas.enums import ErrorCode
from schemas.errors import BlaxcyError

#: The desktop portal's well-known bus name, object path and the interface this
#: backend needs (xdg-desktop-portal spec).
PORTAL_BUS_NAME = "org.freedesktop.portal.Desktop"
PORTAL_OBJECT_PATH = "/org/freedesktop/portal/desktop"
REMOTE_DESKTOP_INTERFACE = "org.freedesktop.portal.RemoteDesktop"
REQUEST_INTERFACE = "org.freedesktop.portal.Request"
SESSION_INTERFACE = "org.freedesktop.portal.Session"
PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"
INTROSPECTABLE_INTERFACE = "org.freedesktop.DBus.Introspectable"

#: Device-type bitmask for ``SelectDevices`` (xdg-desktop-portal spec).
DEVICE_KEYBOARD = 1
DEVICE_POINTER = 2

#: evdev pointer button codes the portal expects (BTN_LEFT/BTN_RIGHT/BTN_MIDDLE).
_BUTTON_CODES: dict[PointerButton, int] = {
    PointerButton.LEFT: 0x110,
    PointerButton.MIDDLE: 0x112,
    PointerButton.RIGHT: 0x111,
}

_STATE_PRESSED = 1
_STATE_RELEASED = 0
_AXIS_VERTICAL = 0
_AXIS_HORIZONTAL = 1
_RESPONSE_SUCCESS = 0

#: How long to wait for a portal request's ``Response`` signal before giving up.
DEFAULT_SESSION_TIMEOUT_S = 30.0

#: The application id the portal records consent against. A reverse-DNS name is
#: what the portal expects; it is not the Linux application id used elsewhere.
DEFAULT_APP_ID = "org.blaxcy.Body"


class PortalError(RuntimeError):
    """A failure talking to the desktop portal (raised by a transport)."""


class PortalTransport(Protocol):
    """The narrow portal surface the backend needs (injectable for tests).

    This is the seam between the input backend and D-Bus: the backend owns the
    input semantics and the transport owns the wire protocol. Tests supply a fake
    transport, so the backend's behaviour is exercised without a portal.
    """

    def availability(self) -> tuple[bool, int | None]:
        """Return whether the RemoteDesktop interface is present and its version."""

    def open_session(
        self,
        *,
        app_id: str,
        device_types: int,
        stream_node: int | None,
        timeout_s: float,
    ) -> str:
        """Create, configure and start a RemoteDesktop session; return its handle."""

    def notify(self, method: str, session: str, *args: Any) -> None:
        """Invoke one ``Notify*`` method on the portal's RemoteDesktop interface."""

    def close_session(self, session: str) -> None:
        """Close the portal session best-effort."""

    def close(self) -> None:
        """Release any transport resources."""


def _plain(value: Any) -> Any:
    """Recursively convert dbus container types to plain Python.

    dbus-python returns ``dbus.Dictionary``/``dbus.Array``/``dbus.String`` and
    their scalar types; the backend should not have to know about any of them.
    """
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


class DbusPortalTransport:
    """The real portal transport, over dbus-python (imported lazily)."""

    def __init__(self) -> None:
        """Connect to the session bus and resolve the portal object.

        Raises:
            PortalError: when dbus-python, the session bus or the portal itself
                is unavailable -- never a partially usable object.
        """
        try:
            import dbus
            import dbus.mainloop.glib
        except Exception as exc:  # pragma: no cover - depends on the host
            raise PortalError(f"dbus-python is not importable: {exc!r}") from exc
        # Signals are dispatched through GLib; nothing else in BLAXCY runs a GLib
        # main loop (the AT-SPI service uses plain threads), so setting the default
        # here cannot steal it from another consumer.
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        self._dbus = dbus
        try:
            self._bus = dbus.SessionBus()
        except Exception as exc:  # pragma: no cover - depends on the host
            raise PortalError(f"no session bus is available: {exc!r}") from exc
        try:
            self._portal = self._bus.get_object(PORTAL_BUS_NAME, PORTAL_OBJECT_PATH)
        except Exception as exc:  # pragma: no cover - depends on the host
            raise PortalError(f"the desktop portal is not on the session bus: {exc!r}") from exc
        self._counter = 0

    def _token(self, prefix: str) -> str:
        """A unique handle/session token for one portal request."""
        self._counter += 1
        return f"blaxcy_{prefix}_{os.getpid()}_{self._counter}"

    def _options(self, extra: dict[str, Any]) -> Any:
        """Build an ``a{sv}`` options dict with a fresh ``handle_token``."""
        options: dict[str, Any] = dict(extra)
        options["handle_token"] = self._token("handle")
        return self._dbus.Dictionary(options, signature="sv")

    def availability(self) -> tuple[bool, int | None]:
        """Introspect the live portal for the RemoteDesktop interface."""
        introspectable = self._dbus.Interface(self._portal, INTROSPECTABLE_INTERFACE)
        try:
            xml = str(introspectable.Introspect())
        except Exception as exc:  # pragma: no cover - depends on the host
            raise PortalError(f"the portal could not be introspected: {exc!r}") from exc
        if REMOTE_DESKTOP_INTERFACE not in xml:
            return False, None
        version: int | None = None
        try:
            properties = self._dbus.Interface(self._portal, PROPERTIES_INTERFACE)
            version = int(properties.Get(REMOTE_DESKTOP_INTERFACE, "version"))
        except Exception:  # pragma: no cover - a portal may omit the property
            version = None
        return True, version

    def open_session(
        self,
        *,
        app_id: str,
        device_types: int,
        stream_node: int | None,
        timeout_s: float,
    ) -> str:
        """Run CreateSession -> SelectDevices -> Start and return the session."""
        remote = self._dbus.Interface(self._portal, REMOTE_DESKTOP_INTERFACE)
        results = self._await_response(
            remote.CreateSession(
                self._options({"session_handle_token": self._token("session")})
            ),
            timeout_s,
        )
        session = results.get("session_handle")
        if not session:
            raise PortalError("the portal did not return a session handle")
        session = str(session)
        selected: dict[str, Any] = {"types": self._dbus.UInt32(int(device_types))}
        self._await_response(
            remote.SelectDevices(session, self._options(selected)), timeout_s
        )
        start_options: dict[str, Any] = {"app_id": app_id} if app_id else {}
        # An empty parent window is valid: the portal shows its own consent dialog.
        self._await_response(
            remote.Start(session, "", self._options(start_options)), timeout_s
        )
        return session

    def _await_response(self, request_path: Any, timeout_s: float) -> dict[str, Any]:
        """Block until the request's ``Response`` signal, or time out honestly."""
        from gi.repository import GLib

        captured: dict[str, Any] = {}
        loop = GLib.MainLoop()

        def _on_response(response: Any, results: Any) -> None:
            captured["response"] = int(response)
            captured["results"] = _plain(results)
            loop.quit()

        path = str(request_path)
        self._bus.add_signal_receiver(
            _on_response,
            signal_name="Response",
            dbus_interface=REQUEST_INTERFACE,
            path=path,
        )
        GLib.timeout_add(max(1, int(max(0.0, timeout_s) * 1000)), loop.quit)
        try:
            loop.run()
        finally:
            with suppress(Exception):
                self._bus.remove_signal_receiver(
                    _on_response,
                    signal_name="Response",
                    dbus_interface=REQUEST_INTERFACE,
                    path=path,
                )
        if "response" not in captured:
            raise PortalError(
                f"the portal did not answer the request within {timeout_s:.1f}s"
            )
        if captured["response"] != _RESPONSE_SUCCESS:
            raise PortalError(
                f"the portal refused the request (response {captured['response']})"
            )
        results = captured.get("results", {})
        return results if isinstance(results, dict) else {}

    def notify(self, method: str, session: str, *args: Any) -> None:
        """Call one ``Notify*`` method, with the session and empty options."""
        remote = self._dbus.Interface(self._portal, REMOTE_DESKTOP_INTERFACE)
        call = getattr(remote, method, None)
        if call is None:
            raise PortalError(f"the portal has no {method} method")
        call(session, self._dbus.Dictionary({}, signature="sv"), *args)

    def close_session(self, session: str) -> None:
        """Ask the portal to close the session, ignoring an already-gone one."""
        try:
            obj = self._bus.get_object(PORTAL_BUS_NAME, session)
            self._dbus.Interface(obj, SESSION_INTERFACE).Close()
        except Exception:  # pragma: no cover - best effort by contract
            pass

    def close(self) -> None:
        """Nothing to release: the session bus is shared and owned by dbus-python."""
        return None


class PortalRemoteDesktopBackend(InputBackend):
    """Pointer and keyboard injection through the RemoteDesktop portal."""

    def __init__(
        self,
        *,
        transport_factory: Any = None,
        app_id: str = DEFAULT_APP_ID,
        stream_node: int | None = None,
        initial_position: tuple[int, int] | None = None,
        session_timeout_s: float = DEFAULT_SESSION_TIMEOUT_S,
        select_devices: int = DEVICE_KEYBOARD | DEVICE_POINTER,
    ) -> None:
        """Create the backend.

        Args:
            transport_factory: Builds the portal transport; defaults to
                :class:`DbusPortalTransport`. Tests inject a fake.
            app_id: The reverse-DNS application id the portal records consent for.
            stream_node: A ScreenCast PipeWire node id enabling *absolute* pointer
                motion. Without it the backend can only move by delta.
            initial_position: A known current pointer position, if the caller has
                one (e.g. from a ScreenCast cursor). Without it the first absolute
                move cannot be expressed and is refused.
            session_timeout_s: Bounded wait for each portal request.
            select_devices: The device-type bitmask to request.
        """
        self._lock = threading.RLock()
        self._transport_factory = transport_factory or DbusPortalTransport
        self._app_id = app_id
        self._stream_node = stream_node
        self._position: tuple[int, int] | None = (
            (int(initial_position[0]), int(initial_position[1]))
            if initial_position is not None
            else None
        )
        self._session_timeout_s = session_timeout_s
        self._select_devices = select_devices
        self._transport: PortalTransport | None = None
        self._session: str | None = None
        self._held_buttons: list[PointerButton] = []
        self._held_keys: list[int] = []

    @property
    def name(self) -> str:
        """Short backend identifier."""
        return "portal-remotedesktop"

    # -- Transport / session lifecycle ----------------------------------------

    def _require_transport(self) -> PortalTransport:
        """Return the transport, creating it on first use."""
        with self._lock:
            if self._transport is None:
                try:
                    self._transport = self._transport_factory()
                except Exception as exc:
                    raise BlaxcyError(
                        ErrorCode.BACKEND_UNAVAILABLE,
                        f"the RemoteDesktop portal transport is unavailable: {exc}",
                        details={"backend": self.name},
                    ) from exc
            return self._transport

    def _require_session(self) -> str:
        """Return the live session handle, starting a session on first use."""
        with self._lock:
            if self._session is not None:
                return self._session
            transport = self._require_transport()
            try:
                session = transport.open_session(
                    app_id=self._app_id,
                    device_types=self._select_devices,
                    stream_node=self._stream_node,
                    timeout_s=self._session_timeout_s,
                )
            except Exception as exc:
                raise BlaxcyError(
                    ErrorCode.BACKEND_UNAVAILABLE,
                    f"a RemoteDesktop session could not be started: {exc}",
                    details={"backend": self.name},
                ) from exc
            self._session = session
            return session

    # -- Probe ----------------------------------------------------------------

    def probe(self) -> BackendProbe:
        """Ask the live portal whether the RemoteDesktop interface exists.

        Nothing is created and nothing is injected: the probe only introspects the
        portal object and reads the interface version (section 28 rule 12). A
        session is started lazily, on first injection, because doing so presents
        the user a consent dialog.
        """
        try:
            transport = self._require_transport()
        except BlaxcyError as exc:
            return BackendProbe(self.name, False, exc.message)
        try:
            present, version = transport.availability()
        except Exception as exc:
            return BackendProbe(
                self.name, False, f"the session bus could not be queried: {exc}"
            )
        if not present:
            return BackendProbe(
                self.name,
                False,
                "the desktop portal does not advertise "
                f"{REMOTE_DESKTOP_INTERFACE} on this session",
            )
        details: dict[str, Any] = {
            "interface_version": version,
            "absolute_pointer": self._stream_node is not None,
            "session_started": self._session is not None,
            "device_types": self._select_devices,
        }
        return BackendProbe(self.name, True, None, details)

    # -- Pointer --------------------------------------------------------------

    def get_pointer_position(self) -> tuple[int, int] | None:
        """Return the last position this backend injected, or ``None``.

        The portal has no readback, so this is only ever what BLAXCY itself last
        moved to -- never a fabricated coordinate.
        """
        with self._lock:
            return self._position

    def move_pointer(self, x: int, y: int) -> None:
        """Inject a pointer move to absolute ``(x, y)``.

        Uses ``NotifyPointerMotionAbsolute`` when a ScreenCast stream node is
        configured; otherwise it moves by the delta from the last known position.
        With no stream node *and* no known position an absolute move cannot be
        expressed, and this refuses rather than guessing a delta.
        """
        session = self._require_session()
        target = (int(x), int(y))
        with self._lock:
            transport = self._require_transport()
            if self._stream_node is not None:
                transport.notify(
                    "NotifyPointerMotionAbsolute",
                    session,
                    int(self._stream_node),
                    float(target[0]),
                    float(target[1]),
                )
            elif self._position is not None:
                delta_x = target[0] - self._position[0]
                delta_y = target[1] - self._position[1]
                if delta_x or delta_y:
                    transport.notify(
                        "NotifyPointerMotion", session, float(delta_x), float(delta_y)
                    )
            else:
                raise BlaxcyError(
                    ErrorCode.BACKEND_UNAVAILABLE,
                    "the RemoteDesktop portal has no pointer readback and no "
                    "ScreenCast stream node is configured, so an absolute move "
                    "cannot be expressed",
                    details={"backend": self.name},
                )
            self._position = target

    def press_button(self, button: PointerButton) -> None:
        """Press a pointer button and record it as held."""
        session = self._require_session()
        with self._lock:
            self._require_transport().notify(
                "NotifyPointerButton", session, _BUTTON_CODES[button], _STATE_PRESSED
            )
            if button not in self._held_buttons:
                self._held_buttons.append(button)

    def release_button(self, button: PointerButton) -> None:
        """Release a pointer button and clear it from the held set."""
        session = self._require_session()
        with self._lock:
            self._require_transport().notify(
                "NotifyPointerButton", session, _BUTTON_CODES[button], _STATE_RELEASED
            )
            while button in self._held_buttons:
                self._held_buttons.remove(button)

    def scroll(self, *, vertical: int = 0, horizontal: int = 0) -> None:
        """Inject wheel steps; positive is down/right, negative up/left."""
        session = self._require_session()
        with self._lock:
            transport = self._require_transport()
            if vertical:
                transport.notify(
                    "NotifyPointerAxisDiscrete", session, _AXIS_VERTICAL, int(vertical)
                )
            if horizontal:
                transport.notify(
                    "NotifyPointerAxisDiscrete", session, _AXIS_HORIZONTAL, int(horizontal)
                )

    # -- Keyboard -------------------------------------------------------------

    def resolve_key(self, keysym: int) -> KeyResolution | None:
        """Resolve a keysym for the portal.

        The portal injects by keysym (``NotifyKeyboardKeysym``), so the keysym
        *is* the token injected: there is no keycode table to consult and no Shift
        level to reason about. A non-positive keysym has nothing to inject.
        """
        if int(keysym) <= 0:
            return None
        return KeyResolution(keysym=int(keysym), keycode=int(keysym), shift=False)

    def key_press(self, keycode: int) -> None:
        """Press a keysym and record it as held."""
        session = self._require_session()
        with self._lock:
            self._require_transport().notify(
                "NotifyKeyboardKeysym", session, int(keycode), _STATE_PRESSED
            )
            if int(keycode) not in self._held_keys:
                self._held_keys.append(int(keycode))

    def key_release(self, keycode: int) -> None:
        """Release a keysym and clear it from the held set."""
        session = self._require_session()
        with self._lock:
            self._require_transport().notify(
                "NotifyKeyboardKeysym", session, int(keycode), _STATE_RELEASED
            )
            while int(keycode) in self._held_keys:
                self._held_keys.remove(int(keycode))

    # -- Ownership reporting (sections 63, 64) --------------------------------

    @property
    def held_keys(self) -> tuple[int, ...]:
        """Every keysym currently held down, in press order."""
        with self._lock:
            return tuple(self._held_keys)

    @property
    def held_buttons(self) -> tuple[PointerButton, ...]:
        """Every pointer button currently held down, in press order."""
        with self._lock:
            return tuple(self._held_buttons)

    # -- Synchronisation and cleanup ------------------------------------------

    def flush(self) -> None:
        """No-op: portal ``Notify*`` calls are synchronous D-Bus method calls."""
        return None

    def release_all(self) -> None:
        """Release held input, safe from another thread and after a failure.

        The portal transport may already be gone (compositor restart, session
        closed), so each release is guarded individually and the held sets are
        cleared regardless -- a cleanup that raises would leave BLAXCY believing it
        still held the user's modifier.
        """
        with self._lock:
            transport = self._transport
            session = self._session
            if transport is not None and session is not None:
                for key in reversed(self._held_keys):
                    with suppress(Exception):
                        transport.notify(
                            "NotifyKeyboardKeysym", session, key, _STATE_RELEASED
                        )
                for button in reversed(self._held_buttons):
                    with suppress(Exception):
                        transport.notify(
                            "NotifyPointerButton",
                            session,
                            _BUTTON_CODES[button],
                            _STATE_RELEASED,
                        )
            self._held_keys.clear()
            self._held_buttons.clear()

    def close(self) -> None:
        """Release held input, close the portal session and drop the transport."""
        with self._lock:
            self.release_all()
            transport = self._transport
            session = self._session
            self._session = None
            self._transport = None
            if transport is not None:
                if session is not None:
                    with suppress(Exception):
                        transport.close_session(session)
                with suppress(Exception):
                    transport.close()


__all__ = [
    "DEFAULT_APP_ID",
    "DEVICE_KEYBOARD",
    "DEVICE_POINTER",
    "PORTAL_BUS_NAME",
    "PORTAL_OBJECT_PATH",
    "REMOTE_DESKTOP_INTERFACE",
    "DbusPortalTransport",
    "PortalError",
    "PortalRemoteDesktopBackend",
    "PortalTransport",
]
