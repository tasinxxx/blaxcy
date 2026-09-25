"""Window management and activation (specification section 47).

Section 47 is explicit that "ensure window" must be a real activation, not a
focus request that is assumed to have worked. So this module does three things
and reports each honestly:

1. it *asks* the window manager to activate a window (an EWMH
   ``_NET_ACTIVE_WINDOW`` client message, which is the standard, cooperative
   way to do this -- it never fights the window manager by mapping or raising
   windows out from under it);
2. it *reads the active window back* and only reports success when the requested
   window is actually active;
3. it *gives up after a bounded timeout* and says so, rather than looping.

Everything here is read-only with respect to target applications: it never
launches, closes, moves or restyles a window. The Xlib import is lazy and
guarded, so this module imports cleanly on a machine with no display at all --
it only fails when it is actually asked to do something.
"""

from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass, field
from typing import Any

from schemas.enums import ErrorCode
from schemas.errors import BlaxcyError

#: EWMH atom names used below.
_ATOM_ACTIVE_WINDOW = "_NET_ACTIVE_WINDOW"
_ATOM_CLIENT_LIST = "_NET_CLIENT_LIST"
_ATOM_CLIENT_LIST_STACKING = "_NET_CLIENT_LIST_STACKING"
_ATOM_WM_NAME = "_NET_WM_NAME"
_ATOM_WM_PID = "_NET_WM_PID"

#: Default bounded wait for an activation to take effect (section 47).
DEFAULT_ENSURE_WINDOW_TIMEOUT_MS: int = 600

#: Poll interval while waiting for the window manager to act.
_POLL_INTERVAL_SECONDS: float = 0.02


@dataclass(frozen=True)
class WindowInfo:
    """Metadata about one top-level window."""

    window_id: int
    title: str | None = None
    wm_class: str | None = None
    wm_instance: str | None = None
    pid: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-shaped window metadata."""
        return {
            "window_id": self.window_id,
            "title": self.title,
            "wm_class": self.wm_class,
            "wm_instance": self.wm_instance,
            "pid": self.pid,
        }


@dataclass(frozen=True)
class WindowManagerProbe:
    """The result of a functional window-manager probe (section 28)."""

    available: bool
    backend: str | None = None
    reason: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


class WindowController:
    """The narrow window contract the executor depends on.

    Kept as a concrete base rather than an abstract Protocol because the only
    behaviour the executor needs is small and it is genuinely useful to have one
    real implementation beside the test doubles.
    """

    def probe(self) -> WindowManagerProbe:
        """Functionally probe the window-manager backend (no window changes)."""
        raise NotImplementedError

    def active_window(self) -> int | None:
        """Return the currently active window id, or ``None`` when unknown."""
        raise NotImplementedError

    def list_windows(self) -> tuple[WindowInfo, ...]:
        """Return the top-level client windows known to the window manager."""
        raise NotImplementedError

    def window_info(self, window_id: int) -> WindowInfo | None:
        """Return metadata for ``window_id``, or ``None`` when it is gone."""
        raise NotImplementedError

    def activate(self, window_id: int) -> bool:
        """Ask the window manager to activate ``window_id``; return whether asked."""
        raise NotImplementedError

    def ensure_active(self, window_id: int, *, timeout_ms: int | None = None) -> bool:
        """Activate ``window_id`` and confirm it actually became active."""
        raise NotImplementedError


class WindowManager(WindowController):
    """EWMH-backed window control over a live X connection (section 47).

    Args:
        timeout_ms: Default bounded wait for :meth:`ensure_active`.
        sleep: Injectable sleep so tests never really wait.
    """

    def __init__(
        self,
        *,
        timeout_ms: int = DEFAULT_ENSURE_WINDOW_TIMEOUT_MS,
        sleep: Any = time.sleep,
    ) -> None:
        self._timeout_ms = timeout_ms
        self._sleep = sleep
        self._display: Any = None
        self._atoms: dict[str, int] = {}

    # -- Connection -----------------------------------------------------------

    def _connection(self) -> Any:
        """Return a live X display connection, opening one on first use.

        Raises:
            BlaxcyError: ``BACKEND_UNAVAILABLE`` when python-xlib is missing or
                no display can be opened. That is the honest verdict -- there is
                no degraded "pretend the window is active" path.
        """
        if self._display is not None:
            return self._display
        try:
            from Xlib import display as xdisplay
        except Exception as exc:  # pragma: no cover - depends on host packages
            raise BlaxcyError(
                ErrorCode.BACKEND_UNAVAILABLE,
                "python-xlib is not importable, so window control is unavailable",
                details={"error": repr(exc)},
            ) from exc
        try:
            self._display = xdisplay.Display()
        except Exception as exc:
            raise BlaxcyError(
                ErrorCode.BACKEND_UNAVAILABLE,
                "could not open an X display connection for window control",
                details={"error": repr(exc)},
            ) from exc
        return self._display

    def _atom(self, name: str) -> int:
        """Intern (and cache) an atom on the current connection."""
        if name not in self._atoms:
            self._atoms[name] = int(self._connection().intern_atom(name))
        return self._atoms[name]

    def close(self) -> None:
        """Close the X connection, if one is open."""
        display = self._display
        self._display = None
        self._atoms.clear()
        if display is not None:
            # Closing a connection that is already torn down is not an error
            # worth propagating during shutdown.
            with contextlib.suppress(Exception):
                display.close()

    # -- Probing --------------------------------------------------------------

    def probe(self) -> WindowManagerProbe:
        """Functionally probe window control by reading the active window.

        The probe performs no window change: it opens a connection, interns the
        EWMH atoms it needs and reads ``_NET_ACTIVE_WINDOW``. A window manager
        that does not publish that property yields DEGRADED evidence rather than
        a fabricated AVAILABLE.
        """
        try:
            display = self._connection()
            root = display.screen().root
            root.query_tree()
            active = self.active_window()
        except BlaxcyError as exc:
            return WindowManagerProbe(available=False, reason=exc.message, details=exc.details)
        except Exception as exc:
            return WindowManagerProbe(available=False, reason=f"window probe failed: {exc!r}")
        return WindowManagerProbe(
            available=True,
            backend="python-xlib",
            details={"active_window": active, "ewmh": active is not None},
        )

    # -- Reading --------------------------------------------------------------

    def active_window(self) -> int | None:
        """Return the active window id from ``_NET_ACTIVE_WINDOW``, or ``None``."""
        try:
            display = self._connection()
            root = display.screen().root
            prop = root.get_full_property(self._atom(_ATOM_ACTIVE_WINDOW), 0)
        except BlaxcyError:
            raise
        except Exception:
            return None
        if prop is None or not prop.value:
            return None
        value = int(prop.value[0])
        return value or None

    def stacked_windows(self) -> tuple[WindowInfo, ...]:
        """The client windows in stacking order, bottom to top (section 46).

        The occlusion rule is only meaningful relative to a target's own window: a
        window *below* the target does not hide it, and the desktop background is
        below everything. EWMH publishes exactly that order in
        ``_NET_CLIENT_LIST_STACKING``, so it is read from the window manager rather
        than inferred from geometry -- geometry cannot tell behind from in front.

        Returns:
            The stacked windows with their titles and pids. An **empty** tuple means
            the order is *unknown* (the window manager publishes no stacking list,
            or the reading failed), which callers must treat conservatively -- never
            as "nothing is above anything".
        """
        try:
            display = self._connection()
            root = display.screen().root
            prop = root.get_full_property(self._atom(_ATOM_CLIENT_LIST_STACKING), 0)
        except Exception:
            return ()
        if prop is None or not prop.value:
            return ()
        windows: list[WindowInfo] = []
        for raw in prop.value:
            info = self.window_info(int(raw))
            if info is not None:
                windows.append(info)
        return tuple(windows)

    def list_windows(self) -> tuple[WindowInfo, ...]:
        """Return the client windows from ``_NET_CLIENT_LIST``."""
        display = self._connection()
        root = display.screen().root
        prop = root.get_full_property(self._atom(_ATOM_CLIENT_LIST), 0)
        if prop is None or not prop.value:
            return ()
        windows: list[WindowInfo] = []
        for raw in prop.value:
            info = self.window_info(int(raw))
            if info is not None:
                windows.append(info)
        return tuple(windows)

    def window_info(self, window_id: int) -> WindowInfo | None:
        """Return metadata for ``window_id``, or ``None`` when it no longer exists."""
        display = self._connection()
        try:
            window = display.create_resource_object("window", int(window_id))
            title = self._read_title(window)
            wm_class, wm_instance = self._read_class(window)
            pid = self._read_pid(window)
        except Exception:
            return None
        return WindowInfo(
            window_id=int(window_id),
            title=title,
            wm_class=wm_class,
            wm_instance=wm_instance,
            pid=pid,
        )

    # -- Activation -----------------------------------------------------------

    def activate(self, window_id: int) -> bool:
        """Send an EWMH activation request for ``window_id``.

        Returns whether the request was *sent*; it deliberately makes no claim
        that it worked. Use :meth:`ensure_active` when the result matters.
        """
        display = self._connection()
        try:
            from Xlib import X, protocol
        except Exception as exc:  # pragma: no cover - depends on host packages
            raise BlaxcyError(
                ErrorCode.BACKEND_UNAVAILABLE,
                "python-xlib protocol helpers are unavailable",
                details={"error": repr(exc)},
            ) from exc

        root = display.screen().root
        event = protocol.event.ClientMessage(
            window=int(window_id),
            client_type=self._atom(_ATOM_ACTIVE_WINDOW),
            # data[0] = 2 means "source: pager/tool", data[1] = timestamp.
            data=(32, [2, X.CurrentTime, 0, 0, 0]),
        )
        mask = X.SubstructureRedirectMask | X.SubstructureNotifyMask
        try:
            root.send_event(event, event_mask=mask)
            display.flush()
        except Exception as exc:
            raise BlaxcyError(
                ErrorCode.BACKEND_UNAVAILABLE,
                f"could not request window activation: {exc!r}",
                details={"window_id": int(window_id)},
            ) from exc
        return True

    def ensure_active(self, window_id: int, *, timeout_ms: int | None = None) -> bool:
        """Activate ``window_id`` and confirm it, waiting at most ``timeout_ms``.

        Returns:
            ``True`` only when the window manager's own active-window property
            reports ``window_id``. A request that was sent but did not take
            effect returns ``False`` -- never an assumed success (section 47).
        """
        budget_ms = self._timeout_ms if timeout_ms is None else timeout_ms
        if self.active_window() == window_id:
            return True
        if not self.activate(window_id):
            return False
        deadline_s = budget_ms / 1000.0
        waited = 0.0
        while waited < deadline_s:
            self._sleep(_POLL_INTERVAL_SECONDS)
            waited += _POLL_INTERVAL_SECONDS
            if self.active_window() == window_id:
                return True
        return False

    # -- Internals ------------------------------------------------------------

    def _read_title(self, window: Any) -> str | None:
        """Read the UTF-8 window title, falling back to the legacy WM_NAME."""
        with contextlib.suppress(Exception):
            prop = window.get_full_property(self._atom(_ATOM_WM_NAME), 0)
            if prop is not None and prop.value:
                raw = prop.value
                if isinstance(raw, bytes):
                    return raw.decode("utf-8", errors="replace")
                return str(raw)
        try:
            name = window.get_wm_name()
        except Exception:
            return None
        return str(name) if name else None

    def _read_class(self, window: Any) -> tuple[str | None, str | None]:
        """Read ``WM_CLASS`` as ``(class, instance)``; either may be ``None``."""
        try:
            wm_class = window.get_wm_class()
        except Exception:
            return None, None
        if not wm_class:
            return None, None
        parts = list(wm_class)
        if len(parts) >= 2:
            return str(parts[1]), str(parts[0])
        return str(parts[0]), None

    def _read_pid(self, window: Any) -> int | None:
        """Read ``_NET_WM_PID`` when the window publishes it."""
        try:
            prop = window.get_full_property(self._atom(_ATOM_WM_PID), 0)
        except Exception:
            return None
        if prop is None or not prop.value:
            return None
        return int(prop.value[0])
