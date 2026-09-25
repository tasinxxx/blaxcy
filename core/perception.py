"""Perception orchestration (specification sections 33-43, 65).

The orchestrator is the missing link between the perception *sources* and the
state cache. It owns one capture engine, one accessibility service, the optional
OCR engine and the optional visual grounder, and it assembles them into a single
versioned :class:`~schemas.screen_state.ScreenState` that the rest of the Body
acts on -- in the section 38 source order (AT-SPI -> DOM/browser -> OCR ->
visual), never in some other order for convenience.

Two entry points matter:

``perceive()``
    One ordinary cycle: capture -> change detection -> AT-SPI (+ browser
    annotation) -> bounded OCR over the regions the change detector flagged ->
    accept into the cache. This is what the executor's ``perceive`` hook and the
    read-only tools call.

``enrich(query, mode=...)``
    The section 43 fallback cascade for a target the deterministic resolver could
    not deliver: OCR over the relevant regions, then visual grounding, gated
    exactly as sections 41/42 require. It runs only after a deterministic *miss*
    (nothing found, or nothing that cleared ``act_threshold``) -- never on
    ``AMBIGUOUS`` -- and the elements it produces are fed back through the
    ordinary resolve -> lease -> revalidate -> execute -> verify pipeline.
    Nothing here shortcuts that pipeline.

Two consequences of sections 38/40/43 are worth stating plainly, because they are
what the code actually does rather than what a casual reading suggests:

* **OCR contributes evidence, never clickability.** Its output is
  ``TEXT_FRAGMENT`` elements with ``clickable=False``, and the resolver only
  considers actionable elements, so an OCR fragment can never *become* the click
  target. It enriches the observation -- visible text the Brain and
  ``describe_region`` can read -- and the cascade then continues to visual
  grounding, which is section 38's last-resort clickability source (and which does
  produce an actionable element).
* **Changed regions are a weak OCR region source, and that is sections 34/40
  meeting, not a bug.** Section 34 marks a region ``MEANINGFUL`` against the
  *thumbnail* while section 40 refuses any region above ``max_area_ratio`` (1.5%)
  of the desktop; on a full-HD desktop those thresholds coincide, so a classified
  change is normally too large to OCR. The usable region source is therefore the
  resolver's own near-miss candidates -- the controls that matched partially --
  with the changed regions kept as a fallback for a change that is small enough.

Deliberate non-responsibilities, because they are what keeps this honest:

* The orchestrator never injects input, never issues a lease and never resolves
  on a caller's behalf. It observes, annotates and accepts a state.
* It runs no background capture thread. Perception is pull-driven: a caller asks
  for an observation and gets one. The only long-lived perception thread is the
  accessibility service's ``T-A11Y`` owner (section 35), which already exists;
  adding a capture loop would be a scheduling change with no correctness benefit
  and a real risk of observing a desktop nobody asked about.
* ``enrich`` accepts an enriched state rather than returning loose elements, so
  the executor's section 45 revalidation can see the same observation the
  resolution was made against. A visual match that never reached the state would
  be a resolution the revalidation could not confirm -- a fake path, not a fast
  one.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from config.settings import Settings
from core.accessibility import AccessibilityService
from core.browser_accessibility import discover_contexts
from core.calibration import layout_from_topology
from core.change_detector import ChangeDetector
from core.event_bus import EventBus
from core.frame_engine import Frame, FrameEngine
from core.ocr import OcrEngine
from core.state_cache import StateCache
from core.target_resolver import ResolutionResult, ResolutionStatus, TargetResolver
from core.visual_grounder import VisualGrounder
from policy.guards import GuardOutcome, check_visual_fallback
from schemas.elements import ElementQuery, UIElement
from schemas.enums import (
    SIGNIFICANT_CHANGE_CLASSES,
    ErrorCode,
    PolicyMode,
    UIRole,
)
from schemas.errors import BlaxcyError
from schemas.geometry import MonitorGeometry, MonitorLayout, Rect, Size
from schemas.screen_state import ScreenState, WindowStackEntry


@dataclass(frozen=True)
class EnrichmentReport:
    """The outcome of a section 43 fallback cascade.

    ``state`` is the accepted observation when ``enriched`` is true; the caller
    re-resolves against the cache (or this state) afterwards.
    """

    enriched: bool
    ocr_elements: int = 0
    visual_elements: int = 0
    refused: str | None = None
    state: ScreenState | None = None


@dataclass
class _Counters:
    """Lifetime counters, so a diagnostic answer is a measurement not a guess."""

    cycles: int = 0
    capture_failures: int = 0
    rejected_updates: int = 0
    ocr_runs: int = 0
    ocr_elements: int = 0
    enrichments: int = 0
    enrichment_ocr: int = 0
    enrichment_visual: int = 0
    visual_refusals: int = 0
    visual_failures: int = 0
    browser_contexts: int = 0
    poison: list[str] = field(default_factory=list)


class PerceptionOrchestrator:
    """Builds one versioned :class:`ScreenState` from every perception source.

    Args:
        settings: Full configuration; ``[capture]``, ``[perception]``, ``[ocr]``,
            ``[visual]``, ``[safety]`` and ``[verification]`` are consulted.
        frame_engine: The section 33 capture engine. Required -- without pixels
            there is no perception at all.
        state_cache: The current-state authority the assembled state is offered
            to (and that upserts the version).
        accessibility: The section 35 AT-SPI owner. ``None`` means the session
            has no accessibility tree, which is reported honestly rather than
            substituted with an empty one.
        ocr: The section 40 fallback engine. ``None`` disables the OCR stage.
        visual: The section 41 grounder. ``None`` disables the visual stage.
        resolver: The section 43 resolver used to decide whether the fallback
            cascade is even needed. ``None`` builds the configured default, with
            the semantic scorer wired exactly as the executor wires it.
        change_detector: The section 34 detector. ``None`` builds the configured
            default.
        window_manager: Optional section 47 window control, used only to read the
            active window id / class so blocked-application policy has a
            window-manager-derived candidate as well as the AT-SPI app name.
        event_bus: Optional bus, for the one event this layer emits (a rejected
            update is already counted by the cache).
        clock: Monotonic clock, injectable for deterministic tests.
        wall_clock: Wall clock used for the human-facing timestamp.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        frame_engine: FrameEngine,
        state_cache: StateCache,
        accessibility: AccessibilityService | None = None,
        ocr: OcrEngine | None = None,
        visual: VisualGrounder | None = None,
        resolver: TargetResolver | None = None,
        change_detector: ChangeDetector | None = None,
        window_manager: Any = None,
        event_bus: EventBus | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._settings = settings
        self._engine = frame_engine
        self._cache = state_cache
        self._a11y = accessibility
        self._ocr = ocr
        self._visual = visual
        self._windows = window_manager
        self._bus = event_bus
        self._clock = clock
        self._wall_clock = wall_clock
        self._resolver = resolver if resolver is not None else _default_resolver(settings)
        self._detector = (
            change_detector
            if change_detector is not None
            else ChangeDetector(settings.perception, clock=clock, wall_clock=wall_clock)
        )
        self._last_frame: Frame | None = None
        self._started = False
        self._counters = _Counters()

    # -- Lifecycle -------------------------------------------------------------

    @property
    def started(self) -> bool:
        """Whether :meth:`start` has been called and not yet undone."""
        return self._started

    def start(self) -> None:
        """Open capture and the accessibility tree, tolerating an unavailable one.

        A session with no accessibility bus is a real, reportable state: capture
        still works and OBSERVE is still useful, so startup does not pretend the
        whole Body is broken -- nor does it pretend AT-SPI worked.
        """
        if self._started:
            return
        self._engine.open()
        if self._a11y is not None and not self._a11y.is_running:
            try:
                self._a11y.start()
            except BlaxcyError as exc:
                self._counters.poison.append(f"accessibility unavailable: {exc.code.value}")
        self._started = True

    def stop(self) -> None:
        """Release capture and the accessibility thread. Idempotent."""
        if self._a11y is not None:
            self._a11y.stop()
        self._engine.close()
        self._last_frame = None
        self._started = False

    def __enter__(self) -> PerceptionOrchestrator:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    # -- The perceive hook -----------------------------------------------------

    def perceive(self, *, force: bool = False) -> ScreenState | None:
        """Observe the desktop once and offer the result to the state cache.

        Returns the cache's accepted state (which may be the previous one when a
        capture failed) or ``None`` when nothing has ever been observed.
        """
        del force  # the cache already decides which observations are new
        try:
            frame = self._engine.grab()
        except BlaxcyError:
            self._counters.capture_failures += 1
            # A failed capture is not an observation: the honest answer is the
            # cache's current state (or nothing), never a synthesized one.
            return self._cache.current

        delta = self._delta_for(frame)
        elements = self._accessibility_elements()
        ocr_elements = self._ocr_pass(frame, delta)
        combined = elements + ocr_elements
        state = self._build_state(frame, combined)

        accepted = self._cache.update_screen_state(state, delta=delta)
        if accepted is None:
            self._counters.rejected_updates += 1
            return self._cache.current

        self._last_frame = frame
        self._counters.cycles += 1
        self._counters.ocr_elements += len(ocr_elements)
        return accepted

    def __call__(self) -> ScreenState | None:
        """Make the orchestrator directly usable as the executor's hook."""
        return self.perceive()

    # -- The section 43 fallback cascade ---------------------------------------

    def enrich(self, query: ElementQuery, *, mode: PolicyMode) -> EnrichmentReport:
        """Try the perception fallbacks and accept an enriched state.

        The caller has already established that the deterministic cascade found
        nothing (status ``NOT_FOUND``). This method re-checks that premise
        against a *fresh* observation, runs the OCR stage over the changed
        regions, then -- only if OCR still produced nothing that resolves and only
        under the section 41/42 gate -- the visual stage, and accepts the result
        so the ordinary resolution, lease, revalidation and verification run
        against exactly these elements.

        Never called for an ``AMBIGUOUS`` result: ambiguity is a decision, and a
        perception fallback is not entitled to overrule it (section 43).
        """
        if not query.text or not query.text.strip():
            # Nothing to look for; visual grounding would refuse an empty
            # description anyway, so do not spend a capture on it.
            return EnrichmentReport(enriched=False, refused="empty target description")

        try:
            frame = self._engine.grab()
        except BlaxcyError as exc:
            self._counters.capture_failures += 1
            return EnrichmentReport(enriched=False, refused=exc.code.value)

        delta = self._delta_for(frame)
        base = self._accessibility_elements()
        active = self._active_window()
        current = self._cache.current
        if current is None:
            return EnrichmentReport(enriched=False, refused=ErrorCode.CAPTURE_FAILED.value)

        # Re-check the premise: if the deterministic cascade already resolves
        # against the fresh observation there is nothing to enrich. Ambiguity
        # ends the attempt outright (section 43's absolute rule).
        deterministic = self._resolver.resolve(
            query, base, occluders=base, state=current
        )
        if deterministic.status is ResolutionStatus.AMBIGUOUS:
            return EnrichmentReport(enriched=False, refused="ambiguous; never enriched")
        if deterministic.status is ResolutionStatus.RESOLVED:
            return EnrichmentReport(enriched=False, refused="already resolved")

        extra: list[UIElement] = []
        ocr_elements = self._ocr_pass(frame, delta, _candidate_regions(deterministic))
        if ocr_elements:
            self._counters.enrichment_ocr += 1
            extra.extend(ocr_elements)

        if not self._resolves(query, base + tuple(extra), current):
            visual_elements, refusal = self._visual_pass(frame, query, base, mode, active)
            if refusal is not None:
                return EnrichmentReport(
                    enriched=False,
                    ocr_elements=len(ocr_elements),
                    refused=refusal,
                )
            if visual_elements:
                self._counters.enrichment_visual += 1
                extra.extend(visual_elements)

        if not extra:
            return EnrichmentReport(enriched=False, ocr_elements=len(ocr_elements))

        try:
            enriched = self._build_state(frame, base + tuple(extra), window=active)
        except BlaxcyError as exc:  # pragma: no cover - defensive
            return EnrichmentReport(enriched=False, refused=exc.code.value)
        accepted = self._cache.update_screen_state(enriched, delta=delta)
        if accepted is None:
            self._counters.rejected_updates += 1
            return EnrichmentReport(enriched=False, refused="stale enrichment")
        self._last_frame = frame
        self._counters.enrichments += 1
        self._counters.ocr_elements += len(ocr_elements)
        return EnrichmentReport(
            enriched=True,
            ocr_elements=len(ocr_elements),
            visual_elements=len(extra) - len(ocr_elements),
            state=accepted,
        )

    def resolve_target(self, query: ElementQuery, *, mode: PolicyMode) -> ResolutionResult:
        """Resolve ``query`` through the full section 43 cascade, read-only.

        This is the read path's entry point (``find_element``). It runs the
        deterministic cascade, then delegates to :meth:`enrich` when nothing was
        found, and finally reports the resolution against whatever state the
        cache now holds. It never injects input and never issues a lease.
        """
        current = self._cache.current
        if current is None:
            current = self.perceive()
        if current is None:
            return self._resolver.resolve(query, (), state=None)
        first = self._resolver.resolve(
            query, current.elements, occluders=current.elements, state=current
        )
        if first.is_resolved or first.status is ResolutionStatus.AMBIGUOUS:
            return first
        report = self.enrich(query, mode=mode)
        if not report.enriched:
            return first
        refreshed = self._cache.current
        if refreshed is None:
            return first
        return self._resolver.resolve(
            query, refreshed.elements, occluders=refreshed.elements, state=refreshed
        )

    # -- Stages ----------------------------------------------------------------

    def _accessibility_elements(self) -> tuple[UIElement, ...]:
        """The AT-SPI observation, annotated with any discovered browser URL."""
        if self._a11y is None:
            return ()
        try:
            elements = self._a11y.elements()
        except BlaxcyError as exc:
            self._counters.poison.append(f"accessibility query failed: {exc.code.value}")
            return ()
        return _annotate_browsers(elements, self._counters)

    def _ocr_pass(
        self,
        frame: Frame,
        delta: Any,
        near_miss: Sequence[Rect] = (),
    ) -> tuple[UIElement, ...]:
        """OCR the *relevant* regions, and only those (section 40).

        Two bounded region sources, in order:

        1. the boxes of the resolver's own near-miss candidates -- the controls
           that matched the description partially but not well enough. This is
           the section 43 cascade's own notion of "relevant": the deterministic
           stages pointed at somewhere concrete and fell short, so reading that
           area is a targeted second look rather than a scan;
        2. the regions a *significant* change flagged, when there are no
           candidates at all.

        With neither, there is no bounded evidence about where text might be, and
        the honest result is no OCR at all -- the cascade falls through to visual
        grounding instead of scanning the desktop, which section 40 forbids.
        """
        engine = self._ocr
        if engine is None:
            return ()
        candidates = [rect for rect in near_miss if rect.area > 0.0]
        if not candidates:
            candidates = list(_changed_regions(delta))
        if not candidates:
            return ()
        # Credential geometry is excluded by the engine itself; passing it again
        # here would be a second, weaker copy of the same rule.
        try:
            self._counters.ocr_runs += 1
            result = engine.recognize(
                frame,
                candidates=candidates,
                frame_id=frame.frame_id,
                generation=frame.generation,
            )
        except BlaxcyError as exc:
            self._counters.poison.append(f"ocr failed: {exc.code.value}")
            return ()
        return engine.as_elements(result, frame_id=frame.frame_id, generation=frame.generation)

    def _visual_pass(
        self,
        frame: Frame,
        query: ElementQuery,
        elements: tuple[UIElement, ...],
        mode: PolicyMode,
        active: _ActiveWindow,
    ) -> tuple[tuple[UIElement, ...], str | None]:
        """The gated visual stage: returns elements, or a refusal reason.

        Every refusal is one of the section 41/42 branches, composed by
        :func:`policy.guards.check_visual_fallback` so the rule is not
        re-implemented here.
        """
        grounder = self._visual
        if grounder is None:
            return (), "visual grounding is not wired"
        if not grounder.enabled:
            return (), "visual grounding is disabled by configuration"

        verdict: GuardOutcome = check_visual_fallback(
            mode=mode,
            enabled=grounder.enabled,
            application=self._application_strings(elements, active),
            protected=self._settings.safety.protected_applications,
            blocked=self._settings.safety.blocked_applications,
        )
        if not verdict.ok:
            self._counters.visual_refusals += 1
            return (), verdict.message

        credentials = tuple(element for element in elements if element.is_password)
        try:
            result = grounder.ground(
                frame,
                query=query.text or "",
                mode=mode,
                password_elements=credentials,
                frame_id=frame.frame_id,
                generation=frame.generation,
            )
            produced = grounder.as_elements(
                result,
                frame_id=frame.frame_id,
                generation=frame.generation,
                role=query.role_hint or UIRole.UNKNOWN,
                owner_app=active.app,
                owner_window_id=active.window_id,
            )
        except BlaxcyError as exc:
            # A transport or privacy refusal is a real outcome, not an error to
            # swallow: it is counted and reported, and the cascade simply stops.
            self._counters.visual_failures += 1
            return (), exc.code.value
        return produced, None

    # -- Assembly --------------------------------------------------------------

    def _build_state(
        self,
        frame: Frame,
        elements: Sequence[UIElement],
        *,
        window: _ActiveWindow | None = None,
    ) -> ScreenState:
        """Assemble the immutable observation from a frame and its elements."""
        active = window if window is not None else self._active_window()
        layout = self._layout(frame)
        return ScreenState(
            frame_id=frame.frame_id,
            state_version=frame.frame_id,
            generation=frame.generation,
            timestamp=frame.captured_at,
            monotonic=frame.captured_at_monotonic,
            layout=layout,
            elements=tuple(elements),
            active_window_id=active.window_id,
            active_app=active.app,
            thumbnail_size=Size(
                width=frame.thumbnail_size[0], height=frame.thumbnail_size[1]
            ),
            capture_backend=frame.backend,
            window_stack=self._window_stack(),
        )

    def _window_stack(self) -> tuple[WindowStackEntry, ...]:
        """The window manager's stacking order, or ``()`` when it is unknown.

        Read fresh for every observation, because the order changes as windows are
        raised and lowered. An empty result is a genuine "unknown" and is passed on
        as such: the section 46 rule falls back to its conservative behaviour rather
        than assuming nothing is in front (sections 4 rule 8, 46).
        """
        reader = getattr(self._windows, "stacked_windows", None)
        if reader is None:
            return ()
        try:
            windows = reader()
        except Exception:
            return ()
        entries: list[WindowStackEntry] = []
        for window in windows:
            entries.append(
                WindowStackEntry(
                    window_id=int(window.window_id),
                    title=window.title,
                    pid=window.pid,
                )
            )
        return tuple(entries)

    def _layout(self, frame: Frame) -> MonitorLayout:
        """The monitor topology, derived from the backend and the frame itself.

        A topology is never invented: when the backend reports nothing usable the
        captured frame's own rectangle is used as the single monitor, which is a
        measurement of what was actually captured.
        """
        layout = layout_from_topology(self._engine.monitor_topology)
        if layout is not None:
            return layout
        rect = frame.desktop_rect
        return MonitorLayout(
            monitors=(
                MonitorGeometry(
                    monitor_id=0,
                    origin_x=rect.x,
                    origin_y=rect.y,
                    width=max(1, int(rect.width)),
                    height=max(1, int(rect.height)),
                    is_primary=True,
                ),
            )
        )

    def _delta_for(self, frame: Frame) -> Any:
        """The classified delta from the previous frame, when there is one."""
        previous = self._last_frame
        if previous is None or frame.frame_id <= previous.frame_id:
            return None
        try:
            return self._detector.compare(previous, frame)
        except ValueError:  # pragma: no cover - defensive: ids are checked above
            return None

    # -- Window context --------------------------------------------------------

    def _active_window(self) -> _ActiveWindow:
        """Read the active window id and a representative application string.

        The window manager read is best-effort evidence, never a substitute for
        AT-SPI's own application name: both end up as section 58/42 candidates.
        """
        manager = self._windows
        if manager is None:
            return _ActiveWindow()
        try:
            window_id = manager.active_window()
            info = None
            if window_id is not None:
                # A controller that cannot describe a window contributes less
                # context; it must not turn a perception cycle into a failure.
                reader = getattr(manager, "window_info", None)
                if callable(reader):
                    info = reader(window_id)
        except BlaxcyError:
            return _ActiveWindow()
        if info is None:
            return _ActiveWindow(window_id=window_id)
        app = info.wm_class or info.wm_instance or info.title
        return _ActiveWindow(window_id=window_id, app=app)

    def _application_strings(
        self, elements: Sequence[UIElement], active: _ActiveWindow
    ) -> tuple[str | None, ...]:
        """Every application string a section 42/58 list should be matched against."""
        candidates: list[str | None] = []
        if active.app:
            candidates.append(active.app)
        for element in elements:
            if element.owner_app and element.owner_app not in candidates:
                candidates.append(element.owner_app)
        return tuple(candidates)

    # -- Diagnostics -----------------------------------------------------------

    def diagnostics(self) -> dict[str, Any]:
        """A JSON-shaped measurement of what this layer has actually done."""
        return {
            "started": self._started,
            "cycles": self._counters.cycles,
            "capture_failures": self._counters.capture_failures,
            "rejected_updates": self._counters.rejected_updates,
            "ocr_available": self._ocr is not None,
            "ocr_runs": self._counters.ocr_runs,
            "ocr_elements": self._counters.ocr_elements,
            "visual_available": self._visual is not None,
            "visual_refusals": self._counters.visual_refusals,
            "visual_failures": self._counters.visual_failures,
            "enrichments": self._counters.enrichments,
            "enrichment_ocr": self._counters.enrichment_ocr,
            "enrichment_visual": self._counters.enrichment_visual,
            "browser_contexts": self._counters.browser_contexts,
            "accessibility": "wired" if self._a11y is not None else "absent",
            "notes": list(self._counters.poison),
        }

    def _resolves(
        self, query: ElementQuery, elements: tuple[UIElement, ...], state: ScreenState
    ) -> bool:
        """Whether ``query`` resolves (or became ambiguous) against ``elements``."""
        result = self._resolver.resolve(query, elements, occluders=elements, state=state)
        return result.status in (ResolutionStatus.RESOLVED, ResolutionStatus.AMBIGUOUS)


@dataclass(frozen=True)
class _ActiveWindow:
    """The window context of one observation, as far as it is knowable."""

    window_id: int | None = None
    app: str | None = None


def _changed_regions(delta: Any) -> tuple[Rect, ...]:
    """The significant changed rectangles a delta reported, or an empty tuple."""
    if delta is None:
        return ()
    return tuple(
        region.rect
        for region in delta.regions
        if region.change_class in SIGNIFICANT_CHANGE_CLASSES
    )


def _candidate_regions(resolution: ResolutionResult) -> tuple[Rect, ...]:
    """The boxes of a resolution's own candidates, for the OCR stage.

    These are the elements that matched the description *partially*: the exact
    answer to "where should a second look be aimed?". A candidate without usable
    geometry contributes nothing, because a region invented from nothing would be
    exactly the unbounded scan section 40 forbids.
    """
    regions: list[Rect] = []
    for candidate in resolution.candidates:
        box = candidate.element.bbox
        if box is not None and box.area > 0.0:
            regions.append(box)
    return tuple(regions)


def _annotate_browsers(
    elements: Sequence[UIElement], counters: _Counters
) -> tuple[UIElement, ...]:
    """Attach a discovered browser URL to that browser's elements (section 36).

    The browser layer consumes accessibility observations rather than calling
    AT-SPI itself, so there is still exactly one accessibility owner (section 35).
    An element is only annotated when a real URL was discovered -- never with a
    guessed one -- and the element's own geometry and provenance are untouched.
    """
    if not elements:
        return ()
    try:
        contexts = discover_contexts(elements)
    except Exception:  # pragma: no cover - defensive: discovery is pure
        return tuple(elements)
    if not contexts:
        return tuple(elements)
    counters.browser_contexts += len(contexts)
    by_app = {
        context.app_name: context.url
        for context in contexts
        if context.url is not None
    }
    if not by_app:
        return tuple(elements)
    annotated: list[UIElement] = []
    for element in elements:
        url = by_app.get(element.owner_app or "")
        if url is None or element.browser_url == url:
            annotated.append(element)
        else:
            annotated.append(element.model_copy(update={"browser_url": url}))
    return tuple(annotated)


def _default_resolver(settings: Settings) -> TargetResolver:
    """Build the resolver the orchestrator uses, wired as the executor wires it.

    Kept as one helper so the semantic score floor the section 43.1 cache uses is
    applied here too: if the two resolutions disagreed, the fallback would be
    deciding on a different cascade than the caller it is helping.
    """
    from core.resolver_cache import scorer_or_none

    return TargetResolver(
        settings.resolver,
        semantic_scorer=scorer_or_none(),
        semantic_match_floor=float(settings.resolver_cache.semantic_match_floor),
    )


__all__ = [
    "EnrichmentReport",
    "PerceptionOrchestrator",
]
