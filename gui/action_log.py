"""Action log (specification sections 65, 70, 71).

The log is a *view of the one event bus*, in arrival order. A separate
"GUI log" maintained beside the bus would be a second, drifting account of what
happened, and section 65 deliberately has exactly one stream that the state
cache, the safety path and the GUI all observe.

Two properties matter for correctness rather than looks:

* **Events arrive on arbitrary threads** -- the executor, the AT-SPI owner
  thread, the Brain loop -- and Qt widgets may only be touched on the GUI thread
  (section 71). Every entry is therefore marshalled through a queued signal.
* **The log is bounded.** A long session must not grow an unbounded widget, so
  the view retains a hard maximum of lines and drops the oldest.

Nothing here is load-bearing: losing the log loses no state, and the log is never
used to decide anything.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Final

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QPlainTextEdit, QVBoxLayout, QWidget

from core.event_bus import EventBus, Subscription
from schemas.events import Event, EventType

#: Retained lines. A rolling window, matching the bus's own bounded history.
DEFAULT_MAX_LINES: Final[int] = 2000

#: Payload keys worth surfacing on the one-line entry, in a stable order.
_SUMMARY_KEYS: Final[tuple[str, ...]] = (
    "error_code",
    "verification",
    "reason",
    "message",
    "mode",
    "previous_mode",
    "current_mode",
    "sequence_id",
    "step_index",
    "total_steps",
    "halt_reason",
    "tool",
    "target",
)


def format_event(event: Event, *, wall_clock: Callable[[], float] = time.time) -> str:
    """Render one event as a single log line.

    The format is deliberately boring and stable: a timestamp, the event type,
    then the payload fields a human needs. It never truncates a value silently.
    """
    stamp = time.strftime("%H:%M:%S", time.localtime(event.timestamp))
    milliseconds = int((event.timestamp % 1) * 1000)
    parts: list[str] = []
    payload = event.payload
    for key in _SUMMARY_KEYS:
        if key in payload:
            parts.append(f"{key}={payload[key]}")
    for key in sorted(set(payload) - set(_SUMMARY_KEYS)):
        value = payload[key]
        parts.append(f"{key}={value}")
    suffix = "" if not parts else "  " + " ".join(parts)
    return f"{stamp}.{milliseconds:03d}  {event.event_type.value}{suffix}"


class ActionLog(QWidget):
    """A bounded, append-only rendering of the event bus (sections 65, 71)."""

    #: Emitted from whatever thread published the event; delivered queued.
    entry_received = Signal(str)

    def __init__(
        self,
        *,
        max_lines: int = DEFAULT_MAX_LINES,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        if max_lines <= 0:
            raise ValueError("max_lines must be positive")
        self._max_lines = int(max_lines)
        self._subscription: Subscription | None = None
        self._received = 0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._view = QPlainTextEdit(self)
        self._view.setObjectName("action_log_view")
        self._view.setReadOnly(True)
        self._view.setMaximumBlockCount(self._max_lines)
        self._view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        layout.addWidget(self._view)

        self.entry_received.connect(
            self._on_entry, Qt.ConnectionType.QueuedConnection
        )

    # -- Wiring ----------------------------------------------------------------

    def attach(self, bus: EventBus) -> Subscription:
        """Observe every event on ``bus`` (a wildcard subscription)."""
        if self._subscription is not None:
            self._subscription.cancel()
        self._subscription = bus.subscribe(self._on_event)
        return self._subscription

    def detach(self) -> None:
        """Stop observing. Idempotent."""
        if self._subscription is not None:
            self._subscription.cancel()
            self._subscription = None

    # -- Ingest ----------------------------------------------------------------

    def _on_event(self, event: Event) -> None:
        """Bus callback: may run on any thread, so it only emits."""
        try:
            self.entry_received.emit(format_event(event))
        except RuntimeError:  # pragma: no cover - widget gone during shutdown
            return

    def _on_entry(self, line: str) -> None:
        """GUI-thread append."""
        self._view.appendPlainText(line)

    def append(self, event: Event) -> None:
        """Append one event from any thread (the same path the bus uses)."""
        self._on_event(event)

    # -- Inspection ------------------------------------------------------------

    def line_count(self) -> int:
        """Lines currently retained (bounded by ``max_lines``)."""
        return self._view.blockCount()

    def text(self) -> str:
        """The whole retained log as text."""
        return self._view.toPlainText()

    def lines(self) -> tuple[str, ...]:
        """Retained lines split out, for tests."""
        body = self.text()
        return tuple(body.splitlines()) if body else ()

    def received(self) -> int:
        """How many events have been handed to the view (not how many show)."""
        return self._received

    def clear(self) -> None:
        """Drop every retained line. Does not detach."""
        self._view.clear()

    def event_types_seen(self) -> tuple[str, ...]:
        """Distinct event types present in the retained window, in first-seen order.

        Derived from the rendered lines rather than from a side ledger, so this
        cannot report an event the log never actually showed.
        """
        seen: list[str] = []
        for line in self.lines():
            pieces = line.split()
            if len(pieces) >= 2 and pieces[1] not in seen:
                seen.append(pieces[1])
        return tuple(seen)


__all__ = ["DEFAULT_MAX_LINES", "ActionLog", "EventType", "format_event"]
