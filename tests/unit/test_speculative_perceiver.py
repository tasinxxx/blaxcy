"""Speculative perception (specification section 33.2).

The batching layer ships enabled by default, which means the **background**
worker (``T-SPECULATE``) is what production actually runs. These tests cover that
path directly -- the sequence-runner tests deliberately run the perceiver inline
(``background=False``) so their assertions stay deterministic -- and the section
4 rule 31 properties that justify enabling it: a hint is read-side only, it can
never block the thread that owns the sequence, a structural change discards it,
and a credential description is never speculated at all.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Sequence
from typing import Any

import pytest

from config.settings import ResolverSettings, SequenceSettings
from core.speculative_perceiver import (
    MAX_RETAINED_HINTS,
    Speculation,
    SpeculativePerceiver,
)
from core.target_resolver import ResolutionResult, TargetResolver
from schemas.elements import ElementQuery, UIElement
from schemas.enums import ChangeClass, UIRole
from schemas.screen_state import ScreenState
from tests.harness.phase8 import box_of
from tests.harness.phase101 import workflow_state

pytestmark = pytest.mark.batching


def _resolver() -> TargetResolver:
    """A real resolver, so a speculation is a real resolution."""
    return TargetResolver(ResolverSettings())


def _perceiver(*, enabled: bool = True, background: bool = True) -> SpeculativePerceiver:
    """A perceiver over the real resolver, in the given mode."""
    return SpeculativePerceiver(
        SequenceSettings(speculative_perception=enabled),
        _resolver(),
        background=background,
    )


class _SlowResolver(TargetResolver):
    """A real resolver that pauses before resolving, to expose a blocking caller.

    Subclasses :class:`TargetResolver` so the perceiver's declared resolver type
    still holds. If the scheduling path ever ran the job inline, a caller would
    pay the sleep; running it on the worker means the caller does not.
    """

    def __init__(self, delay: float) -> None:
        """Create a resolver that pauses ``delay`` seconds before each resolution."""
        super().__init__(ResolverSettings())
        self._delay = delay
        self.calls = 0

    def resolve(
        self,
        query: ElementQuery,
        elements: Sequence[UIElement],
        *,
        occluders: Sequence[Any] = (),
        state: ScreenState | None = None,
    ) -> ResolutionResult:
        """Sleep, then resolve for real."""
        self.calls += 1
        time.sleep(self._delay)
        return super().resolve(query, elements, occluders=occluders, state=state)


def _play() -> ElementQuery:
    """The workflow's unique play control, as a description."""
    return ElementQuery(text="Play")


# -- The flag ----------------------------------------------------------------


def test_the_perceiver_is_inert_when_the_flag_is_off() -> None:
    """Section 4 rule 31: with the flag off, nothing is scheduled or stored."""
    perceiver = _perceiver(enabled=False)

    scheduled = perceiver.speculate("s5", _play(), workflow_state(1))

    assert scheduled is False
    assert perceiver.stats()["requested"] == 0
    assert perceiver.peek("s5") is None


# -- The production background worker ----------------------------------------


def test_a_background_speculation_is_takeable_once_it_completes() -> None:
    """The worker really resolves, and the result is only ever a hint."""
    perceiver = _perceiver(background=True)
    try:
        state = workflow_state(1)

        assert perceiver.speculate("s5", _play(), state) is True
        assert perceiver.wait_idle(2.0) is True

        hint = perceiver.take("s5", state=state)

        assert isinstance(hint, Speculation)
        assert hint is not None
        assert hint.element_id == "play"
        assert hint.identity_path == "/p/play"
        # A hint carries no lease and no coordinate that could reach input.
        assert not hasattr(hint, "lease")
        assert perceiver.stats()["taken"] == 1
    finally:
        perceiver.shutdown()


def test_the_background_worker_never_blocks_the_caller() -> None:
    """Section 33.2/32: scheduling must not stall the sequence thread."""
    slow = _SlowResolver(delay=0.25)
    perceiver = SpeculativePerceiver(
        SequenceSettings(speculative_perception=True), slow, background=True
    )
    try:
        state = workflow_state(1)

        started = time.perf_counter()
        assert perceiver.speculate("s5", _play(), state) is True
        elapsed = time.perf_counter() - started

        # Well under the resolver's own 250 ms: the call schedules, it does not
        # resolve. A slow perception pass therefore cannot delay T-EXEC.
        assert elapsed < 0.1
        assert perceiver.wait_idle(2.0) is True
        assert slow.calls == 1
        assert perceiver.take("s5", state=state) is not None
    finally:
        perceiver.shutdown()


def test_shutdown_joins_the_worker_and_is_idempotent() -> None:
    """Section 64: an optimization may not leave an orphan thread behind."""
    perceiver = _perceiver(background=True)
    try:
        perceiver.speculate("s5", _play(), workflow_state(1))
        assert perceiver.wait_idle(2.0) is True
        assert any(thread.name == "T-SPECULATE" for thread in threading.enumerate())

        perceiver.shutdown()
        perceiver.shutdown()  # idempotent

        assert not any(thread.name == "T-SPECULATE" for thread in threading.enumerate())
    finally:
        perceiver.shutdown()


# -- Safety boundaries -------------------------------------------------------


def test_a_credential_description_is_never_speculated() -> None:
    """Section 55: a password step always resolves fresh, never prefetched."""
    perceiver = _perceiver()

    scheduled = perceiver.speculate(
        "pw", ElementQuery(text="Password", role_hint=UIRole.PASSWORD_INPUT), workflow_state(1)
    )

    assert scheduled is False
    assert perceiver.stats()["requested"] == 0
    assert perceiver.peek("pw") is None
    assert perceiver.stats()["completed"] == 0


def test_a_structural_change_discards_a_hint_and_a_trivial_one_does_not() -> None:
    """Section 34/33.2: only MEANINGFUL/MAJOR invalidates the region it touched."""
    perceiver = _perceiver(background=True)
    try:
        state = workflow_state(1)
        play_box = box_of(state.elements[4])
        perceiver.speculate("s5", _play(), state)
        assert perceiver.wait_idle(2.0) is True

        # An animation elsewhere invalidates nothing.
        assert perceiver.note_change(ChangeClass.ANIMATION, (play_box,)) == 0
        assert perceiver.peek("s5") is not None

        # A structural change to the hinted element's own region does.
        assert perceiver.note_change(ChangeClass.MEANINGFUL, (play_box,)) == 1
        assert perceiver.peek("s5") is None
        assert perceiver.take("s5", state=state) is None
        assert perceiver.stats()["discarded_change"] == 1
    finally:
        perceiver.shutdown()


def test_a_stale_generation_discards_the_hint() -> None:
    """Section 32/33.2: a new generation supersedes a hint (fail-closed)."""
    perceiver = _perceiver(background=True)
    try:
        state = workflow_state(1)
        perceiver.speculate("s5", _play(), state)
        assert perceiver.wait_idle(2.0) is True

        assert perceiver.take("s5", state=workflow_state(1, generation=2)) is None
        assert perceiver.stats()["discarded_stale"] == 1
    finally:
        perceiver.shutdown()


def test_latest_request_wins_and_the_queue_is_bounded() -> None:
    """Section 32: no unbounded perception queue; a stale job is dropped."""
    perceiver = _perceiver(background=True)
    try:
        state = workflow_state(1)
        for _ in range(50):
            assert perceiver.speculate("next", _play(), state) is True

        assert perceiver.wait_idle(2.0) is True
        stats = perceiver.stats()

        assert stats["requested"] == 50
        # Latest-request-wins: the repeated key is superseded, never queued 50 deep.
        assert stats["superseded"] >= 1
        assert stats["outstanding"] <= 1
        assert perceiver.take("next", state=state) is not None
    finally:
        perceiver.shutdown()


def test_distinct_unconsumed_hints_cannot_grow_without_bound() -> None:
    """A caller that never takes its hints still cannot leak memory (section 32)."""
    perceiver = _perceiver(background=True)
    try:
        state = workflow_state(1)
        for index in range(50):
            assert perceiver.speculate(f"k{index}", _play(), state) is True

        assert perceiver.wait_idle(2.0) is True

        assert perceiver.stats()["outstanding"] <= MAX_RETAINED_HINTS
    finally:
        perceiver.shutdown()
