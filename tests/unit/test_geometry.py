"""Coordinate geometry and transforms (specification section 31)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from schemas.enums import CoordinateSpace
from schemas.geometry import (
    Affine2D,
    GeometryMap,
    MonitorGeometry,
    MonitorInputTransform,
    MonitorLayout,
    Point,
    Rect,
)


def _single_monitor_layout() -> MonitorLayout:
    """One 1920x1080 monitor at the desktop origin."""
    return MonitorLayout(
        monitors=(MonitorGeometry(monitor_id=0, width=1920, height=1080, is_primary=True),)
    )


def _two_monitor_layout() -> MonitorLayout:
    """A primary monitor with a second monitor to its right."""
    return MonitorLayout(
        monitors=(
            MonitorGeometry(monitor_id=0, width=1920, height=1080, is_primary=True),
            MonitorGeometry(monitor_id=1, origin_x=1920, origin_y=0, width=1280, height=1024),
        )
    )


def test_point_carries_its_space() -> None:
    """A coordinate without a space is impossible to construct."""
    with pytest.raises(ValidationError):
        Point.model_validate({"x": 1.0, "y": 2.0})


def test_point_distance_requires_same_space() -> None:
    """Comparing across spaces is a programming error, not a silent guess."""
    a = Point(x=0.0, y=0.0, space=CoordinateSpace.FRAME)
    b = Point(x=3.0, y=4.0, space=CoordinateSpace.DESKTOP)
    with pytest.raises(ValueError):
        a.distance_to(b)


def test_point_rounded_is_half_away_from_zero() -> None:
    """Input injection needs integers without a systematic bias."""
    assert Point(x=1.5, y=2.4, space=CoordinateSpace.INPUT).rounded() == (2, 2)
    assert Point(x=-1.5, y=-2.4, space=CoordinateSpace.INPUT).rounded() == (-2, -2)


def test_rect_contains_is_half_open() -> None:
    """The right/bottom edges are exclusive, so adjacent rects do not overlap."""
    rect = Rect(x=0.0, y=0.0, width=10.0, height=10.0, space=CoordinateSpace.DESKTOP)
    assert rect.contains(Point(x=0.0, y=0.0, space=CoordinateSpace.DESKTOP)) is True
    assert rect.contains(Point(x=10.0, y=5.0, space=CoordinateSpace.DESKTOP)) is False


def test_rect_occlusion_ratio() -> None:
    """Section 46: occlusion is the fraction of the target covered."""
    target = Rect(x=0.0, y=0.0, width=100.0, height=100.0, space=CoordinateSpace.DESKTOP)
    half = Rect(x=0.0, y=0.0, width=100.0, height=50.0, space=CoordinateSpace.DESKTOP)
    none = Rect(x=200.0, y=0.0, width=10.0, height=10.0, space=CoordinateSpace.DESKTOP)
    assert target.occlusion_ratio(half) == pytest.approx(0.5)
    assert target.occlusion_ratio(none) == 0.0
    assert target.occlusion_ratio(target) == pytest.approx(1.0)


def test_rect_intersection_returns_none_when_disjoint() -> None:
    """Disjoint rectangles have no overlap rectangle."""
    a = Rect(x=0.0, y=0.0, width=5.0, height=5.0, space=CoordinateSpace.DESKTOP)
    b = Rect(x=10.0, y=10.0, width=5.0, height=5.0, space=CoordinateSpace.DESKTOP)
    assert a.intersection(b) is None
    assert a.intersects(b) is False


def test_layout_requires_unique_ids_and_single_primary() -> None:
    """A layout with duplicate ids or two primaries is not a valid topology."""
    with pytest.raises(ValidationError):
        MonitorLayout(
            monitors=(
                MonitorGeometry(monitor_id=0, width=100, height=100),
                MonitorGeometry(monitor_id=0, width=100, height=100),
            )
        )
    with pytest.raises(ValidationError):
        MonitorLayout(
            monitors=(
                MonitorGeometry(monitor_id=0, width=100, height=100, is_primary=True),
                MonitorGeometry(monitor_id=1, width=100, height=100, is_primary=True),
            )
        )


def test_desktop_rect_is_the_union_of_monitors() -> None:
    """The desktop space spans every monitor."""
    layout = _two_monitor_layout()
    desktop = layout.desktop_rect
    assert (desktop.x, desktop.y) == (0.0, 0.0)
    assert (desktop.width, desktop.height) == (1920 + 1280, 1080)


def test_monitor_at_resolves_the_right_monitor() -> None:
    """A desktop point resolves to the monitor that contains it."""
    layout = _two_monitor_layout()
    assert layout.monitor_at(Point(x=10.0, y=10.0, space=CoordinateSpace.DESKTOP)).monitor_id == 0  # type: ignore[union-attr]
    assert layout.monitor_at(Point(x=2000.0, y=10.0, space=CoordinateSpace.DESKTOP)).monitor_id == 1  # type: ignore[union-attr]
    assert layout.monitor_at(Point(x=5000.0, y=10.0, space=CoordinateSpace.DESKTOP)) is None


def test_affine_identity_and_inverse_round_trip() -> None:
    """Applying then inverting returns the original coordinate."""
    transform = Affine2D(a=1.1, b=0.05, c=-3.0, d=-0.02, e=0.9, f=12.0)
    x, y = transform.apply(100.0, 200.0)
    inv = transform.invert()
    back_x, back_y = inv.apply(x, y)
    assert back_x == pytest.approx(100.0)
    assert back_y == pytest.approx(200.0)


def test_affine_singular_cannot_invert() -> None:
    """A degenerate transform is rejected rather than producing garbage."""
    with pytest.raises(ValueError):
        Affine2D(a=1.0, b=2.0, c=0.0, d=2.0, e=4.0, f=0.0).invert()


def test_affine_compose_applies_other_first() -> None:
    """``self.compose(other)`` is apply-other-then-self."""
    shift = Affine2D(c=10.0, f=20.0)
    scale = Affine2D(a=2.0, e=2.0)
    # Compose shift after scale: (1,1) -> scale -> (2,2) -> shift -> (12,22).
    composed = shift.compose(scale)
    assert composed.apply(1.0, 1.0) == pytest.approx((12.0, 22.0))


def test_frame_to_desktop_pipeline_with_frame_scale() -> None:
    """FRAME -> MONITOR -> DESKTOP honours the frame scale factor."""
    layout = MonitorLayout(
        monitors=(MonitorGeometry(monitor_id=0, width=1000, height=500, frame_scale=0.5),)
    )
    geomap = GeometryMap(layout=layout)
    monitor = layout.require_monitor(0)
    # A 250x125 FRAME point is a 500x250 MONITOR point on the monitor origin.
    desktop = geomap.to_desktop(Point(x=250.0, y=125.0, space=CoordinateSpace.FRAME), monitor=monitor)
    assert (desktop.x, desktop.y) == (500.0, 250.0)
    assert desktop.space is CoordinateSpace.DESKTOP


def test_desktop_to_monitor_round_trip_on_second_monitor() -> None:
    """Desktop <-> monitor conversion accounts for the monitor origin."""
    layout = _two_monitor_layout()
    geomap = GeometryMap(layout=layout)
    desktop = Point(x=2000.0, y=50.0, space=CoordinateSpace.DESKTOP)
    monitor_point, monitor = geomap.desktop_to_monitor(desktop)
    assert monitor.monitor_id == 1
    assert (monitor_point.x, monitor_point.y) == (80.0, 50.0)
    assert geomap.monitor_to_desktop(monitor_point, monitor) == desktop


def test_geometry_map_requires_monitor_for_frame_points() -> None:
    """FRAME/MONITOR coordinates cannot be normalised without their monitor."""
    geomap = GeometryMap(layout=_single_monitor_layout())
    with pytest.raises(ValueError):
        geomap.to_desktop(Point(x=1.0, y=1.0, space=CoordinateSpace.FRAME))


def test_geometry_map_rejects_input_points_in_to_desktop() -> None:
    """INPUT -> DESKTOP is only valid through a calibration inverse."""
    geomap = GeometryMap(layout=_single_monitor_layout())
    with pytest.raises(ValueError):
        geomap.to_desktop(Point(x=1.0, y=1.0, space=CoordinateSpace.INPUT))


def test_geometry_map_applies_calibration_transform() -> None:
    """A calibrated per-monitor transform is used for DESKTOP -> INPUT."""
    layout = _single_monitor_layout()
    geomap = GeometryMap(
        layout=layout,
        input_transforms=(
            MonitorInputTransform(monitor_id=0, desktop_to_input=Affine2D(c=10.0, f=-5.0)),
        ),
        calibrated=True,
    )
    result = geomap.desktop_to_input(Point(x=100.0, y=100.0, space=CoordinateSpace.DESKTOP))
    assert result.space is CoordinateSpace.INPUT
    assert (result.x, result.y) == (110.0, 95.0)


def test_geometry_map_rejects_transform_for_unknown_monitor() -> None:
    """A transform that references a monitor outside the layout is invalid."""
    with pytest.raises(ValidationError):
        GeometryMap(
            layout=_single_monitor_layout(),
            input_transforms=(MonitorInputTransform(monitor_id=7, desktop_to_input=Affine2D()),),
        )


def test_prepare_input_point_clamps_into_desktop() -> None:
    """Section 48: injection coordinates are clamped, never sent off-screen."""
    geomap = GeometryMap(layout=_single_monitor_layout())
    result = geomap.prepare_input_point(Point(x=5000.0, y=5000.0, space=CoordinateSpace.DESKTOP))
    assert result.space is CoordinateSpace.INPUT
    assert result.x <= 1920.0
    assert result.y <= 1080.0


def test_clamp_to_input_bounds_when_known() -> None:
    """A backend smaller than the desktop clamps to its own bounds."""
    layout = _single_monitor_layout()
    geomap = GeometryMap(
        layout=layout,
        input_bounds=Rect(x=0.0, y=0.0, width=800.0, height=600.0, space=CoordinateSpace.INPUT),
    )
    result = geomap.prepare_input_point(Point(x=1500.0, y=1000.0, space=CoordinateSpace.DESKTOP))
    assert result.x <= 800.0
    assert result.y <= 600.0
