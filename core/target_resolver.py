"""Target resolution, element leases and occlusion (specification sections 43-46).

This module turns a Brain-supplied target *description* (an
:class:`~schemas.elements.ElementQuery`) into a ranked, scored, explicit set of
candidates over the elements the perception layer actually observed. It is
deliberately pure: it never injects input, never changes focus, never touches a
display and never mutates the state it is given. Everything below is
deterministic arithmetic over the elements already in a
:class:`~schemas.screen_state.ScreenState`.

Three guarantees from the specification are enforced here rather than trusted to
callers:

* **The absolute ambiguity rule** (section 43). If two visible candidates share
  the same normalized text *and* the same role, the result is ``AMBIGUOUS`` --
  even under AUTONOMOUS policy, even with perfect confidence, even if a future
  resolver cache or visual-grounding source claims otherwise. Ambiguity is never
  out-voted. The rule is judged over the candidates **competitive with a decisive
  text match**: when the query names text and at least one candidate matched it
  decisively, only the text-matching candidates are considered, so neither a
  role-only control nor an unrelated duplicate elsewhere on the desktop can veto a
  unique, decisively-named target. A text-less query, or a query whose only matches
  are weak substring/fuzzy hits, judges every candidate exactly as before.
* **Occlusion is not "element exists"** (section 46). A target more than 50%
  covered by a known occluder is not actionable, and BLAXCY never clicks
  "through" another window. With no geometry at all the target is not silently
  treated as clickable either: occlusion is reported as unassessable.
* **A lease is bound to one observation** (section 44). :func:`bind_lease` stamps
  a lease with the exact ``frame_id``/``state_version``/``generation`` of the
  state it was resolved against, so a lease can never silently outlive the
  perception it was based on.

The resolver cache and rapidfuzz semantic matching of section 43.1 are wired in
here as an **optional hint**, never as an authority: when a
:class:`~core.resolver_cache.ResolverCache` is supplied it tells the resolver
where the element last was, the resolver re-queries that identity against the
live elements, and the cache can never change a score, the candidate set, the
ambiguity verdict or the threshold. With no cache (or a disabled one) the
decisions are identical, only computed from scratch (section 4 rule 31).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from config.settings import ResolverSettings
from core.resolver_cache import (
    CacheLookup,
    CacheOutcome,
    IdentityHint,
    ResolverCache,
    identity_path,
)
from schemas.elements import ElementCandidate, ElementQuery, UIElement
from schemas.enums import SOURCE_BASE_CONFIDENCE
from schemas.geometry import Rect
from schemas.leases import DEFAULT_LEASE_TTL_MS, ElementLease, issue_lease
from schemas.screen_state import ScreenState, WindowStackEntry

#: Default occlusion threshold (section 46): more covered than this is not
#: actionable. The rule is conservative by design -- only an exact backend may
#: ever prove otherwise, and no such proof exists at this phase.
DEFAULT_OCCLUSION_THRESHOLD: float = 0.50

#: Cascade stage order (section 43). A candidate's reported stage is the first
#: of these that produced a real match.
CASCADE_STAGES: tuple[str, ...] = (
    "exact_text",
    "case_insensitive_text",
    "normalized_text",
    "accessible_name",
    "role",
    "context",
    "dom_path",
    # The weak text signals are appended last: they are text evidence, but they are
    # not a match of the query's own text, so a real cascade stage outranks them
    # when one is reported and they are excluded from TEXT_MATCH_STAGES below.
    "substring_text",
    "fuzzy_text",
)

#: The cascade stages that represent a match of the query's *own text* (section
#: 43). A candidate whose stage is one of these actually matched the requested
#: text; a candidate that matched only the role/context -- or only the weak
#: substring / section 43.1 fuzzy signals -- is a weaker stage and must not create
#: an ambiguity against a decisive text match. The weak signals are deliberately
#: **not** listed. A generic control whose label merely shares a token with the
#: query scores 100 on ``token_set_ratio`` (``("Search Address Bar", "Search")``
#: is such a pair), which used to land it inside the gap of a decisive
#: accessible-name match and veto the action. A weak signal still contributes to
#: the score; it is simply not grounds for refusing a decisive match.
TEXT_MATCH_STAGES: frozenset[str] = frozenset(
    {"exact_text", "case_insensitive_text", "normalized_text", "accessible_name"}
)

#: A candidate whose text component is above this is a **decisive** textual match
#: (exact, case-insensitive, normalized or accessible-name), as opposed to the
#: weak substring (0.55) and section 43.1 fuzzy (at most 0.80) signals. Only a
#: decisive match narrows the ambiguity rule to the text-matching candidates, so a
#: genuinely weak match still obeys the full conservative rule.
DECISIVE_TEXT_FLOOR: float = 0.80

#: An object that can occlude a target: a perceived element, or a bare rect
#: (e.g. a window known to sit above the target's window).
Occluder = UIElement | Rect


class ResolutionStatus(StrEnum):
    """The outcome of a resolution attempt (section 43)."""

    RESOLVED = "RESOLVED"
    NOT_FOUND = "NOT_FOUND"
    AMBIGUOUS = "AMBIGUOUS"
    BELOW_THRESHOLD = "BELOW_THRESHOLD"


class OcclusionAssessment(BaseModel):
    """How covered a target is by known occluders (section 46)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    target_id: str = Field(min_length=1)
    ratio: float = Field(ge=0.0, le=1.0)
    threshold: float = Field(ge=0.0, le=1.0)
    blocked: bool
    assessed: bool = Field(
        description="False when the target has no geometry, so coverage cannot be measured."
    )
    declared_occluded: bool = Field(
        description="True when perception itself already marked the element occluded."
    )
    covering: tuple[str, ...] = ()
    reason: str

    @property
    def is_actionable(self) -> bool:
        """True when the target may be acted on, judged only by occlusion."""
        return self.assessed and not self.blocked and not self.declared_occluded

    def to_dict(self) -> dict[str, Any]:
        """JSON-shaped form for the tool envelope."""
        return {
            "target_id": self.target_id,
            "ratio": self.ratio,
            "threshold": self.threshold,
            "blocked": self.blocked,
            "assessed": self.assessed,
            "declared_occluded": self.declared_occluded,
            "covering": list(self.covering),
            "reason": self.reason,
        }


class ResolutionResult(BaseModel):
    """A complete, explicit resolution outcome (section 43)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: ResolutionStatus
    query: ElementQuery
    candidates: tuple[ElementCandidate, ...] = ()
    best: ElementCandidate | None = None
    reason: str
    considered: int = Field(ge=0)
    matched_stage: str | None = None
    ambiguity: bool = False
    occlusion: OcclusionAssessment | None = None
    cache: CacheLookup | None = Field(
        default=None,
        description=(
            "The section 43.1 cache lookup that preceded this resolution. Purely "
            "diagnostic: it never contributed a score or a candidate."
        ),
    )

    @property
    def cache_hit(self) -> bool:
        """True when a live identity hint was available for this description."""
        return self.cache is not None and self.cache.hit

    @property
    def is_resolved(self) -> bool:
        """True when a single unambiguous candidate cleared the threshold."""
        return self.status is ResolutionStatus.RESOLVED

    @property
    def element(self) -> UIElement | None:
        """The winning element, or ``None`` when unresolved."""
        return self.best.element if self.best is not None else None

    @property
    def is_actionable(self) -> bool:
        """True when resolved *and* not blocked by occlusion (sections 43, 46)."""
        if not self.is_resolved:
            return False
        if self.occlusion is None:
            return True
        return self.occlusion.is_actionable

    def to_dict(self) -> dict[str, Any]:
        """JSON-shaped result, safe to send to the Brain (never element secrets)."""
        return {
            "status": self.status.value,
            "reason": self.reason,
            "considered": self.considered,
            "matched_stage": self.matched_stage,
            "ambiguity": self.ambiguity,
            "best": self.best.to_dict() if self.best is not None else None,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "occlusion": self.occlusion.to_dict() if self.occlusion is not None else None,
            "cache": self.cache.to_dict() if self.cache is not None else None,
        }


def _normalize(value: str) -> str:
    """Whitespace-collapsed, case-folded text (section 43 normalization)."""
    return " ".join(value.split()).casefold()


def _element_label(element: UIElement) -> str:
    """The label used for normalized-text ambiguity checks."""
    return _normalize(element.text or element.accessible_name or "")


def _occluder_bbox(occluder: Occluder) -> Rect | None:
    """The rectangle of an occluder, when it has one."""
    if isinstance(occluder, Rect):
        return occluder
    return occluder.bbox


def _occluder_id(occluder: Occluder, index: int) -> str:
    """A stable identifier for an occluder, for reporting."""
    if isinstance(occluder, Rect):
        return f"rect:{index}"
    return occluder.element_id or f"element:{index}"


def assess_occlusion(
    target: UIElement,
    occluders: Sequence[Occluder] = (),
    *,
    threshold: float = DEFAULT_OCCLUSION_THRESHOLD,
) -> OcclusionAssessment:
    """Assess how much of ``target`` is covered by ``occluders`` (section 46).

    Coverage is the *maximum* single-occluder ratio, not the sum: overlapping
    occluders must not be double-counted into an inflated figure.

    Args:
        target: The element about to be acted on.
        occluders: Known objects above the target. Only same-space geometry is
            compared; a mismatched-space occluder is ignored rather than
            silently converted (section 31).
        threshold: Coverage strictly greater than this is not actionable.
    """
    if target.occluded:
        return OcclusionAssessment(
            target_id=target.element_id,
            ratio=1.0,
            threshold=threshold,
            blocked=True,
            assessed=True,
            declared_occluded=True,
            reason="perception already marked this element as occluded",
        )

    bbox = target.bbox
    if bbox is None:
        return OcclusionAssessment(
            target_id=target.element_id,
            ratio=0.0,
            threshold=threshold,
            blocked=False,
            assessed=False,
            declared_occluded=False,
            reason="target has no geometry, so occlusion cannot be assessed",
        )

    best_ratio = 0.0
    covering: list[str] = []
    for index, occluder in enumerate(occluders):
        if isinstance(occluder, UIElement):
            if occluder.element_id == target.element_id:
                continue
            if not occluder.visible:
                continue
        other = _occluder_bbox(occluder)
        if other is None or other.space is not bbox.space:
            continue
        ratio = bbox.occlusion_ratio(other)
        if ratio > best_ratio:
            best_ratio = ratio
        if ratio > threshold:
            covering.append(_occluder_id(occluder, index))

    blocked = best_ratio > threshold
    reason = (
        f"covered {best_ratio:.2f} > threshold {threshold:.2f}"
        if blocked
        else f"nothing covers more than {threshold:.2f} of the target"
    )
    return OcclusionAssessment(
        target_id=target.element_id,
        ratio=min(1.0, best_ratio),
        threshold=threshold,
        blocked=blocked,
        assessed=True,
        declared_occluded=False,
        covering=tuple(covering),
        reason=reason,
    )


def _is_ancestor_path(ancestor: str | None, descendant: str | None) -> bool:
    """Whether ``ancestor``'s accessibility path contains ``descendant``.

    Paths are ``/``-separated node indices (``desktop[0]/25/0/1``). A container is
    a *segment* prefix of everything it holds, so ``desktop[0]/2`` is not an
    ancestor of ``desktop[0]/25`` -- which a plain string-prefix test would
    wrongly conclude.
    """
    if not ancestor or not descendant:
        return False
    outer = ancestor.strip("/").split("/")
    inner = descendant.strip("/").split("/")
    return len(outer) < len(inner) and inner[: len(outer)] == outer


def _title_position(
    title: str | None,
    stack: Sequence[WindowStackEntry],
    indices: Sequence[int],
) -> int | None:
    """The highest stacking position among ``indices`` matching ``title``.

    Titles come from two sources -- the accessibility tree's window node and the
    window manager's ``_NET_WM_NAME`` -- and they are not always byte-identical
    (a browser publishes a decorated name to the window manager and a different
    one to accessibility). Matching therefore falls back to containment in either
    direction. Taking the highest of several matches is the conservative choice:
    the topmost instance is the one that could be in front.
    """
    if not title:
        return None
    exact = [index for index in indices if stack[index].title == title]
    if exact:
        return exact[-1]
    contained: list[int] = []
    for index in indices:
        name = stack[index].title
        if name and (name in title or title in name):
            contained.append(index)
    return contained[-1] if contained else None


def _window_position(
    element: UIElement,
    stack: Sequence[WindowStackEntry],
    *,
    lowest: bool,
) -> int | None:
    """The stacking position of the window that owns ``element``, or ``None``.

    The pid is tried first because it is exact: accessibility reports the process
    id of the window's owner (``get_process_id()``) and the window manager reports
    the same process in ``_NET_WM_PID``, so the two join without any guessing. The
    title is the fallback for hosts where either side omits the pid, and it is a
    heuristic by nature.

    ``lowest`` breaks an ambiguity, and the direction is a safety decision rather
    than a cosmetic one:

    * for the **target**, assume the lowest candidate window, so that as many
      windows as possible count as being above it;
    * for a **candidate occluder**, assume the highest, so that it counts as being
      above as often as possible.

    Both directions widen the resulting occluder set, which is the only direction
    a safety check may err in.
    """
    if not stack:
        return None
    indices = list(range(len(stack)))
    if element.owner_app_pid is not None:
        same_pid = [index for index in indices if stack[index].pid == element.owner_app_pid]
        if len(same_pid) == 1:
            return same_pid[0]
        if same_pid:
            by_title = _title_position(element.owner_window_title, stack, same_pid)
            if by_title is not None:
                return by_title
            return same_pid[0] if lowest else same_pid[-1]
    return _title_position(element.owner_window_title, stack, indices)


def occluders_above(
    target: UIElement,
    candidates: Sequence[Occluder],
    stack: Sequence[WindowStackEntry] = (),
) -> tuple[Occluder, ...]:
    """Narrow a candidate occluder set to the objects that can hide ``target``.

    Section 46 asks whether something is *in front of* the target. Callers hand
    over every perceived element, which includes the desktop background and the
    windows **behind** the target -- objects that cannot hide it, and which on a
    real desktop make every target read as fully covered. Window stacking is the
    only honest source of "in front" (geometry cannot tell behind from in front),
    so it is used whenever it is known:

    * the target's **ancestors** are dropped. A container cannot hide what it
      contains, and on a real desktop the target's own window frame and the
      ``desktop[0]`` root cover it completely;
    * both the target's window and the candidate's window have a known position ->
      the candidate counts only when its window is **at or above** the target's.
      The same window counts, because an overlay inside one window really can
      cover a control in it;
    * either position is unknown, or no stacking information is available -> the
      candidate is **kept**.

    Keeping the unknown case is deliberate: a missing stacking list must never
    become a permission, so without this information the check stays exactly as
    conservative as it was before this function existed. Non-element occluders
    (bare rectangles) carry no ownership at all and are likewise kept.
    """
    kept: list[Occluder] = []
    for candidate in candidates:
        if isinstance(candidate, UIElement) and _is_ancestor_path(
            candidate.atspi_path, target.atspi_path
        ):
            continue
        kept.append(candidate)
    target_position = _window_position(target, stack, lowest=True)
    if not stack or target_position is None:
        return tuple(kept)
    return tuple(
        candidate
        for candidate in kept
        if (
            not isinstance(candidate, UIElement)
            or (position := _window_position(candidate, stack, lowest=False)) is None
            or position > target_position
            or (
                position == target_position
                and (candidate.clickable or candidate.effective_clickable)
            )
        )
    )


class TargetResolver:
    """Deterministic target resolution over observed elements (section 43).

    Args:
        settings: The ``[resolver]`` configuration (weights, threshold, limits).
        semantic_scorer: Optional ``(query, label) -> 0..100`` fuzzy score. This
            is the section 43.1 hook; it is inert unless supplied, because that
            optimization ships disabled (section 4 rule 31). When supplied it
            still only feeds the ``text`` weight and can never override the
            ambiguity rule.
        semantic_match_floor: Below this, a fuzzy score is not a text match.
        cache: Optional section 43.1 cache. It is a traversal-order hint only:
            a hit can never change a score, the candidate set or the ambiguity
            verdict, so a disabled cache yields identical decisions.
        occlusion_threshold: Coverage above this is not actionable (section 46).
    """

    def __init__(
        self,
        settings: ResolverSettings,
        *,
        semantic_scorer: Callable[[str, str], float] | None = None,
        semantic_match_floor: float = 100.0,
        occlusion_threshold: float = DEFAULT_OCCLUSION_THRESHOLD,
        cache: ResolverCache | None = None,
    ) -> None:
        self.settings = settings
        self._semantic_scorer = semantic_scorer
        self._semantic_match_floor = semantic_match_floor
        self._occlusion_threshold = occlusion_threshold
        self._cache = cache
        self._resolutions = 0
        self._ambiguous = 0
        self._not_found = 0
        self._cache_hits = 0

    # -- Resolution -----------------------------------------------------------

    def resolve(
        self,
        query: ElementQuery,
        elements: Sequence[UIElement],
        *,
        occluders: Sequence[Occluder] = (),
        state: ScreenState | None = None,
    ) -> ResolutionResult:
        """Resolve ``query`` against ``elements`` (section 43).

        Args:
            query: The target description. It may name text, a role, context, a
                window, or any combination -- never a coordinate or a lease.
            elements: The elements to search, normally ``ScreenState.elements``.
            occluders: Objects known to sit above the target, for the section 46
                check against the winning candidate.
            state: The observation ``elements`` came from. Supplying it enables
                the section 43.1 cache to *record* a successful resolution (and
                to know the state's active window); it is never used for scoring.
        """
        lookup = self._lookup(query, elements, state=state)
        considered = len(elements)
        scored: list[tuple[float, str, ElementCandidate, str | None]] = []
        for element in elements:
            if not element.visible:
                continue
            if query.require_actionable and not element.is_actionable:
                continue
            if query.window_id is not None and element.owner_window_id != query.window_id:
                continue
            candidate, stage = self._score(query, element)
            if stage is None:
                continue
            scored.append((candidate.score, element.element_id, candidate, stage))

        self._resolutions += 1
        if not scored:
            self._not_found += 1
            return ResolutionResult(
                status=ResolutionStatus.NOT_FOUND,
                query=query,
                reason="no visible element matched the requested target",
                considered=considered,
                occlusion=None,
                cache=lookup,
            )

        # Deterministic order: score descending, then element id ascending.
        scored.sort(key=lambda item: (-item[0], item[1]))
        ranked = tuple(item[2] for item in scored)
        top_score, _, top, top_stage = scored[0]
        kept = ranked[: self.settings.max_candidates]

        ambiguous, ambiguity_reason = self._check_ambiguity(scored, query)
        # Section 46: only objects *above* the winner can hide it. The caller's
        # candidate list is everything it perceived, so it is narrowed to the
        # windows stacked at or above the target's (never widened -- an unknown
        # stacking order leaves the list exactly as it was).
        occlusion = assess_occlusion(
            top.element,
            occluders_above(
                top.element, occluders, state.window_stack if state is not None else ()
            ),
            threshold=self._occlusion_threshold,
        )

        if ambiguous:
            self._ambiguous += 1
            return ResolutionResult(
                status=ResolutionStatus.AMBIGUOUS,
                query=query,
                candidates=kept,
                best=None,
                reason=ambiguity_reason,
                considered=considered,
                matched_stage=top_stage,
                ambiguity=True,
                occlusion=occlusion,
                cache=lookup,
            )

        if top_score < self.settings.act_threshold:
            return ResolutionResult(
                status=ResolutionStatus.BELOW_THRESHOLD,
                query=query,
                candidates=kept,
                best=None,
                reason=(
                    f"best candidate scored {top_score:.3f}, below act_threshold "
                    f"{self.settings.act_threshold:.3f}"
                ),
                considered=considered,
                matched_stage=top_stage,
                occlusion=occlusion,
                cache=lookup,
            )

        self._record(query, top.element, state=state)
        return ResolutionResult(
            status=ResolutionStatus.RESOLVED,
            query=query,
            candidates=kept,
            best=top,
            reason=f"resolved at stage '{top_stage}' with score {top_score:.3f}",
            considered=considered,
            matched_stage=top_stage,
            occlusion=occlusion,
            cache=lookup,
        )

    # -- Section 43.1 cache (hint only) ---------------------------------------

    def _lookup(
        self,
        query: ElementQuery,
        elements: Sequence[UIElement],
        *,
        state: ScreenState | None,
    ) -> CacheLookup | None:
        """Consult the cache and re-verify its hint against the live elements.

        A hint is only usable when the identity path it names is still present
        in the observation being resolved against. A hint whose element is gone
        is dropped here, and the resolution continues down the normal cascade --
        the cache never stands in for a live lookup (section 43.1).
        """
        cache = self._cache
        if cache is None:
            return None
        lookup = cache.lookup(
            query,
            active_window_id=None if state is None else state.active_window_id,
        )
        hint: IdentityHint | None = lookup.hint
        if hint is None:
            return lookup
        if any(identity_path(element) == hint.identity_path for element in elements):
            self._cache_hits += 1
            return lookup
        # The identity path is gone from this observation, so *this* entry is
        # provably wrong and is dropped; other windows' hints for the same
        # description are left alone.
        cache.forget(query, hint)
        return CacheLookup(
            CacheOutcome.MISS,
            reason="the hinted identity path is no longer present in this observation",
        )

    def _record(
        self, query: ElementQuery, element: UIElement, *, state: ScreenState | None
    ) -> None:
        """Record a successful resolution in the cache, when there is one.

        The observation's active window is passed through as the window key's
        fallback, because an element the accessibility traversal did not
        attribute to a window carries ``owner_window_id = None`` (the common
        case on a live desktop). Without this, the hint would be keyed under
        ``None`` while a lookup filters on the real active window, so the entry
        could never be found -- the cache would be silently inert (section 43.1).
        """
        if self._cache is None or state is None:
            return
        self._cache.record(query, element, state, active_window_id=state.active_window_id)

    # -- Scoring --------------------------------------------------------------

    def _score(self, query: ElementQuery, element: UIElement) -> tuple[ElementCandidate, str | None]:
        """Score one element, returning its candidate and cascade stage.

        A ``None`` stage means the element did not match any constrained
        dimension and is not a candidate at all. Neutral components (a dimension
        the query does not constrain) contribute a full ``1.0`` so that, for
        example, a role-only query is not silently penalised on text.
        """
        text_score, text_stage = self._text_component(query, element)
        role_score, role_stage = self._role_component(query, element)
        context_score, context_stage = self._context_component(query, element)
        source_score = self._source_component(element)
        geometry_score = self._geometry_component(element)

        score = (
            self.settings.weight_text * text_score
            + self.settings.weight_source * source_score
            + self.settings.weight_role * role_score
            + self.settings.weight_context * context_score
            + self.settings.weight_geometry * geometry_score
        )
        score = max(0.0, min(1.0, score))

        breakdown = {
            "text": text_score,
            "source": source_score,
            "role": role_score,
            "context": context_score,
            "geometry": geometry_score,
        }
        candidate = ElementCandidate(element=element, score=score, breakdown=breakdown)
        stage = self._first_stage(text_stage, role_stage, context_stage)
        return candidate, stage

    def _first_stage(self, *stages: str | None) -> str | None:
        """The earliest cascade stage among the stages that matched."""
        found = {stage for stage in stages if stage is not None}
        if not found:
            return None
        for stage in CASCADE_STAGES:
            if stage in found:
                return stage
        return None

    def _text_component(self, query: ElementQuery, element: UIElement) -> tuple[float, str | None]:
        """Section 43 text cascade: exact → case-insensitive → normalized → name.

        The visible text is checked first; the accessible name is a later,
        more weakly typed stage unless it is the only label available. A
        substring is a deliberately weak signal, and an optional fuzzy score
        (section 43.1) is graded below every real textual match.
        """
        if query.text is None or not query.text.strip():
            return 1.0, None
        raw = query.text
        folded = raw.casefold()
        normalized = _normalize(raw)
        best_score = 0.0
        best_stage: str | None = None

        def consider(score: float, stage: str) -> None:
            nonlocal best_score, best_stage
            if score > best_score:
                best_score = score
                best_stage = stage

        if element.text:
            if element.text == raw:
                consider(1.0, "exact_text")
            elif element.text.casefold() == folded:
                consider(0.95, "case_insensitive_text")
            elif _normalize(element.text) == normalized:
                consider(0.90, "normalized_text")
        if element.accessible_name and element.accessible_name != element.text:
            if element.accessible_name == raw:
                consider(0.95, "accessible_name")
            elif element.accessible_name.casefold() == folded:
                consider(0.90, "accessible_name")
            elif _normalize(element.accessible_name) == normalized:
                consider(0.85, "accessible_name")

        if best_score == 0.0 and normalized:
            # Substring is a weak, honest signal, not an exact match.
            for value in (element.text, element.accessible_name):
                if value and normalized in _normalize(value):
                    consider(0.55, "substring_text")

        if best_score == 0.0 and self._semantic_scorer is not None and normalized:
            for value in (element.text, element.accessible_name):
                if not value:
                    continue
                fuzzy = self._fuzzy_score(raw, element, value)
                if fuzzy is None:
                    continue
                if fuzzy >= self._semantic_match_floor:
                    # A fuzzy hit feeds the text weight, but is graded below any
                    # real textual match (and below DECISIVE_TEXT_FLOOR) so it can
                    # never masquerade as exact -- nor create an ambiguity against
                    # a decisive match (see TEXT_MATCH_STAGES).
                    consider(0.40 + 0.40 * (fuzzy / 100.0), "fuzzy_text")

        return best_score, best_stage

    def _fuzzy_score(self, query_text: str, element: UIElement, label: str) -> float | None:
        """The fuzzy score for one label, memoized when a cache is present.

        The scorer is a pure function of the two strings and the memo key
        includes the label itself, so a reused value is by construction the same
        number the scorer would have returned (section 43.1).
        """
        scorer = self._semantic_scorer
        if scorer is None:
            return None
        cache = self._cache
        if cache is not None:
            memo = cache.semantic_score(query_text, element.identity, label)
            if memo is not None:
                return memo
        score = float(scorer(query_text, label))
        if cache is not None:
            cache.record_semantic_score(query_text, element.identity, label, score)
        return score

    def _role_component(self, query: ElementQuery, element: UIElement) -> tuple[float, str | None]:
        """Role match: neutral when unconstrained, binary when constrained."""
        if query.role_hint is None:
            return 1.0, None
        if element.role is query.role_hint:
            return 1.0, "role"
        return 0.0, None

    def _context_component(
        self, query: ElementQuery, element: UIElement
    ) -> tuple[float, str | None]:
        """Context/window match (section 43 context stage)."""
        if query.context is None and query.window_id is None:
            return 1.0, None

        parts: list[float] = []
        if query.window_id is not None:
            parts.append(1.0 if element.owner_window_id == query.window_id else 0.0)
        if query.context is not None and query.context.strip():
            needle = _normalize(query.context)
            haystacks = [
                element.owner_app or "",
                element.browser_url or "",
                element.text or "",
                element.accessible_name or "",
                element.role.value,
            ]
            if any(needle in _normalize(value) for value in haystacks if value):
                parts.append(1.0)
            else:
                tokens = set(needle.split())
                overlap = any(value and tokens & set(_normalize(value).split()) for value in haystacks)
                parts.append(0.5 if overlap else 0.0)
        score = sum(parts) / len(parts) if parts else 1.0
        stage = "context" if score > 0.0 else None
        return score, stage

    def _source_component(self, element: UIElement) -> float:
        """Section 39 source base rate as the source weight input."""
        return SOURCE_BASE_CONFIDENCE[element.source]

    def _geometry_component(self, element: UIElement) -> float:
        """Geometry sanity: a real box beats a bare center beats nothing."""
        if element.bbox is not None and element.bbox.area > 0.0:
            return 1.0
        if element.center is not None:
            return 0.7
        return 0.0

    def _check_ambiguity(
        self,
        scored: Sequence[tuple[float, str, ElementCandidate, str | None]],
        query: ElementQuery,
    ) -> tuple[bool, str]:
        """Apply section 43's absolute and gap ambiguity rules.

        Deterministic and independent of confidence: two candidates that share a
        normalized label and a role are ambiguous no matter how sure perception
        is, and a near-tie at the top is ambiguous too.

        Both rules are judged over the candidates **competitive with a decisive
        text match**. When the query names text and at least one candidate matched
        it decisively (a real textual match, not a weak substring or fuzzy hit),
        the rule is applied to the text-matching candidates alone: a candidate
        that matched only the role is a weaker cascade stage (section 43) and must
        not create an ambiguity against the winner, and unrelated duplicates
        elsewhere on the desktop (for example two same-named buttons in a panel)
        must not veto a unique, decisively-named target either. A role-only match
        scores at most ``0.60`` while a decisive text match scores at least
        ``0.94``, so the winner is unchanged. When the query names no text, or
        nothing matched its text decisively, every candidate is considered exactly
        as before -- the rule is never weakened for a text-less or weak-match
        query.
        """
        considered: Sequence[tuple[float, str, ElementCandidate, str | None]] = scored
        if query.normalized_text:
            text_matches = [item for item in scored if item[3] in TEXT_MATCH_STAGES]
            decisive = any(
                item[2].breakdown.get("text", 0.0) > DECISIVE_TEXT_FLOOR for item in text_matches
            )
            if decisive:
                considered = text_matches

        by_identity: dict[tuple[str, str], int] = {}
        for _, _, candidate, _ in considered:
            label = _element_label(candidate.element)
            if not label:
                continue
            key = (label, candidate.element.role.value)
            by_identity[key] = by_identity.get(key, 0) + 1
        duplicates = [key for key, count in by_identity.items() if count > 1]
        if duplicates:
            label, role = duplicates[0]
            return (
                True,
                f"two visible candidates share normalized text '{label}' and role {role}",
            )

        if len(considered) >= 2:
            gap = considered[0][0] - considered[1][0]
            if gap <= self.settings.ambiguity_gap:
                return (
                    True,
                    f"top two candidates are within {self.settings.ambiguity_gap:.3f} "
                    f"(gap {gap:.3f})",
                )
        return False, ""

    # -- Reporting ------------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        """A JSON-shaped health snapshot for logs and benchmarks."""
        return {
            "resolutions": self._resolutions,
            "ambiguous": self._ambiguous,
            "not_found": self._not_found,
            "max_candidates": self.settings.max_candidates,
            "act_threshold": self.settings.act_threshold,
            "ambiguity_gap": self.settings.ambiguity_gap,
            "semantic_matching": self._semantic_scorer is not None,
            "occlusion_threshold": self._occlusion_threshold,
            "cache_enabled": self._cache is not None and self._cache.enabled,
            "cache_hits": self._cache_hits,
        }


def bind_lease(
    candidate: ElementCandidate,
    state: ScreenState,
    *,
    ttl_ms: int = DEFAULT_LEASE_TTL_MS,
    now_monotonic: float | None = None,
    lease_id: str | None = None,
) -> ElementLease:
    """Issue a fresh lease for ``candidate`` bound to ``state`` (section 44).

    The lease carries the exact ``frame_id``/``state_version``/``generation`` of
    the observation the candidate was resolved against. Any later revalidation
    (section 45) compares the live state against those stamps; there is no code
    path here that reissues or extends an existing lease.
    """
    return issue_lease(
        candidate.element,
        frame_id=state.frame_id,
        state_version=state.state_version,
        generation=state.generation,
        ttl_ms=ttl_ms,
        now_monotonic=now_monotonic,
        lease_id=lease_id,
    )
