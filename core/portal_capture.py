"""Wayland capture through the XDG ScreenCast portal + PipeWire (sections 30, 33).

On a native Wayland session ``mss`` (an X11/XShm reader) cannot see compositor
state: the specification is explicit that capture there must go through the
ScreenCast portal and PipeWire. This module implements that path as a
:class:`~core.frame_engine.CaptureBackend` so the *existing* frame engine --
bounded retention, change classification, honest failure -- consumes its frames
unchanged. It is deliberately not a parallel perception path: the only thing that
changes is where the pixels come from.

The flow follows the xdg-desktop-portal ScreenCast specification:

1. ``CreateSession`` -> ``SelectSources`` (monitors) -> ``Start``. ``Start`` is
   the interactive step: the compositor shows its own consent dialog and the
   user picks what to share. BLAXCY never bypasses or pre-answers that dialog --
   an empty ``parent_window`` is sent, so the portal shows it itself.
2. The ``Start`` response carries ``streams`` -- ``(node_id, props)`` pairs.
   The node id is the PipeWire node the compositor streams the desktop into.
3. The node is consumed over PipeWire as a video stream. Each pushed buffer is
   converted to plain BGRA bytes -- exactly what ``FrameEngine.grab`` expects
   from a backend.

Honesty rules carried over from the rest of the project:

* **The probe is functional but does not steal input or pixels.** It talks to the
  live portal (and, when asked, verifies that a PipeWire consumer can be built)
  without creating a session and without raising a consent dialog.
* **No consent, no capture.** Until a user grants the session, every grab fails
  with ``CAPTURE_FAILED`` and the capability report says ``UNAVAILABLE`` -- it
  never falls back to a black frame or a guess.
* **The portal's coordinate space is the captured stream's own pixel space.**
  The consumer reports the stream's real dimensions via ``monitor_bounds``, which
  is what the frame engine stamps into every frame; per-monitor geometry beyond
  that is reported as the single captured surface (the portal shares what the
  user chose, and that choice is authoritative).

Everything D-Bus/PipeWire is behind two small protocols
(:class:`ScreencastTransport` and :class:`PipeWireSource`), so the session
semantics -- request/response handling, buffer conversion, frame ownership --
are unit-tested with fakes exactly like the RemoteDesktop input backend.
``dbus``/``gi.repository.Gst``/``pw`` are imported lazily inside the real
transports, so importing this module never requires them.
"""

from __future__ import annotations

import os
import threading
import time
from contextlib import suppress
from typing import Any, Protocol

from schemas.enums import ErrorCode
from schemas.errors import BlaxcyError

#: The desktop portal's well-known bus name/object path and the ScreenCast
#: interface (xdg-desktop-portal spec). Same object as RemoteDesktop.
SCREENCAST_BUS_NAME = "org.freedesktop.portal.Desktop"
SCREENCAST_OBJECT_PATH = "/org/freedesktop/portal/desktop"
SCREENCAST_INTERFACE = "org.freedesktop.portal.ScreenCast"

#: Source-type bitmask for ``SelectSources``: 1 == monitors (xdg-desktop-portal).
SOURCE_MONITOR = 1

#: Cursor modes the portal may be asked for: 1 == hidden, 2 == embedded, 4 == metadata.
CURSOR_METADATA = 4

#: Persist mode 2 == "persistent" (xdg-desktop-portal 1.6+); older portals reject
#: the key, so it is only sent when the interface reports version >= 4.
_PERSISTENT_MODE = 2
_PERSIST_MODE_VERSION = 4

_RESPONSE_SUCCESS = 0

#: The application id the portal records consent against.
PORTAL_APP_ID_DEFAULT = "org.blaxcy.Body"

#: How long to wait for a portal request's ``Response`` signal.
DEFAULT_REQUEST_TIMEOUT_S = 60.0

#: How long a buffer consumer waits for a frame before reporting a capture error.
#: The frame engine's own three-strike failure policy handles the rest.
DEFAULT_FRAME_TIMEOUT_S = 2.0


class ScreencastError(RuntimeError):
    """A failure talking to the ScreenCast portal (raised by a transport)."""


class ScreencastTransport(Protocol):
    """The narrow portal surface the capture backend needs (injectable for tests)."""

    def availability(self) -> tuple[bool, int | None]:
        """Whether the ScreenCast interface is present and its version."""

    def open_session(
        self,
        *,
        app_id: str,
        source_types: int,
        cursor_mode: int,
        timeout_s: float,
    ) -> tuple[str, tuple[int, ...]]:
        """Create, configure and start a session; return ``(handle, node_ids)``."""

    def close_session(self, session: str) -> None:
        """Close the portal session best-effort."""

    def close(self) -> None:
        """Release transport resources."""


class PipeWireSource(Protocol):
    """One frame consumer for a ScreenCast node (injectable for tests)."""

    name: str

    def start(self, node_id: int) -> tuple[int, int]:
        """Connect to ``node_id``; return the stream's ``(width, height)``."""

    def grab(self) -> tuple[bytes, int, int]:
        """The next frame as ``(bgra_bytes, width, height)``; blocks briefly."""

    def close(self) -> None:
        """Release the stream."""


def _plain(value: Any) -> Any:
    """Recursively convert dbus container types to plain Python."""
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


class DbusScreencastTransport:
    """The real ScreenCast transport, over dbus-python (imported lazily)."""

    def __init__(self) -> None:
        """Connect to the session bus and resolve the portal object.

        Raises:
            ScreencastError: when dbus-python, the session bus or the portal
                itself is unavailable -- never a partially usable object.
        """
        try:
            import dbus
            import dbus.mainloop.glib
        except Exception as exc:  # pragma: no cover - depends on the host
            raise ScreencastError(f"dbus-python is not importable: {exc!r}") from exc
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        self._dbus = dbus
        try:
            self._bus = dbus.SessionBus()
        except Exception as exc:  # pragma: no cover - depends on the host
            raise ScreencastError(f"no session bus is available: {exc!r}") from exc
        try:
            self._portal = self._bus.get_object(SCREENCAST_BUS_NAME, SCREENCAST_OBJECT_PATH)
        except Exception as exc:  # pragma: no cover - depends on the host
            raise ScreencastError(f"the desktop portal is not on the session bus: {exc!r}") from exc
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
        """Introspect the live portal for the ScreenCast interface."""
        introspectable = self._dbus.Interface(
            self._portal, "org.freedesktop.DBus.Introspectable"
        )
        try:
            xml = str(introspectable.Introspect())
        except Exception as exc:  # pragma: no cover - depends on the host
            raise ScreencastError(f"the portal could not be introspected: {exc!r}") from exc
        if SCREENCAST_INTERFACE not in xml:
            return False, None
        version: int | None = None
        try:
            properties = self._dbus.Interface(self._portal, "org.freedesktop.DBus.Properties")
            version = int(properties.Get(SCREENCAST_INTERFACE, "version"))
        except Exception:  # pragma: no cover - a portal may omit the property
            version = None
        return True, version

    def open_session(
        self,
        *,
        app_id: str,
        source_types: int,
        cursor_mode: int,
        timeout_s: float,
    ) -> tuple[str, tuple[int, ...]]:
        """Run CreateSession -> SelectSources -> Start and return nodes."""
        remote = self._dbus.Interface(self._portal, SCREENCAST_INTERFACE)
        available, version = self.availability()
        if not available:
            raise ScreencastError(
                "the portal does not advertise org.freedesktop.portal.ScreenCast"
            )
        options: dict[str, Any] = {
            "session_handle_token": self._token("session"),
            "cursor_mode": self._dbus.UInt32(int(cursor_mode)),
        }
        if version is not None and version >= _PERSIST_MODE_VERSION:
            options["persist_mode"] = self._dbus.UInt32(_PERSISTENT_MODE)
        results = self._await_response(
            remote.CreateSession(self._options(options)), timeout_s
        )
        session = results.get("session_handle")
        if not session:
            raise ScreencastError("the portal did not return a session handle")
        session = str(session)
        self._await_response(
            remote.SelectSources(
                session,
                self._options({"types": self._dbus.UInt32(int(source_types))}),
            ),
            timeout_s,
        )
        # An empty parent window is valid: the portal shows its own consent dialog.
        started = self._await_response(
            remote.Start(session, "", self._options({"app_id": app_id} if app_id else {})),
            timeout_s,
        )
        streams = started.get("streams") or []
        nodes: list[int] = []
        for stream in streams:
            # streams is a list of (node_id, props) pairs.
            if isinstance(stream, (list, tuple)) and stream:
                nodes.append(int(stream[0]))
            elif isinstance(stream, int):
                nodes.append(stream)
        if not nodes:
            raise ScreencastError(
                "the user granted the session but the portal returned no stream nodes"
            )
        return session, tuple(nodes)

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
            dbus_interface="org.freedesktop.portal.Request",
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
                    dbus_interface="org.freedesktop.portal.Request",
                    path=path,
                )
        if "response" not in captured:
            raise ScreencastError(
                f"the portal did not answer the request within {timeout_s:.1f}s"
            )
        if captured["response"] != _RESPONSE_SUCCESS:
            raise ScreencastError(
                f"the portal refused the request (response {captured['response']})"
            )
        results = captured.get("results", {})
        return results if isinstance(results, dict) else {}

    def close_session(self, session: str) -> None:
        """Ask the portal to close the session, ignoring an already-gone one."""
        try:
            obj = self._bus.get_object(SCREENCAST_BUS_NAME, session)
            self._dbus.Interface(obj, "org.freedesktop.portal.Session").Close()
        except Exception:  # pragma: no cover - best effort by contract
            pass

    def close(self) -> None:
        """Nothing to release: the session bus is shared and owned by dbus-python."""
        return None


class GstPipeWireSource:
    """A PipeWire frame consumer over GStreamer's pipewiresrc (lazy imports).

    GStreamer's ``pipewiresrc`` element is the maintained way to consume a
    ScreenCast node; it negotiates the stream format and hands over buffers. A
    custom ``appsink`` converts every pushed buffer to plain BGRA bytes.

    The heavy imports live in :meth:`start`, so constructing the source is cheap
    and a host without GStreamer reports that honestly when capture begins.
    """

    name = "pipewire-gst"

    def __init__(self, *, frame_timeout_s: float = DEFAULT_FRAME_TIMEOUT_S) -> None:
        self._frame_timeout_s = frame_timeout_s
        self._lock = threading.Lock()
        self._latest: tuple[bytes, int, int] | None = None
        self._condition = threading.Condition(self._lock)
        self._pipeline: Any = None
        self._appsink: Any = None

    def start(self, node_id: int) -> tuple[int, int]:
        """Connect to ``node_id`` and wait for the first frame's geometry."""
        try:
            import gi

            gi.require_version("Gst", "1.0")
            from gi.repository import Gst
        except Exception as exc:  # pragma: no cover - depends on the host
            raise BlaxcyError(
                ErrorCode.CAPTURE_FAILED,
                "GStreamer (for PipeWire capture) is not importable",
                details={"error": f"{type(exc).__name__}: {exc}"},
            ) from exc
        Gst.init(None)
        pipeline_text = (
            f"pipewiresrc path={int(node_id)} keepalive-time=1000 ! "
            "video/x-raw,format=BGRx ! "
            "videoconvert ! "
            "appsink name=sink emit-signals=true max-buffers=2 drop=true sync=false"
        )
        self._pipeline = Gst.parse_launch(pipeline_text)
        self._appsink = self._pipeline.get_by_name("sink")
        self._appsink.connect("new-sample", self._on_sample)
        self._pipeline.set_state(Gst.State.PLAYING)
        # The first sample carries the real geometry; wait briefly for it so the
        # frame engine can stamp the stream's own size, not a guess.
        deadline = time.monotonic() + self._frame_timeout_s
        while time.monotonic() < deadline:
            with self._lock:
                if self._latest is not None:
                    _, width, height = self._latest
                    return width, height
            time.sleep(0.02)
        raise BlaxcyError(
            ErrorCode.CAPTURE_FAILED,
            "the PipeWire stream produced no frame to learn its geometry from",
            details={"node_id": int(node_id)},
        )

    def _on_sample(self, appsink: Any) -> Any:
        """Convert one GStreamer sample into the latest BGRA frame."""
        sample = appsink.emit("pull-sample")
        if sample is None:
            return None
        try:
            from gi.repository import Gst

            caps = sample.get_caps()
            width = int(caps.get_structure(0).get_value("width"))
            height = int(caps.get_structure(0).get_value("height"))
            buffer = sample.get_buffer()
            success, map_info = buffer.map(Gst.MapFlags.READ)
            if not success:
                return None
            try:
                raw = bytes(map_info.data)
            finally:
                buffer.unmap(map_info)
        except Exception:  # pragma: no cover - defensive: a bad sample is dropped
            return None
        with self._condition:
            # BGRx is 4 bytes per pixel; keep exactly one frame.
            self._latest = (raw[: width * height * 4], width, height)
            self._condition.notify_all()
        return None

    def grab(self) -> tuple[bytes, int, int]:
        """Return the latest frame, or raise when none has arrived in time."""
        deadline = time.monotonic() + self._frame_timeout_s
        with self._condition:
            while True:
                if self._latest is not None:
                    return self._latest
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    raise RuntimeError("the PipeWire stream produced no frame in time")
                self._condition.wait(remaining)

    def close(self) -> None:
        """Stop the pipeline. Idempotent."""
        pipeline = self._pipeline
        self._pipeline = None
        self._appsink = None
        if pipeline is None:
            return
        with suppress(Exception):
            from gi.repository import Gst

            pipeline.send_event(Gst.Event.new_eos())
            pipeline.set_state(Gst.State.NULL)


class PortalCaptureBackend:
    """A ``FrameEngine`` capture backend over the ScreenCast portal + PipeWire.

    Satisfies :class:`~core.frame_engine.CaptureBackend` structurally, so the
    frame engine consumes its frames unchanged (bounded retention, change
    classification, honest failure all stay in force).

    Args:
        transport: The portal transport; defaults to the real dbus one.
        source_factory: Builds the PipeWire consumer for a node id.
        app_id: Reverse-DNS application id recorded against consent.
        request_timeout_s: How long each portal request may take.
        frame_timeout_s: How long a grab waits for a compositor frame.
    """

    name = "portal-screencast"

    def __init__(
        self,
        *,
        transport: ScreencastTransport | None = None,
        source_factory: Any = None,
        app_id: str = "org.blaxcy.Body",
        request_timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S,
        frame_timeout_s: float = DEFAULT_FRAME_TIMEOUT_S,
    ) -> None:
        self._transport = transport if transport is not None else DbusScreencastTransport()
        self._source_factory = source_factory if source_factory is not None else GstPipeWireSource
        self._app_id = app_id
        self._request_timeout_s = request_timeout_s
        self._frame_timeout_s = frame_timeout_s
        self._session: str | None = None
        self._source: Any = None
        self._bounds: tuple[int, int, int, int] = (0, 0, 0, 0)
        self._lock = threading.Lock()

    # -- Lifecycle ------------------------------------------------------------

    def _ensure_session(self) -> None:
        """Open the portal session and the PipeWire consumer, once.

        The consent dialog is raised by ``Start`` (with an empty parent window),
        so the *first* grab is the interactive one. Until it succeeds, capture
        keeps failing honestly; nothing is fabricated while permission is
        pending.
        """
        with self._lock:
            if self._source is not None:
                return
            try:
                session, nodes = self._transport.open_session(
                    app_id=self._app_id,
                    source_types=SOURCE_MONITOR,
                    cursor_mode=CURSOR_METADATA,
                    timeout_s=self._request_timeout_s,
                )
            except Exception as exc:
                # No consent (or no portal) means no pixels: report the taxonomy
                # failure the frame engine understands rather than a bare error.
                raise BlaxcyError(
                    ErrorCode.CAPTURE_FAILED,
                    f"the ScreenCast session could not be established: {exc!r}",
                ) from exc
            self._session = session
            try:
                source = self._source_factory(frame_timeout_s=self._frame_timeout_s)
                width, height = source.start(nodes[0])
            except Exception as exc:
                # Never leave a half-open portal session behind, and never let a
                # bare exception escape: a failure here is a CAPTURE_FAILED, the
                # taxonomy the frame engine (and its disarm policy) understands.
                with suppress(Exception):
                    self._transport.close_session(session)
                self._session = None
                raise BlaxcyError(
                    ErrorCode.CAPTURE_FAILED,
                    f"the PipeWire capture source could not start: {exc!r}",
                ) from exc
            self._source = source
            self._bounds = (0, 0, int(width), int(height))

    # -- The CaptureBackend contract -------------------------------------------

    def probe(self) -> dict[str, Any]:
        """A functional, non-invasive probe of the capture path.

        Talks to the live portal and reports whether the ScreenCast interface is
        advertised (and at which version). It never creates a session, so no
        consent dialog appears and no pixels are taken. A probe that merely
        imported ``dbus`` would prove nothing (section 28 rule 12).
        """
        try:
            available, version = self._transport.availability()
        except Exception as exc:
            return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}
        if not available:
            return {
                "available": False,
                "reason": "the portal does not advertise org.freedesktop.portal.ScreenCast",
            }
        return {"available": True, "portal_version": version}

    def grab(self) -> tuple[bytes, int, int]:
        """One desktop frame as BGRA bytes, or ``CAPTURE_FAILED``."""
        self._ensure_session()
        source = self._source
        if source is None:  # pragma: no cover - _ensure_session guarantees this
            raise BlaxcyError(ErrorCode.CAPTURE_FAILED, "the capture source is unavailable")
        try:
            raw, width, height = source.grab()
        except BlaxcyError:
            raise
        except Exception as exc:
            raise BlaxcyError(
                ErrorCode.CAPTURE_FAILED,
                f"the PipeWire capture source failed: {exc!r}",
            ) from exc
        return raw, width, height

    def monitor_bounds(self) -> tuple[int, int, int, int]:
        """The captured region in the stream's own pixel space."""
        with self._lock:
            return self._bounds

    def monitors(self) -> tuple[dict[str, int], ...]:
        """The single captured surface the user consented to share."""
        left, top, width, height = self.monitor_bounds()
        return ({"left": left, "top": top, "width": width, "height": height},)

    def close(self) -> None:
        """Close the PipeWire consumer and the portal session. Idempotent."""
        with self._lock:
            source, self._source = self._source, None
            session, self._session = self._session, None
        if source is not None:
            with suppress(Exception):
                source.close()
        if session is not None:
            with suppress(Exception):
                self._transport.close_session(session)
        with suppress(Exception):
            self._transport.close()


def probe_portal_capture() -> dict[str, Any]:
    """The functional, non-invasive ScreenCast probe used by capability probing.

    Never creates a session, never raises a consent dialog, never reads pixels:
    it asks the live portal whether the ScreenCast interface is there.
    """
    try:
        backend = PortalCaptureBackend()
    except Exception as exc:  # pragma: no cover - defensive
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}
    try:
        return backend.probe()
    finally:
        with suppress(Exception):
            backend.close()


__all__ = [
    "CURSOR_METADATA",
    "DEFAULT_FRAME_TIMEOUT_S",
    "DEFAULT_REQUEST_TIMEOUT_S",
    "PORTAL_APP_ID_DEFAULT",
    "SCREENCAST_BUS_NAME",
    "SCREENCAST_INTERFACE",
    "SCREENCAST_OBJECT_PATH",
    "SOURCE_MONITOR",
    "DbusScreencastTransport",
    "GstPipeWireSource",
    "PipeWireSource",
    "PortalCaptureBackend",
    "ScreencastError",
    "ScreencastTransport",
    "probe_portal_capture",
]
