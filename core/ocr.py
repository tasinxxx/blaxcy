"""OCR -- a fallback perception source (specification sections 39, 40, 42, 55, 80).

OCR is deliberately **not** a primary source. It runs only when the
deterministic sources (AT-SPI, browser accessibility) cannot answer, and it is
fed only the small, relevant regions a caller asks about -- never the whole
desktop. The section 40 limits are enforced here rather than trusted to callers:

* only regions at least ``minimum_width`` x ``minimum_height`` are considered,
* at most ``max_regions`` are processed per call, largest-first and
  deterministically ordered so two runs on the same input do the same work,
* a region whose area exceeds ``max_area_ratio`` of the desktop is refused
  (a "region" that big is a whole-screen call in disguise),
* the whole call is bounded by ``deadline_ms``: once the budget is spent the
  engine stops, reports ``truncated_by_deadline``, and returns what it actually
  produced rather than pretending it finished.

Three invariants are enforced in code, not left to callers:

* **Password fields are never OCR'd** (sections 42, 55). Any candidate that
  overlaps a password element's bounding box is dropped before the engine is
  invoked, and the drop is counted.
* **OCR text is never treated as clickable.** A recognised fragment is a
  ``TEXT_FRAGMENT`` with ``clickable=False``; deriving clickability is a later
  decision that must be backed by real UI behaviour (section 38).
* **Confidence is computed, never assumed.** Every fragment's confidence comes
  from :func:`perception_confidence` with the ``OCR`` base rate (0.75) and the
  geometry/freshness multipliers (section 39); OCR text never reaches AT-SPI
  confidence.

Results are cached by an xxhash of the actual cropped pixels plus the crop's
geometry, so an unchanged region is not re-OCR'd. The cache is bounded and
TTL'd; it is a speed hint only, and a cache hit still yields a real recognised
result rather than a fabricated one.
"""

from __future__ import annotations

import importlib
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol

import cv2
import numpy as np
import numpy.typing as npt
import xxhash

from config.settings import OcrSettings
from core.frame_engine import Frame
from schemas.capability import Capability
from schemas.elements import UIElement, perception_confidence
from schemas.enums import (
    CapabilityName,
    CapabilityStatus,
    CoordinateSpace,
    ErrorCode,
    PerceptionSource,
    UIRole,
)
from schemas.errors import BlaxcyError
from schemas.geometry import Rect

#: Backend identifier reported in capability/benchmark output.
OCR_BACKEND_NAME: Final[str] = "pytesseract"

#: Default OCR result cache bounds. The cache is a hint, never a source of truth.
DEFAULT_CACHE_ENTRIES: Final[int] = 256
DEFAULT_CACHE_TTL_SECONDS: Final[float] = 30.0

_OCR_FIX_HINT: Final[str] = (
    "Install tesseract-ocr (and tesseract-ocr-eng for English) and ensure "
    "'pytesseract' is importable in the project venv."
)


@dataclass(frozen=True)
class RawWord:
    """One recognised token in the *crop's* pixel coordinates."""

    text: str
    left: int
    top: int
    width: int
    height: int
    confidence: float


@dataclass(frozen=True)
class OcrWord:
    """A recognised token in DESKTOP coordinates (section 31)."""

    text: str
    rect: Rect
    confidence: float


@dataclass(frozen=True)
class OcrRegion:
    """One OCR'd region: its text, its DESKTOP rectangle, and its words."""

    text: str
    rect: Rect
    confidence: float
    words: tuple[OcrWord, ...]
    cached: bool
    elapsed_ms: float


@dataclass(frozen=True)
class RegionSelection:
    """Which candidate regions survived the section 40 limits, and why not."""

    regions: tuple[Rect, ...]
    too_small: int
    excluded: int
    over_limit: int
    oversized: int


@dataclass(frozen=True)
class OcrResult:
    """The complete result of one bounded OCR call."""

    regions: tuple[OcrRegion, ...]
    considered: int
    selection: RegionSelection
    truncated_by_deadline: bool
    failures: int
    elapsed_ms: float

    @property
    def text(self) -> str:
        """All recognised text, joined in region order (may be empty)."""
        return " ".join(region.text for region in self.regions if region.text).strip()


class OcrBackend(Protocol):
    """The minimal contract the engine needs from an OCR backend."""

    name: str

    def recognize(
        self,
        image: npt.NDArray[np.uint8],
        *,
        language: str,
        timeout: float | None,
    ) -> tuple[RawWord, ...]:
        """Recognise text in ``image``, returning words in image pixel coords."""
        ...


class PytesseractBackend:
    """The ``pytesseract`` backend (the only one this phase implements)."""

    name = OCR_BACKEND_NAME

    def recognize(
        self,
        image: npt.NDArray[np.uint8],
        *,
        language: str,
        timeout: float | None,
    ) -> tuple[RawWord, ...]:
        """Run Tesseract and map its word boxes, or raise ``OCR_FAILED``."""
        try:
            pytesseract = importlib.import_module("pytesseract")
            output = pytesseract.Output
        except Exception as exc:
            raise BlaxcyError(
                ErrorCode.OCR_FAILED,
                f"pytesseract is not importable: {exc!r}",
                details={"backend": OCR_BACKEND_NAME, "fix_hint": _OCR_FIX_HINT},
            ) from exc

        gray = _to_grayscale(image)
        kwargs: dict[str, Any] = {"output_type": output.DICT}
        if timeout is not None and timeout > 0:
            kwargs["timeout"] = timeout
        try:
            data = pytesseract.image_to_data(gray, lang=language, **kwargs)
        except Exception as exc:
            raise BlaxcyError(
                ErrorCode.OCR_FAILED,
                f"tesseract failed: {exc!r}",
                details={"backend": OCR_BACKEND_NAME, "fix_hint": _OCR_FIX_HINT},
            ) from exc

        words: list[RawWord] = []
        texts = data.get("text", [])
        for index in range(len(texts)):
            text = str(texts[index] or "").strip()
            if not text:
                continue
            try:
                confidence = float(data["conf"][index])
            except (KeyError, TypeError, ValueError):
                continue
            # Tesseract reports -1 for non-word rows (blocks/lines).
            if confidence < 0:
                continue
            words.append(
                RawWord(
                    text=text,
                    left=int(data["left"][index]),
                    top=int(data["top"][index]),
                    width=int(data["width"][index]),
                    height=int(data["height"][index]),
                    confidence=min(1.0, confidence / 100.0),
                )
            )
        return tuple(words)


class OcrCache:
    """A bounded, TTL'd LRU cache keyed by cropped-pixel hash (section 40)."""

    def __init__(
        self,
        *,
        max_entries: int = DEFAULT_CACHE_ENTRIES,
        ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Create a cache.

        Args:
            max_entries: Maximum retained results (must be positive).
            ttl_seconds: How long an entry stays valid.
            clock: Monotonic clock (injectable for deterministic tests).
        """
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._max_entries = max_entries
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._entries: OrderedDict[str, tuple[float, OcrRegion]] = OrderedDict()

    def get(self, key: str) -> OcrRegion | None:
        """Return a live cached region, refreshing its LRU position."""
        entry = self._entries.get(key)
        if entry is None:
            return None
        stored_at, region = entry
        if (self._clock() - stored_at) > self._ttl_seconds:
            del self._entries[key]
            return None
        self._entries.move_to_end(key)
        return region

    def put(self, key: str, region: OcrRegion) -> None:
        """Store a region, evicting the least-recently-used entry if needed."""
        self._entries[key] = (self._clock(), region)
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)

    def clear(self) -> None:
        """Drop every cached entry."""
        self._entries.clear()

    def __len__(self) -> int:
        """Number of currently retained entries (expired ones until next access)."""
        return len(self._entries)


class OcrEngine:
    """Bounded, fallback-only OCR over requested DESKTOP regions."""

    def __init__(
        self,
        settings: OcrSettings,
        *,
        backend: OcrBackend | None = None,
        language: str = "eng",
        cache: OcrCache | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Create an engine.

        Args:
            settings: The ``[ocr]`` configuration section (section 40 limits).
            backend: Override for tests; defaults to :class:`PytesseractBackend`.
            language: Tesseract language code.
            cache: Override for tests; defaults to a bounded TTL LRU cache.
            clock: Monotonic clock used for the deadline and cache TTL.
        """
        self._settings = settings
        self._backend: OcrBackend = backend if backend is not None else PytesseractBackend()
        self._language = language
        self._cache = cache if cache is not None else OcrCache(clock=clock)
        self._clock = clock
        self._calls = 0
        self._cache_hits = 0
        self._failures = 0
        self._regions_processed = 0
        self._last_error: str | None = None

    # -- Selection (section 40) ----------------------------------------------

    def should_process(self, rect: Rect, *, desktop: Rect) -> bool:
        """Whether a candidate region is worth OCR'ing at all (section 40).

        Rejects regions smaller than the minimum geometry and regions whose area
        exceeds ``max_area_ratio`` of the desktop -- OCR is a fallback for
        readable fragments, not a screen-wide scan.
        """
        if rect.width < self._settings.minimum_width or rect.height < self._settings.minimum_height:
            return False
        if desktop.area <= 0:
            return False
        return (rect.area / desktop.area) <= self._settings.max_area_ratio

    def select_regions(
        self,
        candidates: Sequence[Rect],
        *,
        desktop: Rect,
        excluded: Sequence[Rect] = (),
    ) -> RegionSelection:
        """Apply the section 40 limits to ``candidates``, deterministically.

        Ordering is largest-area-first, then top-to-bottom, then left-to-right,
        so the *same* input always yields the same selected set. Excluded
        regions (for example password fields) are dropped before ordering.
        """
        too_small = 0
        excluded_count = 0
        oversized = 0
        kept: list[Rect] = []
        for rect in candidates:
            if any(rect.intersects(other) for other in excluded):
                excluded_count += 1
                continue
            if rect.width < self._settings.minimum_width or rect.height < self._settings.minimum_height:
                too_small += 1
                continue
            if desktop.area <= 0 or (rect.area / desktop.area) > self._settings.max_area_ratio:
                oversized += 1
                continue
            kept.append(rect)
        kept.sort(key=lambda r: (-r.area, r.y, r.x))
        over_limit = max(0, len(kept) - self._settings.max_regions)
        selected = tuple(kept[: self._settings.max_regions])
        return RegionSelection(
            regions=selected,
            too_small=too_small,
            excluded=excluded_count,
            over_limit=over_limit,
            oversized=oversized,
        )

    # -- Recognition ----------------------------------------------------------

    def recognize(
        self,
        frame: Frame,
        *,
        candidates: Sequence[Rect],
        excluded: Sequence[Rect] = (),
        password_elements: Sequence[UIElement] = (),
        frame_id: int | None = None,
        generation: int | None = None,
    ) -> OcrResult:
        """OCR the selected candidate regions of ``frame``, within the deadline.

        Args:
            frame: The captured frame; its ``desktop_rect`` maps pixels to
                DESKTOP space.
            candidates: DESKTOP-space regions the caller believes may hold text.
            excluded: Extra regions never to OCR (e.g. already-known secrets).
            password_elements: Elements whose bounding boxes must never be OCR'd
                (sections 42, 55). Their rectangles are always excluded.
            frame_id: Frame this call is against; recorded on emitted elements.
            generation: Perception generation; recorded on emitted elements.

        Returns:
            An :class:`OcrResult` whose regions carry DESKTOP-space geometry.

        Raises:
            BlaxcyError: ``OCR_FAILED`` when every attempted region failed. A
                partial success is returned with ``failures`` set rather than
                raising, because partial evidence is still real evidence.
        """
        started = self._clock()
        self._calls += 1

        excluded_rects = tuple(excluded) + password_regions(password_elements)
        selection = self.select_regions(candidates, desktop=frame.desktop_rect, excluded=excluded_rects)

        regions: list[OcrRegion] = []
        requested = 0
        for rect in selection.regions:
            elapsed_ms = (self._clock() - started) * 1000.0
            if elapsed_ms >= self._settings.deadline_ms:
                break
            requested += 1
            remaining_s = max(0.0, (self._settings.deadline_ms - elapsed_ms) / 1000.0)
            region = self._recognize_region(
                frame,
                rect=rect,
                frame_id=frame_id,
                generation=generation,
                timeout_s=remaining_s if remaining_s > 0 else None,
            )
            if region is not None:
                regions.append(region)

        truncation = requested < len(selection.regions)
        failures = requested - len(regions)
        if requested > 0 and not regions and failures == requested:
            raise BlaxcyError(
                ErrorCode.OCR_FAILED,
                f"OCR failed for all {requested} requested region(s)",
                details={"backend": self._backend.name, "last_error": self._last_error},
            )
        return OcrResult(
            regions=tuple(regions),
            considered=len(candidates),
            selection=selection,
            truncated_by_deadline=truncation,
            failures=failures,
            elapsed_ms=(self._clock() - started) * 1000.0,
        )

    def read_text(
        self,
        frame: Frame,
        *,
        rect: Rect,
        frame_id: int | None = None,
        generation: int | None = None,
        excluded: Sequence[Rect] = (),
    ) -> str | None:
        """OCR exactly one region and return its text, or ``None``.

        Used by the section 36 browser address-bar fallback. A failure or an
        empty read returns ``None`` (honest absence), never an invented URL.
        """
        if any(rect.intersects(other) for other in excluded):
            return None
        if rect.width < self._settings.minimum_width or rect.height < self._settings.minimum_height:
            return None
        region = self._recognize_region(
            frame, rect=rect, frame_id=frame_id, generation=generation, timeout_s=None
        )
        if region is None or not region.text:
            return None
        return region.text

    def as_elements(
        self,
        result: OcrResult,
        *,
        frame_id: int | None = None,
        generation: int | None = None,
        owner_app: str | None = None,
    ) -> tuple[UIElement, ...]:
        """Convert OCR regions into typed elements with honest provenance.

        Every element is a ``TEXT_FRAGMENT`` with ``clickable=False``: OCR
        proves text is *visible*, never that it is *clickable* (section 38).
        """
        elements: list[UIElement] = []
        for region in result.regions:
            if not region.text:
                continue
            elements.append(
                UIElement(
                    element_id=_region_element_id(region),
                    role=UIRole.TEXT_FRAGMENT,
                    text=region.text,
                    accessible_name=region.text,
                    bbox=region.rect,
                    center=region.rect.center,
                    coordinate_space=CoordinateSpace.DESKTOP,
                    clickable=False,
                    effective_clickable=False,
                    enabled=True,
                    visible=True,
                    occluded=False,
                    owner_app=owner_app,
                    frame_id=frame_id,
                    timestamp=time.time(),
                    source=PerceptionSource.OCR,
                    confidence=region.confidence,
                )
            )
        return tuple(elements)

    # -- Capability / diagnostics --------------------------------------------

    def capability(self) -> Capability:
        """Report the OCR capability honestly, with evidence (section 28)."""
        try:
            pytesseract = importlib.import_module("pytesseract")
        except Exception as exc:
            return Capability(
                name=CapabilityName.OCR,
                status=CapabilityStatus.UNAVAILABLE,
                backend=OCR_BACKEND_NAME,
                reason=f"pytesseract is not importable: {exc!r}",
                fix_hint=_OCR_FIX_HINT,
            )
        started = self._clock()
        try:
            version = str(pytesseract.get_tesseract_version())
        except Exception as exc:
            return Capability(
                name=CapabilityName.OCR,
                status=CapabilityStatus.UNAVAILABLE,
                backend=OCR_BACKEND_NAME,
                reason=f"tesseract engine not usable: {exc!r}",
                fix_hint=_OCR_FIX_HINT,
            )
        latency_ms = (self._clock() - started) * 1000.0
        return Capability(
            name=CapabilityName.OCR,
            status=CapabilityStatus.AVAILABLE,
            backend=OCR_BACKEND_NAME,
            latency_ms=round(latency_ms, 3),
            details={
                "tesseract_version": version,
                "language": self._language,
                "limits": {
                    "minimum_width": self._settings.minimum_width,
                    "minimum_height": self._settings.minimum_height,
                    "max_regions": self._settings.max_regions,
                    "max_area_ratio": self._settings.max_area_ratio,
                    "deadline_ms": self._settings.deadline_ms,
                },
            },
        )

    def diagnostics(self) -> dict[str, Any]:
        """JSON-shaped health/performance evidence for logs and benchmarks."""
        return {
            "backend": self._backend.name,
            "calls": self._calls,
            "cache_hits": self._cache_hits,
            "cache_entries": len(self._cache),
            "regions_processed": self._regions_processed,
            "failures": self._failures,
            "last_error": self._last_error,
            "language": self._language,
        }

    # -- Internals ------------------------------------------------------------

    def _recognize_region(
        self,
        frame: Frame,
        *,
        rect: Rect,
        frame_id: int | None,
        generation: int | None,
        timeout_s: float | None,
    ) -> OcrRegion | None:
        """Crop, cache-check and OCR one region. Records (never hides) failure.

        ``frame_id``/``generation`` are accepted for future element stamping and
        are intentionally unused here -- recognition itself is frame-agnostic.
        """
        started = self._clock()
        crop = _crop_frame(frame, rect)
        if crop is None:
            self._fail("crop was empty")
            return None

        key = _region_key(rect, crop)
        cached = self._cache.get(key)
        if cached is not None:
            self._cache_hits += 1
            self._regions_processed += 1
            return OcrRegion(
                text=cached.text,
                rect=cached.rect,
                confidence=cached.confidence,
                words=cached.words,
                cached=True,
                elapsed_ms=float((self._clock() - started) * 1000.0),
            )

        try:
            raw = self._backend.recognize(crop, language=self._language, timeout=timeout_s)
        except BlaxcyError as exc:
            self._fail(exc.message)
            return None

        words = _map_words(raw, crop=crop, rect=rect)
        text = " ".join(word.text for word in words).strip()
        confidence = perception_confidence(
            PerceptionSource.OCR,
            geometry_sanity=1.0,
            freshness=1.0,
            source_consistency=_word_agreement(words),
        )
        region = OcrRegion(
            text=text,
            rect=rect,
            confidence=confidence,
            words=words,
            cached=False,
            elapsed_ms=float((self._clock() - started) * 1000.0),
        )
        self._cache.put(key, region)
        self._regions_processed += 1
        return region

    def _fail(self, message: str) -> None:
        """Record a region failure without aborting the whole call."""
        self._failures += 1
        self._last_error = message


def password_regions(elements: Sequence[UIElement]) -> tuple[Rect, ...]:
    """Return the bounding boxes of password elements (sections 42, 55).

    Extraction is best-effort: a password element without geometry contributes
    nothing, but the engine also never *needs* to be told -- callers must not
    request OCR over a credential field in the first place.
    """
    rects: list[Rect] = []
    for element in elements:
        if element.is_password and element.bbox is not None:
            rects.append(element.bbox)
    return tuple(rects)


def _to_grayscale(image: npt.NDArray[np.uint8]) -> npt.NDArray[np.uint8]:
    """Convert BGRA/BGR/grayscale input to 2-D grayscale for Tesseract."""
    if image.ndim == 2:
        return image
    if image.ndim != 3:
        raise ValueError(f"unsupported image shape for OCR: {image.shape}")
    if image.shape[2] == 4:
        return np.asarray(cv2.cvtColor(image, cv2.COLOR_BGRA2GRAY), dtype=np.uint8)
    if image.shape[2] == 3:
        return np.asarray(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY), dtype=np.uint8)
    if image.shape[2] == 1:
        return image[:, :, 0]
    raise ValueError(f"unsupported channel count for OCR: {image.shape[2]}")


def _crop_frame(frame: Frame, rect: Rect) -> npt.NDArray[np.uint8] | None:
    """Crop ``frame`` to a DESKTOP-space ``rect``, or ``None`` when empty.

    The frame's full image is in FRAME pixel space; its ``desktop_rect`` maps
    DESKTOP coordinates onto those pixels, so the scale is derived, never assumed.
    """
    desktop = frame.desktop_rect
    if desktop.width <= 0 or desktop.height <= 0:
        return None
    scale_x = frame.width / desktop.width
    scale_y = frame.height / desktop.height
    x0 = round((rect.x - desktop.x) * scale_x)
    y0 = round((rect.y - desktop.y) * scale_y)
    x1 = round((rect.x + rect.width - desktop.x) * scale_x)
    y1 = round((rect.y + rect.height - desktop.y) * scale_y)
    x0 = max(0, min(frame.width, x0))
    y0 = max(0, min(frame.height, y0))
    x1 = max(0, min(frame.width, x1))
    y1 = max(0, min(frame.height, y1))
    if x1 <= x0 or y1 <= y0:
        return None
    return np.asarray(frame.image[y0:y1, x0:x1], dtype=np.uint8)


def _map_words(
    raw_words: Sequence[RawWord],
    *,
    crop: npt.NDArray[np.uint8],
    rect: Rect,
) -> tuple[OcrWord, ...]:
    """Map backend word boxes from crop pixels into DESKTOP space."""
    crop_height, crop_width = int(crop.shape[0]), int(crop.shape[1])
    if crop_width <= 0 or crop_height <= 0:
        return ()
    scale_x = rect.width / crop_width
    scale_y = rect.height / crop_height
    words: list[OcrWord] = []
    for raw in raw_words:
        if raw.width <= 0 or raw.height <= 0:
            continue
        words.append(
            OcrWord(
                text=raw.text,
                rect=Rect(
                    x=rect.x + raw.left * scale_x,
                    y=rect.y + raw.top * scale_y,
                    width=raw.width * scale_x,
                    height=raw.height * scale_y,
                    space=CoordinateSpace.DESKTOP,
                ),
                confidence=raw.confidence,
            )
        )
    return tuple(words)


def _word_agreement(words: Sequence[OcrWord]) -> float:
    """The section 39 ``source_consistency`` multiplier for an OCR region.

    With no words there is nothing to trust, so the multiplier is 0. With one
    word it is 1.0; with several, it is the mean word confidence, so a region
    full of low-confidence guesses is not reported at single-word confidence.
    """
    if not words:
        return 0.0
    if len(words) == 1:
        return 1.0
    return sum(word.confidence for word in words) / len(words)


def _region_key(rect: Rect, crop: npt.NDArray[np.uint8]) -> str:
    """A content hash for one cropped region: pixels plus geometry (section 40).

    Two different regions that happen to hold identical pixels are still
    distinct OCR inputs because the geometry is part of the key.
    """
    hasher = xxhash.xxh64()
    hasher.update(np.ascontiguousarray(crop).tobytes())
    hasher.update(
        f"|{rect.x:.2f},{rect.y:.2f},{rect.width:.2f},{rect.height:.2f}".encode()
    )
    return hasher.hexdigest()


def _region_element_id(region: OcrRegion) -> str:
    """A stable element id for an OCR region, derived from geometry + text."""
    payload = (
        f"{region.rect.x:.2f},{region.rect.y:.2f},"
        f"{region.rect.width:.2f},{region.rect.height:.2f}|{region.text}"
    )
    return f"ocr:{xxhash.xxh64(payload.encode('utf-8')).hexdigest()}"
