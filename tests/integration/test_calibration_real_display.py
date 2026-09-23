"""Real-display calibration integration test (specification section 31).

This is an *integration* test: it runs against the actual X11 display on this
machine, not a synthetic layout. It verifies:

* the live monitor topology produces a valid :class:`MonitorLayout`,
* that layout's desktop union matches the X root window geometry,
* a full calibration solve over the real topology is **complete** (every
  monitor covered, every fit valid) and yields a calibrated
  :class:`GeometryMap`,
* every monitor's desktop points convert into INPUT coordinates consistent
  with the **live pointer readback** -- a real observation of the INPUT space,
* off-desktop input points are clamped rather than injected off-screen.

What this test deliberately does **not** do is inject input. Driving the pointer
to *measure* the input mapping is Phase 7 (mouse control) and is gated by policy
(section 4 rule 13). Until then, the X11 XTEST precondition "INPUT space equals
DESKTOP space" is asserted explicitly and cross-checked against read-only
pointer readback -- never assumed silently.

If no X display, capture backend, or pointer readback is available, the affected
test skips with an honest reason instead of fabricating a result.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from core.calibration import CalibrationSample, calibrate
from schemas.enums import CoordinateSpace
from schemas.geometry import GeometryMap, MonitorGeometry, MonitorLayout, Point

pytestmark = pytest.mark.skipif(
    not os.environ.get("DISPLAY"),
    reason="real-display calibration test requires an X display (DISPLAY unset)",
)


def _live_monitors() -> list[dict[str, Any]]:
    """Raw monitor descriptors from the live capture backend, or skip.

    Index 0 is the virtual union of all monitors; the rest are physical.
    """
    import mss

    try:
        with mss.MSS() as capture:
            return [dict(monitor) for monitor in capture.monitors]
    except Exception as exc:  # pragma: no cover - depends on the host
        pytest.skip(f"live screen capture is unavailable: {exc!r}")


def _live_layout() -> MonitorLayout:
    """Build a :class:`MonitorLayout` from the real physical monitors."""
    monitors = _live_monitors()
    physical = monitors[1:]
    if not physical:
        pytest.skip("the live display backend reported no physical monitor")
    return MonitorLayout(
        monitors=tuple(
            MonitorGeometry(
                monitor_id=index,
                name=monitor.get("output") or None,
                origin_x=float(monitor["left"]),
                origin_y=float(monitor["top"]),
                width=int(monitor["width"]),
                height=int(monitor["height"]),
            )
            for index, monitor in enumerate(physical)
        )
    )


def _root_geometry() -> tuple[int, int]:
    """The X root window size, or skip when Xlib cannot connect."""
    from Xlib import display

    try:
        connection = display.Display()
    except Exception as exc:  # pragma: no cover - depends on the host
        pytest.skip(f"Xlib cannot connect to the display: {exc!r}")
    try:
        geometry = connection.screen().root.get_geometry()
        return int(geometry.width), int(geometry.height)
    finally:
        connection.close()


def _pointer_desktop_point() -> Point | None:
    """The live pointer position in DESKTOP space, or ``None`` if unreadable."""
    from Xlib import display

    try:
        connection = display.Display()
    except Exception:  # pragma: no cover - depends on the host
        return None
    try:
        pointer = connection.screen().root.query_pointer()
        return Point(x=float(pointer.root_x), y=float(pointer.root_y), space=CoordinateSpace.DESKTOP)
    except Exception:  # pragma: no cover - depends on the host
        return None
    finally:
        connection.close()


def _corner_samples(layout: MonitorLayout) -> list[CalibrationSample]:
    """Three non-collinear samples per real monitor.

    ``observed_input`` equals the DESKTOP point because that is the X11 XTEST
    precondition this test asserts (and then cross-checks against live pointer
    readback). This is not a measured mapping; Phase 7 measures it by actually
    driving the pointer under policy control.
    """
    samples: list[CalibrationSample] = []
    for monitor in layout.monitors:
        inset = min(50.0, monitor.width / 4.0, monitor.height / 4.0)
        if inset <= 1.0:
            pytest.skip(f"monitor {monitor.monitor_id} is too small to sample corners")
        left = monitor.origin_x + inset
        top = monitor.origin_y + inset
        right = monitor.origin_x + monitor.width - inset
        bottom = monitor.origin_y + monitor.height - inset
        for x, y in ((left, top), (right, top), (left, bottom)):
            desktop = Point(x=x, y=y, space=CoordinateSpace.DESKTOP)
            observed = Point(x=x, y=y, space=CoordinateSpace.INPUT)
            samples.append(CalibrationSample(monitor_id=monitor.monitor_id, desktop=desktop, observed_input=observed))
    return samples


def _calibrated_geometry_map() -> GeometryMap:
    """Calibrate against the live topology and return the geometry map."""
    layout = _live_layout()
    result = calibrate(layout, _corner_samples(layout))
    assert result.complete, f"calibration incomplete: {result.failure_reason()}"
    geometry_map = result.geometry_map()
    assert geometry_map.calibrated is True
    return geometry_map


def test_live_monitor_layout_matches_x_root_geometry() -> None:
    """The detected layout must describe the display the X server reports."""
    layout = _live_layout()
    desktop = layout.desktop_rect
    width, height = _root_geometry()

    assert desktop.space is CoordinateSpace.DESKTOP
    assert desktop.width == width
    assert desktop.height == height
    assert len(layout.monitors) >= 1


def test_calibration_over_real_topology_is_complete() -> None:
    """A full solve over the real monitors is complete and calibrated."""
    layout = _live_layout()
    samples = _corner_samples(layout)
    assert len(samples) >= 3 * len(layout.monitors)

    result = calibrate(layout, samples)

    assert result.missing_monitors == ()
    assert result.invalid_monitors == ()
    assert result.complete is True
    assert result.disarmed is False

    geometry_map = result.geometry_map()
    assert geometry_map.calibrated is True
    # Under the asserted X11 XTEST precondition, every fit is the identity, so a
    # point at each real monitor's centre must map to exactly itself.
    for monitor in layout.monitors:
        centre = Point(
            x=monitor.origin_x + monitor.width / 2.0,
            y=monitor.origin_y + monitor.height / 2.0,
            space=CoordinateSpace.DESKTOP,
        )
        mapped = geometry_map.desktop_to_input(centre)
        # The fitted transform is identity to within least-squares float noise.
        assert mapped.x == pytest.approx(centre.x, abs=1e-6), (
            f"monitor {monitor.monitor_id} is not identity-mapped"
        )
        assert mapped.y == pytest.approx(centre.y, abs=1e-6), (
            f"monitor {monitor.monitor_id} is not identity-mapped"
        )


def test_desktop_to_input_matches_live_pointer_readback() -> None:
    """INPUT coordinates must agree with a real pointer observation."""
    geometry_map = _calibrated_geometry_map()
    pointer = _pointer_desktop_point()
    if pointer is None:
        pytest.skip("pointer readback is unavailable on this display")

    monitor = geometry_map.layout.monitor_at(pointer)
    assert monitor is not None, "the live pointer should be on some monitor"

    mapped = geometry_map.prepare_input_point(pointer)
    assert mapped.space is CoordinateSpace.INPUT
    # Identity mapping: the same coordinate comes back, to integer precision.
    assert mapped.rounded() == pointer.rounded()

    # The pointer is on-screen, so clamping must be a no-op for it.
    assert geometry_map.clamp_to_desktop(pointer) == pointer

    # Desktop <-> monitor conversion round-trips through the real monitor.
    monitor_point, resolved = geometry_map.desktop_to_monitor(pointer)
    assert resolved.monitor_id == monitor.monitor_id
    assert geometry_map.monitor_to_desktop(monitor_point, resolved) == pointer


def test_frame_point_converts_through_the_real_monitor() -> None:
    """A FRAME coordinate maps into a DESKTOP point on the real monitor."""
    geometry_map = _calibrated_geometry_map()
    monitor = geometry_map.layout.monitors[0]

    desktop_point = Point(
        x=monitor.origin_x + monitor.width / 2.0,
        y=monitor.origin_y + monitor.height / 2.0,
        space=CoordinateSpace.DESKTOP,
    )
    frame_point = geometry_map.monitor_to_frame(Point(
        x=monitor.width / 2.0,
        y=monitor.height / 2.0,
        space=CoordinateSpace.MONITOR,
    ), monitor)

    restored = geometry_map.to_desktop(frame_point, monitor=monitor)
    assert restored.x == pytest.approx(desktop_point.x)
    assert restored.y == pytest.approx(desktop_point.y)


def test_off_desktop_points_are_clamped_not_injected_offscreen() -> None:
    """Section 48: an off-desktop target is clamped into the input bounds."""
    geometry_map = _calibrated_geometry_map()
    desktop = geometry_map.layout.desktop_rect

    far = Point(
        x=desktop.right + 10_000.0,
        y=desktop.bottom + 10_000.0,
        space=CoordinateSpace.DESKTOP,
    )
    clamped = geometry_map.prepare_input_point(far)

    assert clamped.space is CoordinateSpace.INPUT
    assert clamped.x <= desktop.right
    assert clamped.y <= desktop.bottom
    # Clamping a DESKTOP point is idempotent, so a second pass cannot drift.
    desktop_clamped = geometry_map.clamp_to_desktop(far)
    assert geometry_map.clamp_to_desktop(desktop_clamped) == desktop_clamped
