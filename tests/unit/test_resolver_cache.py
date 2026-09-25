"""Resolver cache and semantic matching (specification section 43.1).

The section 43.1 cache is the first half of the batching layer that can silently
become a second source of truth, so these tests are about *limits* as much as
about function: what it refuses to cache, what invalidates it, and the fact that
turning it off produces the same targeting decisions rather than merely similar
ones.
"""

from __future__ import annotations

from typing import Any

import pytest

from config.settings import ResolverCacheSettings, ResolverSettings
from core.resolver_cache import (
    CacheOutcome,
    ResolverCache,
    build_semantic_scorer,
    identity_path,
)
from core.target_resolver import ResolutionStatus, TargetResolver
from schemas.elements import ElementQuery
from schemas.enums import UIRole
from schemas.geometry import Rect
from tests.harness.phase8 import box_of, make_element, make_state
from tests.harness.phase101 import (
    duplicate_controls,
    workflow_elements,
)


class Clock:
    """A monotonic clock the test advances by hand."""

    def __init__(self, start: float = 100.0) -> None:
        """Create a clock at ``start``."""
        self.now = start

    def __call__(self) -> float:
        """Read the current time."""
        return self.now

    def advance(self, seconds: float) -> None:
        """Move the clock forward."""
        self.now += seconds


def _cache(*, enabled: bool = True, clock: Clock | None = None, ttl: float = 10.0, size: int = 256) -> ResolverCache:
    """A cache with the given configuration."""
    return ResolverCache(
        ResolverCacheSettings(enabled=enabled, ttl_seconds=ttl, max_entries=size),
        clock=clock if clock is not None else Clock(),
    )


def _resolver(cache: ResolverCache | None, *, scorer: Any = None, floor: float = 100.0) -> TargetResolver:
    """A resolver with (or without) a cache."""
    return TargetResolver(
        ResolverSettings(), semantic_scorer=scorer, semantic_match_floor=floor, cache=cache
    )


# -- Disabled means inert ------------------------------------------------------


def test_a_disabled_cache_stores_and_returns_nothing() -> None:
    """``enabled = false`` must make the cache a no-op, not a slow cache."""
    cache = _cache(enabled=False)
    state = make_state(elements=workflow_elements())
    element = workflow_elements()[0]
    query = ElementQuery(text="Search")

    assert cache.record(query, element, state) is CacheOutcome.DISABLED
    lookup = cache.lookup(query)
    assert lookup.outcome is CacheOutcome.DISABLED
    assert lookup.hint is None
    assert cache.semantic_score("a", "b", "c") is None
    assert cache.stats()["entries"] == 0


# -- Identity hints -----------------------------------------------------------


def test_a_recorded_resolution_is_looked_up_by_identity_path() -> None:
    """A hit reports where the element was, never a coordinate."""
    cache = _cache()
    state = make_state(elements=workflow_elements())
    element = workflow_elements()[0]
    query = ElementQuery(text="Search")

    assert cache.record(query, element, state) is CacheOutcome.HIT
    lookup = cache.lookup(query)

    assert lookup.hit is True
    hint = lookup.hint
    assert hint is not None
    assert hint.identity_path == element.atspi_path
    assert hint.element_id == element.element_id
    assert hint.frame_id == state.frame_id
    assert hint.generation == state.generation
    # The hint carries geometry for region invalidation, not for input.
    assert hint.bbox == element.bbox


def test_the_key_is_text_plus_role_so_a_different_role_does_not_hit() -> None:
    """The role hint is part of the key (section 43.1)."""
    cache = _cache()
    state = make_state(elements=workflow_elements())
    assert cache.record(ElementQuery(text="Search"), workflow_elements()[0], state) is CacheOutcome.HIT

    assert cache.lookup(ElementQuery(text="Search", role_hint=UIRole.LINK)).outcome is CacheOutcome.MISS
    assert cache.lookup(ElementQuery(text="search")).outcome is CacheOutcome.HIT  # normalized


def test_a_credential_target_is_never_cached() -> None:
    """A PASSWORD_INPUT target is excluded outright (sections 42, 55)."""
    cache = _cache()
    password = make_element(
        "pw", role=UIRole.PASSWORD_INPUT, text=None, password=True, accessible_name="Password"
    )
    state = make_state(elements=(password,))
    query = ElementQuery(text="Password", role_hint=UIRole.PASSWORD_INPUT)

    assert cache.record(query, password, state) is CacheOutcome.EXCLUDED
    assert cache.lookup(query).outcome is CacheOutcome.MISS
    assert cache.stats()["excluded_password_targets"] == 1


def test_an_element_without_an_identity_path_is_not_cacheable() -> None:
    """Only a re-queryable AT-SPI/DOM path counts as identity."""
    cache = _cache()
    plain = make_element("plain", atspi_path=None, dom_path=None)
    assert identity_path(plain) is None
    state = make_state(elements=(plain,))

    assert cache.record(ElementQuery(text="Send"), plain, state) is CacheOutcome.NO_IDENTITY
    assert cache.stats()["skipped_without_identity_path"] == 1


def test_a_dom_path_is_an_acceptable_identity() -> None:
    """A browser element's DOM path is as re-queryable as an AT-SPI one."""
    cache = _cache()
    dom = make_element("dom", atspi_path=None, dom_path="/html/body/button[2]")
    state = make_state(elements=(dom,))
    assert cache.record(ElementQuery(text="Send"), dom, state) is CacheOutcome.HIT
    hint = cache.lookup(ElementQuery(text="Send")).hint
    assert hint is not None and hint.identity_path == "/html/body/button[2]"


def test_the_lookup_refuses_to_guess_between_windows() -> None:
    """Two windows' hints for one description are not interchangeable."""
    cache = _cache()
    here = make_element("here", owner_window_id=1)
    there = make_element("there", owner_window_id=2)
    state = make_state(elements=(here, there))
    cache.record(ElementQuery(text="Send"), here, state)
    cache.record(ElementQuery(text="Send"), there, state)

    ambiguous = cache.lookup(ElementQuery(text="Send"))
    assert ambiguous.outcome is CacheOutcome.AMBIGUOUS_KEY

    specific = cache.lookup(ElementQuery(text="Send", window_id=2))
    assert specific.hit is True
    assert specific.hint is not None and specific.hint.element_id == "there"

    active = cache.lookup(ElementQuery(text="Send"), active_window_id=1)
    assert active.hit is True
    assert active.hint is not None and active.hint.element_id == "here"


# -- Invalidation (section 43.1) ---------------------------------------------


def test_hints_expire_after_their_ttl() -> None:
    """A hint older than the TTL is gone, not merely doubted."""
    clock = Clock()
    cache = _cache(clock=clock, ttl=10.0)
    state = make_state(elements=workflow_elements())
    cache.record(ElementQuery(text="Search"), workflow_elements()[0], state)

    clock.advance(9.9)
    assert cache.lookup(ElementQuery(text="Search")).hit is True
    clock.advance(0.2)
    assert cache.lookup(ElementQuery(text="Search")).outcome is CacheOutcome.MISS
    assert cache.stats()["entries"] == 0


def test_the_store_is_bounded_and_evicts_least_recently_used() -> None:
    """No unbounded growth: the oldest entry is the one that goes."""
    cache = _cache(size=2)
    state = make_state(elements=workflow_elements())
    elements = workflow_elements()
    cache.record(ElementQuery(text="Search"), elements[0], state)
    cache.record(ElementQuery(text="Song results"), elements[3], state)
    cache.record(ElementQuery(text="Play"), elements[4], state)

    assert cache.stats()["entries"] == 2
    assert cache.lookup(ElementQuery(text="Search")).outcome is CacheOutcome.MISS
    assert cache.lookup(ElementQuery(text="Play")).hit is True


def test_a_closed_window_invalidates_only_its_own_hints() -> None:
    """Window change and window closing drop that window's entries (§43.1)."""
    cache = _cache()
    here = make_element("here", owner_window_id=1)
    there = make_element("there", owner_window_id=2)
    state = make_state(elements=(here, there))
    cache.record(ElementQuery(text="Send"), here, state)
    cache.record(ElementQuery(text="Click"), there, state)

    assert cache.invalidate_window(1) == 1
    assert cache.lookup(ElementQuery(text="Send")).outcome is CacheOutcome.MISS
    assert cache.lookup(ElementQuery(text="Click")).hit is True


def test_a_new_generation_invalidates_every_hint() -> None:
    """Section 32: a new perception generation supersedes the observations."""
    cache = _cache()
    state = make_state(elements=workflow_elements(), generation=1)
    cache.record(ElementQuery(text="Search"), workflow_elements()[0], state)

    assert cache.invalidate_generation(1) == 0
    assert cache.lookup(ElementQuery(text="Search")).hit is True
    assert cache.invalidate_generation(2) == 1
    assert cache.lookup(ElementQuery(text="Search")).outcome is CacheOutcome.MISS


def test_a_structural_change_invalidates_the_regions_it_touched() -> None:
    """A MEANINGFUL/MAJOR change drops the hints inside it, and only those."""
    cache = _cache()
    elements = workflow_elements()
    state = make_state(elements=elements)
    cache.record(ElementQuery(text="Search"), elements[0], state)
    cache.record(ElementQuery(text="Play"), elements[4], state)

    dropped = cache.invalidate_regions((Rect(x=90.0, y=90.0, width=60.0, height=60.0, space=box_of(workflow_elements()[0]).space),))

    assert dropped == 1
    assert cache.lookup(ElementQuery(text="Search")).outcome is CacheOutcome.MISS
    assert cache.lookup(ElementQuery(text="Play")).hit is True


def test_a_geometryless_hint_is_always_invalidated_by_a_change() -> None:
    """No geometry means "cannot be shown to be unaffected", so it is dropped."""
    cache = _cache()
    element = make_element("float", bbox=None, center=None)
    state = make_state(elements=(element,))
    cache.record(ElementQuery(text="Send"), element, state)

    assert cache.invalidate_regions((Rect(x=0.0, y=0.0, width=1.0, height=1.0, space=box_of(workflow_elements()[0]).space),)) == 1
    assert cache.lookup(ElementQuery(text="Send")).outcome is CacheOutcome.MISS


# -- Semantic matching (section 43.1) ----------------------------------------


def test_the_semantic_scorer_is_a_pure_0_to_100_function() -> None:
    """The installed rapidfuzz scorer is used, and its scale is verified (§82)."""
    scorer = build_semantic_scorer()

    assert scorer("Send", "Send") == pytest.approx(100.0)
    assert scorer("Send message", "Send") == pytest.approx(100.0)  # token-subset
    assert scorer("alpha", "omega") < 70.0
    assert 0.0 <= scorer("search icon", "Search") <= 100.0


def test_a_memoized_semantic_score_is_the_same_number() -> None:
    """The memo is keyed by content, so it can only save the call, never change it."""
    calls: list[tuple[str, str]] = []

    def counting_scorer(query: str, label: str) -> float:
        calls.append((query, label))
        return 88.0

    cache = _cache()
    resolver = _resolver(cache, scorer=counting_scorer, floor=70.0)
    # The label deliberately shares no substring with the query, so the fuzzy
    # stage is the only thing that can match it (a substring match would short
    # circuit the scorer before the memo could be exercised).
    elements = (make_element("e1", text="Transmit note"),)
    state = make_state(elements=elements)
    query = ElementQuery(text="Send")

    first = resolver.resolve(query, elements, state=state)
    second = resolver.resolve(query, elements, state=state)

    assert first.status is second.status
    assert [c.score for c in first.candidates] == [c.score for c in second.candidates]
    # The scorer ran once for the label; the second resolution reused the number.
    assert len(calls) == 1
    assert cache.stats()["semantic_hits"] == 1


def test_the_semantic_floor_decides_whether_a_fuzzy_hit_counts() -> None:
    """Below the floor, a fuzzy score is not a text match at all (section 43.1)."""
    elements = (make_element("e1", text="Send message"),)

    permissive = _resolver(None, scorer=lambda q, label: 69.0, floor=70.0)
    assert permissive.resolve(ElementQuery(text="Send message", role_hint=UIRole.BUTTON), elements).status is ResolutionStatus.RESOLVED

    strict = _resolver(None, scorer=lambda q, label: 69.0, floor=70.0)
    strict_result = strict.resolve(
        ElementQuery(text="Totally unrelated", role_hint=UIRole.BUTTON), elements
    )
    assert strict_result.status is not ResolutionStatus.RESOLVED

    exact_only = _resolver(None, scorer=lambda q, label: 100.0, floor=100.0)
    hit = exact_only.resolve(ElementQuery(text="Send message"), elements)
    assert hit.status is ResolutionStatus.RESOLVED


def test_two_fuzzy_matches_are_ambiguous_like_any_other_tie() -> None:
    """A fuzzy hit still obeys the absolute and gap ambiguity rules (section 43)."""
    elements = (
        make_element("a", text="Send note", role=UIRole.BUTTON),
        make_element("b", text="Send memo", role=UIRole.BUTTON),
    )
    resolver = _resolver(None, scorer=lambda q, label: 90.0, floor=70.0)

    result = resolver.resolve(ElementQuery(text="Send", role_hint=UIRole.BUTTON), elements)

    assert result.status is ResolutionStatus.AMBIGUOUS
    assert result.best is None


# -- The resolver's use of the cache -----------------------------------------


def test_a_hit_is_reported_without_changing_the_decision() -> None:
    """The hint is diagnostic: same status, same winner, same score."""
    elements = workflow_elements()
    state = make_state(elements=elements)
    query = ElementQuery(text="Search", role_hint=UIRole.BUTTON)

    plain = _resolver(None).resolve(query, elements, state=state)
    cached = _resolver(_cache()).resolve(query, elements, state=state)

    assert plain.status is cached.status is ResolutionStatus.RESOLVED
    assert plain.cache_hit is False
    assert cached.cache_hit is False  # first resolution: nothing recorded yet
    assert plain.best is not None and cached.best is not None
    assert plain.best.element_id == cached.best.element_id
    assert plain.best.score == pytest.approx(cached.best.score)


def test_a_second_resolution_reports_a_hit_and_re_scores_live() -> None:
    """A hit still re-queries and re-scores against the live elements (§43.1)."""
    elements = workflow_elements()
    state = make_state(elements=elements)
    resolver = _resolver(_cache())
    query = ElementQuery(text="Search", role_hint=UIRole.BUTTON)

    resolver.resolve(query, elements, state=state)
    second = resolver.resolve(query, elements, state=state)
    assert second.cache_hit is True

    # Same identity path, different label: the score must follow the live label,
    # so a stale cache entry can never decide a match.
    relabelled = tuple(
        make_element("search_icon", text="Search now", role=UIRole.BUTTON, bbox=e.bbox, center=e.center)
        if e.element_id == "search_icon"
        else e
        for e in elements
    )
    third = resolver.resolve(ElementQuery(text="Search", role_hint=UIRole.BUTTON), relabelled, state=state)
    assert third.cache_hit is True  # the identity path is still present
    assert third.best is not None
    assert third.best.element.text == "Search now"


def test_a_primed_hint_is_verified_before_it_is_used() -> None:
    """The section 33.2 prefetcher primes a hint; a lookup still re-verifies it."""
    from core.resolver_cache import IdentityHint

    elements = workflow_elements()
    state = make_state(elements=elements)
    cache = _cache()
    icon = elements[0]
    cache.prime(
        ElementQuery(text="Search", role_hint=UIRole.BUTTON),
        IdentityHint(
            identity_path=icon.atspi_path or "",
            element_id=icon.element_id,
            role=icon.role.value,
            owner_window_id=icon.owner_window_id,
            frame_id=state.frame_id,
            state_version=state.state_version,
            generation=state.generation,
            patch_hash=None,
            bbox=icon.bbox,
            recorded_at=100.0,
        ),
    )
    lookup = cache.lookup(ElementQuery(text="Search", role_hint=UIRole.BUTTON))
    assert lookup.hit is True

    # A primed hint for an element that is not there is discarded by the
    # resolver, so a speculation cannot vouch for something it cannot show.
    resolver = _resolver(cache)
    without = tuple(e for e in elements if e.element_id != "search_icon")
    result = resolver.resolve(ElementQuery(text="Search", role_hint=UIRole.BUTTON), without)
    assert result.status is not ResolutionStatus.RESOLVED
    assert result.cache_hit is False
    assert cache.stats()["entries"] == 0


def test_a_primed_hint_is_stamped_fresh_by_the_cache_not_by_its_caller() -> None:
    """Section 33.2/43.1: a primed hint must be usable, never born expired.

    The cache owns the clock its TTL is measured against, so it stamps a primed
    hint on insert. The sequence runner's adapter supplies ``recorded_at=0.0``
    because it has no clock of its own -- which must not yield an already-expired
    hint, since that made a speculation useless by construction and charged a
    miss plus an invalidation for every lookup of it.
    """
    from core.resolver_cache import IdentityHint

    clock = Clock()
    cache = _cache(clock=clock)
    elements = workflow_elements()
    icon = elements[0]
    query = ElementQuery(text="Search", role_hint=UIRole.BUTTON)
    cache.prime(
        query,
        IdentityHint(
            identity_path=icon.atspi_path or "",
            element_id=icon.element_id,
            role=icon.role.value,
            owner_window_id=icon.owner_window_id,
            frame_id=1,
            state_version=1,
            generation=1,
            patch_hash=None,
            bbox=icon.bbox,
            recorded_at=0.0,
        ),
    )

    lookup = cache.lookup(query, active_window_id=icon.owner_window_id)

    assert lookup.hit is True
    assert cache.stats()["invalidations"] == 0
    assert lookup.hint is not None
    assert lookup.hint.recorded_at == clock.now


def test_enabling_the_cache_cannot_change_any_targeting_decision() -> None:
    """The section 4 rule 31 property: identical decisions, only faster.

    Every resolved field is compared -- status, winner, candidate order, every
    score -- across many queries, against a fresh resolver, a disabled cache and
    an enabled (and by then populated) cache. If this ever differs, the cache has
    stopped being a hint.
    """
    scorer = build_semantic_scorer()
    elements = workflow_elements() + duplicate_controls()
    state = make_state(elements=elements)
    queries = tuple(
        ElementQuery(**kwargs)
        for kwargs in (
            {"text": "Search"},
            {"text": "Search", "role_hint": UIRole.BUTTON},
            {"text": "Search field", "role_hint": UIRole.TEXT_INPUT},
            {"text": "Song results", "role_hint": UIRole.LIST_ITEM},
            {"text": "Play", "role_hint": UIRole.BUTTON},
            {"text": "Duplicate action", "role_hint": UIRole.BUTTON},
            {"text": "Song results extra", "role_hint": UIRole.LIST_ITEM},
            {"text": "nothing like this"},
            {"text": "Go", "context": "fixture"},
        )
    )

    def fingerprint(result: Any) -> tuple[Any, ...]:
        """Everything about a resolution that a caller could act on."""
        return (
            result.status,
            None if result.best is None else result.best.element_id,
            None if result.best is None else round(result.best.score, 12),
            tuple(c.element_id for c in result.candidates),
            tuple(round(c.score, 12) for c in result.candidates),
            result.reason,
            result.considered,
            result.ambiguity,
        )

    def run(resolver: TargetResolver) -> list[tuple[Any, ...]]:
        """Resolve every query twice, so a warm cache is exercised too."""
        return [fingerprint(resolver.resolve(q, elements, state=state)) for q in queries] * 2

    baseline = run(_resolver(None, scorer=scorer, floor=70.0))
    assert baseline == run(_resolver(_cache(enabled=False), scorer=scorer, floor=70.0))
    assert baseline == run(_resolver(_cache(enabled=True), scorer=scorer, floor=70.0))


def test_a_hint_whose_element_is_gone_falls_through_to_the_cascade() -> None:
    """Re-query failure is a miss, never a silent substitution.

    The list item is used rather than the search icon so the fall-through is
    unambiguous: no other workflow control shares the ``LIST_ITEM`` role, so a
    substituted candidate could not be mistaken for a successful re-query.
    """
    elements = workflow_elements()
    state = make_state(elements=elements)
    cache = _cache()
    resolver = _resolver(cache)
    query = ElementQuery(text="Song results", role_hint=UIRole.LIST_ITEM)
    assert resolver.resolve(query, elements, state=state).status is ResolutionStatus.RESOLVED

    without = tuple(e for e in elements if e.element_id != "results")
    result = resolver.resolve(query, without, state=state)

    assert result.status is ResolutionStatus.NOT_FOUND
    assert result.best is None
    assert result.cache_hit is False
    assert cache.stats()["entries"] == 0
