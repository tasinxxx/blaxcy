"""Emergency stop UI (specification sections 63, 64, 71).

One button, one shortcut, one explicit re-arm. The button does not "stop" anything
itself: it calls the *wired* Body's stop (``BlaxcyApplication.trigger_emergency_stop``
-> ``control/emergency_stop.py``), which latches, terminates execution, releases
every tracked key and button, cancels the loops and forces OBSERVE.

The UI then reports **what the stop actually did** -- the measured latency, how
many keys and buttons it released, and any step that failed -- because a button
that simply displayed "stopped because I was clicked" would be fake status
(section 4 rule 8). If the stop reports a failed step, that is shown.

Re-arming is deliberately a separate, explicit control: a stop that cleared itself
would not be a stop (section 63).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

#: The stop shortcut. An application-wide key so it works wherever focus is.
STOP_SHORTCUT: str = "Ctrl+Shift+Escape"


def describe_stop(result: Any) -> str:
    """Render an ``EmergencyStopResult`` honestly, including its failures."""
    latched = bool(getattr(result, "latched", False))
    latency = getattr(result, "latency_ms", None)
    buttons = tuple(getattr(result, "released_buttons", ()) or ())
    keys = tuple(getattr(result, "released_keys", ()) or ())
    errors = tuple(getattr(result, "errors", ()) or ())
    already = bool(getattr(result, "already_latched", False))
    forced = bool(getattr(result, "forced_observe", False))

    if not latched:
        return "STOP DID NOT LATCH - the Body is not stopped"
    verb = "already latched" if already else "latched"
    parts = [
        f"stop {verb}",
        f"latency {latency:.2f} ms" if isinstance(latency, (int, float)) else "latency UNKNOWN",
        f"released {len(buttons)} buttons",
        f"{len(keys)} keys",
        f"OBSERVE {'forced' if forced else 'NOT forced'}",
    ]
    text = ", ".join(parts)
    if errors:
        text += f" | FAILED STEPS: {'; '.join(str(e) for e in errors)}"
    return text


class EmergencyStopUI(QWidget):
    """Section 63's stop control and its honest result read-out."""

    def __init__(
        self,
        *,
        on_stop: Callable[[str], Any],
        on_reset: Callable[[], bool],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._on_stop = on_stop
        self._on_reset = on_reset
        self._latched = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        row = QHBoxLayout()
        self.stop_button = QPushButton("EMERGENCY STOP", self)
        self.stop_button.setObjectName("emergency_stop_button")
        self.stop_button.setToolTip(f"Stop immediately ({STOP_SHORTCUT})")
        self.reset_button = QPushButton("Re-arm (clear stop)", self)
        self.reset_button.setObjectName("emergency_reset_button")
        self.reset_button.setEnabled(False)
        row.addWidget(self.stop_button)
        row.addWidget(self.reset_button)
        layout.addLayout(row)

        self._status = QLabel("not latched", self)
        self._status.setObjectName("emergency_stop_status")
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

        self.safety_notice = QLabel(
            "Stopping releases every key and button BLAXCY holds.", self
        )
        self.safety_notice.setWordWrap(True)
        layout.addWidget(self.safety_notice)

        self.stop_button.clicked.connect(self.trigger)
        self.reset_button.clicked.connect(self.reset)

        self.stop_shortcut = QShortcut(QKeySequence(STOP_SHORTCUT), self)
        self.stop_shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
        self.stop_shortcut.activated.connect(self.trigger)

    # -- Actions ---------------------------------------------------------------

    def trigger(self) -> Any:
        """Run the real stop and display what it returned."""
        try:
            result = self._on_stop("user request from the GUI")
        except Exception as exc:  # a stop that raised must be visible, not silent
            self._status.setText(f"stop raised: {exc!r}")
            return None
        self._status.setText(describe_stop(result))
        self.update_state(
            latched=bool(getattr(result, "latched", False)),
            last_latency_ms=getattr(result, "latency_ms", None),
        )
        return result

    def reset(self) -> bool:
        """Clear the latch, explicitly, and report whether it was cleared."""
        try:
            cleared = bool(self._on_reset())
        except Exception as exc:
            self._status.setText(f"re-arm raised: {exc!r}")
            return False
        if cleared:
            self._status.setText("re-armed: the stop latch is clear")
            self.update_state(latched=False, last_latency_ms=None)
        else:
            self._status.setText("re-arm returned False: the stop is still latched")
        return cleared

    # -- State -----------------------------------------------------------------

    def update_state(
        self,
        *,
        latched: bool,
        last_latency_ms: float | None = None,
        triggers: int | None = None,
    ) -> None:
        """Reflect the Body's real latch state (never the button's own memory)."""
        self._latched = latched
        self.reset_button.setEnabled(latched)
        self.stop_button.setEnabled(not latched)
        if latched:
            text = "LATCHED - automation is stopped and OBSERVE is forced"
            if isinstance(last_latency_ms, (int, float)):
                text += f" (last stop {last_latency_ms:.2f} ms)"
            if triggers is not None:
                text += f", triggers: {triggers}"
            self._status.setText(text)
        elif not self._status.text().startswith(("re-armed", "stop raised")):
            self._status.setText("not latched")

    @property
    def latched(self) -> bool:
        """The latch state this control last observed."""
        return self._latched

    def status_text(self) -> str:
        """The rendered result text (used by tests)."""
        return self._status.text()


__all__ = ["STOP_SHORTCUT", "EmergencyStopUI", "describe_stop"]
