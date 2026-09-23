"""Frame engine: bounded capture, adaptive profiles, honest failure (section 33).

These tests drive the engine through an injected fake backend so they exercise
the real engine logic (buffer bounds, profile switching, error/reinitialize
accounting) without depending on a live display. A separate integration test
covers real mss capture.
"""

from __future__ import annotations

import numpy as np
import pytest

from config.settings import CaptureSettings
from core.frame_engine import (
    CAPTURE_BACKEND_NAME,
    MAX_REINITIALIZE_FAILURES,
    CaptureProfile,
    Frame,
    FrameEngine,
)
from schemas.enums import CoordinateSpace, ErrorCode
from schemas.errors import BlaxcyError

_THUMB = (32, 18)


class FakeBackend:
    """A capture backend that returns synthetic solid frames."""

    def __init__(
        self,
        *,
        width: int = 64,
        height: int = 36,
        fail_grab: bool = False,
        raw_override: bytes | None = None,
    ) -> None:
        self.width = width
        self.height = height
        self.fail_grab = fail_grab
        self.raw_override = raw_override
        self.grabs = 0
        self.closed = False

    def grab(self) -> tuple[bytes, int, int]:
        if self.fail_grab:
            raise RuntimeError("synthetic grab failure")
        self.grabs += 1
        value = self.grabs % 256
        raw = self.raw_override
        if raw is None:
            raw = bytes([value]) * (self.width * self.height * 4)
        return raw, self.width, self.height

    def monitor_bounds(self) -> tuple[int, int, int, int]:
        return (10, 20, self.width, self.height)

    def monitors(self) -> tuple[dict[str, int], ...]:
        return ({"left": 10, "top": 20, "width": self.width, "height": self.height},)

    def close(self) -> None:
        self.closed = True


class ScriptedFactory:
    """Creates backends in order, then raises once the script is exhausted."""

    def __init__(self, backends: list[FakeBackend]) -> None:
        self._backends = list(backends)
        self.created: list[FakeBackend] = []

    def __call__(self) -> FakeBackend:
        if not self._backends:
            raise RuntimeError("synthetic backend init failure")
        backend = self._backends.pop(0)
        self.created.append(backend)
        return backend


def _engine(factory: ScriptedFactory, **overrides: int) -> FrameEngine:
    """Build an engine with a small thumbnail and injected backend factory."""
    settings = CaptureSettings(**overrides)
    return FrameEngine(
        settings,
        backend_factory=factory,
        thumbnail_size=_THUMB,
    )


def test_grab_produces_bgra_frame_with_grayscale_thumbnail() -> None:
    """Section 33: full frame is BGRA uint8; thumbnail is 480x270 grayscale."""
    factory = ScriptedFactory([FakeBackend()])
    frame = _engine(factory).grab()

    assert isinstance(frame, Frame)
    assert frame.image.shape == (36, 64, 4)
    assert frame.image.dtype == np.uint8
    assert frame.thumbnail.shape == (_THUMB[1], _THUMB[0])
    assert frame.thumbnail.dtype == np.uint8
    assert frame.thumbnail.ndim == 2
    assert frame.backend == CAPTURE_BACKEND_NAME


def test_frame_ids_advance_and_desktop_rect_is_desktop_space() -> None:
    """Frames are individually identified; the captured region is DESKTOP space."""
    factory = ScriptedFactory([FakeBackend()])
    engine = _engine(factory)
    first = engine.grab()
    second = engine.grab()

    assert second.frame_id == first.frame_id + 1
    assert first.desktop_rect.space is CoordinateSpace.DESKTOP
    assert (first.desktop_rect.x, first.desktop_rect.y) == (10.0, 20.0)
    assert (first.desktop_rect.width, first.desktop_rect.height) == (64.0, 36.0)


def test_monitor_topology_is_empty_until_the_backend_is_open() -> None:
    """Topology is evidence from a live backend, never a guess."""
    engine = _engine(ScriptedFactory([FakeBackend()]))
    # Read into locals on purpose: comparing the *property* against the empty
    # tuple makes static analysis narrow the member expression for the rest of
    # the test, and the populated comparison below then looks non-overlapping.
    before = engine.monitor_topology
    assert before == ()
    engine.grab()
    after = engine.monitor_topology
    assert after == ({"left": 10, "top": 20, "width": 64, "height": 36},)


def test_monitor_topology_reports_unknown_when_the_backend_fails() -> None:
    """A backend that cannot describe its monitors yields no topology."""

    class BrokenTopologyBackend(FakeBackend):
        def monitors(self) -> tuple[dict[str, int], ...]:
            raise RuntimeError("topology unavailable")

    engine = _engine(ScriptedFactory([BrokenTopologyBackend()]))
    engine.grab()
    assert engine.monitor_topology == ()


def test_memory_is_bounded_to_two_frames_and_eight_thumbnails() -> None:
    """Section 33: no unbounded image queue -- 2 full frames, 8 thumbnails."""
    factory = ScriptedFactory([FakeBackend()])
    engine = _engine(factory, max_full_frames=2, max_thumbnails=8)
    for _ in range(12):
        engine.grab()

    assert len(engine.full_frames) == 2
    assert len(engine.thumbnails) == 8
    # The retained frames are the newest ones, not the oldest.
    assert engine.full_frames[-1].frame_id == 11


def test_profiles_map_to_the_configured_target_intervals() -> None:
    """Section 33 adaptive targets: idle 2 FPS, normal 10, active 30."""
    engine = _engine(ScriptedFactory([FakeBackend()]))
    engine.set_profile(CaptureProfile.IDLE)
    assert engine.target_interval_ms == pytest.approx(500.0)
    engine.set_profile(CaptureProfile.NORMAL)
    assert engine.target_interval_ms == pytest.approx(100.0)
    engine.set_profile(CaptureProfile.ACTIVE)
    assert engine.target_interval_ms == pytest.approx(1000.0 / 30.0)


def _profile_of(engine: FrameEngine) -> CaptureProfile:
    """Read the current profile through a function boundary.

    Going through a call keeps mypy from carrying the in-block ``assert``
    narrowing of ``engine.profile`` past the context manager.
    """
    return engine.profile


def test_active_window_restores_the_previous_profile() -> None:
    """Section 33.1: the boost applies only for the task's duration."""
    engine = _engine(ScriptedFactory([FakeBackend()]))
    engine.set_profile(CaptureProfile.NORMAL)
    with engine.active_window():
        assert _profile_of(engine) is CaptureProfile.ACTIVE
    assert _profile_of(engine) is CaptureProfile.NORMAL


def test_active_window_restores_profile_even_on_failure() -> None:
    """A task that raises must not leave capture stuck at the active rate."""
    engine = _engine(ScriptedFactory([FakeBackend()]))
    with pytest.raises(RuntimeError), engine.active_window():
        raise RuntimeError("task aborted")
    assert engine.profile is CaptureProfile.IDLE


def test_generation_bump_is_monotonic_and_stamped_on_frames() -> None:
    """Section 32: a new generation invalidates older leases."""
    engine = _engine(ScriptedFactory([FakeBackend()]))
    assert engine.generation == 0
    assert engine.grab().generation == 0
    assert engine.bump_generation() == 1
    assert engine.grab().generation == 1


def test_three_consecutive_errors_reinitialize_the_backend() -> None:
    """Section 33: three consecutive capture errors -> backend reinitialization."""
    factory = ScriptedFactory([FakeBackend(fail_grab=True), FakeBackend(fail_grab=True)])
    engine = _engine(factory)

    for _ in range(3):
        with pytest.raises(BlaxcyError) as excinfo:
            engine.grab()
        assert excinfo.value.code is ErrorCode.CAPTURE_FAILED

    # The original backend was replaced after the third consecutive failure.
    assert len(factory.created) == 2
    assert engine.stats()["consecutive_errors"] == 0
    assert engine.input_disarmed is False


def test_successful_capture_resets_the_error_counter() -> None:
    """A single good frame clears the consecutive-error streak."""
    backend = FakeBackend()
    factory = ScriptedFactory([backend])
    engine = _engine(factory)
    backend.fail_grab = True
    with pytest.raises(BlaxcyError):
        engine.grab()
    assert engine.stats()["consecutive_errors"] == 1
    backend.fail_grab = False
    engine.grab()
    assert engine.stats()["consecutive_errors"] == 0
    assert engine.stats()["total_errors"] == 1


def test_repeated_reinitialization_failure_disarms_input() -> None:
    """Section 33: repeated reinit failure -> CAPTURE_FAILED and input disarmed."""
    factory = ScriptedFactory([FakeBackend(fail_grab=True)])
    engine = _engine(factory)

    # Three errors trigger the first (failing) reinitialization; repeat until
    # MAX_REINITIALIZE_FAILURES consecutive reinitializations have failed.
    # 3 errors per streak x 3 failed reinitializations.
    for _ in range(3 * MAX_REINITIALIZE_FAILURES):
        with pytest.raises(BlaxcyError):
            engine.grab()

    assert engine.capture_failed is True
    assert engine.input_disarmed is True
    # Once disarmed, later grabs fail fast without touching a backend.
    with pytest.raises(BlaxcyError) as excinfo:
        engine.grab()
    assert "disarmed" in excinfo.value.message


def test_malformed_raw_buffer_is_a_capture_failure_not_a_crash() -> None:
    """A backend returning the wrong byte count is reported honestly."""
    backend = FakeBackend(raw_override=b"too-short")
    engine = _engine(ScriptedFactory([backend]))
    with pytest.raises(BlaxcyError) as excinfo:
        engine.grab()
    assert excinfo.value.code is ErrorCode.CAPTURE_FAILED
    assert engine.stats()["consecutive_errors"] == 1


def test_open_failure_is_reported_as_capture_failed() -> None:
    """A backend that cannot be created does not fabricate a frame."""
    engine = _engine(ScriptedFactory([]))
    with pytest.raises(BlaxcyError) as excinfo:
        engine.grab()
    assert excinfo.value.code is ErrorCode.CAPTURE_FAILED
    assert engine.capture_failed is True


def test_measured_fps_is_none_until_two_captures_exist() -> None:
    """Reports a measurement, never a promise (sections 23, 76)."""
    engine = _engine(ScriptedFactory([FakeBackend()]))
    assert engine.stats()["measured_fps"] is None
    engine.grab()
    assert engine.stats()["measured_fps"] is None
    engine.grab()
    fps = engine.measured_fps
    assert fps is not None
    assert fps > 0.0


def test_close_is_idempotent_and_releases_the_backend() -> None:
    """Closing twice is safe and the backend is actually closed."""
    backend = FakeBackend()
    engine = _engine(ScriptedFactory([backend]))
    engine.open()
    engine.close()
    engine.close()
    assert backend.closed is True
