"""OCR: bounded, fallback-only recognition (specification sections 39, 40, 55).

Most tests drive a fake backend so the *engine's* behaviour -- region selection,
the deadline, the pixel-keyed cache, password exclusion, confidence derivation
and element construction -- is verified without depending on Tesseract. One
functional test exercises the real ``pytesseract`` backend against rendered text
and is skipped when the engine is not installed, so its result is never faked.
"""

from __future__ import annotations

import numpy as np
import numpy.typing as npt
import pytest

from config.settings import OcrSettings
from core.frame_engine import Frame
from core.ocr import (
    OcrCache,
    OcrEngine,
    OcrRegion,
    RawWord,
    password_regions,
)
from schemas.elements import UIElement
from schemas.enums import (
    CapabilityStatus,
    CoordinateSpace,
    ErrorCode,
    PerceptionSource,
    UIRole,
)
from schemas.errors import BlaxcyError
from schemas.geometry import Rect

_DESKTOP_WIDTH = 1920
_DESKTOP_HEIGHT = 1080
_DESKTOP = Rect(
    x=0.0, y=0.0, width=float(_DESKTOP_WIDTH), height=float(_DESKTOP_HEIGHT), space=CoordinateSpace.DESKTOP
)


class FakeClock:
    """A monotonic clock that advances a fixed step on every read."""

    def __init__(self, step: float = 0.0) -> None:
        self._now = 0.0
        self._step = step

    def __call__(self) -> float:
        self._now += self._step
        return self._now


class FakeBackend:
    """An OCR backend returning fixed words, with call accounting."""

    name = "fake"

    def __init__(self, words: tuple[RawWord, ...] = (), *, fail: bool = False) -> None:
        self._words = words
        self._fail = fail
        self.calls: list[tuple[int, float | None]] = []

    def recognize(
        self,
        image: npt.NDArray[np.uint8],
        *,
        language: str,
        timeout: float | None,
    ) -> tuple[RawWord, ...]:
        self.calls.append((int(image.size), timeout))
        if self._fail:
            raise BlaxcyError(ErrorCode.OCR_FAILED, "fake backend failure")
        return self._words


def _word(text: str = "Play", *, conf: float = 1.0, left: int = 10, top: int = 5) -> RawWord:
    """A raw word whose box is in crop-pixel coordinates."""
    return RawWord(text=text, left=left, top=top, width=40, height=18, confidence=conf)


def _frame(*, frame_id: int = 0, generation: int = 0) -> Frame:
    """A frame whose desktop matches its pixels 1:1, filled with a solid colour."""
    image = np.full((_DESKTOP_HEIGHT, _DESKTOP_WIDTH, 4), 200, dtype=np.uint8)
    return Frame(
        frame_id=frame_id,
        generation=generation,
        captured_at_monotonic=0.0,
        captured_at=0.0,
        desktop_rect=_DESKTOP,
        image=image,
        thumbnail=np.zeros((270, 480), dtype=np.uint8),
        latency_ms=0.0,
        backend="fake",
    )


def _rect(x: float, y: float, width: float, height: float) -> Rect:
    """A DESKTOP-space rectangle."""
    return Rect(x=x, y=y, width=width, height=height, space=CoordinateSpace.DESKTOP)


def _settings(*, max_regions: int = 6, deadline_ms: int = 300) -> OcrSettings:
    """OcrSettings using section 40 defaults, overridable per test."""
    return OcrSettings(max_regions=max_regions, deadline_ms=deadline_ms)


def _password_element(bbox: Rect | None = None) -> UIElement:
    """A password input element (its text is always None)."""
    return UIElement(
        element_id="pw",
        role=UIRole.PASSWORD_INPUT,
        password=True,
        bbox=bbox,
        center=bbox.center if bbox is not None else None,
        coordinate_space=CoordinateSpace.DESKTOP if bbox is not None else None,
        source=PerceptionSource.ATSPI,
        confidence=0.95,
    )


# -- Region selection (section 40) ------------------------------------------


def test_select_regions_applies_size_and_count_limits() -> None:
    """Too-small regions, oversized regions and surplus regions are reported."""
    engine = OcrEngine(_settings(max_regions=2), backend=FakeBackend(), clock=FakeClock())
    candidates = [
        _rect(0, 0, 200, 20),  # below minimum_height (14)? no, 20 ok -- keep as valid
        _rect(300, 0, 30, 40),  # below minimum_width (40) -> too small
        _rect(0, 100, 1000, 1000),  # larger than 1.5% of the desktop -> oversized
        _rect(400, 0, 300, 60),
        _rect(800, 0, 400, 60),
        _rect(1200, 0, 500, 60),
    ]
    selection = engine.select_regions(candidates, desktop=_DESKTOP)

    assert selection.too_small == 1
    assert selection.oversized == 1
    assert selection.over_limit == 2
    assert len(selection.regions) == 2
    # Largest-first ordering: the 500x60 region wins over the 400x60 one.
    assert selection.regions[0].width == 500


def test_select_regions_is_deterministic_for_ties() -> None:
    """Equal-area regions are ordered top-to-bottom, then left-to-right."""
    engine = OcrEngine(_settings(max_regions=10), backend=FakeBackend(), clock=FakeClock())
    candidates = [_rect(500, 0, 200, 50), _rect(0, 100, 200, 50), _rect(0, 0, 200, 50)]
    selection = engine.select_regions(candidates, desktop=_DESKTOP)
    assert [(r.x, r.y) for r in selection.regions] == [(0, 0), (500, 0), (0, 100)]


def test_select_regions_drops_excluded_regions() -> None:
    """An excluded region is dropped and counted before ordering."""
    engine = OcrEngine(_settings(), backend=FakeBackend(), clock=FakeClock())
    selection = engine.select_regions(
        [_rect(0, 0, 200, 50), _rect(300, 0, 200, 50)],
        desktop=_DESKTOP,
        excluded=[_rect(290, 0, 220, 60)],
    )
    assert selection.excluded == 1
    assert [r.x for r in selection.regions] == [0]


def test_should_process_rejects_tiny_and_oversized() -> None:
    """The public predicate agrees with the selection limits."""
    engine = OcrEngine(_settings(), backend=FakeBackend(), clock=FakeClock())
    assert engine.should_process(_rect(0, 0, 200, 50), desktop=_DESKTOP) is True
    assert engine.should_process(_rect(0, 0, 20, 50), desktop=_DESKTOP) is False
    assert engine.should_process(_rect(0, 0, 1200, 900), desktop=_DESKTOP) is False


# -- Recognition and caching -------------------------------------------------


def test_recognize_maps_words_into_desktop_space() -> None:
    """Word boxes are scaled from crop pixels into DESKTOP coordinates."""
    backend = FakeBackend((_word(left=10, top=5),))
    engine = OcrEngine(_settings(), backend=backend, clock=FakeClock())
    rect = _rect(100, 50, 200, 100)

    result = engine.recognize(_frame(), candidates=[rect])

    assert len(result.regions) == 1
    region = result.regions[0]
    assert region.text == "Play"
    assert region.words[0].rect == _rect(110, 55, 40, 18)
    # One word -> source_consistency 1.0 -> OCR base rate 0.75 (section 39).
    assert region.confidence == pytest.approx(0.75)
    assert result.truncated_by_deadline is False


def test_recognize_confidence_falls_with_disagreement() -> None:
    """Several words make the region's confidence the mean word confidence."""
    backend = FakeBackend((_word(conf=1.0, left=0), _word(conf=0.5, left=60)))
    engine = OcrEngine(_settings(), backend=backend, clock=FakeClock())

    result = engine.recognize(_frame(), candidates=[_rect(0, 0, 300, 60)])

    assert result.regions[0].confidence == pytest.approx(0.75 * 0.75)


def test_cache_hit_avoids_second_backend_call() -> None:
    """Identical geometry and pixels are OCR'd once; the hit is marked cached."""
    backend = FakeBackend((_word(),))
    engine = OcrEngine(_settings(), backend=backend, clock=FakeClock())
    rect = _rect(0, 0, 300, 60)

    first = engine.recognize(_frame(), candidates=[rect])
    second = engine.recognize(_frame(frame_id=1), candidates=[rect])

    assert len(backend.calls) == 1
    assert first.regions[0].cached is False
    assert second.regions[0].cached is True
    assert engine.diagnostics()["cache_hits"] == 1


def test_deadline_truncates_and_reports_honestly() -> None:
    """A spent deadline stops the call and sets truncated_by_deadline."""
    # A 1 ms deadline with a 1 ms-per-read clock expires before the first region.
    clock = FakeClock(step=0.001)
    engine = OcrEngine(_settings(deadline_ms=1), backend=FakeBackend((_word(),)), clock=clock)

    result = engine.recognize(_frame(), candidates=[_rect(0, 0, 300, 60)])

    assert result.regions == ()
    assert result.truncated_by_deadline is True


def test_all_regions_failing_raises_ocr_failed() -> None:
    """When every attempted region fails, the call is an OCR_FAILED error."""
    engine = OcrEngine(
        _settings(), backend=FakeBackend(fail=True), clock=FakeClock()
    )
    with pytest.raises(BlaxcyError) as excinfo:
        engine.recognize(_frame(), candidates=[_rect(0, 0, 300, 60)])
    assert excinfo.value.code is ErrorCode.OCR_FAILED


# -- Password exclusion (sections 42, 55) ------------------------------------


def test_password_elements_are_never_ocrd() -> None:
    """A candidate overlapping a password element is excluded before OCR runs."""
    backend = FakeBackend((_word(),))
    engine = OcrEngine(_settings(), backend=backend, clock=FakeClock())
    password_box = _rect(400, 100, 200, 40)

    result = engine.recognize(
        _frame(),
        candidates=[_rect(0, 0, 300, 60), password_box],
        password_elements=[_password_element(password_box)],
    )

    assert result.selection.excluded == 1
    assert len(backend.calls) == 1  # only the non-password region was sent
    assert all(not password_box.intersects(r.rect) for r in result.regions)


def test_password_regions_helper_skips_geometryless_elements() -> None:
    """Only password elements with geometry contribute exclusion rectangles."""
    box = _rect(10, 10, 100, 30)
    assert password_regions([_password_element(box), _password_element(None)]) == (box,)


def test_read_text_returns_none_over_an_excluded_region() -> None:
    """The single-region reader refuses an excluded rectangle outright."""
    engine = OcrEngine(_settings(), backend=FakeBackend((_word(),)), clock=FakeClock())
    box = _rect(0, 0, 300, 60)
    assert engine.read_text(_frame(), rect=box, excluded=[box]) is None


def test_read_text_returns_recognised_text() -> None:
    """The single-region reader returns the recognised text."""
    engine = OcrEngine(_settings(), backend=FakeBackend((_word("Hello"),)), clock=FakeClock())
    assert engine.read_text(_frame(), rect=_rect(0, 0, 300, 60)) == "Hello"


# -- Elements ----------------------------------------------------------------


def test_as_elements_builds_non_clickable_ocr_fragments() -> None:
    """OCR elements are TEXT_FRAGMENTs that prove visibility, never clickability."""
    engine = OcrEngine(_settings(), backend=FakeBackend((_word("Search"),)), clock=FakeClock())
    rect = _rect(0, 0, 300, 60)
    result = engine.recognize(_frame(frame_id=7), candidates=[rect])

    elements = engine.as_elements(result, frame_id=7, generation=0, owner_app="firefox")

    assert len(elements) == 1
    element = elements[0]
    assert element.role is UIRole.TEXT_FRAGMENT
    assert element.text == "Search"
    assert element.source is PerceptionSource.OCR
    assert element.clickable is False and element.effective_clickable is False
    assert element.frame_id == 7
    assert element.owner_app == "firefox"
    assert element.element_id.startswith("ocr:")


def test_element_id_is_stable_for_the_same_region() -> None:
    """The same geometry and text always yield the same element id."""
    engine = OcrEngine(_settings(), backend=FakeBackend((_word("Go"),)), clock=FakeClock())
    rect = _rect(0, 0, 300, 60)
    first = engine.as_elements(engine.recognize(_frame(), candidates=[rect]))
    second = engine.as_elements(engine.recognize(_frame(frame_id=1), candidates=[rect]))
    assert first[0].element_id == second[0].element_id


# -- Cache -------------------------------------------------------------------


def test_cache_evicts_least_recently_used() -> None:
    """The cache is bounded and evicts the oldest entry."""
    cache = OcrCache(max_entries=2, clock=FakeClock())
    region = OcrRegion(
        text="x", rect=_rect(0, 0, 100, 40), confidence=0.75, words=(), cached=False, elapsed_ms=0.0
    )
    cache.put("a", region)
    cache.put("b", region)
    _ = cache.get("a")  # refresh "a"
    cache.put("c", region)

    assert cache.get("b") is None
    assert cache.get("a") is not None
    assert cache.get("c") is not None


def test_cache_expires_entries() -> None:
    """An entry older than its TTL is treated as absent."""
    clock = FakeClock()
    cache = OcrCache(max_entries=4, ttl_seconds=1.0, clock=clock)
    region = OcrRegion(
        text="x", rect=_rect(0, 0, 100, 40), confidence=0.75, words=(), cached=False, elapsed_ms=0.0
    )
    cache.put("k", region)
    assert cache.get("k") is not None

    clock._now += 2.0
    assert cache.get("k") is None


def test_cache_rejects_invalid_bounds() -> None:
    """A non-positive bound is a programming error, not a silent default."""
    with pytest.raises(ValueError):
        OcrCache(max_entries=0)
    with pytest.raises(ValueError):
        OcrCache(ttl_seconds=0.0)


# -- Capability --------------------------------------------------------------


def test_capability_reports_engine_evidence() -> None:
    """The capability reports AVAILABLE with the engine version and limits."""
    engine = OcrEngine(_settings(), backend=FakeBackend(), clock=FakeClock())
    capability = engine.capability()
    assert capability.name.value == "ocr"
    if capability.status is CapabilityStatus.AVAILABLE:
        assert capability.details["tesseract_version"]
        assert capability.details["limits"]["max_regions"] == 6
    else:  # pragma: no cover - only when tesseract is genuinely absent
        assert capability.reason and capability.fix_hint


# -- Real backend (skipped when Tesseract is not installed) -------------------


def _tesseract_available() -> bool:
    """True when the real pytesseract/Tesseract stack can actually run."""
    try:
        import pytesseract

        pytesseract.get_tesseract_version()
    except Exception:
        return False
    return True


@pytest.mark.skipif(not _tesseract_available(), reason="tesseract engine is not installed")
def test_real_pytesseract_backend_reads_rendered_text() -> None:
    """The real backend recognises text rendered into a frame (fallback path)."""
    from PIL import Image, ImageDraw, ImageFont

    canvas = Image.new("L", (_DESKTOP_WIDTH, _DESKTOP_HEIGHT), color=255)
    draw = ImageDraw.Draw(canvas)
    try:
        font = ImageFont.load_default(size=28)
        draw.text((50, 60), "Play", fill=0, font=font)
    except TypeError:  # pragma: no cover - older Pillow without sized default font
        draw.text((50, 60), "Play", fill=0)
    gray = np.array(canvas, dtype=np.uint8)
    bgra = np.stack(
        [gray, gray, gray, np.full_like(gray, 255)], axis=-1
    )

    frame = Frame(
        frame_id=0,
        generation=0,
        captured_at_monotonic=0.0,
        captured_at=0.0,
        desktop_rect=_DESKTOP,
        image=bgra,
        thumbnail=np.zeros((270, 480), dtype=np.uint8),
        latency_ms=0.0,
        backend="pytesseract",
    )
    engine = OcrEngine(OcrSettings(deadline_ms=5000))
    result = engine.recognize(
        frame,
        candidates=[_rect(40, 40, 240, 80)],
    )

    assert result.regions, "the real OCR backend returned no regions"
    assert "play" in result.text.casefold()
