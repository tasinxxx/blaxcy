"""Mouse sequence tests (specification sections 31, 48, 49).

These assert the *ordered* physical sequence and its failure semantics, not that
"a function returned". The properties that matter: coordinates are transformed
and clamped before injection, motion is flushed before a button is pressed, the
pointer is verified/corrected where readback exists, and a failed move can never
be followed by a click.
"""

from __future__ import annotations

import pytest

from config.settings import InputSettings
from control.backends.base import PointerButton
from control.mouse import MouseController
from core.calibration import identity_geometry_map
from schemas.enums import CoordinateSpace, ErrorCode
from schemas.errors import BlaxcyError
from schemas.geometry import GeometryMap, MonitorGeometry, MonitorLayout, Point, Rect
from tests.harness.fake_input_backend import FakeInputBackend


class SleepRecorder:
    """Records requested sleeps instead of waiting."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def _layout() -> MonitorLayout:
    """A single 1920x1080 monitor at the desktop origin."""
    return MonitorLayout(
        monitors=(MonitorGeometry(monitor_id=0, width=1920, height=1080, is_primary=True),)
    )


def _geometry(input_bounds: Rect | None = None) -> GeometryMap:
    """An X11-style identity geometry map (INPUT == DESKTOP)."""
    layout = _layout()
    if input_bounds is not None:
        return GeometryMap(layout=layout, input_bounds=input_bounds)
    return identity_geometry_map(layout)


def _point(x: float, y: float) -> Point:
    return Point(x=x, y=y, space=CoordinateSpace.DESKTOP)


def _settings(**overrides: object) -> InputSettings:
    return InputSettings.model_validate(overrides)


def _controller(
    backend: FakeInputBackend,
    *,
    geometry: GeometryMap | None = None,
    settings: InputSettings | None = None,
    sleeps: SleepRecorder | None = None,
) -> MouseController:
    return MouseController(
        backend,
        geometry if geometry is not None else _geometry(),
        settings,
        sleep=sleeps if sleeps is not None else SleepRecorder(),
    )


# -- Section 48: move ---------------------------------------------------------


def test_move_injects_transformed_coordinate_and_verifies_by_readback() -> None:
    """A move injects the point and is verified against a real readback."""
    backend = FakeInputBackend()
    result = _controller(backend).move_to(_point(500.0, 300.0))
    assert result.ok is True
    assert result.injected == (500, 300)
    assert result.readback == (500, 300)
    assert result.verified is True
    assert backend.payloads("move") == [("move", 500, 300)]


def test_move_clamps_an_off_desktop_point_before_injection() -> None:
    """An off-desktop target is clamped, never injected off-screen (section 48)."""
    backend = FakeInputBackend()
    result = _controller(backend).move_to(_point(-100.0, -100.0))
    assert result.injected == (0, 0)
    result_far = _controller(backend).move_to(_point(5000.0, 5000.0))
    assert result_far.injected[0] <= 1920
    assert result_far.injected[1] <= 1080


def test_move_respects_known_input_bounds() -> None:
    """When a backend declares smaller INPUT bounds, the integer stays inside."""
    backend = FakeInputBackend()
    geometry = _geometry(input_bounds=Rect(x=0.0, y=0.0, width=800.0, height=600.0, space=CoordinateSpace.INPUT))
    result = _controller(backend, geometry=geometry).move_to(_point(5000.0, 5000.0))
    assert result.injected == (799, 599)


def test_move_without_readback_is_honestly_unverified() -> None:
    """No readback support means verified=False and an explicit reason."""
    backend = FakeInputBackend(readback_supported=False)
    result = _controller(backend).move_to(_point(10.0, 10.0))
    assert result.ok is True
    assert result.readback is None
    assert result.readback_available is False
    assert result.verified is False
    assert result.reason is not None


def test_move_corrects_once_when_readback_is_off_target() -> None:
    """A bounded correction re-injects until the readback matches (section 48)."""
    reads = {"count": 0}

    def readback() -> tuple[int, int]:
        reads["count"] += 1
        return (10, 10) if reads["count"] == 1 else (100, 100)

    backend = FakeInputBackend(readback_fn=readback)
    result = _controller(backend, settings=_settings(max_pointer_corrections=1)).move_to(
        _point(100.0, 100.0)
    )
    assert result.ok is True
    assert result.corrected is True
    assert result.corrections == 1
    assert result.verified is True
    assert backend.payloads("move") == [("move", 100, 100), ("move", 100, 100)]


def test_move_fails_closed_when_readback_never_reaches_the_target() -> None:
    """Exhausted corrections plus a wrong readback is a failure, not a warning."""
    backend = FakeInputBackend(readback_fn=lambda: (10, 10))
    result = _controller(backend, settings=_settings(max_pointer_corrections=1)).move_to(
        _point(100.0, 100.0)
    )
    assert result.ok is False
    assert result.verified is False
    assert result.corrections == 1
    assert result.reason is not None and "off target" in result.reason


# -- Section 48: click / scroll ----------------------------------------------


def test_click_sequence_is_move_settle_press_release_settle() -> None:
    """The click order and the two settle delays are exactly as specified."""
    backend = FakeInputBackend()
    sleeps = SleepRecorder()
    result = _controller(backend, sleeps=sleeps).click(_point(200.0, 200.0))
    assert result.ok is True
    assert result.button is PointerButton.LEFT
    assert backend.event_names() == ["move", "flush", "press", "flush", "release", "flush"]
    assert sleeps.calls == [0.04, 0.12]


def test_click_never_presses_when_the_move_failed() -> None:
    """A failed move aborts the click before any button is pressed."""
    backend = FakeInputBackend(readback_fn=lambda: (0, 0))
    result = _controller(backend).click(_point(200.0, 200.0))
    assert result.ok is False
    assert "press" not in backend.event_names()
    assert result.reason is not None


def test_double_click_presses_twice_and_right_click_uses_button_3() -> None:
    """Click count and button identity are honoured."""
    backend = FakeInputBackend()
    double = _controller(backend).double_click(_point(1.0, 1.0))
    assert double.count == 2
    assert [p[1] for p in backend.payloads("press")] == [1, 1]

    right_backend = FakeInputBackend()
    right = _controller(right_backend).right_click(_point(2.0, 2.0))
    assert right.button is PointerButton.RIGHT
    assert [p[1] for p in right_backend.payloads("press")] == [3]


def test_scroll_moves_then_injects_wheel_clicks() -> None:
    """Scrolling positions the pointer first, then injects the wheel event."""
    backend = FakeInputBackend()
    result = _controller(backend).scroll(_point(50.0, 60.0), vertical=-2, horizontal=1)
    assert result.ok is True
    assert backend.payloads("scroll") == [("scroll", -2, 1)]


def test_click_rejects_a_zero_count() -> None:
    """A click count below one is a caller error, not a silent no-op."""
    with pytest.raises(ValueError):
        _controller(FakeInputBackend()).click(_point(0.0, 0.0), count=0)


# -- Section 49: drag ---------------------------------------------------------


def test_drag_presses_moves_and_releases_without_leaving_the_button_held() -> None:
    """The drag interpolation runs between a real press and release."""
    backend = FakeInputBackend()
    result = _controller(backend).drag(_point(100.0, 100.0), _point(200.0, 150.0), steps=2)
    assert result.ok is True
    assert backend.event_names() == [
        "move",
        "flush",
        "press",
        "flush",
        "move",
        "flush",
        "move",
        "flush",
        "release",
        "flush",
    ]
    assert backend.held_buttons == []
    assert result.destination == (200, 150)


def test_drag_releases_the_button_when_a_move_fails_mid_way() -> None:
    """Section 52: an unexpected failure mid-drag never leaves input held."""
    backend = FakeInputBackend(raise_on_move=2)
    with pytest.raises(BlaxcyError) as excinfo:
        _controller(backend).drag(_point(100.0, 100.0), _point(200.0, 200.0), steps=3)
    assert excinfo.value.code is ErrorCode.INTERNAL_ERROR
    assert backend.held_buttons == []
    assert "release" in backend.event_names()


def test_drag_rejects_invalid_steps() -> None:
    """A drag needs at least one interpolation step."""
    with pytest.raises(ValueError):
        _controller(FakeInputBackend()).drag(_point(0.0, 0.0), _point(1.0, 1.0), steps=0)


# -- Hygiene ------------------------------------------------------------------


def test_release_all_delegates_to_the_backend() -> None:
    """The section 52/63 hygiene hook reaches the backend."""
    backend = FakeInputBackend()
    _controller(backend).release_all()
    assert backend.event_names() == ["release_all"]


def test_close_releases_and_closes() -> None:
    """Closing releases input before tearing the backend down."""
    backend = FakeInputBackend()
    _controller(backend).close()
    assert "release_all" in backend.event_names()
    assert backend.event_names()[-1] == "close"
