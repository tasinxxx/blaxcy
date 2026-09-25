"""Confirmation: section 56's dialog and the thread bridge that feeds it (71).

The executor calls its confirmation callback from the thread that is about to
inject input -- a thread that *must* block until a human answers, because it is
holding the single-writer action slot. Qt forbids touching a widget from that
thread. This module is the seam between the two, and it is deliberately the only
place in the GUI that blocks.

Design rules, every one of them a refusal rather than a permission:

* **Deny is the default in every failure mode.** A timeout, a dialog that
  raises, a closed window, no ``QApplication`` at all, a second concurrent
  request, or an emergency stop that latches while the dialog is open all
  resolve to "not confirmed". A bug here can refuse work; it can never authorise
  an action.
* **No implicit Enter acceptance** (section 71). ``Confirm`` starts disabled and
  is not the dialog's default button, auto-default is off, and ``Escape`` denies.
  The countdown is a floor constant, not a setting: :data:`MIN_COUNTDOWN_SECONDS`
  cannot be configured away.
* **The dialog shows what the Body will do**, not a generic prompt: the
  decision's message -- which for a terminal submission is the literal command
  (section 54) -- plus the tool and action class.
* **Credential content is never rendered** (sections 42, 55, 70). A payload
  marked ``sensitive`` has its text-bearing fields suppressed, so the dialog
  shows the fact of the action and never the secret.

The bridge is wired by :mod:`gui.app` as ``BlaxcyApplication(confirmation=...)``,
which is the *only* way the executor receives a confirmation callback at all
(see ``core/application.py``): without one, a destructive action is refused with
``CONFIRMATION_REQUIRED`` rather than proceeding.
"""

from __future__ import annotations

import contextlib
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Final

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.event_bus import EventBus, Subscription
from schemas.events import Event, EventType

#: The section 71 countdown. A floor, not a tuning knob: see ``_tick``.
MIN_COUNTDOWN_SECONDS: Final[float] = 3.0

#: How long a blocked executor thread waits for a human before the request is
#: refused. Long enough to read the dialog, bounded so a stuck or closed GUI can
#: never hold the single-writer action slot forever.
DEFAULT_TIMEOUT_SECONDS: Final[float] = 120.0

#: Detail keys whose values are credential-like content, never rendered.
SENSITIVE_DETAIL_KEYS: Final[frozenset[str]] = frozenset(
    {"text", "value", "content", "secret", "password", "credential"}
)

#: Detail keys shown verbatim, in this order (the section 54/71 set first).
DETAIL_ORDER: Final[tuple[str, ...]] = (
    "tool",
    "action_class",
    "target",
    "application",
    "owner_app",
    "window",
    "window_id",
    "command",
    "key",
    "combo",
    "destination",
    "visible_command",
    "sensitive",
)


@dataclass(frozen=True)
class ConfirmationRequest:
    """One request for a human decision, carried as a value."""

    request_id: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls, message: str, details: dict[str, Any] | None = None
    ) -> ConfirmationRequest:
        """Build a request with a fresh id."""
        return cls(
            request_id=uuid.uuid4().hex,
            message=message,
            details=dict(details or {}),
        )

    def display_fields(self) -> tuple[tuple[str, str], ...]:
        """The ``(label, value)`` rows to show, credential content excluded.

        Unknown keys are shown too (in a stable order): a confirmation that hid
        part of what the Body was about to do would be a worse dialog, and the
        only thing deliberately withheld is credential-like content.
        """
        sensitive = bool(self.details.get("sensitive"))
        rows: list[tuple[str, str]] = []
        for key in DETAIL_ORDER:
            if key not in self.details:
                continue
            if sensitive and key in SENSITIVE_DETAIL_KEYS:
                continue
            rows.append((key, str(self.details[key])))
        for key in sorted(set(self.details) - set(DETAIL_ORDER)):
            if sensitive and key in SENSITIVE_DETAIL_KEYS:
                continue
            rows.append((key, str(self.details[key])))
        return tuple(rows)


#: How the bridge builds its dialog. Injected so a test can drive the bridge
#: without Qt timing; production always uses :class:`ConfirmationDialog`.
DialogFactory = Callable[[ConfirmationRequest], Any]


class ConfirmationDialog(QDialog):
    """Section 56's confirmation, with the section 71 countdown.

    The dialog is modal and answers exactly two questions: did a human press
    ``Confirm``, and did they do it after the countdown. ``approved`` is ``False``
    for every other outcome, including being closed.
    """

    def __init__(
        self,
        request: ConfirmationRequest,
        *,
        clock: Callable[[], float] = time.monotonic,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.request = request
        self._clock = clock
        self._started_at = clock()
        self._approved = False

        self.setWindowTitle("BLAXCY - confirmation required")
        self.setModal(True)
        self.setMinimumWidth(480)

        layout = QVBoxLayout(self)
        heading = QLabel("This action needs your confirmation.", self)
        heading.setWordWrap(True)
        layout.addWidget(heading)

        message = QLabel(request.message, self)
        message.setWordWrap(True)
        message.setObjectName("confirmation_message")
        layout.addWidget(message)

        fields = request.display_fields()
        if fields:
            form = QFormLayout()
            for label, value in fields:
                field_label = QLabel(value, self)
                field_label.setWordWrap(True)
                form.addRow(f"{label}:", field_label)
            layout.addLayout(form)

        self.countdown_label = QLabel("", self)
        self.countdown_label.setObjectName("confirmation_countdown")
        layout.addWidget(self.countdown_label)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel_button = QPushButton("Cancel", self)
        self.confirm_button = QPushButton("Confirm", self)
        self.confirm_button.setObjectName("confirmation_confirm")
        self.cancel_button.setObjectName("confirmation_cancel")
        # Section 71: no implicit Enter acceptance. Neither button is the default
        # and auto-default is off, so Return cannot press Confirm for the user.
        for button in (self.cancel_button, self.confirm_button):
            button.setAutoDefault(False)
            button.setDefault(False)
        self.confirm_button.setEnabled(False)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.confirm_button)
        layout.addLayout(buttons)

        self.confirm_button.clicked.connect(self._on_confirm)
        self.cancel_button.clicked.connect(self.reject)

        self._timer = QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self.refresh_countdown)
        self._timer.start()
        self.refresh_countdown()

    # -- Read-only state -------------------------------------------------------

    @property
    def approved(self) -> bool:
        """True only after a human pressed Confirm following the countdown."""
        return self._approved

    @property
    def confirm_enabled(self) -> bool:
        """Whether the Confirm button is currently pressable."""
        return self.confirm_button.isEnabled()

    @property
    def remaining_seconds(self) -> float:
        """Seconds left before Confirm unlocks (never negative)."""
        return max(0.0, MIN_COUNTDOWN_SECONDS - (self._clock() - self._started_at))

    # -- Behaviour -------------------------------------------------------------

    def refresh_countdown(self) -> None:
        """Re-evaluate the countdown. Driven by the timer, and callable directly."""
        remaining = self.remaining_seconds
        if remaining > 0.0:
            self.countdown_label.setText(f"Confirm unlocks in {remaining:.1f}s")
            self._timer.start()
            return
        self.countdown_label.setText("Confirm is unlocked.")
        self.confirm_button.setEnabled(True)
        self._timer.stop()

    def _on_confirm(self) -> None:
        """Accept, but only if the countdown has actually elapsed.

        A second, independent check beside the disabled button: a programmatic
        click (or a stray space bar on a focused button) cannot shortcut the wait.
        """
        if self.remaining_seconds > 0.0:
            return
        self._approved = True
        self.accept()


@dataclass
class _Pending:
    """One outstanding request awaiting a GUI answer."""

    request: ConfirmationRequest
    answered: threading.Event = field(default_factory=threading.Event)
    approved: bool = False
    dialog: Any = None


class ConfirmationBridge(QObject):
    """Turns the executor's synchronous confirmation callback into a dialog.

    :meth:`request` is called on the *action* thread and blocks until the GUI
    thread answers, so the executor never injects input on an unanswered
    premise. Every failure path denies.
    """

    #: Emitted on the caller's thread; delivered queued to the GUI thread.
    requested = Signal(object)

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        event_bus: EventBus | None = None,
        dialog_factory: DialogFactory | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._clock = clock
        self._timeout = float(timeout_seconds)
        self._factory: DialogFactory = (
            dialog_factory
            if dialog_factory is not None
            else (lambda request: ConfirmationDialog(request, clock=self._clock))
        )
        self._lock = threading.Lock()
        self._pending: _Pending | None = None
        self._subscriptions: list[Subscription] = []
        self._granted = 0
        self._denied = 0
        self._timeouts = 0
        self._preempted = 0
        self._unavailable = 0
        self._busy = 0
        # A queued connection is what makes this safe: the slot always runs on the
        # GUI thread even though the signal is emitted from the action thread.
        self.requested.connect(self._on_requested, Qt.ConnectionType.QueuedConnection)
        if event_bus is not None:
            self.attach(event_bus)

    def attach(self, event_bus: EventBus) -> tuple[Subscription, ...]:
        """Refuse a pending request when a stop or takeover preempts it.

        Sections 62/63: a stop or a takeover while the dialog is open is the human
        overriding the question, so the answer is no. Attached after construction
        because the bus belongs to the assembled Body, which needs this bridge's
        callback at *its* construction time.
        """
        added: list[Subscription] = []
        for event_type in (EventType.EMERGENCY_STOP, EventType.HUMAN_TAKEOVER):
            subscription = event_bus.subscribe(
                self._on_preemption_event, event_type=event_type
            )
            self._subscriptions.append(subscription)
            added.append(subscription)
        return tuple(added)

    # -- The callback the executor is given ------------------------------------

    def request(self, message: str, details: dict[str, Any] | None = None) -> bool:
        """Ask a human, blocking until they answer or the request is refused.

        Returns:
            ``True`` only when a human confirmed after the countdown. ``False``
            for every other outcome (no GUI, timeout, a second concurrent
            request, a stop, a broken dialog).
        """
        request = ConfirmationRequest.create(message, details)
        if QApplication.instance() is None:
            # No GUI thread exists, so nobody can answer. Refuse rather than
            # block for the timeout or assume an answer (sections 54, 71).
            with self._lock:
                self._unavailable += 1
            return False
        pending = _Pending(request=request)
        with self._lock:
            if self._pending is not None:
                # One dialog at a time: the executor is serial, so a second
                # request means something unexpected is happening. Fail closed.
                self._busy += 1
                return False
            self._pending = pending
        try:
            self.requested.emit(request)
            answered = pending.answered.wait(self._timeout)
        finally:
            with self._lock:
                self._pending = None
        if not answered:
            with self._lock:
                self._timeouts += 1
            return False
        with self._lock:
            if pending.approved:
                self._granted += 1
            else:
                self._denied += 1
        return bool(pending.approved)

    # -- GUI-thread slot -------------------------------------------------------

    def _on_requested(self, request: ConfirmationRequest) -> None:
        """Show the dialog on the GUI thread and record the human's answer."""
        with self._lock:
            pending = self._pending
        if pending is None or pending.request.request_id != request.request_id:
            # Withdrawn (a stop, a timeout) before it could be shown: never show a
            # dialog for a request nobody is waiting on any more.
            return
        dialog = self._factory(request)
        pending.dialog = dialog
        approved = False
        try:
            dialog.exec()
            approved = bool(getattr(dialog, "approved", False))
        except Exception:  # a broken dialog denies, never grants
            approved = False
        finally:
            pending.dialog = None
            # Teardown must not raise, and must not mask the answer we recorded.
            with contextlib.suppress(Exception):
                dialog.deleteLater()
        pending.approved = approved
        pending.answered.set()

    # -- Preemption (sections 62, 63) ------------------------------------------

    def _on_preemption_event(self, event: Event) -> None:
        """Refuse the outstanding request because the human just overrode it."""
        self.deny_pending(event.event_type.value)

    def deny_pending(self, reason: str) -> int:
        """Refuse the outstanding request, if any. Returns how many it refused.

        Safe to call from any thread; the dialog itself is dismissed through the
        GUI thread's event loop (a modal ``exec`` is running its own loop, so the
        deferred call is delivered).
        """
        with self._lock:
            pending = self._pending
            if pending is None or pending.answered.is_set():
                return 0
            self._preempted += 1
            dialog = pending.dialog
            pending.approved = False
            pending.answered.set()
        if dialog is not None:
            QTimer.singleShot(0, dialog.reject)
        return 1

    # -- Observability ---------------------------------------------------------

    @property
    def pending(self) -> bool:
        """Whether a request is currently awaiting an answer."""
        with self._lock:
            return self._pending is not None

    def stats(self) -> dict[str, int]:
        """Counters for the action log: what was asked and how it was answered."""
        with self._lock:
            return {
                "granted": self._granted,
                "denied": self._denied,
                "timeouts": self._timeouts,
                "preempted": self._preempted,
                "unavailable": self._unavailable,
                "busy": self._busy,
            }

    def close(self) -> None:
        """Refuse anything pending and stop listening for preemption events."""
        self.deny_pending("bridge closed")
        for subscription in self._subscriptions:
            subscription.cancel()
        self._subscriptions.clear()


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "MIN_COUNTDOWN_SECONDS",
    "ConfirmationBridge",
    "ConfirmationDialog",
    "ConfirmationRequest",
    "DialogFactory",
]
