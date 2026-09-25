"""The BLAXCY window (specification sections 56, 62, 63, 71).

A single window over the *assembled* Body. It observes and it requests; it never
decides. Every control calls the same entry points the Brain does --
``set_mode``, ``trigger_emergency_stop``, ``reset_emergency_stop``,
``begin_takeover``, ``resume_takeover`` -- so an operator's click travels
policy -> resolve -> lease -> revalidate -> execute -> verify exactly like a Brain
tool call. There is no second decision path, and nothing here injects input.

Three rules from section 71 are load-bearing and visible in the code:

* **Report what the Body did, not what was asked.** Mode changes re-read
  ``status()`` afterwards, so a refused or force-latched mode is what shows.
* **Never invent an action.** The window has no "click here" affordance; the only
  actions it can start are the safety and mode controls below.
* **Only the GUI thread touches widgets.** The log and the confirmation dialog
  marshal their work through queued signals (see ``gui.action_log`` and
  ``gui.confirmation``).

**Self-exclusion.** The window title and the Qt application name both carry
:data:`SELF_EXCLUSION_TOKEN`; :func:`self_excluded_settings` in ``gui.app`` adds
that token to the policy's blocked-application list, so a request to act on
BLAXCY's own window is refused rather than clicked. The token is applied by code
in the composition root, so a user config cannot remove the guarantee.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Final

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from gui.action_log import ActionLog
from gui.capability_panel import CapabilityPanel
from gui.emergency_stop_ui import EmergencyStopUI
from gui.status_panel import StatusPanel
from schemas.enums import PolicyMode

#: Human-readable window title.
BLAXCY_WINDOW_TITLE: Final[str] = "BLAXCY Control"

#: Section 71 self-exclusion: a token present in this window's title *and* its Qt
#: application name, added to the policy's blocked-application list by
#: ``gui.app.self_excluded_settings``. Matching is case-folded substring, so both
#: the title and the window class match it.
SELF_EXCLUSION_TOKEN: Final[str] = "BLAXCY Control"

#: How often the window re-reads the Body's status. Fast enough to watch a
#: sequence progress, slow enough not to compete with the capture engine.
DEFAULT_REFRESH_MS: Final[int] = 500


class MainWindow(QMainWindow):
    """The section 71 window: a view over ``BlaxcyApplication``."""

    def __init__(
        self,
        application: Any,
        *,
        bridge: Any = None,
        refresh_ms: int = DEFAULT_REFRESH_MS,
        clock: Callable[[], float] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._app = application
        self._bridge = bridge
        self._clock = clock
        self._last_report: dict[str, Any] | None = None

        session = getattr(getattr(application, "session", None), "session_type", None)
        session_name = getattr(session, "value", None) or "unknown"
        self.setWindowTitle(f"{BLAXCY_WINDOW_TITLE} - {session_name}")

        central = QWidget(self)
        layout = QVBoxLayout(central)

        self.header = QLabel(BLAXCY_WINDOW_TITLE, central)
        self.header.setObjectName("header")
        layout.addWidget(self.header)

        self.connection = QLabel("connection: UNKNOWN", central)
        self.connection.setObjectName("connection_state")
        layout.addWidget(self.connection)

        layout.addWidget(self._build_controls(central))

        self.status_panel = StatusPanel(central)
        tabs = QTabWidget(central)
        tabs.addTab(self.status_panel, "Status")
        self.capability_panel = CapabilityPanel(central)
        tabs.addTab(self.capability_panel, "Capabilities")
        self.action_log = ActionLog(parent=central)
        tabs.addTab(self.action_log, "Action log")
        layout.addWidget(tabs)
        self.tabs = tabs

        self.setCentralWidget(central)

        # The log observes the one bus; no second delivery path (section 65).
        bus = getattr(application, "bus", None)
        if bus is not None:
            self.action_log.attach(bus)

        self._timer = QTimer(self)
        self._timer.setInterval(int(refresh_ms))
        self._timer.timeout.connect(self.refresh)
        self.refresh()

    # -- Construction helpers --------------------------------------------------

    def _build_controls(self, parent: QWidget) -> QWidget:
        """The mode, takeover and stop controls (the only actions available)."""
        box = QGroupBox("Control", parent)
        layout = QVBoxLayout(box)

        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("mode:", box))
        self.mode_selector = QComboBox(box)
        self.mode_selector.setObjectName("mode_selector")
        for mode in PolicyMode:
            # The item data is the mode *value* (a plain string) so that
            # ``findData`` against a status report -- which carries the same
            # string -- is an exact comparison rather than a variant guess.
            self.mode_selector.addItem(mode.value, mode.value)
        self.mode_selector.currentIndexChanged.connect(self._on_mode_selected)
        mode_row.addWidget(self.mode_selector)
        self.mode_note = QLabel("", box)
        self.mode_note.setObjectName("mode_note")
        mode_row.addWidget(self.mode_note, 1)
        layout.addLayout(mode_row)

        takeover_row = QHBoxLayout()
        self.takeover_button = QPushButton("Human takeover", box)
        self.takeover_button.setObjectName("takeover_button")
        self.resume_button = QPushButton("Resume automation", box)
        self.resume_button.setObjectName("resume_button")
        self.resume_button.setEnabled(False)
        self.takeover_button.clicked.connect(self._on_takeover)
        self.resume_button.clicked.connect(self._on_resume)
        takeover_row.addWidget(self.takeover_button)
        takeover_row.addWidget(self.resume_button)
        takeover_row.addStretch(1)
        layout.addLayout(takeover_row)

        self.stop_ui = EmergencyStopUI(
            on_stop=self._app.trigger_emergency_stop,
            on_reset=self._app.reset_emergency_stop,
            parent=box,
        )
        layout.addWidget(self.stop_ui)
        return box

    # -- Lifecycle -------------------------------------------------------------

    def start_refresh(self) -> None:
        """Begin polling the Body's status."""
        self._timer.start()

    def stop_refresh(self) -> None:
        """Stop polling (called on close, so no timer outlives the window)."""
        self._timer.stop()

    def closeEvent(self, event: Any) -> None:  # noqa: N802 - Qt's name
        """Stop the timer and detach from the bus before the window goes away."""
        self.stop_refresh()
        self.action_log.detach()
        super().closeEvent(event)

    # -- Refresh ---------------------------------------------------------------

    def refresh(self) -> dict[str, Any] | None:
        """Re-read the Body's status and render it. Returns the report."""
        try:
            report: dict[str, Any] = self._app.status()
        except Exception as exc:  # a broken status read must be visible
            self.connection.setText(f"connection: status read failed: {exc!r}")
            return None
        self._last_report = report
        self._render(report)
        return report

    def _render(self, report: dict[str, Any]) -> None:
        """Push one report into every panel, including the honest failures."""
        started = report.get("started")
        brain = report.get("brain") or {}
        connection = (
            f"connection: {'started' if started else 'NOT started'} | "
            f"session {report.get('session_type', 'UNKNOWN')} | "
            f"brain {brain.get('adapter') or brain.get('reason') or 'not connected'}"
        )
        self.connection.setText(connection)

        self.status_panel.update_status(report)
        self.capability_panel.set_report(getattr(self._app, "capabilities", None))

        mode = report.get("mode")
        self._sync_mode_selector(mode)
        mode_status = report.get("mode_status") or {}
        note = ""
        if mode_status.get("forced"):
            note = f"forced: {mode_status.get('reason') or 'forced by the Body'}"
        remaining = mode_status.get("autonomous_expires_in_seconds")
        if isinstance(remaining, (int, float)):
            note = f"autonomous expires in {remaining:.0f}s"
        self.mode_note.setText(note)

        stop = report.get("emergency_stop") or {}
        self.stop_ui.update_state(
            latched=bool(stop.get("latched")),
            last_latency_ms=stop.get("last_latency_ms"),
            triggers=stop.get("triggers"),
        )

        takeover = report.get("takeover") or {}
        active = bool(takeover.get("active"))
        self.resume_button.setEnabled(active)
        self.takeover_button.setEnabled(not active)

    def _sync_mode_selector(self, mode: Any) -> None:
        """Show the Body's actual mode without re-triggering a mode change."""
        if mode is None:
            return
        index = self.mode_selector.findData(mode)
        if index < 0:
            index = self.mode_selector.findText(str(mode))
        if index < 0 or index == self.mode_selector.currentIndex():
            return
        blocked = self.mode_selector.blockSignals(True)
        self.mode_selector.setCurrentIndex(index)
        self.mode_selector.blockSignals(blocked)

    # -- Control handlers ------------------------------------------------------

    def _on_mode_selected(self, index: int) -> None:
        """Request a mode change, then show what the Body actually did."""
        data = self.mode_selector.itemData(index)
        if data is None:
            return
        try:
            mode = PolicyMode(data)
        except ValueError:
            self.mode_note.setText(f"unknown mode requested: {data!r}")
            return
        try:
            self._app.set_mode(mode, reason="requested from the GUI")
        except Exception as exc:
            self.mode_note.setText(f"mode change failed: {exc!r}")
        # Re-read: a refusal or a forced OBSERVE must be what the selector shows.
        self.refresh()

    def _on_takeover(self) -> None:
        """Hand the desktop back to the human (section 62)."""
        try:
            self._app.begin_takeover("human took control from the GUI")
        except Exception as exc:
            self.connection.setText(f"connection: takeover failed: {exc!r}")
        self.refresh()

    def _on_resume(self) -> None:
        """Resume explicitly; the Body invalidates leases and re-perceives."""
        try:
            self._app.resume_takeover()
        except Exception as exc:
            self.connection.setText(f"connection: resume failed: {exc!r}")
        self.refresh()

    # -- Inspection ------------------------------------------------------------

    @property
    def last_report(self) -> dict[str, Any] | None:
        """The most recent status report rendered (used by tests)."""
        return self._last_report

    def render_status(self, report: dict[str, Any] | None) -> None:
        """Render an explicit report (test/offline use)."""
        if report is None:
            return
        self._last_report = report
        self._render(report)


__all__ = [
    "BLAXCY_WINDOW_TITLE",
    "DEFAULT_REFRESH_MS",
    "SELF_EXCLUSION_TOKEN",
    "MainWindow",
]
