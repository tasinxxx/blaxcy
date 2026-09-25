"""Resolver cache and semantic text matching (specification section 43.1).

The same target description -- ``"search icon"``, ``"Send"`` -- is resolved over
and over across a session, and repeatedly across the steps of one
``run_sequence`` (section 66.1). Re-running the whole cascade for a description
that *just* resolved against an *unchanged* region is wasted work. This module
is the cache that removes that work, and the rules below are what keep it from
becoming a second, weaker source of truth.

**It is a hint, never an authority.** A hit tells the resolver "try this identity
path first". The resolver still re-queries that identity against the live
elements, re-scores it under the normal weights, applies the same absolute
ambiguity rule, and the executor still issues a fresh lease (section 44) and runs
full revalidation (section 45) before any input. Nothing here can authorise
input, and nothing here is ever a confidence source (section 39).

**Disabling it is provably behaviour-identical, only slower.** Two mechanisms
are cached, and both are pure functions of their arguments:

* the *identity hint* is only ever a traversal-order hint and a statement of
  where an element was last seen; a miss or a disabled cache changes only how
  long the resolver takes, never which candidate wins;
* the *semantic score memo* keys a fuzzy score by ``(query, element identity,
  label)``. The scorer is a pure function of the two strings, so reusing a
  memoized value can only save the call -- never change the number.

That second property is also how this stays honest against section 4 rule 31: an
optimization may ship enabled only when it demonstrably cannot change a decision.

**Credential targets are excluded outright.** A ``PASSWORD_INPUT`` element is
never recorded (section 55), so no credential-context target can be resolved
"from cache" -- which is the same reason section 33.2 excludes them from
speculation.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Concatenate, Final

from config.settings import ResolverCacheSettings
from schemas.elements import ElementQuery, UIElement
from schemas.geometry import Rect


def _synchronized[**P, R](
    method: Callable[Concatenate[ResolverCache, P], R],
) -> Callable[Concatenate[ResolverCache, P], R]:
    """Run a cache method under the cache's lock.

    The cache is shared between the live resolution path and the section 33.2
    speculative perceiver's background thread, so every read and write is
    serialised. An ``RLock`` is used because a caller may legitimately nest
    calls (a lookup that invalidates, an invalidate that reports).
    """

    def wrapper(self: ResolverCache, /, *args: P.args, **kwargs: P.kwargs) -> R:
        with self._lock:
            return method(self, *args, **kwargs)

    # ``functools.wraps`` is avoided only because it muddies the decorator's
    # generic signature; the two attributes callers rely on are preserved.
    wrapper.__name__ = method.__name__
    wrapper.__doc__ = method.__doc__
    return wrapper


#: Default semantic-match floor (section 43.1): a rapidfuzz score below this is
#: not considered a text match at all.
DEFAULT_SEMANTIC_MATCH_FLOOR: Final[int] = 70

#: The role whose targets are never cached (section 55).
_NEVER_CACHED_ROLES: Final[frozenset[str]] = frozenset({"PASSWORD_INPUT"})


class CacheOutcome(StrEnum):
    """Why a lookup or a record produced (or refused to produce) a hint."""

    HIT = "HIT"
    MISS = "MISS"
    DISABLED = "DISABLED"
    EXCLUDED = "EXCLUDED"
    NO_IDENTITY = "NO_IDENTITY"
    AMBIGUOUS_KEY = "AMBIGUOUS_KEY"


@dataclass(frozen=True)
class IdentityHint:
    """The element that last resolved a target description (section 43.1).

    Deliberately carries the element's *identity path*, never a coordinate: a
    coordinate would be a stale claim about where something was, whereas an
    identity path can be re-queried and re-verified against live state.
    """

    identity_path: str
    element_id: str
    role: str
    owner_window_id: int | None
    frame_id: int
    state_version: int
    generation: int
    patch_hash: str | None
    bbox: Rect | None
    recorded_at: float
    #: The window component of the key this hint was stored under, so the exact
    #: entry can be dropped later without guessing (see :meth:`ResolverCache.forget`).
    cache_window_id: int | None = None

    def age_ms(self, now: float) -> float:
        """Milliseconds since this hint was recorded."""
        return max(0.0, (now - self.recorded_at) * 1000.0)

    def is_expired(self, now: float, ttl_seconds: float) -> bool:
        """True when the hint is older than its TTL."""
        return (now - self.recorded_at) > ttl_seconds

    def affects(self, region: Rect) -> bool:
        """True when a change inside ``region`` could have moved this element.

        A hint with no geometry cannot be shown to be unaffected, so it is
        treated as affected -- fail-closed, exactly like an unassessable
        occlusion (section 46).
        """
        if self.bbox is None:
            return True
        if self.bbox.space is not region.space:
            return True
        return self.bbox.intersects(region)

    def to_dict(self) -> dict[str, Any]:
        """JSON-shaped hint for logs and benchmark evidence."""
        return {
            "identity_path": self.identity_path,
            "element_id": self.element_id,
            "role": self.role,
            "owner_window_id": self.owner_window_id,
            "frame_id": self.frame_id,
            "state_version": self.state_version,
            "generation": self.generation,
            "patch_hash": self.patch_hash,
            "cache_window_id": self.cache_window_id,
            "speculative_only": False,
        }


@dataclass(frozen=True)
class CacheLookup:
    """A lookup's verdict: the outcome, the hint (when any) and the reason."""

    outcome: CacheOutcome
    hint: IdentityHint | None = None
    reason: str = ""

    @property
    def hit(self) -> bool:
        """True only for a real, usable hit."""
        return self.outcome is CacheOutcome.HIT and self.hint is not None

    def to_dict(self) -> dict[str, Any]:
        """JSON-shaped lookup for the envelope and the benchmark report."""
        return {
            "outcome": self.outcome.value,
            "reason": self.reason,
            "hint": None if self.hint is None else self.hint.to_dict(),
        }


def identity_path(element: UIElement) -> str | None:
    """The element's re-queryable identity path, if it has one.

    Only AT-SPI/DOM paths count. An element with neither is *not* cacheable:
    a name-hash identity (``schemas.elements.identity_fingerprint``) is too weak
    to be re-queried live, and caching it would let a hint silently stand in for
    a real lookup.
    """
    return element.atspi_path or element.dom_path


def normalized_query_text(query: ElementQuery) -> str:
    """The cache key's text component: the query's normalized text."""
    return query.normalized_text or ""


def _label_of(element: UIElement) -> str:
    """The label a semantic score is computed against."""
    return element.text or element.accessible_name or ""


class ResolverCache:
    """Bounded, TTL'd identity hints plus a pure semantic-score memo (section 43.1).

    Args:
        settings: The ``[resolver_cache]`` configuration. ``enabled = false``
            makes every method a no-op or a ``DISABLED`` miss -- producing
            identical decisions, only slower (section 4 rule 31).
        clock: Monotonic clock, injectable for deterministic TTL tests.
    """

    def __init__(
        self,
        settings: ResolverCacheSettings,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._clock = clock
        self._lock = threading.RLock()
        #: OrderedDict gives LRU order: the front is the least recently used.
        self._hints: OrderedDict[tuple[str, str, int | None], IdentityHint] = OrderedDict()
        self._scores: OrderedDict[tuple[str, str, str], float] = OrderedDict()
        self._generation: int | None = None
        self.hits = 0
        self.misses = 0
        self.excluded = 0
        self.no_identity = 0
        self.invalidations = 0
        self.semantic_hits = 0
        self.semantic_misses = 0

    # -- Configuration --------------------------------------------------------

    @property
    def enabled(self) -> bool:
        """Whether the cache may be used at all."""
        return self._settings.enabled

    @property
    def semantic_match_floor(self) -> int:
        """The floor below which a fuzzy score is not a text match."""
        return self._settings.semantic_match_floor

    # -- Identity hints -------------------------------------------------------

    @_synchronized
    def lookup(
        self,
        query: ElementQuery,
        *,
        active_window_id: int | None = None,
    ) -> CacheLookup:
        """Find the hint for ``query``, or report an honest miss (section 43.1).

        The key is ``(normalized text, role hint, owner window)``. When the query
        does not name a window and more than one window has a hint for the same
        description, the lookup refuses to guess: returning one window's element
        for another window's request would be exactly the silent substitution
        section 43 forbids.
        """
        if not self.enabled:
            return CacheLookup(CacheOutcome.DISABLED, reason="resolver cache is disabled")
        text, role = _key_parts(query)
        window_id = query.window_id if query.window_id is not None else active_window_id
        now = self._clock()

        candidates = [
            (key, hint)
            for key, hint in self._hints.items()
            if key[0] == text and key[1] == role
        ]
        if not candidates:
            self.misses += 1
            return CacheLookup(CacheOutcome.MISS, reason="no hint for this description")

        if window_id is not None:
            chosen = next((item for item in candidates if item[0][2] == window_id), None)
            if chosen is None:
                self.misses += 1
                return CacheLookup(
                    CacheOutcome.MISS, reason="no hint for this description in the active window"
                )
        elif len(candidates) == 1:
            chosen = candidates[0]
        else:
            self.misses += 1
            return CacheLookup(
                CacheOutcome.AMBIGUOUS_KEY,
                reason=(
                    f"this description has hints in {len(candidates)} windows and no "
                    "active window was supplied; refusing to guess"
                ),
            )

        key, hint = chosen
        if hint.is_expired(now, self._settings.ttl_seconds):
            self._hints.pop(key, None)
            self.invalidations += 1
            self.misses += 1
            return CacheLookup(CacheOutcome.MISS, reason="the hint expired")
        if self._generation is not None and hint.generation != self._generation:
            self._hints.pop(key, None)
            self.invalidations += 1
            self.misses += 1
            return CacheLookup(
                CacheOutcome.MISS, reason="a newer perception generation invalidated the hint"
            )

        self._hints.move_to_end(key)
        self.hits += 1
        return CacheLookup(CacheOutcome.HIT, hint=hint, reason="identity hint")

    @_synchronized
    def record(
        self,
        query: ElementQuery,
        element: UIElement,
        state: Any,
        *,
        active_window_id: int | None = None,
    ) -> CacheOutcome:
        """Remember ``element`` as the answer to ``query`` (section 43.1).

        Args:
            query: The description that was resolved.
            element: The element it resolved to.
            state: The observation it was resolved against; its
                ``frame_id``/``state_version``/``generation`` are stored as the
                stamps the hint is only valid against.
            active_window_id: The active window at the time, used when neither
                the query nor the element names one.
        """
        if not self.enabled:
            return CacheOutcome.DISABLED
        if element.role.value in _NEVER_CACHED_ROLES:
            # A credential target is never cached, never re-resolved "from
            # cache" and never treated as a known identity (sections 42, 55).
            self.excluded += 1
            return CacheOutcome.EXCLUDED
        path = identity_path(element)
        if path is None:
            self.no_identity += 1
            return CacheOutcome.NO_IDENTITY
        text, role = _key_parts(query)
        window_id = element.owner_window_id
        if window_id is None:
            window_id = query.window_id if query.window_id is not None else active_window_id
        hint = IdentityHint(
            identity_path=path,
            element_id=element.element_id,
            role=element.role.value,
            owner_window_id=element.owner_window_id,
            frame_id=int(getattr(state, "frame_id", 0)),
            state_version=int(getattr(state, "state_version", 0)),
            generation=int(getattr(state, "generation", 0)),
            patch_hash=element.patch_hash or getattr(state, "patch_hash", None),
            bbox=element.bbox,
            recorded_at=self._clock(),
            cache_window_id=window_id,
        )
        self._hints[(text, role, window_id)] = hint
        self._hints.move_to_end((text, role, window_id))
        self._evict_hints()
        return CacheOutcome.HIT

    @_synchronized
    def forget(self, query: ElementQuery, hint: IdentityHint) -> bool:
        """Drop the exact entry ``hint`` came from, and nothing else.

        Used when a hint's identity path is no longer present in the live
        elements: the entry is provably wrong, but the other windows' hints for
        the same description may still be perfectly good.
        """
        key = (*_key_parts(query), hint.cache_window_id)
        if self._hints.pop(key, None) is None:
            return False
        self.invalidations += 1
        return True

    @_synchronized
    def prime(self, query: ElementQuery, hint: IdentityHint, *, window_id: int | None = None) -> None:
        """Seed a hint without a live resolution.

        Used by the section 33.2 speculative perceiver: a speculation that
        succeeded is still only a *hint* about where an element was, so it is
        inserted here and re-verified by the next real lookup rather than being
        handed to the resolver as an answer.

        The cache stamps ``recorded_at`` itself, exactly as :meth:`record` does.
        A primed hint is, by definition, something just observed, so a
        caller-supplied timestamp can only make it born-expired -- which is what
        a hard-coded epoch value did: the hint was dead on arrival, so it could
        never help and every lookup of it counted a miss plus an invalidation
        instead (section 33.2). Time is the cache's to own, not the caller's.
        """
        if not self.enabled:
            return
        if hint.role in _NEVER_CACHED_ROLES:
            self.excluded += 1
            return
        text, role = _key_parts(query)
        key = (text, role, hint.owner_window_id if window_id is None else window_id)
        self._hints[key] = replace(hint, cache_window_id=key[2], recorded_at=self._clock())
        self._hints.move_to_end(key)
        self._evict_hints()

    # -- Semantic score memo (section 43.1) -----------------------------------

    @_synchronized
    def semantic_score(self, query_text: str, element_identity: str, label: str) -> float | None:
        """A memoized fuzzy score for ``(query, element identity, label)``.

        Returns ``None`` on a miss so the caller computes and records the real
        value. The key includes the *label*, so a relabelled element can never
        reuse an old number: the memo is keyed by content, not by position.
        """
        if not self.enabled:
            return None
        key = (query_text, element_identity, label)
        cached = self._scores.get(key)
        if cached is None:
            self.semantic_misses += 1
            return None
        self._scores.move_to_end(key)
        self.semantic_hits += 1
        return cached

    @_synchronized
    def record_semantic_score(
        self, query_text: str, element_identity: str, label: str, score: float
    ) -> None:
        """Remember a fuzzy score (a pure function of the three key parts)."""
        if not self.enabled:
            return
        key = (query_text, element_identity, label)
        self._scores[key] = float(score)
        self._scores.move_to_end(key)
        while len(self._scores) > self._settings.max_entries:
            self._scores.popitem(last=False)

    # -- Invalidation (section 43.1) ------------------------------------------

    @_synchronized
    def invalidate_all(self, reason: str = "invalidated") -> int:
        """Drop every identity hint. Returns how many were dropped."""
        dropped = len(self._hints)
        if dropped:
            self.invalidations += dropped
        self._hints.clear()
        self._last_reason = reason
        return dropped

    @_synchronized
    def invalidate_window(self, window_id: int) -> int:
        """Drop every hint owned by ``window_id`` (a window closed or changed)."""
        keys = [key for key in self._hints if key[2] == window_id]
        for key in keys:
            self._hints.pop(key, None)
        self.invalidations += len(keys)
        return len(keys)

    @_synchronized
    def invalidate_generation(self, generation: int) -> int:
        """Drop every hint older than ``generation`` (section 32).

        A new perception generation supersedes the leases and the observations
        the hints were derived from, so no hint survives it.
        """
        if self._generation is None:
            self._generation = generation
            return 0
        if generation == self._generation:
            return 0
        self._generation = generation
        return self.invalidate_all("generation advanced")

    @_synchronized
    def invalidate_regions(self, regions: Sequence[Rect]) -> int:
        """Drop hints whose element could have been affected by a structural change.

        Called for a ``MEANINGFUL`` or ``MAJOR`` change (section 34): a hint
        about where an element was is not worth keeping once the region it lives
        in has structurally changed. A hint with no geometry is always dropped
        (see :meth:`IdentityHint.affects`).
        """
        if not regions:
            return 0
        keys = [
            key
            for key, hint in self._hints.items()
            if any(hint.affects(region) for region in regions)
        ]
        for key in keys:
            self._hints.pop(key, None)
        self.invalidations += len(keys)
        return len(keys)

    # -- Reporting ------------------------------------------------------------

    @_synchronized
    def stats(self) -> dict[str, Any]:
        """A JSON-shaped health snapshot for logs and the benchmark report."""
        total = self.hits + self.misses
        return {
            "enabled": self.enabled,
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": (self.hits / total) if total else None,
            "entries": len(self._hints),
            "max_entries": self._settings.max_entries,
            "ttl_seconds": self._settings.ttl_seconds,
            "semantic_hits": self.semantic_hits,
            "semantic_misses": self.semantic_misses,
            "semantic_entries": len(self._scores),
            "excluded_password_targets": self.excluded,
            "skipped_without_identity_path": self.no_identity,
            "invalidations": self.invalidations,
        }

    # -- Internals ------------------------------------------------------------

    def _evict_hints(self) -> None:
        """Keep the hint store inside its bound, evicting least-recently-used."""
        while len(self._hints) > self._settings.max_entries:
            self._hints.popitem(last=False)

    #: Set by :meth:`invalidate_all`; kept only for diagnostics.
    _last_reason: str = ""


def _key_parts(query: ElementQuery) -> tuple[str, str]:
    """The ``(normalized text, role hint)`` part of a cache key."""
    return (normalized_query_text(query), "" if query.role_hint is None else query.role_hint.value)


def build_semantic_scorer() -> Callable[[str, str], float]:
    """The section 43.1 fuzzy scorer, backed by the installed ``rapidfuzz``.

    ``rapidfuzz.fuzz.token_set_ratio`` is used because it is order-insensitive
    and tolerant of an added or missing word -- precisely the ``"Send message"``
    / ``"Send"`` near-miss the section describes. The import is lazy so a
    missing wheel degrades to "no semantic matching" instead of an import error
    at startup (section 82: verify the installed API before relying on it).

    Raises:
        ImportError: When ``rapidfuzz`` is not installed. Callers treat that as
            ``semantic_matching_unavailable`` rather than guessing a score.
    """
    from rapidfuzz import fuzz

    def scorer(query: str, label: str) -> float:
        """Score ``label`` against ``query`` on the 0-100 rapidfuzz scale."""
        return float(fuzz.token_set_ratio(query, label))

    return scorer


def scorer_or_none() -> Callable[[str, str], float] | None:
    """The semantic scorer when ``rapidfuzz`` is importable, else ``None``."""
    try:
        return build_semantic_scorer()
    except ImportError:  # pragma: no cover - rapidfuzz is a core dependency
        return None


__all__ = [
    "DEFAULT_SEMANTIC_MATCH_FLOOR",
    "CacheLookup",
    "CacheOutcome",
    "IdentityHint",
    "ResolverCache",
    "build_semantic_scorer",
    "identity_path",
    "normalized_query_text",
    "scorer_or_none",
]
