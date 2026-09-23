"""Typed coordinate geometry and transforms (specification section 31).

A coordinate never exists without its coordinate space. The specification
defines four spaces:

``FRAME``
    Pixels inside a captured (possibly downscaled) monitor frame; origin at the
    monitor's top-left.
``MONITOR``
    Physical pixels local to one monitor; origin at that monitor's top-left.
``DESKTOP``
    The virtual union of every monitor (the X root-window coordinate space).
``INPUT``
    The coordinate space the active input backend actually injects in.

The mandated pipeline is ``FRAME -> MONITOR -> DESKTOP -> INPUT``. Nothing in
this module injects input or touches a display; it is pure, deterministic
arithmetic over a :class:`MonitorLayout` plus an optional per-monitor
``DESKTOP -> INPUT`` affine (see :mod:`core.calibration`).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from schemas.enums import CoordinateSpace

#: Tolerance for "did this point move more than the allowed amount" checks.
GEOMETRY_EPSILON: float = 1e-6


class Size(BaseModel):
    """A width/height magnitude. Orientation-free, so it carries no space."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    width: float = Field(ge=0.0)
    height: float = Field(ge=0.0)

    @property
    def area(self) -> float:
        """Width times height."""
        return self.width * self.height


class Point(BaseModel):
    """A single coordinate, always tagged with the space it lives in."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    x: float
    y: float
    space: CoordinateSpace

    def rounded(self) -> tuple[int, int]:
        """Integer device coordinates, rounded half away from zero.

        Input injection takes integers; rounding here (rather than truncating)
        avoids a systematic half-pixel bias across a row of targets.
        """
        return round(self.x), round(self.y)

    def translated(self, dx: float, dy: float) -> Point:
        """Return this point shifted by ``(dx, dy)`` within the same space."""
        return Point(x=self.x + dx, y=self.y + dy, space=self.space)

    def distance_to(self, other: Point) -> float:
        """Euclidean distance to ``other``; both points must share a space."""
        self._require_same_space(other)
        return float(((self.x - other.x) ** 2 + (self.y - other.y) ** 2) ** 0.5)

    def moved_beyond(self, other: Point, tolerance_px: float) -> bool:
        """True when the distance to ``other`` exceeds ``tolerance_px``.

        Used by section 45 revalidation: a target that moved ``<= 4 px`` is
        treated as the same target.
        """
        return self.distance_to(other) > tolerance_px

    def _require_same_space(self, other: Point) -> None:
        if self.space is not other.space:
            raise ValueError(
                f"cannot compare a {self.space.value} point with a {other.space.value} point"
            )


class Rect(BaseModel):
    """An axis-aligned rectangle in one explicit coordinate space."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    x: float
    y: float
    width: float = Field(ge=0.0)
    height: float = Field(ge=0.0)
    space: CoordinateSpace

    @property
    def right(self) -> float:
        """Exclusive right edge."""
        return self.x + self.width

    @property
    def bottom(self) -> float:
        """Exclusive bottom edge."""
        return self.y + self.height

    @property
    def area(self) -> float:
        """Enclosed area."""
        return self.width * self.height

    @property
    def center(self) -> Point:
        """The geometric center, in this rectangle's space."""
        return Point(x=self.x + self.width / 2.0, y=self.y + self.height / 2.0, space=self.space)

    def contains(self, point: Point) -> bool:
        """True when ``point`` lies inside the half-open rectangle."""
        self._require_same_space(point.space)
        return self.x <= point.x < self.right and self.y <= point.y < self.bottom

    def intersects(self, other: Rect) -> bool:
        """True when the two rectangles overlap at all."""
        self._require_same_space(other.space)
        return self.x < other.right and other.x < self.right and self.y < other.bottom and other.y < self.bottom

    def intersection(self, other: Rect) -> Rect | None:
        """The overlapping rectangle, or ``None`` when they do not overlap."""
        self._require_same_space(other.space)
        left = max(self.x, other.x)
        top = max(self.y, other.y)
        right = min(self.right, other.right)
        bottom = min(self.bottom, other.bottom)
        if right <= left or bottom <= top:
            return None
        return Rect(x=left, y=top, width=right - left, height=bottom - top, space=self.space)

    def occlusion_ratio(self, other: Rect) -> float:
        """Fraction of *this* rectangle covered by ``other`` (section 46).

        ``1.0`` means fully covered; ``0.0`` means no overlap. The default
        actionable rule is ``occlusion > 0.50 => not actionable``.
        """
        overlap = self.intersection(other)
        if overlap is None or self.area <= 0.0:
            return 0.0
        return min(1.0, overlap.area / self.area)

    def translated(self, dx: float, dy: float) -> Rect:
        """Return this rectangle shifted by ``(dx, dy)`` in the same space."""
        return Rect(x=self.x + dx, y=self.y + dy, width=self.width, height=self.height, space=self.space)

    def to_dict(self) -> dict[str, Any]:
        """JSON-shaped form for the tool envelope."""
        return {
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
            "space": self.space.value,
        }

    def _require_same_space(self, space: CoordinateSpace) -> None:
        if self.space is not space:
            raise ValueError(f"cannot combine a {self.space.value} rect with a {space.value} rect")


class MonitorGeometry(BaseModel):
    """One physical monitor and where it sits in the desktop space."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    monitor_id: int = Field(ge=0)
    name: str | None = None
    #: Top-left of the monitor in DESKTOP space.
    origin_x: float = 0.0
    origin_y: float = 0.0
    #: Physical size in MONITOR space.
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    #: FRAME pixels per MONITOR pixel for this monitor's captured frames.
    frame_scale: float = Field(default=1.0, gt=0.0)
    #: Fractional device scale reported by the compositor (informational only).
    device_scale: float = Field(default=1.0, gt=0.0)
    is_primary: bool = False

    @property
    def origin(self) -> Point:
        """The monitor origin in DESKTOP space."""
        return Point(x=self.origin_x, y=self.origin_y, space=CoordinateSpace.DESKTOP)

    @property
    def monitor_rect(self) -> Rect:
        """The full monitor rectangle in MONITOR space."""
        return Rect(x=0.0, y=0.0, width=float(self.width), height=float(self.height), space=CoordinateSpace.MONITOR)

    @property
    def desktop_rect(self) -> Rect:
        """The monitor rectangle expressed in DESKTOP space."""
        return Rect(
            x=self.origin_x,
            y=self.origin_y,
            width=float(self.width),
            height=float(self.height),
            space=CoordinateSpace.DESKTOP,
        )

    @property
    def frame_rect(self) -> Rect:
        """The captured-frame rectangle in FRAME space."""
        return Rect(
            x=0.0,
            y=0.0,
            width=self.width * self.frame_scale,
            height=self.height * self.frame_scale,
            space=CoordinateSpace.FRAME,
        )

    def contains_desktop(self, point: Point) -> bool:
        """True when a DESKTOP point falls on this monitor."""
        if point.space is not CoordinateSpace.DESKTOP:
            raise ValueError(f"expected a DESKTOP point, got {point.space.value}")
        return self.desktop_rect.contains(point)


class MonitorLayout(BaseModel):
    """The full monitor topology, validated to be usable for transforms."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    monitors: tuple[MonitorGeometry, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_topology(self) -> MonitorLayout:
        ids = [m.monitor_id for m in self.monitors]
        if len(ids) != len(set(ids)):
            raise ValueError("monitor_id values must be unique")
        primaries = [m for m in self.monitors if m.is_primary]
        if len(primaries) > 1:
            raise ValueError("at most one monitor may be marked primary")
        return self

    @property
    def desktop_rect(self) -> Rect:
        """The union of every monitor, in DESKTOP space."""
        left = min(m.origin_x for m in self.monitors)
        top = min(m.origin_y for m in self.monitors)
        right = max(m.origin_x + m.width for m in self.monitors)
        bottom = max(m.origin_y + m.height for m in self.monitors)
        return Rect(x=left, y=top, width=right - left, height=bottom - top, space=CoordinateSpace.DESKTOP)

    def by_id(self, monitor_id: int) -> MonitorGeometry | None:
        """Return the monitor with ``monitor_id``, or ``None`` if absent."""
        for monitor in self.monitors:
            if monitor.monitor_id == monitor_id:
                return monitor
        return None

    def monitor_at(self, point: Point) -> MonitorGeometry | None:
        """Return the monitor containing a DESKTOP ``point``, or ``None``."""
        for monitor in self.monitors:
            if monitor.contains_desktop(point):
                return monitor
        return None

    def require_monitor(self, monitor_id: int) -> MonitorGeometry:
        """Return the monitor with ``monitor_id`` or raise ``ValueError``."""
        monitor = self.by_id(monitor_id)
        if monitor is None:
            raise ValueError(f"monitor {monitor_id} is not part of this layout")
        return monitor


class Affine2D(BaseModel):
    """A 2-D affine transform ``x' = a*x + b*y + c``, ``y' = d*x + e*y + f``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    a: float = 1.0
    b: float = 0.0
    c: float = 0.0
    d: float = 0.0
    e: float = 1.0
    f: float = 0.0

    @classmethod
    def identity(cls) -> Affine2D:
        """The identity transform."""
        return cls()

    @property
    def determinant(self) -> float:
        """``a*e - b*d``; zero means the transform is singular."""
        return self.a * self.e - self.b * self.d

    @property
    def is_identity(self) -> bool:
        """True when this transform changes nothing (within epsilon)."""
        return (
            abs(self.a - 1.0) < GEOMETRY_EPSILON
            and abs(self.b) < GEOMETRY_EPSILON
            and abs(self.c) < GEOMETRY_EPSILON
            and abs(self.d) < GEOMETRY_EPSILON
            and abs(self.e - 1.0) < GEOMETRY_EPSILON
            and abs(self.f) < GEOMETRY_EPSILON
        )

    def apply(self, x: float, y: float) -> tuple[float, float]:
        """Map ``(x, y)`` through this transform."""
        return (self.a * x + self.b * y + self.c, self.d * x + self.e * y + self.f)

    def invert(self) -> Affine2D:
        """The inverse transform.

        Raises:
            ValueError: When the transform is singular (determinant ~ 0).
        """
        det = self.determinant
        if abs(det) < GEOMETRY_EPSILON:
            raise ValueError("cannot invert a singular affine transform")
        return Affine2D(
            a=self.e / det,
            b=-self.b / det,
            c=(self.b * self.f - self.e * self.c) / det,
            d=-self.d / det,
            e=self.a / det,
            f=(self.d * self.c - self.a * self.f) / det,
        )

    def compose(self, other: Affine2D) -> Affine2D:
        """``self ∘ other`` -- apply ``other`` first, then ``self``."""
        return Affine2D(
            a=self.a * other.a + self.b * other.d,
            b=self.a * other.b + self.b * other.e,
            c=self.a * other.c + self.b * other.f + self.c,
            d=self.d * other.a + self.e * other.d,
            e=self.d * other.b + self.e * other.e,
            f=self.d * other.c + self.e * other.f + self.f,
        )


class MonitorInputTransform(BaseModel):
    """The DESKTOP -> INPUT mapping for one monitor (from calibration)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    monitor_id: int = Field(ge=0)
    desktop_to_input: Affine2D


class GeometryMap(BaseModel):
    """The validated bridge between FRAME/MONITOR/DESKTOP and INPUT space."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    layout: MonitorLayout
    #: Per-monitor DESKTOP -> INPUT transforms. Empty means identity mapping.
    input_transforms: tuple[MonitorInputTransform, ...] = ()
    #: True only when a validated calibration produced these transforms.
    calibrated: bool = False
    #: Optional hard bounds of the INPUT space (some backends are smaller than
    #: the desktop). ``None`` means "no separate bound known".
    input_bounds: Rect | None = None

    @model_validator(mode="after")
    def _validate_transforms(self) -> GeometryMap:
        seen: set[int] = set()
        for transform in self.input_transforms:
            if transform.monitor_id in seen:
                raise ValueError(f"duplicate input transform for monitor {transform.monitor_id}")
            seen.add(transform.monitor_id)
            if self.layout.by_id(transform.monitor_id) is None:
                raise ValueError(
                    f"input transform references monitor {transform.monitor_id}, absent from the layout"
                )
        if self.input_bounds is not None and self.input_bounds.space is not CoordinateSpace.INPUT:
            raise ValueError("input_bounds must be expressed in INPUT space")
        return self

    # -- Desktop-space helpers ------------------------------------------------

    def monitor_at(self, point: Point) -> MonitorGeometry:
        """Resolve the monitor a DESKTOP ``point`` belongs to.

        Raises:
            ValueError: When the point lies outside every monitor (a coordinate
                that cannot be mapped must fail loudly, never be guessed).
        """
        if point.space is not CoordinateSpace.DESKTOP:
            raise ValueError(f"expected a DESKTOP point, got {point.space.value}")
        monitor = self.layout.monitor_at(point)
        if monitor is None:
            raise ValueError(f"desktop point ({point.x}, {point.y}) is on no monitor")
        return monitor

    def clamp_to_desktop(self, point: Point) -> Point:
        """Clamp a DESKTOP point into the desktop bounds (section 48)."""
        if point.space is not CoordinateSpace.DESKTOP:
            raise ValueError(f"expected a DESKTOP point, got {point.space.value}")
        desktop = self.layout.desktop_rect
        return Point(
            x=min(max(point.x, desktop.x), desktop.right - GEOMETRY_EPSILON),
            y=min(max(point.y, desktop.y), desktop.bottom - GEOMETRY_EPSILON),
            space=CoordinateSpace.DESKTOP,
        )

    # -- Space conversions ----------------------------------------------------

    def frame_to_monitor(self, point: Point, monitor: MonitorGeometry) -> Point:
        """Convert a FRAME point on ``monitor`` into MONITOR space."""
        self._require_space(point.space, CoordinateSpace.FRAME)
        return Point(x=point.x / monitor.frame_scale, y=point.y / monitor.frame_scale, space=CoordinateSpace.MONITOR)

    def monitor_to_frame(self, point: Point, monitor: MonitorGeometry) -> Point:
        """Convert a MONITOR point on ``monitor`` into FRAME space."""
        self._require_space(point.space, CoordinateSpace.MONITOR)
        return Point(x=point.x * monitor.frame_scale, y=point.y * monitor.frame_scale, space=CoordinateSpace.FRAME)

    def monitor_to_desktop(self, point: Point, monitor: MonitorGeometry) -> Point:
        """Convert a MONITOR point on ``monitor`` into DESKTOP space."""
        self._require_space(point.space, CoordinateSpace.MONITOR)
        return Point(x=point.x + monitor.origin_x, y=point.y + monitor.origin_y, space=CoordinateSpace.DESKTOP)

    def desktop_to_monitor(self, point: Point) -> tuple[Point, MonitorGeometry]:
        """Convert a DESKTOP point into its monitor's MONITOR space."""
        monitor = self.monitor_at(point)
        return (
            Point(x=point.x - monitor.origin_x, y=point.y - monitor.origin_y, space=CoordinateSpace.MONITOR),
            monitor,
        )

    def to_desktop(self, point: Point, *, monitor: MonitorGeometry | None = None) -> Point:
        """Normalise any space (except INPUT) into DESKTOP space.

        Args:
            point: The coordinate to convert.
            monitor: Required when ``point`` is in FRAME or MONITOR space.
        """
        if point.space is CoordinateSpace.DESKTOP:
            return point
        if point.space is CoordinateSpace.FRAME:
            return self.monitor_to_desktop(self.frame_to_monitor(point, self._require_monitor(monitor)), self._require_monitor(monitor))
        if point.space is CoordinateSpace.MONITOR:
            return self.monitor_to_desktop(point, self._require_monitor(monitor))
        raise ValueError("INPUT points must be mapped back through a calibration inverse, not to_desktop()")

    def desktop_to_input(self, point: Point) -> Point:
        """Map a DESKTOP point into INPUT space for the owning monitor."""
        monitor = self.monitor_at(point)
        transform = self._transform_for(monitor.monitor_id)
        x, y = transform.apply(point.x, point.y)
        return Point(x=x, y=y, space=CoordinateSpace.INPUT)

    def to_input(self, point: Point, *, monitor: MonitorGeometry | None = None) -> Point:
        """Map a FRAME/MONITOR/DESKTOP point all the way to INPUT space."""
        if point.space is CoordinateSpace.INPUT:
            return point
        return self.desktop_to_input(self.to_desktop(point, monitor=monitor))

    def clamp_to_input(self, point: Point) -> Point:
        """Clamp an INPUT point into ``input_bounds`` when those are known."""
        if point.space is not CoordinateSpace.INPUT:
            raise ValueError(f"expected an INPUT point, got {point.space.value}")
        if self.input_bounds is None:
            return point
        bounds = self.input_bounds
        return Point(
            x=min(max(point.x, bounds.x), bounds.right - GEOMETRY_EPSILON),
            y=min(max(point.y, bounds.y), bounds.bottom - GEOMETRY_EPSILON),
            space=CoordinateSpace.INPUT,
        )

    def prepare_input_point(self, point: Point, *, monitor: MonitorGeometry | None = None) -> Point:
        """Full, validated FRAME/MONITOR/DESKTOP -> clamped INPUT conversion.

        This is the single entry point injection backends should use: it
        converts, clamps to the desktop, maps through calibration, and clamps
        to the input bounds -- in that order -- so a backend never receives a
        coordinate that could land off-screen (section 48).
        """
        desktop = self.clamp_to_desktop(self.to_desktop(point, monitor=monitor))
        return self.clamp_to_input(self.desktop_to_input(desktop))

    def _transform_for(self, monitor_id: int) -> Affine2D:
        for transform in self.input_transforms:
            if transform.monitor_id == monitor_id:
                return transform.desktop_to_input
        return Affine2D.identity()

    @staticmethod
    def _require_monitor(monitor: MonitorGeometry | None) -> MonitorGeometry:
        if monitor is None:
            raise ValueError("a monitor is required to convert FRAME/MONITOR coordinates")
        return monitor

    @staticmethod
    def _require_space(actual: CoordinateSpace, expected: CoordinateSpace) -> None:
        if actual is not expected:
            raise ValueError(f"expected a {expected.value} point, got {actual.value}")
