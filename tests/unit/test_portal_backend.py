"""The XDG RemoteDesktop portal input backend (specification section 30).

The real portal cannot be exercised on an X11 host (the interface is absent), and
starting a session on a Wayland host presents a consent dialog. The backend is
therefore tested against a fake transport: it proves the exact protocol calls,
the held-input bookkeeping and the honest refusals, which is what the backend
owns. The D-Bus transport itself is the thin, untested seam.
"""

from __future__ import annotations

from typing import Any

import pytest

from control.backends.portal import (
    DEVICE_KEYBOARD,
    DEVICE_POINTER,
    PortalRemoteDesktopBackend,
)
from schemas.enums import ErrorCode
from schemas.errors import BlaxcyError


class FakePortalTransport:
    """A recording stand-in for the D-Bus transport."""

    def __init__(
        self,
        *,
        present: bool = True,
        version: int | None = 2,
        session: str = "/session/1",
        fail_open: bool = False,
    ) -> None:
        """Create the fake with a fixed availability and session answer."""
        self.present = present
        self.version = version
        self.session = session
        self.fail_open = fail_open
        self.calls: list[tuple[str, str, tuple[Any, ...]]] = []
        self.opened = 0
        self.open_args: dict[str, Any] = {}
        self.closed_sessions: list[str] = []
        self.closed = False

    def availability(self) -> tuple[bool, int | None]:
        return self.present, self.version if self.present else None

    def open_session(
        self, *, app_id: str, device_types: int, stream_node: int | None, timeout_s: float
    ) -> str:
        self.opened += 1
        self.open_args = {
            "app_id": app_id,
            "device_types": device_types,
            "stream_node": stream_node,
            "timeout_s": timeout_s,
        }
        if self.fail_open:
            raise RuntimeError("the user declined the session")
        return self.session

    def notify(self, method: str, session: str, *args: Any) -> None:
        self.calls.append((method, session, args))

    def close_session(self, session: str) -> None:
        self.closed_sessions.append(session)

    def close(self) -> None:
        self.closed = True


def _backend(transport: FakePortalTransport, **kwargs: Any) -> PortalRemoteDesktopBackend:
    """A backend wired to ``transport``."""
    return PortalRemoteDesktopBackend(transport_factory=lambda: transport, **kwargs)


# -- Probe --------------------------------------------------------------------


def test_probe_reports_the_live_interface_with_its_version() -> None:
    """A present interface is reported with its version and the pointer caveat."""
    transport = FakePortalTransport(present=True, version=2)
    probe = _backend(transport).probe()

    assert probe.available is True
    assert probe.details["interface_version"] == 2
    assert probe.details["absolute_pointer"] is False
    assert probe.details["device_types"] == DEVICE_KEYBOARD | DEVICE_POINTER


def test_probe_is_unavailable_when_the_interface_is_absent() -> None:
    """A portal without RemoteDesktop is reported, never guessed at."""
    probe = _backend(FakePortalTransport(present=False)).probe()

    assert probe.available is False
    assert probe.reason is not None and "RemoteDesktop" in probe.reason


def test_probe_is_unavailable_when_the_transport_cannot_be_built() -> None:
    """A missing bus/dbus module is a capability verdict, not a crash (section 28)."""

    def _refuse() -> Any:
        raise RuntimeError("no session bus")

    probe = PortalRemoteDesktopBackend(transport_factory=_refuse).probe()
    assert probe.available is False
    assert probe.reason is not None and "transport is unavailable" in probe.reason


# -- Keyboard -----------------------------------------------------------------


def test_keyboard_injection_uses_keysyms_and_tracks_held_keys() -> None:
    """Typing is injected by keysym, and every press is tracked for cleanup."""
    transport = FakePortalTransport()
    backend = _backend(transport)

    assert backend.resolve_key(ord("a")) is not None
    resolution = backend.resolve_key(ord("A"))
    assert resolution is not None
    assert resolution.keycode == ord("A")
    assert resolution.shift is False  # the portal handles the shifted keysym directly
    assert backend.resolve_key(0) is None

    backend.key_press(resolution.keycode)
    backend.key_release(resolution.keycode)

    assert transport.calls == [
        ("NotifyKeyboardKeysym", "/session/1", (ord("A"), 1)),
        ("NotifyKeyboardKeysym", "/session/1", (ord("A"), 0)),
    ]
    assert backend.held_keys == ()


def test_a_held_key_is_released_and_cleared_by_release_all() -> None:
    """Section 52/63: a latched stop releases whatever BLAXCY is holding."""
    transport = FakePortalTransport()
    backend = _backend(transport)
    backend.key_press(65)
    backend.key_press(66)
    assert list(backend.held_keys) == [65, 66]

    backend.release_all()

    assert not backend.held_keys
    assert transport.calls[-2:] == [
        ("NotifyKeyboardKeysym", "/session/1", (66, 0)),
        ("NotifyKeyboardKeysym", "/session/1", (65, 0)),
    ]


# -- Pointer ------------------------------------------------------------------


def test_absolute_pointer_motion_uses_the_screencast_stream_node() -> None:
    """With a stream node the portal can place the pointer absolutely."""
    transport = FakePortalTransport()
    backend = _backend(transport, stream_node=42)

    backend.move_pointer(300, 200)
    backend.move_pointer(310, 220)

    assert transport.calls == [
        ("NotifyPointerMotionAbsolute", "/session/1", (42, 300.0, 200.0)),
        ("NotifyPointerMotionAbsolute", "/session/1", (42, 310.0, 220.0)),
    ]
    assert backend.get_pointer_position() == (310, 220)


def test_pointer_moves_by_delta_when_a_position_is_known_but_no_stream_exists() -> None:
    """Without a stream, a known position lets the portal move by delta."""
    transport = FakePortalTransport()
    backend = _backend(transport, initial_position=(100, 100))

    backend.move_pointer(130, 90)
    backend.move_pointer(130, 90)  # no movement -> no D-Bus call

    assert transport.calls == [
        ("NotifyPointerMotion", "/session/1", (30.0, -10.0)),
    ]
    assert backend.get_pointer_position() == (130, 90)


def test_pointer_refuses_an_absolute_move_it_cannot_express() -> None:
    """No readback and no stream is an honest refusal, never a guessed delta."""
    backend = _backend(FakePortalTransport())

    with pytest.raises(BlaxcyError) as excinfo:
        backend.move_pointer(10, 10)

    assert excinfo.value.code is ErrorCode.BACKEND_UNAVAILABLE
    assert "stream node" in excinfo.value.message


def test_buttons_and_scroll_map_onto_the_portal_calls() -> None:
    """Buttons use evdev codes and scroll uses the discrete axis method."""
    transport = FakePortalTransport()
    backend = _backend(transport)

    backend.press_button(_left())
    backend.release_button(_left())
    backend.scroll(vertical=1, horizontal=-2)

    assert transport.calls == [
        ("NotifyPointerButton", "/session/1", (0x110, 1)),
        ("NotifyPointerButton", "/session/1", (0x110, 0)),
        ("NotifyPointerAxisDiscrete", "/session/1", (0, 1)),
        ("NotifyPointerAxisDiscrete", "/session/1", (1, -2)),
    ]
    assert not backend.held_buttons


# -- Session lifecycle --------------------------------------------------------


def test_the_session_is_opened_once_and_reused() -> None:
    """A consent prompt must not appear per action; the session is reused."""
    transport = FakePortalTransport()
    backend = _backend(transport)

    backend.key_press(65)
    backend.key_release(65)
    backend.press_button(_left())
    backend.release_button(_left())

    assert transport.opened == 1
    assert transport.open_args["device_types"] == DEVICE_KEYBOARD | DEVICE_POINTER


def test_a_refused_session_surfaces_as_backend_unavailable() -> None:
    """If the user declines, injection fails loudly rather than silently."""
    backend = _backend(FakePortalTransport(fail_open=True))

    with pytest.raises(BlaxcyError) as excinfo:
        backend.key_press(65)

    assert excinfo.value.code is ErrorCode.BACKEND_UNAVAILABLE
    assert "session" in excinfo.value.message


def test_close_closes_the_session_and_the_transport() -> None:
    """Teardown releases the session and the transport it created."""
    transport = FakePortalTransport()
    backend = _backend(transport)
    backend.key_press(65)

    backend.close()

    assert transport.closed_sessions == ["/session/1"]
    assert transport.closed is True
    assert not backend.held_keys


def test_release_all_is_safe_after_the_transport_is_gone() -> None:
    """A dead transport must not leave BLAXCY believing it holds input."""

    class _Broken(FakePortalTransport):
        broken = False

        def notify(self, method: str, session: str, *args: Any) -> None:
            if self.broken:
                raise RuntimeError("the compositor went away")
            super().notify(method, session, *args)

    transport = _Broken()
    backend = _backend(transport)
    backend.key_press(65)
    backend.press_button(_left())
    transport.broken = True

    backend.release_all()

    assert not backend.held_keys
    assert not backend.held_buttons


def _left() -> Any:
    """The LEFT pointer button, imported here to keep the test body short."""
    from control.backends.base import PointerButton

    return PointerButton.LEFT
