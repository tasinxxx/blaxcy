"""Speculative perception warm-up (specification section 33.2).

Inside a ``run_sequence`` (section 66.1) the Brain has already declared the
*likely* next target when it submitted the plan. Waiting until that step begins
before starting any perception work for it wastes the time the previous step's
own settle/verify window would otherwise absorb for free. This module spends it:
while step *N* runs, it resolves step *N+1*'s declared description against the
**current** observation, strictly as a background hint.

What this is:

* pure read-side prefetching -- it never injects input, never touches a policy
  decision, never takes the executor's action lock and never issues a lease;
* immediately discardable at zero cost -- the result is a *hint* about where an
  element was, and a new generation or a structural change throws it away;
* measured honestly -- how often a hint actually shortened live resolution is
  reported (`useful`) next to how often it was discarded, so the optimization has
  to earn its place rather than being assumed to help.

What this is **not**:

* a substitute for resolution. When step *N+1* really begins, the resolver runs
  its full cascade against the *post-step-N* observation. Nothing here can skip
  lease issuance, revalidation, the absolute ambiguity rule or verification;
* a second perception source. It produces no elements and no confidence -- it
  only says "this identity path held a plausible match a moment ago", which the
  live resolution then re-verifies like any other candidate;
* applicable to credentials. A ``PASSWORD_INPUT`` description is never
  speculated (§55), so a password step is always resolved from scratch.
"""

from __future__ import annotations

import contextlib
import queue
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from config.settings import SequenceSettings
from core.resolver_cache import identity_path
from core.target_resolver import ResolutionStatus, TargetResolver
from schemas.elements import ElementQuery
from schemas.enums import SIGNIFICANT_CHANGE_CLASSES, ChangeClass, UIRole
from schemas.geometry import Rect
from schemas.screen_state import ScreenState

#: A speculation whose region structurally changed is worthless; the section 34
#: classes that count as "structurally changed" are MEANINGFUL and MAJOR.
_INVALIDATING_CHANGES: Final[frozenset[ChangeClass]] = SIGNIFICANT_CHANGE_CLASSES

#: How long a caller waits for a queued speculation to finish in ``wait_idle``.
DEFAULT_WAIT_IDLE_SECONDS: Final[float] = 2.0

#: At most this many completed hints are retained. The design is
#: latest-request-wins with one step of lookahead, so in real use there is one at
#: a time; the cap only stops a caller that speculates many *distinct* keys
#: without consuming them from growing an unbounded result map (section 32).
MAX_RETAINED_HINTS: Final[int] = 4


@dataclass(frozen=True)
class Speculation:
    """One speculative resolution result -- a hint, explicitly marked as such."""

    key: str
    query: ElementQuery
    frame_id: int
    state_version: int
    generation: int
    status: ResolutionStatus
    element_id: str | None = None
    identity_path: str | None = None
    identity: str | None = None
    role: str | None = None
    owner_window_id: int | None = None
    score: float | None = None
    bbox: Rect | None = None
    computation_ms: float = 0.0
    speculative: bool = True

    @property
    def is_resolved(self) -> bool:
        """True when the speculation found a unique, unambiguous candidate."""
        return self.status is ResolutionStatus.RESOLVED and self.identity_path is not None

    def affects(self, region: Rect) -> bool:
        """True when a change inside ``region`` could invalidate this hint.

        A hint with no geometry cannot be shown to be unaffected, so it counts
        as affected -- the fail-closed direction (an extra discard costs a
        recomputation, a wrongly kept hint would cost correctness).
        """
        if self.bbox is None:
            return True
        if self.bbox.space is not region.space:
            return True
        return self.bbox.intersects(region)

    def is_valid_for(self, state: ScreenState) -> bool:
        """True when the speculation still belongs to ``state``'s generation."""
        return self.generation == state.generation

    def to_dict(self) -> dict[str, Any]:
        """JSON-shaped hint for the step result and the benchmark evidence."""
        return {
            "key": self.key,
            "speculative": True,
            "status": self.status.value,
            "element_id": self.element_id,
            "identity_path": self.identity_path,
            "role": self.role,
            "score": self.score,
            "frame_id": self.frame_id,
            "state_version": self.state_version,
            "generation": self.generation,
            "computation_ms": self.computation_ms,
        }


@dataclass(frozen=True)
class _Job:
    """One queued speculation."""

    key: str
    query: ElementQuery
    state: ScreenState
    queued_at: float


class SpeculativePerceiver:
    """A single-worker, latest-request-wins hint prefetcher (section 33.2).

    Args:
        settings: The ``[sequence]`` configuration. ``speculative_perception =
            false`` makes the whole object inert (section 4 rule 31).
        resolver: A resolver used **only** for speculation. It must be a
            dedicated instance: sharing the executor's resolver would pollute
            the live resolution counters, and this class deliberately does not
            need -- or want -- the live cache.
        clock: Monotonic clock, injectable for deterministic tests.
        background: Run the worker on a thread (production). ``False`` computes
            inline, which keeps tests deterministic; the discard rules are
            identical either way.
    """

    def __init__(
        self,
        settings: SequenceSettings,
        resolver: TargetResolver,
        *,
        clock: Any = time.monotonic,
        background: bool = True,
    ) -> None:
        self._settings = settings
        self._resolver = resolver
        self._clock = clock
        self._background = background
        self._lock = threading.RLock()
        self._results: dict[str, Speculation] = {}
        self._pending: _Job | None = None
        #: True while the worker is executing a job. ``_pending`` is cleared
        #: *before* the resolution runs, so it alone cannot distinguish "queued"
        #: from "already running" -- ``wait_idle`` needs both.
        self._running = False
        self._queue: queue.Queue[_Job | None] = queue.Queue(maxsize=1)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.requested = 0
        self.completed = 0
        self.failed = 0
        self.superseded = 0
        self.cancelled = 0
        self.taken = 0
        self.discarded_stale = 0
        self.discarded_change = 0
        self.useful = 0
        self.max_pending = 0

    # -- Configuration --------------------------------------------------------

    @property
    def enabled(self) -> bool:
        """Whether speculation may be scheduled at all."""
        return self._settings.speculative_perception

    # -- Scheduling -----------------------------------------------------------

    def speculate(self, key: str, query: ElementQuery, state: ScreenState) -> bool:
        """Start (or replace) a background resolution of ``query``.

        A credential target is never speculated: the description is dropped and
        the call reports ``False``, so a password step always resolves fresh
        (section 55).

        Returns:
            True when a computation was scheduled.
        """
        if not self.enabled:
            return False
        if query.role_hint is UIRole.PASSWORD_INPUT:
            return False
        job = _Job(key=key, query=query, state=state, queued_at=self._clock())
        with self._lock:
            self.requested += 1
            self.max_pending = max(self.max_pending, self._outstanding() + 1)
            # Latest request wins: a stale pending job is dropped, never queued
            # behind. There is no unbounded perception queue (section 32).
            if self._replace_pending_locked(job):
                self.superseded += 1
        if not self._background:
            self._run_job(job)
            return True
        self._ensure_worker()
        while True:
            try:
                self._queue.put_nowait(job)
                return True
            except queue.Full:
                try:
                    self._queue.get_nowait()
                    with self._lock:
                        self.superseded += 1
                except queue.Empty:  # pragma: no cover - racing consumer
                    continue

    def cancel(self, key: str) -> bool:
        """Drop a pending or completed speculation for ``key`` (zero cost)."""
        with self._lock:
            if self._pending is not None and self._pending.key == key:
                self._pending = None
                self.cancelled += 1
                return True
            if self._results.pop(key, None) is not None:
                self.cancelled += 1
                return True
        return False

    def cancel_all(self) -> int:
        """Drop every pending and completed speculation. Returns the count."""
        with self._lock:
            count = len(self._results) + (1 if self._pending is not None else 0)
            self._results.clear()
            self._pending = None
            self.cancelled += count
        return count

    # -- Consuming ------------------------------------------------------------

    def take(
        self,
        key: str,
        *,
        state: ScreenState | None = None,
        change_class: ChangeClass | None = None,
        regions: Sequence[Rect] = (),
    ) -> Speculation | None:
        """Take the hint for ``key``, discarding it when it is no longer valid.

        Returns ``None`` when there is no hint, the hint belongs to an older
        generation, or a ``MEANINGFUL``/``MAJOR`` change touched the region it
        depended on. A discarded hint is never partially trusted: the caller
        resolves from scratch (section 33.2).
        """
        with self._lock:
            hint = self._results.pop(key, None)
            if hint is None:
                return None
        if state is not None and not hint.is_valid_for(state):
            with self._lock:
                self.discarded_stale += 1
            return None
        if (
            change_class is not None
            and change_class in _INVALIDATING_CHANGES
            and (not regions or any(hint.affects(region) for region in regions))
        ):
            with self._lock:
                self.discarded_change += 1
            return None
        with self._lock:
            self.taken += 1
        return hint

    def note_change(
        self, change_class: ChangeClass, regions: Sequence[Rect] = ()
    ) -> int:
        """Discard hints whose region a structural change touched (§34).

        Called with the observed delta's class after every step. A
        ``TRIVIAL``/``ANIMATION`` change invalidates nothing, which is the whole
        point: an animation elsewhere on screen must not throw away a hint.
        """
        if change_class not in _INVALIDATING_CHANGES:
            return 0
        with self._lock:
            stale = [
                key
                for key, hint in self._results.items()
                if not regions or any(hint.affects(region) for region in regions)
            ]
            for key in stale:
                self._results.pop(key, None)
            self.discarded_change += len(stale)
            if regions and self._pending is not None:
                self._pending = None
        return len(stale)

    def mark_useful(self, key: str) -> None:
        """Record that a hint actually shortened live resolution (§76)."""
        with self._lock:
            self.useful += 1

    def peek(self, key: str) -> Speculation | None:
        """Read a hint without consuming it (diagnostics and tests)."""
        with self._lock:
            return self._results.get(key)

    # -- Lifecycle ------------------------------------------------------------

    def wait_idle(self, timeout: float = DEFAULT_WAIT_IDLE_SECONDS) -> bool:
        """Wait until no speculation is queued or running. Returns success.

        A *completed, unconsumed* hint does not count as busy: it is a finished
        result waiting for the caller, not work in flight. (Waiting on that too
        would make this unable to return while a hint is parked -- and the
        timeout fallback below would then have to claim success it had not
        earned.)
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if self._idle_locked():
                    return True
            time.sleep(0.001)
        with self._lock:
            return self._idle_locked()

    def _idle_locked(self) -> bool:
        """True when nothing is queued and no job is executing (lock held)."""
        return self._pending is None and not self._running and self._queue.empty()

    def shutdown(self, timeout: float = DEFAULT_WAIT_IDLE_SECONDS) -> None:
        """Stop the worker thread. Idempotent; never leaves an orphan thread."""
        self._stop.set()
        thread = self._thread
        if thread is None:
            return
        with contextlib.suppress(queue.Full):
            self._queue.put_nowait(None)
        thread.join(timeout=timeout)
        self._thread = None

    # -- Internals ------------------------------------------------------------

    def _outstanding(self) -> int:
        """How many results and pending jobs exist right now."""
        return len(self._results) + (1 if self._pending is not None else 0)

    def _replace_pending_locked(self, job: _Job) -> bool:
        """Replace the slot's pending job. Returns True when one was dropped."""
        dropped = self._pending is not None
        self._pending = job
        return dropped

    def _ensure_worker(self) -> None:
        """Start the worker thread on first use."""
        if self._thread is not None or self._stop.is_set():
            return
        self._thread = threading.Thread(
            target=self._work, name="T-SPECULATE", daemon=True
        )
        self._thread.start()

    def _work(self) -> None:
        """Worker loop: resolve queued jobs until asked to stop."""
        while not self._stop.is_set():
            try:
                job = self._queue.get(timeout=0.05)
            except queue.Empty:
                continue
            if job is None:
                return
            with self._lock:
                if self._pending is not None and self._pending.key == job.key:
                    self._pending = None
                self._running = True
            try:
                self._run_job(job)
            finally:
                with self._lock:
                    self._running = False

    def _run_job(self, job: _Job) -> None:
        """Resolve one speculative job and store the hint."""
        started = self._clock()
        try:
            resolution = self._resolver.resolve(
                job.query, job.state.elements, occluders=job.state.elements
            )
        except Exception:  # a speculation must never take the process down
            with self._lock:
                self.failed += 1
            return
        elapsed_ms = max(0.0, (self._clock() - started) * 1000.0)
        best = resolution.best
        hint = Speculation(
            key=job.key,
            query=job.query,
            frame_id=job.state.frame_id,
            state_version=job.state.state_version,
            generation=job.state.generation,
            status=resolution.status,
            element_id=None if best is None else best.element.element_id,
            identity_path=None if best is None else identity_path(best.element),
            identity=None if best is None else best.element.identity,
            role=None if best is None else best.element.role.value,
            owner_window_id=None if best is None else best.element.owner_window_id,
            score=None if best is None else best.score,
            bbox=None if best is None else best.element.bbox,
            computation_ms=elapsed_ms,
        )
        with self._lock:
            self.completed += 1
            self._results[job.key] = hint
            # Oldest-first eviction (dicts preserve insertion order), so a
            # caller that never consumes hints cannot grow this without bound.
            while len(self._results) > MAX_RETAINED_HINTS:
                self._results.pop(next(iter(self._results)))

    # -- Reporting ------------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        """A JSON-shaped snapshot, including the hint-usefulness rate (§76).

        The rate is reported against *taken* hints, because a discarded hint was
        never given the chance to help: reporting usefulness against every
        request would flatter the optimization.
        """
        with self._lock:
            taken = self.taken
            return {
                "enabled": self.enabled,
                "background": self._background,
                "requested": self.requested,
                "completed": self.completed,
                "failed": self.failed,
                "superseded": self.superseded,
                "cancelled": self.cancelled,
                "taken": taken,
                "useful": self.useful,
                "usefulness_rate": (self.useful / taken) if taken else None,
                "discarded_stale": self.discarded_stale,
                "discarded_change": self.discarded_change,
                "outstanding": self._outstanding(),
                "max_outstanding": self.max_pending,
            }


def build_speculative_perceiver(
    settings: SequenceSettings,
    *,
    resolver: TargetResolver | None = None,
    clock: Any = time.monotonic,
    background: bool = True,
) -> SpeculativePerceiver:
    """Build a speculative perceiver with its own dedicated resolver.

    A dedicated resolver is deliberate (see the class docstring): speculation
    must not perturb the live resolution counters, and it must not consult the
    live resolver cache, because a hint about a hint is not evidence.
    """
    from core.resolver_cache import scorer_or_none

    dedicated = resolver if resolver is not None else TargetResolver(
        _resolver_settings(), semantic_scorer=scorer_or_none(), semantic_match_floor=70.0
    )
    return SpeculativePerceiver(settings, dedicated, clock=clock, background=background)


def _resolver_settings() -> Any:
    """The default resolver configuration, for the dedicated speculative resolver."""
    from config.settings import Settings

    return Settings().resolver


__all__ = [
    "DEFAULT_WAIT_IDLE_SECONDS",
    "MAX_RETAINED_HINTS",
    "Speculation",
    "SpeculativePerceiver",
    "build_speculative_perceiver",
]
