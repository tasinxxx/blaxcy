"""Typed event bus (specification section 65).

The bus is exercised directly: delivery routing, cancellation (including
cancellation from inside a callback), failure isolation, bounded history, and
re-entrant publication -- the last of which the emergency-stop path depends on.
"""

from __future__ import annotations

import pytest

from core.event_bus import DEFAULT_HISTORY_SIZE, EventBus, Subscription
from schemas.events import Event, EventType


def _event(event_type: EventType = EventType.ACTION_STARTED) -> Event:
    """A fresh event of ``event_type``."""
    return Event.create(event_type)


def test_wildcard_subscriber_receives_every_event() -> None:
    """A ``None`` subscription observes the whole stream."""
    bus = EventBus()
    seen: list[Event] = []
    bus.subscribe(seen.append)

    bus.publish(_event(EventType.STATE_UPDATED))
    bus.publish(_event(EventType.ACTION_STARTED))

    assert [event.event_type for event in seen] == [
        EventType.STATE_UPDATED,
        EventType.ACTION_STARTED,
    ]


def test_typed_subscriber_receives_only_its_type() -> None:
    """A typed subscription ignores other event types but counts as delivered."""
    bus = EventBus()
    seen: list[Event] = []
    bus.subscribe(seen.append, event_type=EventType.EMERGENCY_STOP)

    assert bus.publish(_event(EventType.ACTION_STARTED)) == 0
    assert bus.publish(_event(EventType.EMERGENCY_STOP)) == 1
    assert [event.event_type for event in seen] == [EventType.EMERGENCY_STOP]


def test_subscribe_many_registers_each_type() -> None:
    """``subscribe_many`` returns one handle per requested type."""
    bus = EventBus()
    seen: list[Event] = []
    subscriptions = bus.subscribe_many(
        (EventType.SEQUENCE_STARTED, EventType.SEQUENCE_COMPLETED), seen.append
    )
    assert len(subscriptions) == 2

    bus.publish(_event(EventType.SEQUENCE_STARTED))
    bus.publish(_event(EventType.SEQUENCE_COMPLETED))
    bus.publish(_event(EventType.ACTION_STARTED))

    assert [event.event_type for event in seen] == [
        EventType.SEQUENCE_STARTED,
        EventType.SEQUENCE_COMPLETED,
    ]


def test_cancel_stops_delivery_and_is_idempotent() -> None:
    """Cancelling removes the subscription; cancelling twice is harmless."""
    bus = EventBus()
    seen: list[Event] = []
    subscription = bus.subscribe(seen.append)

    bus.publish(_event())
    subscription.cancel()
    subscription.cancel()
    bus.publish(_event())

    assert len(seen) == 1
    assert subscription.active is False
    assert bus.subscriber_count == 0


def test_cancel_inside_callback_stops_later_delivery() -> None:
    """A subscription cancelled by an earlier callback gets no further events."""
    bus = EventBus()
    seen: list[str] = []
    holder: dict[str, Subscription] = {}

    def cancel_self(_event: Event) -> None:
        seen.append("a")
        holder["sub"].cancel()

    holder["sub"] = bus.subscribe(cancel_self)
    bus.subscribe(lambda _event: seen.append("b"))

    bus.publish(_event())
    bus.publish(_event())

    # First publish: "a" cancels itself, "b" still runs. Second publish: only "b".
    assert seen == ["a", "b", "b"]


def test_subscriber_exception_is_recorded_and_isolated() -> None:
    """One failing subscriber neither blocks the others nor silently disappears."""
    bus = EventBus()
    seen: list[Event] = []

    def boom(_event: Event) -> None:
        raise RuntimeError("subscriber refused")

    bus.subscribe(boom)
    bus.subscribe(seen.append)

    delivered = bus.publish(_event())

    assert delivered == 1
    assert len(seen) == 1
    assert bus.error_count == 1
    assert bus.last_error is not None and "subscriber refused" in bus.last_error


def test_history_is_bounded_and_ordered() -> None:
    """History keeps the most recent events, oldest first, and never grows."""
    bus = EventBus(history_size=3)
    events = [_event() for _ in range(5)]
    for event in events:
        bus.publish(event)

    assert bus.history == tuple(events[-3:])


def test_history_filters_and_latest() -> None:
    """``history_of`` and ``latest`` are type-aware views."""
    bus = EventBus()
    bus.publish(_event(EventType.ACTION_STARTED))
    bus.publish(_event(EventType.ACTION_COMPLETED))
    bus.publish(_event(EventType.ACTION_STARTED))

    assert len(bus.history_of(EventType.ACTION_STARTED)) == 2
    latest = bus.latest(EventType.ACTION_COMPLETED)
    assert latest is not None and latest.event_type is EventType.ACTION_COMPLETED
    assert bus.latest() is not None


def test_clear_history_keeps_subscriptions() -> None:
    """Clearing history does not detach subscribers."""
    bus = EventBus()
    bus.subscribe(lambda _event: None)
    bus.publish(_event())
    bus.clear_history()

    assert bus.history == ()
    assert bus.subscriber_count == 1


def test_reentrant_publish_does_not_deadlock() -> None:
    """A subscriber may publish while an event is being dispatched."""
    bus = EventBus()
    seen: list[EventType] = []

    def relay(event: Event) -> None:
        seen.append(event.event_type)
        if event.event_type is EventType.ACTION_STARTED:
            bus.publish(_event(EventType.ACTION_COMPLETED))

    bus.subscribe(relay)
    bus.publish(_event(EventType.ACTION_STARTED))

    assert seen == [EventType.ACTION_STARTED, EventType.ACTION_COMPLETED]


def test_invalid_history_size_is_rejected() -> None:
    """A non-positive history size is a programming error, not a silent default."""
    with pytest.raises(ValueError):
        EventBus(history_size=0)


def test_default_history_size_is_positive() -> None:
    """The documented default is usable."""
    assert DEFAULT_HISTORY_SIZE > 0
