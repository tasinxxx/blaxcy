"""Coordinate calibration (specification section 31)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from core.calibration import (
    MIN_CALIBRATION_POINTS,
    Calibration,
    CalibrationSample,
    calibrate,
    identity_geometry_map,
    monitor_identity_map,
    solve_desktop_to_input,
)
from schemas.enums import CoordinateSpace, ErrorCode
from schemas.errors import BlaxcyError
from schemas.geometry import MonitorGeometry, MonitorLayout, Point


def _sample(monitor_id: int, dx: float, dy: float, ix: float, iy: float) -> CalibrationSample:
    """Build a calibration sample from bare numbers."""
    return CalibrationSample(
        monitor_id=monitor_id,
        desktop=Point(x=dx, y=dy, space=CoordinateSpace.DESKTOP),
        observed_input=Point(x=ix, y=iy, space=CoordinateSpace.INPUT),
    )


def _layout(*monitor_ids: int) -> MonitorLayout:
    """A layout of 1000x1000 monitors laid out left to right."""
    return MonitorLayout(
        monitors=tuple(
            MonitorGeometry(monitor_id=mid, origin_x=mid * 1000, width=1000, height=1000)
            for mid in monitor_ids
        )
    )


def test_sample_requires_correct_spaces() -> None:
    """A sample must carry a DESKTOP point and an INPUT point."""
    with pytest.raises(ValidationError):
        CalibrationSample(
            monitor_id=0,
            desktop=Point(x=0.0, y=0.0, space=CoordinateSpace.INPUT),
            observed_input=Point(x=0.0, y=0.0, space=CoordinateSpace.INPUT),
        )


def test_identity_samples_recover_identity_transform() -> None:
    """Perfect identity observations recover an identity mapping."""
    samples = [
        _sample(0, 0.0, 0.0, 0.0, 0.0),
        _sample(0, 100.0, 0.0, 100.0, 0.0),
        _sample(0, 0.0, 100.0, 0.0, 100.0),
    ]
    transform, residual = solve_desktop_to_input(samples)
    assert transform.is_identity is True
    assert residual == pytest.approx(0.0)


def test_offset_samples_recover_translation() -> None:
    """A constant input offset is recovered exactly."""
    samples = [
        _sample(0, 0.0, 0.0, 10.0, -5.0),
        _sample(0, 100.0, 0.0, 110.0, -5.0),
        _sample(0, 0.0, 100.0, 10.0, 95.0),
    ]
    transform, _residual = solve_desktop_to_input(samples)
    assert transform.apply(200.0, 300.0) == pytest.approx((210.0, 295.0))


def test_too_few_samples_fails() -> None:
    """Fewer than three samples is a hard calibration failure."""
    with pytest.raises(BlaxcyError) as excinfo:
        solve_desktop_to_input([_sample(0, 0.0, 0.0, 0.0, 0.0), _sample(0, 1.0, 0.0, 1.0, 0.0)])
    assert excinfo.value.code is ErrorCode.CALIBRATION_FAILED
    assert MIN_CALIBRATION_POINTS == 3


def test_collinear_samples_fail() -> None:
    """Collinear samples cannot span 2-D space and must be rejected."""
    samples = [
        _sample(0, 0.0, 0.0, 0.0, 0.0),
        _sample(0, 10.0, 10.0, 10.0, 10.0),
        _sample(0, 20.0, 20.0, 20.0, 20.0),
    ]
    with pytest.raises(BlaxcyError) as excinfo:
        solve_desktop_to_input(samples)
    assert excinfo.value.code is ErrorCode.CALIBRATION_FAILED
    assert "collinear" in excinfo.value.message


def test_residual_over_tolerance_fails() -> None:
    """A fit that does not actually explain the observations is rejected."""
    samples = [
        _sample(0, 0.0, 0.0, 0.0, 0.0),
        _sample(0, 100.0, 0.0, 100.0, 0.0),
        _sample(0, 0.0, 100.0, 0.0, 100.0),
        # A wild outlier that no affine fit can absorb.
        _sample(0, 50.0, 50.0, 500.0, 500.0),
    ]
    with pytest.raises(BlaxcyError) as excinfo:
        solve_desktop_to_input(samples, tolerance_px=2.0)
    assert excinfo.value.code is ErrorCode.CALIBRATION_FAILED
    assert "residual" in excinfo.value.message


def test_calibrate_marks_missing_monitor_incomplete_and_disarms() -> None:
    """A monitor with no samples keeps input disarmed."""
    layout = _layout(0, 1)
    result = calibrate(
        layout,
        [
            _sample(0, 0.0, 0.0, 0.0, 0.0),
            _sample(0, 100.0, 0.0, 100.0, 0.0),
            _sample(0, 0.0, 100.0, 0.0, 100.0),
        ],
    )
    assert isinstance(result, Calibration)
    assert result.complete is False
    assert result.disarmed is True
    assert result.missing_monitors == (1,)
    with pytest.raises(BlaxcyError) as excinfo:
        result.geometry_map()
    assert excinfo.value.code is ErrorCode.CALIBRATION_FAILED


def test_calibrate_complete_builds_calibrated_geometry_map() -> None:
    """Every monitor covered and valid yields a calibrated geometry map."""
    layout = _layout(0)
    result = calibrate(
        layout,
        [
            _sample(0, 0.0, 0.0, 0.0, 0.0),
            _sample(0, 100.0, 0.0, 105.0, 0.0),
            _sample(0, 0.0, 100.0, 0.0, 105.0),
        ],
    )
    assert result.complete is True
    geomap = result.geometry_map()
    assert geomap.calibrated is True
    assert geomap.input_transforms[0].monitor_id == 0
    mapped = geomap.desktop_to_input(Point(x=100.0, y=0.0, space=CoordinateSpace.DESKTOP))
    assert mapped.x == pytest.approx(105.0)


def test_calibrate_rejects_sample_for_unknown_monitor() -> None:
    """A sample about a monitor that is not in the layout is a caller bug."""
    with pytest.raises(BlaxcyError) as excinfo:
        calibrate(_layout(0), [_sample(9, 0.0, 0.0, 0.0, 0.0)])
    assert excinfo.value.code is ErrorCode.CALIBRATION_FAILED


def test_collinear_monitor_recorded_as_invalid() -> None:
    """Degenerate geometry yields an invalid entry, not a crash."""
    layout = _layout(0)
    result = calibrate(
        layout,
        [
            _sample(0, 0.0, 0.0, 0.0, 0.0),
            _sample(0, 10.0, 10.0, 10.0, 10.0),
            _sample(0, 20.0, 20.0, 20.0, 20.0),
        ],
    )
    assert result.complete is False
    assert result.invalid_monitors == (0,)
    assert result.for_monitor(0).valid is False  # type: ignore[union-attr]


def test_identity_and_monitor_maps_are_explicit() -> None:
    """A deliberate identity map is distinguishable from a failed calibration."""
    layout = _layout(0)
    uncalibrated = identity_geometry_map(layout)
    assert uncalibrated.calibrated is False
    per_monitor = monitor_identity_map(layout, layout.require_monitor(0))
    assert per_monitor.calibrated is True
