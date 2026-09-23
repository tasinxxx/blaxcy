"""Coordinate calibration (specification section 31).

Input injection is only trustworthy when the mapping between what BLAXCY
resolves (DESKTOP space) and what the input backend actually injects (INPUT
space) has been *observed*, not assumed. Calibration collects, per monitor, at
least three non-collinear ``(desktop, observed_input)`` samples and fits an
affine transform to them.

If a monitor cannot be calibrated -- too few samples, collinear samples, a
singular fit, or a residual too large to trust -- the result is **invalid**,
which means input stays disarmed (``CALIBRATION_FAILED``). Calibration never
guesses and never silently falls back to an identity mapping when a real
calibration was required.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

from schemas.enums import CoordinateSpace, ErrorCode
from schemas.errors import BlaxcyError
from schemas.geometry import (
    Affine2D,
    GeometryMap,
    MonitorGeometry,
    MonitorInputTransform,
    MonitorLayout,
    Point,
)

#: Minimum samples per monitor (specification section 31: at least 3).
MIN_CALIBRATION_POINTS: int = 3

#: Default acceptable worst-case residual, in INPUT pixels.
DEFAULT_RESIDUAL_TOLERANCE_PX: float = 2.0


class CalibrationSample(BaseModel):
    """One observed ``(desktop, input)`` pair for a monitor."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    monitor_id: int = Field(ge=0)
    desktop: Point
    observed_input: Point

    @model_validator(mode="after")
    def _validate_spaces(self) -> CalibrationSample:
        if self.desktop.space is not CoordinateSpace.DESKTOP:
            raise ValueError("calibration sample 'desktop' must be a DESKTOP point")
        if self.observed_input.space is not CoordinateSpace.INPUT:
            raise ValueError("calibration sample 'observed_input' must be an INPUT point")
        return self


class MonitorCalibration(BaseModel):
    """The fitted DESKTOP -> INPUT transform for a single monitor."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    monitor_id: int = Field(ge=0)
    transform: Affine2D
    sample_count: int = Field(ge=0)
    max_residual_px: float = Field(ge=0.0)
    tolerance_px: float = Field(gt=0.0)

    @property
    def valid(self) -> bool:
        """True when enough non-collinear samples fit within tolerance."""
        return self.sample_count >= MIN_CALIBRATION_POINTS and self.max_residual_px <= self.tolerance_px


class Calibration(BaseModel):
    """A complete, per-monitor calibration result for one layout."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    layout: MonitorLayout
    monitors: tuple[MonitorCalibration, ...] = ()
    tolerance_px: float = Field(default=DEFAULT_RESIDUAL_TOLERANCE_PX, gt=0.0)

    def for_monitor(self, monitor_id: int) -> MonitorCalibration | None:
        """Return the calibration for ``monitor_id``, or ``None``."""
        for calibration in self.monitors:
            if calibration.monitor_id == monitor_id:
                return calibration
        return None

    @property
    def missing_monitors(self) -> tuple[int, ...]:
        """Monitor ids in the layout with no samples at all."""
        with_samples = {c.monitor_id for c in self.monitors if c.sample_count > 0}
        return tuple(m.monitor_id for m in self.layout.monitors if m.monitor_id not in with_samples)

    @property
    def invalid_monitors(self) -> tuple[int, ...]:
        """Monitor ids that have samples but failed validation (count, fit or residual)."""
        return tuple(c.monitor_id for c in self.monitors if c.sample_count > 0 and not c.valid)

    @property
    def complete(self) -> bool:
        """True only when every monitor is covered and every fit is valid."""
        return not self.missing_monitors and not self.invalid_monitors

    @property
    def disarmed(self) -> bool:
        """True while input must remain disarmed (inverse of ``complete``)."""
        return not self.complete

    def failure_reason(self) -> str | None:
        """A human-readable reason the calibration is incomplete, or ``None``."""
        if self.missing_monitors:
            return f"no calibration samples for monitors {list(self.missing_monitors)}"
        if self.invalid_monitors:
            details = []
            for monitor_id in self.invalid_monitors:
                calibration = self.for_monitor(monitor_id)
                if calibration is None:
                    continue
                details.append(
                    f"monitor {monitor_id}: {calibration.sample_count} sample(s), "
                    f"max residual {calibration.max_residual_px:.3f}px"
                )
            return "; ".join(details)
        return None

    def geometry_map(self) -> GeometryMap:
        """Build a calibrated :class:`GeometryMap`.

        Raises:
            BlaxcyError: ``CALIBRATION_FAILED`` when the calibration is not
                complete. Input stays disarmed rather than degrading to an
                assumed identity mapping.
        """
        if not self.complete:
            raise BlaxcyError(
                ErrorCode.CALIBRATION_FAILED,
                f"input remains disarmed: {self.failure_reason()}",
                details={"missing_monitors": list(self.missing_monitors), "invalid": list(self.invalid_monitors)},
            )
        transforms = tuple(
            MonitorInputTransform(monitor_id=c.monitor_id, desktop_to_input=c.transform) for c in self.monitors
        )
        return GeometryMap(layout=self.layout, input_transforms=transforms, calibrated=True)


def identity_geometry_map(layout: MonitorLayout) -> GeometryMap:
    """A map for the case where calibration is *not* required.

    Under X11/XTest the INPUT space coincides with DESKTOP space, so no
    calibration is needed. This is a deliberate, documented identity mapping
    -- distinct from a *failed* calibration silently degrading to identity.
    """
    return GeometryMap(layout=layout, input_transforms=(), calibrated=False)


def solve_desktop_to_input(
    samples: Sequence[CalibrationSample],
    *,
    tolerance_px: float = DEFAULT_RESIDUAL_TOLERANCE_PX,
) -> tuple[Affine2D, float]:
    """Least-squares fit of DESKTOP -> INPUT over ``samples``.

    Returns:
        ``(transform, max_residual_px)``.

    Raises:
        BlaxcyError: ``CALIBRATION_FAILED`` when there are fewer than three
            samples, when the sample points are (near) collinear, or when the
            fitted transform is singular.
    """
    if len(samples) < MIN_CALIBRATION_POINTS:
        raise BlaxcyError(
            ErrorCode.CALIBRATION_FAILED,
            f"need at least {MIN_CALIBRATION_POINTS} non-collinear samples, got {len(samples)}",
            details={"sample_count": len(samples)},
        )

    design = np.array([[s.desktop.x, s.desktop.y, 1.0] for s in samples], dtype=float)
    target_x = np.array([s.observed_input.x for s in samples], dtype=float)
    target_y = np.array([s.observed_input.y for s in samples], dtype=float)

    # A full-rank design matrix is exactly the non-collinearity condition: it
    # means the sample points span 2-D space rather than lying on one line.
    rank = int(np.linalg.matrix_rank(design))
    if rank < 3:
        raise BlaxcyError(
            ErrorCode.CALIBRATION_FAILED,
            "calibration samples are collinear (rank-deficient); need three non-collinear points",
            details={"rank": rank, "sample_count": len(samples)},
        )

    coef_x, _res_x, _rank_x, _sv_x = np.linalg.lstsq(design, target_x, rcond=None)
    coef_y, _res_y, _rank_y, _sv_y = np.linalg.lstsq(design, target_y, rcond=None)

    transform = Affine2D(
        a=float(coef_x[0]),
        b=float(coef_x[1]),
        c=float(coef_x[2]),
        d=float(coef_y[0]),
        e=float(coef_y[1]),
        f=float(coef_y[2]),
    )
    if abs(transform.determinant) < 1e-9:
        raise BlaxcyError(
            ErrorCode.CALIBRATION_FAILED,
            "fitted calibration transform is singular and cannot be inverted",
        )

    residual = 0.0
    for sample in samples:
        px, py = transform.apply(sample.desktop.x, sample.desktop.y)
        distance = ((px - sample.observed_input.x) ** 2 + (py - sample.observed_input.y) ** 2) ** 0.5
        residual = max(residual, distance)

    if residual > tolerance_px:
        raise BlaxcyError(
            ErrorCode.CALIBRATION_FAILED,
            f"calibration residual {residual:.3f}px exceeds tolerance {tolerance_px:.3f}px",
            details={"max_residual_px": residual, "tolerance_px": tolerance_px},
        )

    return transform, residual


def calibrate(
    layout: MonitorLayout,
    samples: Sequence[CalibrationSample],
    *,
    tolerance_px: float = DEFAULT_RESIDUAL_TOLERANCE_PX,
) -> Calibration:
    """Calibrate every monitor in ``layout`` from ``samples``.

    Monitors with too few/collinear samples are recorded as invalid rather than
    raising, so the caller can report *which* monitor blocked calibration and
    keep input disarmed (``complete`` stays ``False``). A sample referencing a
    monitor absent from the layout raises ``CALIBRATION_FAILED`` immediately --
    that is a caller bug, not an environmental limitation.
    """
    known_ids = {m.monitor_id for m in layout.monitors}
    grouped: dict[int, list[CalibrationSample]] = {}
    for sample in samples:
        if sample.monitor_id not in known_ids:
            raise BlaxcyError(
                ErrorCode.CALIBRATION_FAILED,
                f"calibration sample references unknown monitor {sample.monitor_id}",
                details={"monitor_id": sample.monitor_id, "known": sorted(known_ids)},
            )
        grouped.setdefault(sample.monitor_id, []).append(sample)

    results: list[MonitorCalibration] = []
    for monitor in layout.monitors:
        monitor_samples = grouped.get(monitor.monitor_id, [])
        if len(monitor_samples) < MIN_CALIBRATION_POINTS:
            results.append(
                MonitorCalibration(
                    monitor_id=monitor.monitor_id,
                    transform=Affine2D.identity(),
                    sample_count=len(monitor_samples),
                    max_residual_px=float("inf"),
                    tolerance_px=tolerance_px,
                )
            )
            continue
        try:
            transform, residual = solve_desktop_to_input(monitor_samples, tolerance_px=tolerance_px)
        except BlaxcyError:
            # Degenerate geometry (e.g. collinear points): record an explicitly
            # invalid entry so the layout-level report names this monitor.
            results.append(
                MonitorCalibration(
                    monitor_id=monitor.monitor_id,
                    transform=Affine2D.identity(),
                    sample_count=len(monitor_samples),
                    max_residual_px=float("inf"),
                    tolerance_px=tolerance_px,
                )
            )
            continue
        results.append(
            MonitorCalibration(
                monitor_id=monitor.monitor_id,
                transform=transform,
                sample_count=len(monitor_samples),
                max_residual_px=residual,
                tolerance_px=tolerance_px,
            )
        )

    return Calibration(layout=layout, monitors=tuple(results), tolerance_px=tolerance_px)


def monitor_identity_map(layout: MonitorLayout, monitor: MonitorGeometry) -> GeometryMap:
    """A calibrated map for exactly one monitor with an identity transform.

    Useful for tests and for single-monitor sessions; it is still reported as
    ``calibrated=True`` because a (trivially valid) mapping was established for
    that monitor rather than assumed.
    """
    return GeometryMap(
        layout=layout,
        input_transforms=(MonitorInputTransform(monitor_id=monitor.monitor_id, desktop_to_input=Affine2D.identity()),),
        calibrated=True,
    )
