"""Change detection and classification (specification section 34).

The spatial classifier is exercised directly (pure function), and the temporal
animation layer plus the debounce gate are driven with explicit timestamps so
the tests never depend on wall-clock timing.
"""

from __future__ import annotations

import numpy as np
import numpy.typing as npt
import pytest

from config.settings import PerceptionSettings
from core.change_detector import (
    TILE_GRID_COLS,
    TILE_GRID_ROWS,
    ChangeDetector,
    ChangeGate,
    classify_change,
)
from core.frame_engine import Frame
from schemas.enums import ChangeClass, CoordinateSpace
from schemas.geometry import Rect
from schemas.screen_state import ChangeRegion, ScreenDelta

_THUMB_WIDTH = 480
_THUMB_HEIGHT = 270
_DESKTOP = Rect(x=0.0, y=0.0, width=1366.0, height=768.0, space=CoordinateSpace.DESKTOP)


def _blank() -> npt.NDArray[np.uint8]:
    """A blank 480x270 grayscale thumbnail (the canonical section 33 geometry)."""
    return np.zeros((_THUMB_HEIGHT, _THUMB_WIDTH), dtype=np.uint8)


def _with_block(
    base: npt.NDArray[np.uint8], *, x: int, y: int, width: int, height: int
) -> npt.NDArray[np.uint8]:
    """Return a copy of ``base`` with a filled white block."""
    image = base.copy()
    image[y : y + height, x : x + width] = 255
    return image


def _with_patch(
    base: npt.NDArray[np.uint8],
    *,
    value: int,
    x: int = 10,
    y: int = 10,
    width: int = 190,
    height: int = 90,
) -> npt.NDArray[np.uint8]:
    """Return a copy of ``base`` with a large patch set to ``value``.

    Animating the *content* of a patch (rather than moving it) keeps its
    bounding box stable while still producing a visually MEANINGFUL change, so
    the same region can be observed changing repeatedly.
    """
    image = base.copy()
    image[y : y + height, x : x + width] = value
    return image


def _frame(
    thumbnail: npt.NDArray[np.uint8],
    frame_id: int,
    *,
    generation: int = 0,
    desktop: Rect = _DESKTOP,
) -> Frame:
    """Wrap a thumbnail in a Frame for detector tests."""
    return Frame(
        frame_id=frame_id,
        generation=generation,
        captured_at_monotonic=float(frame_id),
        captured_at=0.0,
        desktop_rect=desktop,
        image=np.zeros((thumbnail.shape[0], thumbnail.shape[1], 4), dtype=np.uint8),
        thumbnail=thumbnail,
        latency_ms=1.0,
        backend="fake",
    )


def _delta(change_class: ChangeClass) -> ScreenDelta:
    """Build a ScreenDelta whose single region carries ``change_class``."""
    regions: tuple[ChangeRegion, ...] = ()
    if change_class is not ChangeClass.NONE:
        regions = (
            ChangeRegion(
                rect=Rect(x=0.0, y=0.0, width=10.0, height=10.0, space=CoordinateSpace.DESKTOP),
                change_class=change_class,
            ),
        )
    return ScreenDelta(
        from_frame_id=1,
        to_frame_id=2,
        from_state_version=1,
        to_state_version=2,
        generation=0,
        regions=regions,
        computed_at=0.0,
    )


# ---------------------------------------------------------------------------
# Spatial classification.
# ---------------------------------------------------------------------------


def test_identical_thumbnails_have_no_change() -> None:
    """No changed pixels means no regions at all."""
    assert classify_change(_blank(), _blank(), perception=PerceptionSettings(), desktop_rect=_DESKTOP) == ()


def test_tile_grid_matches_the_specification_geometry() -> None:
    """Section 34: a 30x30 tile grid over a logical 16:9 thumbnail."""
    assert (TILE_GRID_COLS, TILE_GRID_ROWS) == (30, 30)
    assert _THUMB_WIDTH % TILE_GRID_COLS == 0
    assert _THUMB_HEIGHT % TILE_GRID_ROWS == 0


def test_tiny_change_is_trivial() -> None:
    """A single changed pixel cannot be a structural change."""
    current = _blank()
    current[0, 0] = 255
    regions = classify_change(_blank(), current, perception=PerceptionSettings(), desktop_rect=_DESKTOP)
    assert len(regions) == 1
    assert regions[0].change_class is ChangeClass.TRIVIAL


def test_structural_block_is_meaningful() -> None:
    """A region meeting the minimum size is MEANINGFUL (section 34)."""
    current = _with_block(_blank(), x=10, y=10, width=90, height=40)
    regions = classify_change(_blank(), current, perception=PerceptionSettings(), desktop_rect=_DESKTOP)
    assert len(regions) == 1
    assert regions[0].change_class is ChangeClass.MEANINGFUL


def test_area_ratio_alone_can_make_a_change_meaningful() -> None:
    """A region below the min height but above the area ratio is MEANINGFUL."""
    current = _with_block(_blank(), x=0, y=0, width=112, height=18)
    regions = classify_change(_blank(), current, perception=PerceptionSettings(), desktop_rect=_DESKTOP)
    assert len(regions) == 1
    assert regions[0].change_class is ChangeClass.MEANINGFUL


def test_large_change_is_major() -> None:
    """A change covering at least major_area_ratio is MAJOR."""
    current = _with_block(_blank(), x=0, y=0, width=_THUMB_WIDTH, height=90)
    regions = classify_change(_blank(), current, perception=PerceptionSettings(), desktop_rect=_DESKTOP)
    assert len(regions) == 1
    assert regions[0].change_class is ChangeClass.MAJOR


def test_regions_are_reported_in_desktop_space() -> None:
    """Section 45/33.2 consumers compare regions against DESKTOP bboxes."""
    current = _with_block(_blank(), x=0, y=0, width=112, height=18)
    region = classify_change(_blank(), current, perception=PerceptionSettings(), desktop_rect=_DESKTOP)[0]
    assert region.rect.space is CoordinateSpace.DESKTOP
    assert region.rect.x == 0.0
    assert region.rect.y == 0.0
    # 112 thumbnail pixels across 1366 desktop pixels over a 480-pixel thumbnail.
    assert region.rect.width == pytest.approx(112 * 1366.0 / 480.0)


def test_shape_mismatch_is_rejected() -> None:
    """Two thumbnails of different shape cannot be compared."""
    with pytest.raises(ValueError):
        classify_change(
            _blank(),
            np.zeros((10, 10), dtype=np.uint8),
            perception=PerceptionSettings(),
            desktop_rect=_DESKTOP,
        )


def test_non_2d_input_is_rejected() -> None:
    """A colour thumbnail is a caller bug, not something to guess about."""
    with pytest.raises(ValueError):
        classify_change(
            np.zeros((4, 4, 3), dtype=np.uint8),
            np.zeros((4, 4, 3), dtype=np.uint8),
            perception=PerceptionSettings(),
            desktop_rect=_DESKTOP,
        )


# ---------------------------------------------------------------------------
# Temporal classification (animation).
# ---------------------------------------------------------------------------


def test_repeated_change_in_the_same_region_is_animation() -> None:
    """Section 34: content that keeps moving in place is ANIMATION."""
    detector = ChangeDetector(PerceptionSettings())
    base = _blank()
    first = _with_patch(base, value=255)
    second = _with_patch(base, value=128)

    initial = detector.compare(_frame(base, 1), _frame(first, 2), now=0.0)
    assert initial.change_class is ChangeClass.MEANINGFUL

    moving = detector.compare(_frame(first, 2), _frame(second, 3), now=1.0)
    assert moving.change_class is ChangeClass.ANIMATION


def test_animation_window_expires_back_to_structural() -> None:
    """After animation_recheck_seconds the region is a structural change again."""
    detector = ChangeDetector(PerceptionSettings())
    base = _blank()
    first = _with_patch(base, value=255)
    second = _with_patch(base, value=128)
    third = _with_patch(base, value=64)

    detector.compare(_frame(base, 1), _frame(first, 2), now=0.0)
    assert (
        detector.compare(_frame(first, 2), _frame(second, 3), now=1.0).change_class
        is ChangeClass.ANIMATION
    )
    settled = detector.compare(_frame(second, 3), _frame(third, 4), now=5.0)
    assert settled.change_class is ChangeClass.MEANINGFUL


def test_major_change_is_never_downgraded_to_animation() -> None:
    """A large structural change matters regardless of repetition."""
    detector = ChangeDetector(PerceptionSettings())
    base = _blank()
    big = _with_block(base, x=0, y=0, width=_THUMB_WIDTH, height=90)
    bigger = _with_block(base, x=0, y=0, width=_THUMB_WIDTH, height=180)

    assert detector.compare(_frame(base, 1), _frame(big, 2), now=0.0).change_class is ChangeClass.MAJOR
    assert (
        detector.compare(_frame(big, 2), _frame(bigger, 3), now=0.5).change_class
        is ChangeClass.MAJOR
    )


def test_detector_requires_advancing_frame_ids() -> None:
    """A non-advancing comparison is a caller bug and must raise."""
    detector = ChangeDetector(PerceptionSettings())
    frame = _frame(_blank(), 1)
    with pytest.raises(ValueError):
        detector.compare(frame, frame)


def test_reset_forgets_tracked_regions() -> None:
    """A generation change can discard animation tracking."""
    detector = ChangeDetector(PerceptionSettings())
    base = _blank()
    first = _with_patch(base, value=255)
    second = _with_patch(base, value=128)

    detector.compare(_frame(base, 1), _frame(first, 2), now=0.0)
    detector.reset()
    after_reset = detector.compare(_frame(first, 2), _frame(second, 3), now=1.0)
    assert after_reset.change_class is ChangeClass.MEANINGFUL


# ---------------------------------------------------------------------------
# Debounce gate.
# ---------------------------------------------------------------------------


def test_gate_ignores_no_change() -> None:
    """A NONE delta never justifies a perception job."""
    gate = ChangeGate(PerceptionSettings())
    assert gate.should_perceive(_delta(ChangeClass.NONE), now=0.0) is False


def test_gate_debounces_rapid_meaningful_changes() -> None:
    """Section 34: rapid repeats coalesce instead of storming perception."""
    perception = PerceptionSettings(max_perception_jobs_per_second=100)
    gate = ChangeGate(perception)
    delta = _delta(ChangeClass.MEANINGFUL)

    assert gate.should_perceive(delta, now=0.0) is True
    assert gate.should_perceive(delta, now=0.1) is False
    # Exactly one coalesced follow-up perception after the window.
    assert gate.should_perceive(delta, now=0.13) is True


def test_gate_uses_the_longer_major_debounce() -> None:
    """A MAJOR change suppresses repeats for debounce_major_ms."""
    perception = PerceptionSettings(max_perception_jobs_per_second=100)
    gate = ChangeGate(perception)
    delta = _delta(ChangeClass.MAJOR)

    assert gate.should_perceive(delta, now=0.0) is True
    assert gate.should_perceive(delta, now=0.25) is False
    assert gate.should_perceive(delta, now=0.31) is True


def test_gate_honours_the_perception_rate_cap() -> None:
    """max_perception_jobs_per_second is a hard floor on the interval."""
    perception = PerceptionSettings(debounce_short_ms=0, max_perception_jobs_per_second=5)
    gate = ChangeGate(perception)
    assert gate.interval_ms == pytest.approx(200.0)
    delta = _delta(ChangeClass.TRIVIAL)
    assert gate.should_perceive(delta, now=0.0) is True
    assert gate.should_perceive(delta, now=0.1) is False
    assert gate.should_perceive(delta, now=0.2) is True
