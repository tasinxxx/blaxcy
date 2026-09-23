"""Screen state and deltas (specification sections 32, 34, 45, 65)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from schemas.elements import UIElement
from schemas.enums import ChangeClass, CoordinateSpace, PerceptionSource, UIRole
from schemas.geometry import MonitorGeometry, MonitorLayout, Rect
from schemas.screen_state import ChangeRegion, ScreenDelta, ScreenState, classify_delta


def _layout() -> MonitorLayout:
    """A single 800x600 monitor."""
    return MonitorLayout(monitors=(MonitorGeometry(monitor_id=0, width=800, height=600),))


def _element(element_id: str) -> UIElement:
    """A minimal element for state population."""
    return UIElement(element_id=element_id, role=UIRole.BUTTON, source=PerceptionSource.ATSPI)


def _state(
    *,
    frame_id: int = 1,
    state_version: int = 1,
    generation: int = 0,
    monotonic: float = 100.0,
    elements: tuple[UIElement, ...] = (),
) -> ScreenState:
    """Build a ScreenState with sensible defaults."""
    return ScreenState(
        frame_id=frame_id,
        state_version=state_version,
        generation=generation,
        timestamp=1_700_000_000.0,
        monotonic=monotonic,
        layout=_layout(),
        elements=elements,
    )


def test_age_and_staleness_use_the_monotonic_clock() -> None:
    """Section 45: state age is measured monotonically, not by wall clock."""
    state = _state(monotonic=100.0)
    assert state.age_ms(now_monotonic=100.5) == 500.0
    assert state.is_stale(1500, now_monotonic=101.0) is False
    assert state.is_stale(1500, now_monotonic=102.0) is True
    assert state.is_fresh(1500, now_monotonic=101.0) is True


def test_age_never_goes_negative() -> None:
    """A clock reading before capture yields zero, not a negative age."""
    assert _state(monotonic=100.0).age_ms(now_monotonic=99.0) == 0.0


def test_accepts_only_strictly_newer_frames() -> None:
    """Section 32 staleness rules: frame must advance, generation must not regress."""
    current = _state(frame_id=5, generation=2)
    assert _state(frame_id=6, generation=2).is_newer_than(current) is True
    assert _state(frame_id=6, generation=3).is_newer_than(current) is True
    assert _state(frame_id=5, generation=2).is_newer_than(current) is False
    assert _state(frame_id=4, generation=2).is_newer_than(current) is False
    assert _state(frame_id=6, generation=1).is_newer_than(current) is False


def test_state_rejects_duplicate_element_ids() -> None:
    """Two elements cannot share an id within one observation."""
    with pytest.raises(ValidationError):
        _state(elements=(_element("a"), _element("a")))


def test_element_lookup() -> None:
    """Elements are addressable by id."""
    state = _state(elements=(_element("a"), _element("b")))
    assert state.element_by_id("b") is not None
    assert state.element_by_id("z") is None


def test_delta_change_class_is_the_most_severe_region() -> None:
    """The overall class can never be lower than its worst region."""
    region_trivial = ChangeRegion(
        rect=Rect(x=0, y=0, width=10, height=10, space=CoordinateSpace.DESKTOP),
        change_class=ChangeClass.TRIVIAL,
    )
    region_major = ChangeRegion(
        rect=Rect(x=100, y=100, width=50, height=50, space=CoordinateSpace.DESKTOP),
        change_class=ChangeClass.MAJOR,
    )
    delta = ScreenDelta(
        from_frame_id=1,
        to_frame_id=2,
        from_state_version=1,
        to_state_version=2,
        generation=0,
        regions=(region_trivial, region_major),
        computed_at=0.0,
    )
    assert delta.change_class is ChangeClass.MAJOR
    assert delta.is_structural is True


def test_empty_delta_is_none() -> None:
    """No regions means no change at all."""
    delta = classify_delta(
        previous=_state(frame_id=1),
        current=_state(frame_id=2, state_version=2),
        regions=(),
        computed_at=0.0,
    )
    assert delta.change_class is ChangeClass.NONE
    assert delta.is_structural is False


def test_delta_affects_overlapping_region() -> None:
    """Section 33.2: a speculative result dies if its region changed."""
    region = ChangeRegion(
        rect=Rect(x=0, y=0, width=100, height=100, space=CoordinateSpace.DESKTOP),
        change_class=ChangeClass.MEANINGFUL,
    )
    delta = ScreenDelta(
        from_frame_id=1,
        to_frame_id=2,
        from_state_version=1,
        to_state_version=2,
        generation=0,
        regions=(region,),
        computed_at=0.0,
    )
    inside = Rect(x=50, y=50, width=10, height=10, space=CoordinateSpace.DESKTOP)
    outside = Rect(x=500, y=500, width=10, height=10, space=CoordinateSpace.DESKTOP)
    assert delta.affects(inside) is True
    assert delta.affects(outside) is False


def test_delta_rejects_backwards_frame_ids() -> None:
    """A delta cannot describe time flowing backwards."""
    with pytest.raises(ValidationError):
        ScreenDelta(
            from_frame_id=5,
            to_frame_id=4,
            from_state_version=5,
            to_state_version=4,
            generation=0,
            computed_at=0.0,
        )


def test_model_context_is_compact() -> None:
    """The Brain context view summarises rather than dumping elements."""
    state = _state(elements=(_element("a"), _element("b")), frame_id=9)
    context = state.to_model_context()
    assert context["element_count"] == 2
    assert context["frame_id"] == 9
    assert "elements" not in context
