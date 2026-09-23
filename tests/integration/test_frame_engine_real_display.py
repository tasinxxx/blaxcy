"""Real-display frame engine integration test (specification sections 33, 34).

This is an *integration* test: it captures the actual desktop through ``mss``
and runs the real change detector over real frames. It verifies that:

* a live grab produces a BGRA full frame and a 480x270 grayscale thumbnail,
* the frame's DESKTOP rectangle matches what the X server reports,
* the change detector consumes real frames without error and reports regions in
  DESKTOP space,
* the active-profile boost (section 33.1) leaves the bounded buffers bounded.

It asserts structural facts only -- never a specific change class -- because the
content of a live desktop is not deterministic. If capture is unavailable the
test skips with an honest reason instead of fabricating a result.
"""

from __future__ import annotations

import os
import time
from typing import Any

import numpy as np
import pytest

from config.settings import CaptureSettings, PerceptionSettings
from core.change_detector import ChangeDetector
from core.frame_engine import (
    THUMBNAIL_HEIGHT,
    THUMBNAIL_WIDTH,
    CaptureProfile,
    FrameEngine,
)
from schemas.enums import CoordinateSpace
from schemas.errors import BlaxcyError

pytestmark = pytest.mark.skipif(
    not os.environ.get("DISPLAY"),
    reason="real-display frame engine test requires an X display (DISPLAY unset)",
)


def _engine() -> FrameEngine:
    """A real frame engine, or skip when capture is unavailable."""
    engine = FrameEngine(CaptureSettings())
    try:
        engine.open()
    except BlaxcyError as exc:
        pytest.skip(f"live screen capture is unavailable: {exc.message}")
    return engine


def _root_geometry() -> tuple[int, int] | None:
    """The X root window size when Xlib can connect, else ``None``."""
    try:
        from Xlib import display
    except Exception:  # pragma: no cover - depends on the host
        return None
    try:
        connection = display.Display()
    except Exception:  # pragma: no cover - depends on the host
        return None
    try:
        geometry = connection.screen().root.get_geometry()
        return int(geometry.width), int(geometry.height)
    finally:
        connection.close()


def _profile_of(engine: FrameEngine) -> CaptureProfile:
    """Read the current profile through a function boundary (see unit tests)."""
    return engine.profile


def test_live_grab_produces_expected_geometry() -> None:
    """A real grab yields a BGRA frame and the canonical grayscale thumbnail."""
    with _engine() as engine:
        frame = engine.grab()

    assert frame.image.ndim == 3
    assert frame.image.shape[2] == 4
    assert frame.image.dtype == np.uint8
    assert frame.thumbnail.shape == (THUMBNAIL_HEIGHT, THUMBNAIL_WIDTH)
    assert frame.thumbnail.dtype == np.uint8
    assert frame.desktop_rect.space is CoordinateSpace.DESKTOP
    assert frame.desktop_rect.width > 0
    assert frame.desktop_rect.height > 0


def test_live_desktop_rect_matches_x_root_geometry() -> None:
    """The captured region must describe the display the X server reports."""
    root = _root_geometry()
    if root is None:
        pytest.skip("Xlib cannot connect to the display for a geometry cross-check")
    with _engine() as engine:
        frame = engine.grab()
    width, height = root
    assert frame.desktop_rect.width == width
    assert frame.desktop_rect.height == height


def test_change_detector_runs_on_live_frames() -> None:
    """Section 34: real frames flow through the detector without error."""
    with _engine() as engine:
        first = engine.grab()
        time.sleep(0.05)
        second = engine.grab()

    delta = ChangeDetector(PerceptionSettings()).compare(first, second)
    assert delta.from_frame_id == first.frame_id
    assert delta.to_frame_id == second.frame_id
    for region in delta.regions:
        assert region.rect.space is CoordinateSpace.DESKTOP


def test_active_profile_keeps_buffers_bounded() -> None:
    """Section 33/33.1: the boost does not break the bounded memory model."""
    settings = CaptureSettings()
    engine = _engine()
    try:
        engine.set_profile(CaptureProfile.NORMAL)
        with engine.active_window():
            assert engine.target_interval_ms == pytest.approx(1000.0 / settings.active_fps)
            for _ in range(10):
                engine.grab()
        assert _profile_of(engine) is CaptureProfile.NORMAL
    finally:
        engine.close()

    assert len(engine.full_frames) <= settings.max_full_frames
    assert len(engine.thumbnails) <= settings.max_thumbnails
    stats: dict[str, Any] = engine.stats()
    assert stats["total_captures"] == 10
    assert stats["last_latency_ms"] is not None
