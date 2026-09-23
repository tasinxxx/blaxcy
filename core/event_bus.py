"""Typed event bus (specification section 65).

Everything that happens inside BLAXCY is emitted onto one bus, so the state
cache, the GUI action log, the watchdog and any future cancellation consumer all
observe the *same ordered stream*. There is deliberately only one bus: the
batching layer adds sequence events to this bus, never a second delivery path
with different semantics (sections 65, 66.1).

Design rules:

* **The bus never lies about delivery.** A subscriber that raises is recorded
  (``error_count`` / ``last_error``) and skipped; it never prevents delivery to
  the other subscribers and it never silently converts a failure into a success.
* **Dispatch never holds the lock.** The subscriber list is snapshotted under the
  lock, then callbacks run outside it. A subscriber may therefore publish
  re-entrantly (the emergency-stop path relies on being able to emit while an
  action is unwinding) without deadlocking.
* **History is bounded.** A rolling window of the most recent events is retained
  for the GUI/log; it is a convenience view, never the source of truth, and it
  can never grow without bound.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable, Iterable
from typing import Final

from schemas.events import Event, EventType

#: Callback invoked for every event a subscription observes.
EventCallback = Callable[[Event], None]

#: Default number of recent events retained for replay/inspection.
DEFAULT_HISTORY_SIZE: Final[int] = 256


class _Registration:
    """One subscriber entry: a callback and the event type it cares about."""

    __slots__ = ("active", "callback", "event_type")

    def __init__(self, event_type: EventType | None, callback: EventCallback) -> None:
        # ``None`` means "every event type" (a wildcard subscription).
        self.event_type = event_type
        self.callback = callback
        self.active = True


class Subscription:
    """A cancellable handle on one event bus subscription.

    Cancelling a subscription is idempotent. A cancelled subscription never
    receives another event, even if it is cancelled from inside a callback.
    """

    __slots__ = ("_bus", "_registration")

    def __init__(self, bus: EventBus, registration: _Registration) -> None:
        self._bus = bus
        self._registration = registration

    @property
    def event_type(self) -> EventType | None:
        """The event type this subscription observes (``None`` = wildcard)."""
        return self._registration.event_type if self._registration.active else None

    @property
    def active(self) -> bool:
        """True until :meth:`cancel` is called."""
        return self._registration.active

    def cancel(self) -> None:
        """Stop receiving events. Idempotent."""
        if self._registration.active:
            self._registration.active = False
            self._bus._unsubscribe(self._registration)


class EventBus:
    """A thread-safe, bounded, single-stream typed event bus (section 65)."""

    def __init__(self, *, history_size: int = DEFAULT_HISTORY_SIZE) -> None:
        """Create a bus.

        Args:
            history_size: Maximum number of recent events retained. Must be > 0.
        """
        if history_size <= 0:
            raise ValueError("history_size must be positive")
        self._lock = threading.RLock()
        self._registrations: list[_Registration] = []
        self._history: deque[Event] = deque(maxlen=history_size)
        self._delivered = 0
        self._error_count = 0
        self._last_error: str | None = None

    # -- Subscription ---------------------------------------------------------

    def subscribe(
        self,
        callback: EventCallback,
        *,
        event_type: EventType | None = None,
    ) -> Subscription:
        """Register ``callback`` for ``event_type`` (or every type when ``None``)."""
        registration = _Registration(event_type, callback)
        with self._lock:
            self._registrations.append(registration)
        return Subscription(self, registration)

    def subscribe_many(
        self,
        event_types: Iterable[EventType],
        callback: EventCallback,
    ) -> tuple[Subscription, ...]:
        """Register ``callback`` for several event types at once."""
        return tuple(self.subscribe(callback, event_type=event_type) for event_type in event_types)

    def _unsubscribe(self, registration: _Registration) -> None:
        """Remove a registration. No-op when it is already gone."""
        with self._lock:
            try:
                self._registrations.remove(registration)
            except ValueError:
                return

    @property
    def subscriber_count(self) -> int:
        """Number of active subscriptions."""
        with self._lock:
            return len(self._registrations)

    def subscriber_count_for(self, event_type: EventType) -> int:
        """Number of active subscriptions that would receive ``event_type``."""
        with self._lock:
            return sum(
                1
                for registration in self._registrations
                if registration.active
                and (registration.event_type is None or registration.event_type is event_type)
            )

    # -- Publishing -----------------------------------------------------------

    def publish(self, event: Event) -> int:
        """Record ``event`` and deliver it to every matching subscriber.

        Returns:
            The number of subscribers the event was delivered to. The event is
            recorded in history (and counted as delivered) even when nobody is
            listening, because "the bus received it" and "a human saw it" are
            different claims.
        """
        with self._lock:
            self._history.append(event)
            targets = [
                registration
                for registration in self._registrations
                if registration.active
                and (registration.event_type is None or registration.event_type is event.event_type)
            ]

        delivered = 0
        for registration in targets:
            # A subscription cancelled by an earlier callback in this same
            # dispatch must not receive the event.
            if not registration.active:
                continue
            # One bad subscriber must not break the bus or the action that
            # emitted the event; the failure is recorded so it is visible rather
            # than silently swallowed.
            try:
                registration.callback(event)
            except Exception as exc:
                with self._lock:
                    self._error_count += 1
                    self._last_error = f"{type(exc).__name__}: {exc}"
                continue
            delivered += 1

        with self._lock:
            self._delivered += delivered
        return delivered

    def publish_many(self, events: Iterable[Event]) -> int:
        """Publish a batch of events, returning the total delivery count."""
        return sum(self.publish(event) for event in events)

    # -- Inspection -----------------------------------------------------------

    @property
    def history(self) -> tuple[Event, ...]:
        """The retained recent events, oldest first (bounded)."""
        with self._lock:
            return tuple(self._history)

    def history_of(self, event_type: EventType) -> tuple[Event, ...]:
        """The retained recent events of one type, oldest first."""
        with self._lock:
            return tuple(event for event in self._history if event.event_type is event_type)

    def latest(self, event_type: EventType | None = None) -> Event | None:
        """The most recent retained event, optionally filtered by type."""
        with self._lock:
            for event in reversed(self._history):
                if event_type is None or event.event_type is event_type:
                    return event
        return None

    def clear_history(self) -> None:
        """Drop the retained history. Does not touch subscriptions or counters."""
        with self._lock:
            self._history.clear()

    @property
    def delivered_count(self) -> int:
        """Total successful subscriber deliveries since construction."""
        with self._lock:
            return self._delivered

    @property
    def error_count(self) -> int:
        """Total subscriber exceptions observed (never silently discarded)."""
        with self._lock:
            return self._error_count

    @property
    def last_error(self) -> str | None:
        """A short description of the most recent subscriber exception."""
        with self._lock:
            return self._last_error
