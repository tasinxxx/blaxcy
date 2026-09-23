"""Change detection and classification (specification section 34).

The change detector answers one question cheaply: *did the screen change, how
much, and where?* It is the trigger for perception work, not a perception source
itself -- it observes geometry and structure, never semantics.

Pipeline (section 34): downscale -> ``cv2.absdiff`` -> threshold changed pixels
-> tile-level change -> classify structural change. The change classes are
``NONE``, ``TRIVIAL``, ``ANIMATION``, ``MEANINGFUL`` and ``MAJOR``.

Two layers live here:

* :func:`classify_change` -- a pure, deterministic spatial classifier over two
  grayscale thumbnails. It detects *where* the screen changed and how
  structurally, and never returns ``ANIMATION`` because a single pair of frames
  cannot prove motion.
* :class:`ChangeDetector` -- adds the temporal layer. A region that keeps
  changing across successive comparisons (within ``animation_recheck_seconds``)
  is reclassified ``ANIMATION``: it is moving content, not a discrete UI update.
* :class:`ChangeGate` -- coalesces classifications into perception-job
  decisions, honouring ``debounce_short_ms`` / ``debounce_major_ms`` and the
  ``max_perception_jobs_per_second`` rate cap. After a suppressed burst exactly
  one coalesced follow-up perception is allowed.

Thresholds (``meaningful_min_width`` / ``meaningful_min_height`` /
``meaningful_area_ratio`` / ``major_area_ratio``) are expressed in *thumbnail*
pixels, matching the canonical 480x270 grayscale geometry. Change regions are
reported in DESKTOP space so that :meth:`ScreenDelta.affects` can be checked
against element bounding boxes, which live in DESKTOP space.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

import cv2
import numpy as np
import numpy.typing as npt

from config.settings import PerceptionSettings
from core.frame_engine import Frame
from schemas.enums import ChangeClass, CoordinateSpace
from schemas.geometry import Rect
from schemas.screen_state import ChangeRegion, ScreenDelta

#: Tile grid used for the tile-level change stage: 30 columns x 30 rows over a
#: logical 16:9 thumbnail. On the canonical 480x270 thumbnail each tile is
#: exactly 16x9 pixels (section 34).
TILE_GRID_COLS: Final[int] = 30
TILE_GRID_ROWS: Final[int] = 30
TILE_WIDTH: Final[int] = 16
TILE_HEIGHT: Final[int] = 9


@dataclass(frozen=True)
class TileComponent:
    """One connected component of changed tiles, in tile coordinates."""

    x: int
    y: int
    width: int
    height: int


@dataclass(frozen=True)
class _TrackedRegion:
    """A change region being tracked over time for animation detection."""

    rect: Rect
    first_seen: float


def _tile_mask(mask: npt.NDArray[np.uint8]) -> npt.NDArray[np.uint8]:
    """Reduce a per-pixel mask to a 30x30 tile mask (any changed pixel wins)."""
    fraction = cv2.resize(
        mask.astype(np.float32),
        (TILE_GRID_COLS, TILE_GRID_ROWS),
        interpolation=cv2.INTER_AREA,
    )
    return (fraction > 0.0).astype(np.uint8)


def _tile_components(tile_mask: npt.NDArray[np.uint8]) -> tuple[TileComponent, ...]:
    """Find 4-connected components of changed tiles (OpenCV, no extra deps)."""
    count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(
        tile_mask, connectivity=4
    )
    components: list[TileComponent] = []
    for index in range(1, int(count)):
        x, y, width, height, _area = (int(v) for v in stats[index])
        components.append(TileComponent(x=x, y=y, width=width, height=height))
    return tuple(components)


def _thumbnail_rect(component: TileComponent, *, width: int, height: int) -> Rect:
    """Map a tile component to a rectangle in thumbnail pixel coordinates."""
    tile_w = width / TILE_GRID_COLS
    tile_h = height / TILE_GRID_ROWS
    return Rect(
        x=component.x * tile_w,
        y=component.y * tile_h,
        width=component.width * tile_w,
        height=component.height * tile_h,
        space=CoordinateSpace.FRAME,
    )


def _to_desktop(thumb_rect: Rect, *, thumbnail: tuple[int, int], desktop_rect: Rect) -> Rect:
    """Scale a thumbnail-space rectangle into DESKTOP space."""
    thumb_width, thumb_height = thumbnail
    scale_x = desktop_rect.width / thumb_width
    scale_y = desktop_rect.height / thumb_height
    return Rect(
        x=desktop_rect.x + thumb_rect.x * scale_x,
        y=desktop_rect.y + thumb_rect.y * scale_y,
        width=thumb_rect.width * scale_x,
        height=thumb_rect.height * scale_y,
        space=CoordinateSpace.DESKTOP,
    )


def classify_change(
    previous: npt.NDArray[np.uint8],
    current: npt.NDArray[np.uint8],
    *,
    perception: PerceptionSettings,
    desktop_rect: Rect,
) -> tuple[ChangeRegion, ...]:
    """Classify the spatial difference between two grayscale thumbnails.

    This is the deterministic single-pair classifier: it never returns
    ``ANIMATION`` (that requires the temporal view in :class:`ChangeDetector`).

    Args:
        previous: The earlier 480x270 grayscale thumbnail.
        current: The later thumbnail; must share the earlier one's shape.
        perception: The ``[perception]`` thresholds (section 34).
        desktop_rect: The DESKTOP region the thumbnail represents.

    Returns:
        The changed regions in DESKTOP space, classed ``TRIVIAL``,
        ``MEANINGFUL`` or ``MAJOR``. Empty when nothing changed.

    Raises:
        ValueError: When the two thumbnails differ in shape or are not 2-D.
    """
    if previous.shape != current.shape:
        raise ValueError(f"thumbnails differ in shape: {previous.shape} vs {current.shape}")
    if previous.ndim != 2:
        raise ValueError(f"thumbnails must be 2-D grayscale, got ndim={previous.ndim}")

    height, width = previous.shape
    diff = cv2.absdiff(previous, current)
    mask = (diff > perception.pixel_threshold).astype(np.uint8)
    changed_pixels = int(np.count_nonzero(mask))
    if changed_pixels == 0:
        return ()

    total_pixels = int(mask.size)
    changed_ratio = changed_pixels / total_pixels
    is_major = changed_ratio >= perception.major_area_ratio

    regions: list[ChangeRegion] = []
    for component in _tile_components(_tile_mask(mask)):
        thumb_rect = _thumbnail_rect(component, width=width, height=height)
        if is_major:
            change_class = ChangeClass.MAJOR
        elif (
            thumb_rect.width >= perception.meaningful_min_width
            and thumb_rect.height >= perception.meaningful_min_height
        ) or (thumb_rect.area / total_pixels) >= perception.meaningful_area_ratio:
            change_class = ChangeClass.MEANINGFUL
        else:
            change_class = ChangeClass.TRIVIAL
        regions.append(
            ChangeRegion(
                rect=_to_desktop(
                    thumb_rect,
                    thumbnail=(width, height),
                    desktop_rect=desktop_rect,
                ),
                change_class=change_class,
            )
        )
    return tuple(regions)


class ChangeDetector:
    """Stateful change detector: spatial classification plus animation tracking."""

    def __init__(
        self,
        perception: PerceptionSettings,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        """Create a detector.

        Args:
            perception: The ``[perception]`` configuration section.
            clock: Monotonic clock used for animation windows.
            wall_clock: Wall clock used only for ``ScreenDelta.computed_at``.
        """
        self._perception = perception
        self._clock = clock
        self._wall_clock = wall_clock
        self._tracked: list[_TrackedRegion] = []

    def reset(self) -> None:
        """Forget all tracked regions (e.g. on a generation change)."""
        self._tracked.clear()

    def compare(self, previous: Frame, current: Frame, *, now: float | None = None) -> ScreenDelta:
        """Classify the change between two successive frames.

        Args:
            previous: The earlier frame.
            current: The later frame; must have a strictly greater ``frame_id``.
            now: Monotonic timestamp override (for deterministic tests).

        Returns:
            A :class:`ScreenDelta` whose regions are in DESKTOP space.

        Raises:
            ValueError: When ``current`` does not follow ``previous``.
        """
        if current.frame_id <= previous.frame_id:
            raise ValueError(
                f"frame ids must advance: previous={previous.frame_id} current={current.frame_id}"
            )
        moment = self._clock() if now is None else now
        regions = classify_change(
            previous.thumbnail,
            current.thumbnail,
            perception=self._perception,
            desktop_rect=current.desktop_rect,
        )
        classified = self._apply_animation(regions, moment)
        # The frame id is only a provisional state version here: the state cache
        # (core/state_cache.py) restamps the accepted state and this delta with a
        # cache-assigned version, so this value is not authoritative downstream.
        return ScreenDelta(
            from_frame_id=previous.frame_id,
            to_frame_id=current.frame_id,
            from_state_version=previous.frame_id,
            to_state_version=current.frame_id,
            generation=current.generation,
            regions=classified,
            computed_at=self._wall_clock(),
        )

    def _apply_animation(
        self, regions: tuple[ChangeRegion, ...], moment: float
    ) -> tuple[ChangeRegion, ...]:
        """Reclassify repeatedly-changing regions as ``ANIMATION``.

        A region overlapping a region seen in the previous comparison is still
        moving. If that has been true for less than ``animation_recheck_seconds``
        it is animation (moving content); a ``MAJOR`` change is never downgraded,
        because a large structural change matters regardless of repetition.
        """
        tracked: list[_TrackedRegion] = []
        result: list[ChangeRegion] = []
        for region in regions:
            overlap = next(
                (
                    previous
                    for previous in self._tracked
                    if previous.rect.intersects(region.rect)
                ),
                None,
            )
            repeated = overlap is not None and region.change_class is not ChangeClass.MAJOR
            if overlap is None or region.change_class is ChangeClass.MAJOR:
                first_seen = moment
            else:
                first_seen = overlap.first_seen
            change_class = region.change_class
            if repeated and (moment - first_seen) < self._perception.animation_recheck_seconds:
                change_class = ChangeClass.ANIMATION
            tracked.append(_TrackedRegion(rect=region.rect, first_seen=first_seen))
            result.append(ChangeRegion(rect=region.rect, change_class=change_class))
        self._tracked = tracked
        return tuple(result)


class ChangeGate:
    """Coalesces classified changes into perception-job decisions (section 34).

    A change is emitted immediately when it is the first in its debounce window;
    further changes inside the window are suppressed. Because the gate re-checks
    on every delta, the next change after the window ends produces exactly one
    coalesced follow-up perception -- never a storm, never a missed change.
    """

    def __init__(
        self,
        perception: PerceptionSettings,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Create a gate from the ``[perception]`` configuration section."""
        self._perception = perception
        self._clock = clock
        self._last_emit: float | None = None

    def reset(self) -> None:
        """Forget the last emit time."""
        self._last_emit = None

    @property
    def interval_ms(self) -> float:
        """The effective minimum interval between emitted perceptions."""
        rate_floor_ms = 1000.0 / self._perception.max_perception_jobs_per_second
        return max(self._perception.debounce_short_ms, rate_floor_ms)

    def should_perceive(self, delta: ScreenDelta, *, now: float | None = None) -> bool:
        """Decide whether ``delta`` justifies a perception job right now."""
        if delta.change_class is ChangeClass.NONE:
            return False
        moment = self._clock() if now is None else now
        if delta.change_class is ChangeClass.MAJOR:
            interval_ms = max(float(self._perception.debounce_major_ms), self.interval_ms)
        else:
            interval_ms = self.interval_ms
        if self._last_emit is None or (moment - self._last_emit) * 1000.0 >= interval_ms:
            self._last_emit = moment
            return True
        return False
