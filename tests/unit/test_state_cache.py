"""State cache: staleness rejection, fail-closed invalidation, events (sections 32, 65).

The cache is driven through the real :class:`EventBus` so the section 65
contract -- state updates and invalidations are observable on the one shared
stream -- is verified rather than assumed.
"""

from __future__ import annotations

from core.event_bus import EventBus
from core.state_cache import StateCache, StateSnapshot
from schemas.capability import Capability, CapabilityReport
from schemas.elements import UIElement
from schemas.enums import (
    CapabilityName,
    CapabilityStatus,
    ChangeClass,
    CoordinateSpace,
    PerceptionSource,
    UIRole,
    VerificationState,
)
from schemas.events import EventType
from schemas.geometry import MonitorGeometry, MonitorLayout, Rect
from schemas.leases import ElementLease, issue_lease
from schemas.screen_state import ChangeRegion, ScreenDelta, ScreenState
from schemas.sequences import SequenceProgress

_DESKTOP_RECT = Rect(x=0.0, y=0.0, width=40.0, height=20.0, space=CoordinateSpace.DESKTOP)


def _layout(*, width: int = 1920, height: int = 1080) -> MonitorLayout:
    """A single-monitor layout."""
    return MonitorLayout(
        monitors=(
            MonitorGeometry(monitor_id=0, width=width, height=height, is_primary=True),
        )
    )


def _state(
    frame_id: int,
    *,
    generation: int = 0,
    layout: MonitorLayout | None = None,
) -> ScreenState:
    """A minimal perceived state carrying the identity stamps the cache checks."""
    return ScreenState(
        frame_id=frame_id,
        state_version=frame_id,  # deliberately wrong: the cache is the version authority
        generation=generation,
        timestamp=1000.0 + frame_id,
        monotonic=float(frame_id),
        layout=layout if layout is not None else _layout(),
    )


def _delta(
    previous: ScreenState,
    current: ScreenState,
    *,
    change_class: ChangeClass = ChangeClass.NONE,
) -> ScreenDelta:
    """A classified delta between two states, with one region when non-NONE."""
    regions: tuple[ChangeRegion, ...] = ()
    if change_class is not ChangeClass.NONE:
        regions = (ChangeRegion(rect=_DESKTOP_RECT, change_class=change_class),)
    return ScreenDelta(
        from_frame_id=previous.frame_id,
        to_frame_id=current.frame_id,
        from_state_version=previous.state_version,
        to_state_version=current.state_version,
        generation=current.generation,
        regions=regions,
        computed_at=0.0,
    )


def _element(element_id: str = "e1") -> UIElement:
    """A clickable AT-SPI button element usable for lease issuance."""
    return UIElement(
        element_id=element_id,
        role=UIRole.BUTTON,
        text="Button Alpha",
        accessible_name="Button Alpha",
        clickable=True,
        effective_clickable=True,
        source=PerceptionSource.ATSPI,
        confidence=0.95,
        atspi_path=f"/org/a11y/atspi/accessible/{element_id}",
    )


def _lease(
    *,
    lease_id: str = "l1",
    element_id: str = "e1",
    frame_id: int = 0,
    state_version: int = 1,
    generation: int = 0,
    ttl_ms: int = 800,
    now_monotonic: float = 0.0,
) -> ElementLease:
    """Issue a real lease over a real element."""
    return issue_lease(
        _element(element_id),
        frame_id=frame_id,
        state_version=state_version,
        generation=generation,
        ttl_ms=ttl_ms,
        now_monotonic=now_monotonic,
        lease_id=lease_id,
    )


def _report(*, session_type: str = "x11", available: bool = True) -> CapabilityReport:
    """A capability report with one capability whose status can be toggled."""
    return CapabilityReport(
        capabilities=(
            Capability(
                name=CapabilityName.CAPTURE,
                status=CapabilityStatus.AVAILABLE if available else CapabilityStatus.UNAVAILABLE,
                backend="mss" if available else None,
            ),
        ),
        generated_at=1.0,
        session_type=session_type,
    )


# -- Staleness (section 32) ---------------------------------------------------


def test_first_update_is_accepted_and_versioned() -> None:
    """The first observation becomes current with a cache-assigned version."""
    cache = StateCache()
    accepted = cache.update_screen_state(_state(4))

    assert accepted is not None
    assert accepted.state_version == 1
    assert cache.current is accepted
    assert cache.previous is None
    assert cache.state_version == 1
    assert cache.accepted_updates == 1
    assert cache.rejected_updates == 0


def test_stale_frame_id_is_rejected_without_mutation() -> None:
    """An equal or older frame id is dropped and never overwrites current state."""
    cache = StateCache()
    cache.update_screen_state(_state(5))
    current = cache.current

    assert cache.update_screen_state(_state(5)) is None
    assert cache.update_screen_state(_state(3)) is None

    assert cache.current is current
    assert cache.rejected_updates == 2
    assert cache.accepted_updates == 1


def test_older_generation_is_rejected() -> None:
    """A state from an older perception generation is stale even with a newer frame."""
    cache = StateCache()
    cache.update_screen_state(_state(1, generation=2))

    assert cache.update_screen_state(_state(99, generation=1)) is None
    assert cache.current is not None and cache.current.generation == 2


def test_state_version_is_independent_of_frame_id() -> None:
    """Version increments per accepted update, so a skipped frame shows as a gap."""
    cache = StateCache()
    cache.update_screen_state(_state(10))
    accepted = cache.update_screen_state(_state(25))

    assert accepted is not None
    assert accepted.state_version == 2
    assert accepted.frame_id == 25


def test_is_stale_update_matches_rejection_rules() -> None:
    """The predicate the cache uses is exposed and agrees with actual acceptance."""
    cache = StateCache()
    cache.update_screen_state(_state(5, generation=1))

    assert cache.is_stale_update(_state(5, generation=1)) is True
    assert cache.is_stale_update(_state(4, generation=1)) is True
    assert cache.is_stale_update(_state(6, generation=0)) is True
    assert cache.is_stale_update(_state(6, generation=1)) is False


def test_delta_is_restamped_to_cache_versions() -> None:
    """The accepted delta cannot disagree with the state it describes."""
    cache = StateCache()
    first = cache.update_screen_state(_state(0))
    assert first is not None
    second_state = _state(1)
    cache.update_screen_state(
        second_state, delta=_delta(first, second_state, change_class=ChangeClass.TRIVIAL)
    )

    delta = cache.delta
    assert delta is not None
    assert delta.from_state_version == 1
    assert delta.to_state_version == cache.state_version == 2
    assert delta.generation == second_state.generation


# -- Fail-closed invalidation (sections 32, 34, 44) --------------------------


def test_ordinary_update_keeps_leases() -> None:
    """A normal update does not clear leases; they expire on their own TTL."""
    cache = StateCache()
    cache.update_screen_state(_state(0))
    cache.store_lease(_lease())

    cache.update_screen_state(_state(1))

    assert len(cache.leases) == 1


def test_generation_change_clears_leases_and_emits_rejection() -> None:
    """A new generation invalidates every old lease (section 32)."""
    bus = EventBus()
    cache = StateCache(event_bus=bus)
    cache.update_screen_state(_state(0, generation=0))
    cache.store_lease(_lease(generation=0))

    cache.update_screen_state(_state(1, generation=1))

    assert cache.leases == ()
    assert len(bus.history_of(EventType.LEASE_REJECTED)) == 1


def test_layout_change_clears_leases_and_emits_layout_event() -> None:
    """A monitor-topology change invalidates leases and is announced (section 31)."""
    bus = EventBus()
    cache = StateCache(event_bus=bus)
    cache.update_screen_state(_state(0))
    cache.store_lease(_lease())

    cache.update_screen_state(_state(1, layout=_layout(width=2560, height=1440)))

    assert cache.leases == ()
    assert len(bus.history_of(EventType.LAYOUT_CHANGED)) == 1


def test_structural_change_clears_leases() -> None:
    """A MEANINGFUL change invalidates leases (fail-closed)."""
    cache = StateCache()
    first = cache.update_screen_state(_state(0))
    assert first is not None
    cache.store_lease(_lease())

    second = _state(1)
    cache.update_screen_state(
        second, delta=_delta(first, second, change_class=ChangeClass.MEANINGFUL)
    )

    assert cache.leases == ()


def test_trivial_change_keeps_leases_but_announces_screen_change() -> None:
    """A TRIVIAL change is not structural: leases survive, the change is reported."""
    bus = EventBus()
    cache = StateCache(event_bus=bus)
    first = cache.update_screen_state(_state(0))
    assert first is not None
    cache.store_lease(_lease())

    second = _state(1)
    cache.update_screen_state(
        second, delta=_delta(first, second, change_class=ChangeClass.TRIVIAL)
    )

    assert len(cache.leases) == 1
    assert len(bus.history_of(EventType.SCREEN_CHANGED)) == 1


def test_invalidate_leases_reports_count_and_emits() -> None:
    """Explicit invalidation clears everything and reports per-lease rejections."""
    bus = EventBus()
    cache = StateCache(event_bus=bus)
    cache.store_lease(_lease(lease_id="a"))
    cache.store_lease(_lease(lease_id="b"))

    cleared = cache.invalidate_leases(reason="test")

    assert cleared == 2
    assert cache.leases == ()
    assert len(bus.history_of(EventType.LEASE_REJECTED)) == 2


# -- Lease storage (section 44) ----------------------------------------------


def test_lease_store_lookup_and_drop() -> None:
    """Leases are addressable and individually removable."""
    cache = StateCache()
    cache.store_lease(_lease(lease_id="abc"))

    assert cache.lease("abc") is not None
    assert cache.drop_lease("abc") is True
    assert cache.drop_lease("abc") is False
    assert cache.lease("abc") is None


def test_prune_expired_leases_uses_ttl() -> None:
    """Only leases past their TTL are pruned."""
    cache = StateCache()
    cache.store_lease(_lease(lease_id="short", ttl_ms=100, now_monotonic=0.0))
    cache.store_lease(_lease(lease_id="long", ttl_ms=10_000, now_monotonic=0.0))

    pruned = cache.prune_expired_leases(now_monotonic=1.0)

    assert pruned == 1
    assert [lease.lease_id for lease in cache.leases] == ["long"]


# -- Capabilities / task / action / verification / sequence -------------------


def test_capabilities_emit_only_when_changed() -> None:
    """An identical report does not spam the bus; a real change does."""
    bus = EventBus()
    cache = StateCache(event_bus=bus)

    cache.set_capabilities(_report())
    cache.set_capabilities(_report())
    cache.set_capabilities(_report(available=False))

    assert cache.capabilities is not None
    assert len(bus.history_of(EventType.CAPABILITY_CHANGED)) == 2


def test_active_task_action_and_verification_round_trip() -> None:
    """Task/action/verification are stored and readable."""
    cache = StateCache()
    initial = cache.verification
    assert initial is VerificationState.NOT_APPLICABLE

    cache.set_active_task("task-1")
    cache.set_active_action("click")
    cache.set_verification(VerificationState.VERIFIED)

    assert cache.active_task == "task-1"
    assert cache.active_action == "click"
    assert cache.verification is VerificationState.VERIFIED


def test_sequence_progress_round_trip() -> None:
    """Sequence progress is stored for the GUI indicator and can be cleared."""
    cache = StateCache()
    progress = SequenceProgress(sequence_id="seq-1", current_index=1, total_steps=3, completed_indices=(0,))

    cache.set_sequence_progress(progress)
    assert cache.sequence is not None and cache.sequence.sequence_id == "seq-1"

    cache.set_sequence_progress(None)
    assert cache.sequence is None


# -- Events / snapshot / lifecycle -------------------------------------------


def test_state_update_emits_state_updated_only() -> None:
    """An accepted update with no delta emits STATE_UPDATED and nothing else."""
    bus = EventBus()
    cache = StateCache(event_bus=bus)
    cache.update_screen_state(_state(0))

    assert len(bus.history_of(EventType.STATE_UPDATED)) == 1
    assert bus.history_of(EventType.SCREEN_CHANGED) == ()


def test_rejected_update_emits_nothing() -> None:
    """A stale update is silent: it never masquerades as a state change."""
    bus = EventBus()
    cache = StateCache(event_bus=bus)
    cache.update_screen_state(_state(5))
    before = len(bus.history)

    assert cache.update_screen_state(_state(5)) is None
    assert len(bus.history) == before


def test_snapshot_reflects_all_fields() -> None:
    """A snapshot is a consistent, complete read."""
    cache = StateCache()
    cache.update_screen_state(_state(0))
    cache.store_lease(_lease())
    cache.set_capabilities(_report())
    cache.set_active_task("t")
    cache.set_active_action("click")
    cache.set_verification(VerificationState.UNVERIFIED)
    cache.set_sequence_progress(SequenceProgress(sequence_id="s", current_index=0, total_steps=1))

    snapshot = cache.snapshot()

    assert isinstance(snapshot, StateSnapshot)
    assert snapshot.current is not None
    assert len(snapshot.leases) == 1
    assert snapshot.capabilities is not None
    assert snapshot.active_task == "t"
    assert snapshot.active_action == "click"
    assert snapshot.verification is VerificationState.UNVERIFIED
    assert snapshot.sequence is not None
    assert snapshot.state_version == 1
    assert snapshot.accepted_updates == 1
    assert snapshot.to_dict()["verification"] == "UNVERIFIED"


def test_clear_resets_everything() -> None:
    """``clear`` returns the cache to its pre-first-observation state."""
    cache = StateCache()
    cache.update_screen_state(_state(0))
    cache.store_lease(_lease())
    cache.set_active_task("t")

    cache.clear()

    assert cache.current is None
    assert cache.previous is None
    assert cache.delta is None
    assert cache.leases == ()
    assert cache.active_task is None
    assert cache.state_version == 0
    assert cache.snapshot().accepted_updates == 0
