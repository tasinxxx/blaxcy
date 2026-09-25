"""Builders for the perception-orchestrator and composition-root tests.

The orchestrator's job is to join four perception sources, one change detector and
a state cache. Testing it needs scripted sources and a screen whose change is a
*real* classified delta -- not a hand-written ``ScreenDelta`` -- because the whole
point of the OCR stage is that it is driven by what the change detector saw.

So the capture backend here returns solid BGRA frames whose level moves on every
grab: a wall-to-wall change is ``MAJOR``, which the animation reclassification can
never downgrade, which is what makes "a change happened, so OCR has a relevant
region" deterministic instead of timing-dependent.

No test here injects physical input; input controllers, where used, run on
:class:`~tests.harness.fake_input_backend.FakeInputBackend`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import numpy.typing as npt

from config.settings import Settings
from core.accessibility import AccessibilityService
from core.change_detector import ChangeDetector
from core.event_bus import EventBus
from core.frame_engine import FrameEngine
from core.ocr import OcrEngine, RawWord
from core.perception import PerceptionOrchestrator
from core.state_cache import StateCache
from core.target_resolver import TargetResolver
from core.visual_grounder import RawVisualMatch, VisualGrounder
from schemas.elements import UIElement
from schemas.enums import CoordinateSpace, ErrorCode, UIRole
from schemas.errors import BlaxcyError
from schemas.events import Event
from schemas.geometry import Rect
from tests.harness.phase8 import make_element, make_layout


class ScriptedCaptureBackend:
    """A capture backend painting a flat screen plus test-controlled patches.

    The patch list *is* the script: a test grabs once, adds a patch, grabs again,
    and the change detector sees exactly the patch the test drew. That matters
    because the change detector's classification (and therefore whether the OCR
    stage has a region at all) is driven by real pixels, not by a hand-written
    delta.

    Args:
        width: Frame width in pixels.
        height: Frame height in pixels.
        base: Greyscale level of the unpainted screen.
        fail: Raise on every grab, to exercise the capture-failure path.
        origin: The captured region's DESKTOP-space origin.
    """

    def __init__(
        self,
        *,
        width: int = 1920,
        height: int = 1080,
        base: int = 10,
        fail: bool = False,
        origin: tuple[int, int] = (0, 0),
    ) -> None:
        """Create the backend with a flat screen and no patches."""
        self.width = width
        self.height = height
        self.base = base
        self.fail = fail
        self.origin = origin
        self.patches: list[tuple[int, int, int, int, int]] = []
        self.grabs = 0
        self.closed = False

    def add_patch(self, x: int, y: int, width: int, height: int, level: int = 200) -> None:
        """Paint a rectangle at a different level, in DESKTOP pixels."""
        self.patches.append((x, y, width, height, level))

    def clear_patches(self) -> None:
        """Return the painted screen to its flat base."""
        self.patches.clear()

    def grab(self) -> tuple[bytes, int, int]:
        """Return one BGRA frame: the flat base plus every patch."""
        if self.fail:
            raise RuntimeError("synthetic capture failure")
        self.grabs += 1
        frame = np.full((self.height, self.width, 4), self.base, dtype=np.uint8)
        for x, y, width, height, level in self.patches:
            frame[y : y + height, x : x + width, :] = level
        return frame.tobytes(), self.width, self.height

    def monitor_bounds(self) -> tuple[int, int, int, int]:
        """The captured desktop rectangle."""
        return (self.origin[0], self.origin[1], self.width, self.height)

    def monitors(self) -> tuple[dict[str, int], ...]:
        """One physical monitor covering the captured region."""
        return (
            {
                "left": self.origin[0],
                "top": self.origin[1],
                "width": self.width,
                "height": self.height,
            },
        )

    def close(self) -> None:
        """Record the close."""
        self.closed = True


class ScriptedA11yBackend:
    """An accessibility backend returning a fixed element list.

    ``initialize`` succeeds so the service reports AVAILABLE; a service built over
    it behaves exactly like the real one for caching, marshalling and events.
    """

    def __init__(
        self,
        elements: Sequence[UIElement] = (),
        *,
        fail_enumerate: bool = False,
        events_per_poll: int = 0,
    ) -> None:
        """Create the backend with its element list."""
        self.elements = list(elements)
        self.fail_enumerate = fail_enumerate
        self.events_per_poll = events_per_poll
        self.enumerate_calls = 0
        self.initialized = False

    def initialize(self) -> None:
        """Mark the backend initialized."""
        self.initialized = True

    def shutdown(self) -> None:
        """Mark the backend shut down."""
        self.initialized = False

    def enumerate_elements(self) -> list[UIElement]:
        """Return the fixed element list."""
        self.enumerate_calls += 1
        if self.fail_enumerate:
            raise BlaxcyError(ErrorCode.A11Y_TIMEOUT, "synthetic a11y failure")
        return list(self.elements)

    def query_roles(self, roles: Sequence[UIRole]) -> list[UIElement]:
        """Return the fixed elements whose role is requested."""
        return [element for element in self.elements if element.role in roles]

    def poll_events(self) -> bool:
        """Report a scripted number of observed events."""
        return self.events_per_poll > 0

    def diagnostics(self) -> dict[str, Any]:
        """Evidence for the capability report."""
        return {"synthetic": True, "elements": len(self.elements)}


class ScriptedOcrBackend:
    """An OCR backend that fills each requested crop with one word."""

    name = "fake-ocr"

    def __init__(self, text: str = "Play", *, confidence: float = 1.0, fail: bool = False) -> None:
        """Create the backend with the text it should report."""
        self.text = text
        self.confidence = confidence
        self.fail = fail
        self.calls: list[tuple[int, float | None]] = []

    def recognize(
        self,
        image: npt.NDArray[np.uint8],
        *,
        language: str,
        timeout: float | None,
    ) -> tuple[RawWord, ...]:
        """Report one word covering the whole crop, so its box maps back exactly."""
        self.calls.append((int(image.size), timeout))
        if self.fail:
            raise BlaxcyError(ErrorCode.OCR_FAILED, "synthetic ocr failure")
        height, width = int(image.shape[0]), int(image.shape[1])
        return (
            RawWord(
                text=self.text,
                left=0,
                top=0,
                width=max(1, width),
                height=max(1, height),
                confidence=self.confidence,
            ),
        )


class ScriptedVisualBackend:
    """A visual backend returning scripted normalised matches."""

    name = "fake-visual"

    def __init__(
        self,
        matches: Sequence[RawVisualMatch] = (),
        *,
        error: Exception | None = None,
    ) -> None:
        """Create the backend with its scripted answer."""
        self.matches = tuple(matches)
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def locate(
        self,
        image: npt.NDArray[np.uint8],
        *,
        query: str,
        max_results: int,
        timeout_ms: int,
    ) -> tuple[RawVisualMatch, ...]:
        """Record the request and return (or raise) the scripted answer."""
        self.calls.append({"query": query, "max_results": max_results, "timeout_ms": timeout_ms})
        if self.error is not None:
            raise self.error
        return self.matches

    @property
    def called(self) -> bool:
        """Whether the backend was invoked at all (the gate's observable)."""
        return bool(self.calls)


@dataclass
class PerceptionEnv:
    """A wired orchestrator over scripted sources, plus the scripted pieces."""

    orchestrator: PerceptionOrchestrator
    cache: StateCache
    settings: Settings
    capture: ScriptedCaptureBackend
    a11y_backend: ScriptedA11yBackend
    accessibility: AccessibilityService
    ocr_backend: ScriptedOcrBackend
    visual_backend: ScriptedVisualBackend
    frame_engine: FrameEngine
    resolver: TargetResolver
    visual: VisualGrounder
    events: list[Event] = field(default_factory=list)

    def set_elements(self, elements: Sequence[UIElement]) -> None:
        """Replace the accessibility backend's fixed element list."""
        self.a11y_backend.elements = list(elements)
        # The service caches for its TTL; a forced refresh is what a caller that
        # just changed the desktop would get anyway.
        self.accessibility.refresh()


def build_perception_env(
    *,
    settings: Settings | None = None,
    elements: Sequence[UIElement] = (),
    with_ocr: bool = True,
    with_visual: bool = True,
    ocr_text: str = "Play",
    visual_matches: Sequence[RawVisualMatch] = (),
    capture_fail: bool = False,
    frame_size: tuple[int, int] = (1920, 1080),
    capture: ScriptedCaptureBackend | None = None,
) -> PerceptionEnv:
    """Build a fully wired orchestrator over scripted perception sources.

    The accessibility service is a *real* service over a scripted backend, so the
    section 35 threading, caching and event marshalling are exercised, and the
    orchestrator consumes it exactly as it does in production.
    """
    active = settings if settings is not None else Settings()
    capture_backend = capture if capture is not None else ScriptedCaptureBackend(
        width=frame_size[0], height=frame_size[1], fail=capture_fail
    )
    engine = FrameEngine(
        active.capture,
        backend_factory=lambda: capture_backend,
        thumbnail_size=(480, 270),
    )
    a11y_backend = ScriptedA11yBackend(elements)
    accessibility = AccessibilityService(active.accessibility, backend_factory=lambda: a11y_backend)
    bus = EventBus()
    cache = StateCache(event_bus=bus)
    resolver = TargetResolver(active.resolver)
    ocr_backend = ScriptedOcrBackend(ocr_text)
    ocr_engine = OcrEngine(active.ocr, backend=ocr_backend)
    visual_backend = ScriptedVisualBackend(visual_matches)
    visual = VisualGrounder(active.visual, backend=visual_backend)
    orchestrator = PerceptionOrchestrator(
        active,
        frame_engine=engine,
        state_cache=cache,
        accessibility=accessibility,
        ocr=ocr_engine if with_ocr else None,
        visual=visual if with_visual else None,
        resolver=resolver,
        change_detector=ChangeDetector(active.perception),
        window_manager=None,
        event_bus=bus,
    )
    events: list[Event] = []
    bus.subscribe(events.append)
    env = PerceptionEnv(
        orchestrator=orchestrator,
        cache=cache,
        settings=active,
        capture=capture_backend,
        a11y_backend=a11y_backend,
        accessibility=accessibility,
        ocr_backend=ocr_backend,
        visual_backend=visual_backend,
        frame_engine=engine,
        resolver=resolver,
        visual=visual,
        events=events,
    )
    env.orchestrator.start()
    return env


def desktop_box(x: float, y: float, width: float, height: float) -> Rect:
    """A DESKTOP-space rectangle, so nothing crosses coordinate spaces (section 31)."""
    return Rect(x=x, y=y, width=width, height=height, space=CoordinateSpace.DESKTOP)


def button(element_id: str, text: str, *, box: Rect | None = None) -> UIElement:
    """A clickable AT-SPI button, for a scripted tree."""
    return make_element(
        element_id,
        text=text,
        role=UIRole.BUTTON,
        bbox=box,
        center=None if box is None else box.center,
    )


def visual_match(
    label: str = "Play",
    *,
    x: float = 0.1,
    y: float = 0.1,
    width: float = 0.05,
    height: float = 0.05,
    confidence: float = 0.9,
) -> RawVisualMatch:
    """One normalised visual match covering a small, sane region."""
    return RawVisualMatch(
        label=label, confidence=confidence, x=x, y=y, width=width, height=height
    )


__all__ = [
    "PerceptionEnv",
    "ScriptedA11yBackend",
    "ScriptedCaptureBackend",
    "ScriptedOcrBackend",
    "ScriptedVisualBackend",
    "build_perception_env",
    "button",
    "desktop_box",
    "make_layout",
    "visual_match",
]
