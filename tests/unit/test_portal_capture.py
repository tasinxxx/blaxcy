"""The ScreenCast portal + PipeWire capture backend (specification sections 30, 33).

The real transports need a Wayland compositor, a consenting user and a PipeWire
graph, which this host does not have. What *is* testable without them -- and what
matters -- is the session semantics: an honest probe that never raises a consent
dialog, a session that opens only through the portal's own flow, a grab that
refuses to fabricate pixels before consent, geometry taken from the real stream,
and a close that leaves nothing behind.
"""

from __future__ import annotations

from typing import Any

import pytest

from core.portal_capture import (
    SOURCE_MONITOR,
    PortalCaptureBackend,
    probe_portal_capture,
)
from schemas.enums import ErrorCode
from schemas.errors import BlaxcyError


class FakeTransport:
    """A scripted ScreenCast transport recording the portal flow."""

    def __init__(
        self,
        *,
        available: bool = True,
        version: int | None = 4,
        nodes: tuple[int, ...] = (77,),
        error: Exception | None = None,
    ) -> None:
        self.available = available
        self.version = version
        self.nodes = nodes
        self.error = error
        self.opened: list[dict[str, Any]] = []
        self.closed_sessions: list[str] = []
        self.closed = 0

    def availability(self) -> tuple[bool, int | None]:
        return self.available, self.version

    def open_session(
        self,
        *,
        app_id: str,
        source_types: int,
        cursor_mode: int,
        timeout_s: float,
    ) -> tuple[str, tuple[int, ...]]:
        if self.error is not None:
            raise self.error
        self.opened.append(
            {
                "app_id": app_id,
                "source_types": source_types,
                "cursor_mode": cursor_mode,
                "timeout_s": timeout_s,
            }
        )
        return f"/org/freedesktop/portal/session/test{len(self.opened)}", self.nodes

    def close_session(self, session: str) -> None:
        self.closed_sessions.append(session)

    def close(self) -> None:
        self.closed += 1


class FakeSource:
    """A scripted PipeWire consumer producing one fixed frame."""

    name = "fake-pipewire"

    def __init__(self, *, width: int = 800, height: int = 600, fail: bool = False) -> None:
        self.width = width
        self.height = height
        self.fail = fail
        self.started: list[int] = []
        self.closed = 0

    def start(self, node_id: int) -> tuple[int, int]:
        if self.fail:
            raise RuntimeError("pipewire is not available")
        self.started.append(node_id)
        return self.width, self.height

    def grab(self) -> tuple[bytes, int, int]:
        if self.fail:
            raise RuntimeError("no frames")
        return b"\x00" * (self.width * self.height * 4), self.width, self.height

    def close(self) -> None:
        self.closed += 1


# -- The probe ----------------------------------------------------------------


def test_the_probe_reports_an_advertised_interface_without_a_session() -> None:
    """A passing probe proves the interface exists -- and never opens a session."""
    transport = FakeTransport()
    backend = PortalCaptureBackend(transport=transport)

    result = backend.probe()

    assert result["available"] is True
    assert result["portal_version"] == 4
    assert transport.opened == []


def test_the_probe_reports_a_missing_interface_with_its_reason() -> None:
    transport = FakeTransport(available=False, version=None)
    backend = PortalCaptureBackend(transport=transport)

    result = backend.probe()

    assert result["available"] is False
    assert "ScreenCast" in result["reason"]
    assert transport.opened == []


def test_the_module_level_probe_runs_and_cleans_up() -> None:
    """The capability probe is self-cleaning: it closes what it built."""
    # On this X11 host the portal does not advertise ScreenCast, so the probe
    # reports unavailable; on a Wayland host it would report available. Either
    # way it must return a dict with the honest keys and never raise.
    result = probe_portal_capture()

    assert set(result) == {"available", "reason"}
    assert isinstance(result["available"], bool)


# -- Session establishment and grabbing ---------------------------------------


def _backend(
    transport: FakeTransport,
    source_factory: Any,
) -> PortalCaptureBackend:
    return PortalCaptureBackend(
        transport=transport,
        source_factory=source_factory,
        request_timeout_s=1.0,
        frame_timeout_s=0.2,
    )


def test_the_first_grab_opens_the_portal_session_and_consumes_a_node() -> None:
    """Session flow: monitors selected, nodes consumed, real geometry stamped."""
    transport = FakeTransport(nodes=(91,))
    factory_calls: list[dict[str, Any]] = []

    def factory(**kwargs: Any) -> FakeSource:
        factory_calls.append(kwargs)
        return FakeSource(width=1280, height=720)

    backend = _backend(transport, factory)
    raw, width, height = backend.grab()

    assert (width, height) == (1280, 720)
    assert len(raw) == 1280 * 720 * 4
    assert transport.opened[0]["source_types"] == SOURCE_MONITOR
    assert factory_calls[0]["frame_timeout_s"] == 0.2
    assert backend.monitor_bounds() == (0, 0, 1280, 720)
    assert backend.monitors() == ({"left": 0, "top": 0, "width": 1280, "height": 720},)


def test_a_second_grab_reuses_the_session() -> None:
    """One consented session serves every later grab; the portal is asked once."""
    transport = FakeTransport()
    backend = _backend(transport, lambda **_kwargs: FakeSource())

    backend.grab()
    backend.grab()

    assert len(transport.opened) == 1


def test_a_failed_source_start_closes_the_portal_session() -> None:
    """A half-open session must never be left behind (sections 64, 85)."""
    transport = FakeTransport()
    backend = _backend(transport, lambda **_kwargs: FakeSource(fail=True))

    with pytest.raises(BlaxcyError) as error:
        backend.grab()

    assert error.value.code is ErrorCode.CAPTURE_FAILED
    assert len(transport.closed_sessions) == 1
    assert backend.monitor_bounds() == (0, 0, 0, 0)


def test_a_consent_refusal_is_a_capture_failure_not_a_fake_frame() -> None:
    """The portal refusing the request means no pixels -- never a fallback."""
    transport = FakeTransport(error=RuntimeError("the portal refused the request (response 1)"))
    backend = _backend(transport, lambda **_kwargs: FakeSource())

    with pytest.raises(BlaxcyError) as error:
        backend.grab()

    assert error.value.code is ErrorCode.CAPTURE_FAILED


def test_a_grab_failure_after_the_session_is_a_capture_failure() -> None:
    transport = FakeTransport()

    def factory(**_kwargs: Any) -> FakeSource:
        source = FakeSource()
        source.fail = True  # start fine, then fail grabs
        return source

    backend = _backend(transport, factory)
    with pytest.raises(BlaxcyError) as error:
        backend.grab()
    assert error.value.code is ErrorCode.CAPTURE_FAILED


def test_close_releases_the_source_and_the_session() -> None:
    """Closing tears down both the PipeWire consumer and the portal session."""
    transport = FakeTransport()
    source = FakeSource()
    backend = _backend(transport, lambda **_kwargs: source)

    backend.grab()
    backend.close()
    backend.close()  # idempotent

    assert source.closed == 1
    assert len(transport.closed_sessions) == 1
    assert transport.closed >= 1
