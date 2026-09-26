"""BLAXCY test fixture application (specification section 74).

A real Qt application exposing every control the fixture spec requires:

    Button Alpha, Button Beta, duplicate Alpha buttons, a text input, a password
    input, a menu, a dialog, a canvas, an icon-only control, a movable target, a
    destroyed target, an occluding window, focus changes, and state-changing
    controls -- plus the fixed 5-control workflow fixture (search icon -> text
    input -> submit -> result list -> play-like button).

It is driven over a line-delimited JSON control channel on stdin/stdout so a
test (or a later BLAXCY phase) can change state deterministically without
relying on synthetic input. This file is a *fixture*: it is never shipped with
the product and it never contacts the network.

Protocol
--------
On startup the app prints one line::

    {"event": "ready", "title": ..., "inventory": [...]}

Thereafter every command line receives exactly one response line::

    {"event": "ack",   "cmd": ..., ...}      # success
    {"event": "error", "cmd": ..., "message": ...}

Commands: ping, inventory, stats, set_text, click, toggle, move, destroy,
set_occluder, set_focus, open_dialog, quit.

Launch directly by path so no package installation is required::

    python tests/fixtures/fixture_app.py --offscreen
"""

from __future__ import annotations

import argparse
import json
import os
import select
import sys
from typing import Any

from PySide6.QtCore import QPoint, QSize, Qt, QTimer
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPaintEvent, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

#: Accessible name shared by the duplicate controls, so the resolver must treat
#: them as AMBIGUOUS (specification section 43).
DUPLICATE_ALPHA_NAME = "Button Alpha"

#: The five workflow controls, in the order the run_sequence fixture expects.
WORKFLOW_ORDER = ("search_icon", "search_input", "submit_button", "result_list", "play_button")

#: Section 76 benchmark controls: laid out 2 columns x 3 rows so the whole set fits
#: well inside any window position. Each is large enough that a repaint spans at
#: least five thumbnail tiles wide and three tall (the section 34 ``MEANINGFUL``
#: thresholds on a 1366x768 desktop), and the controls are separated by more than a
#: tile so two controls' changes never merge into one component -- which the
#: temporal layer would then reclassify ANIMATION.
BENCH_CONTROL_WIDTH = 240
BENCH_CONTROL_HEIGHT = 80
BENCH_CONTROL_GAP = 50
BENCH_CONTROL_HGAP = 20

#: Section 74/76 workflow-controls layout: the same five-control pipeline laid out
#: at the benchmark scale, so each control's own repaint clears the section 34
#: ``MEANINGFUL`` thresholds and a real click on it is verifiable (section 60)
#: rather than merely ``TRIVIAL``. Sized and spaced like the benchmark controls for
#: the same reason -- a change smaller than a thumbnail tile establishes nothing.
WORKFLOW_CONTROL_WIDTH = 240
WORKFLOW_CONTROL_HEIGHT = 80
WORKFLOW_CONTROL_VGAP = 36

#: The accessible name the workflow layout gives its search field. It must read as
#: a *navigation* field (section 36) because only such a field's editable content
#: may be read (sections 42, 55); without that, a typed step is honestly
#: ``UNVERIFIED`` and the workflow can never complete.
WORKFLOW_SEARCH_FIELD_NAME = "Search Address Bar"


class CanvasWidget(QWidget):
    """A non-trivial painted surface: present, visible and intentionally not a label."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("canvas")
        self.setAccessibleName("Canvas")
        self.setFixedSize(160, 90)

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 - Qt override
        """Paint a deterministic pattern so change detection has real signal."""
        painter = QPainter(self)
        try:
            painter.fillRect(self.rect(), QColor(32, 32, 48))
            painter.setPen(QColor(200, 200, 240))
            painter.drawRect(self.rect().adjusted(1, 1, -2, -2))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "CANVAS")
        finally:
            painter.end()


def _make_icon() -> QIcon:
    """Build an icon in code so the fixture needs no external asset."""
    pixmap = QPixmap(24, 24)
    pixmap.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pixmap)
    try:
        painter.setBrush(QColor(120, 90, 220))
        painter.setPen(QColor(240, 240, 255))
        painter.drawEllipse(2, 2, 20, 20)
    finally:
        painter.end()
    return QIcon(pixmap)


class FixtureWindow(QMainWindow):
    """The fixture window: widget inventory, counters and command operations."""

    def __init__(
        self, title: str, *, bench_controls: bool = False, workflow_controls: bool = False
    ) -> None:
        super().__init__()
        self.setWindowTitle(title)
        self._bench_controls = bench_controls
        self._workflow_controls = workflow_controls
        #: Per-control toggle state for the benchmark controls (specification
        #: section 76). Kept separate from ``_counters`` so a benchmark can prove a
        #: real click actually changed the control rather than only bumping a count.
        self._bench_state: dict[int, bool] = {}
        self._bench_buttons: list[QPushButton] = []
        #: Per-control highlight state for the workflow-controls layout, kept out of
        #: ``_counters`` so a benchmark can prove the control really repainted.
        self._highlight_state: dict[str, bool] = {}
        self.setObjectName("fixture_window")

        self._counters: dict[str, int] = {}
        self._destroyed: list[str] = []
        self._dialog: QDialog | None = None
        self._focus_object_name: str | None = None

        # Track Qt's own focus changes. This stays truthful on the offscreen
        # platform, where a window may never become "active" but focus is still
        # assigned and the focusChanged signal still fires.
        app = QApplication.instance()
        if isinstance(app, QApplication):
            app.focusChanged.connect(self._on_focus_changed)

        root = QWidget()
        root.setObjectName("fixture_root")
        layout = QVBoxLayout(root)

        layout.addWidget(QLabel("BLAXCY Test Fixture"))

        # --- duplicate-Ambiguity cluster -------------------------------------
        self.btn_alpha = self._push_button("btn_alpha", "Button Alpha")
        self.btn_alpha_dup_1 = self._push_button("btn_alpha_dup_1", DUPLICATE_ALPHA_NAME)
        self.btn_alpha_dup_2 = self._push_button("btn_alpha_dup_2", DUPLICATE_ALPHA_NAME)
        self.btn_beta = self._push_button("btn_beta", "Button Beta")

        # --- text inputs ------------------------------------------------------
        self.text_input = QLineEdit()
        self.text_input.setObjectName("text_input")
        self.text_input.setAccessibleName("Text Input")
        self.text_input.setPlaceholderText("type here")
        layout.addWidget(self.text_input)

        self.password_input = QLineEdit()
        self.password_input.setObjectName("password_input")
        self.password_input.setAccessibleName("Password Input")
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addWidget(self.password_input)

        # --- icon-only control ------------------------------------------------
        self.icon_only = QPushButton()
        self.icon_only.setObjectName("icon_only")
        self.icon_only.setAccessibleName("Icon Only Control")
        self.icon_only.setIcon(_make_icon())
        self.icon_only.setText("")
        self.icon_only.setFixedSize(36, 36)

        # --- movable / destroyed targets -------------------------------------
        self.movable = self._push_button("movable", "Movable Target")
        self.destroyed_target = self._push_button("destroyed_target", "Destroyed Target")

        # --- canvas -----------------------------------------------------------
        self.canvas = CanvasWidget()

        # --- state-changing controls -----------------------------------------
        self.state_checkbox = QCheckBox("Toggle State")
        self.state_checkbox.setObjectName("state_checkbox")
        self.state_checkbox.setAccessibleName("Toggle State")
        self.state_checkbox.stateChanged.connect(lambda _state: self._bump("state_checkbox"))

        # --- section 74 workflow fixture (search -> type -> submit -> list -> play)
        self.search_icon = QPushButton()
        self.search_icon.setObjectName("search_icon")
        self.search_icon.setAccessibleName("Search")
        self.search_icon.setIcon(_make_icon())
        self.search_icon.setText("")
        self.search_icon.setFixedSize(36, 36)
        self.search_icon.clicked.connect(self._on_search_clicked)

        self.search_input = QLineEdit()
        self.search_input.setObjectName("search_input")
        self.search_input.setAccessibleName("Search Box")

        self.submit_button = self._push_button("submit_button", "Submit")
        self.submit_button.clicked.connect(self._on_submit_clicked)

        self.result_list = QListWidget()
        self.result_list.setObjectName("result_list")
        self.result_list.setAccessibleName("Results")
        self.result_list.setFixedHeight(72)

        self.play_button = self._push_button("play_button", "Play Button")
        self.play_button.setEnabled(False)

        # --- assembling the layout -------------------------------------------
        widgets: list[QWidget] = [
            self.btn_alpha,
            self.btn_alpha_dup_1,
            self.btn_alpha_dup_2,
            self.btn_beta,
            self.text_input,
            self.password_input,
            self.icon_only,
            self.movable,
            self.destroyed_target,
            self.canvas,
            self.state_checkbox,
            QLabel("— workflow —"),
            self.search_icon,
            self.search_input,
            self.submit_button,
            self.result_list,
            self.play_button,
        ]
        if bench_controls:
            # Section 76 benchmark workload: a dedicated layout of large controls
            # that repaint at their *own* region when clicked, so a real click
            # produces a MEANINGFUL change overlapping the target and can actually
            # be verified. The ordinary controls are excluded from this layout (and
            # kept parented but hidden, so none becomes a stray top-level window);
            # if they were included the column would over-constrain the window and
            # Qt would compress the gaps, merging two controls' changes into one
            # component that the temporal layer then reclassifies ANIMATION. Only
            # built when the caller asks, so no existing fixture test sees them.
            self._bench_buttons = [self._bench_button(index) for index in range(1, 6)]
            grid = QGridLayout()
            grid.setHorizontalSpacing(BENCH_CONTROL_HGAP)
            grid.setVerticalSpacing(BENCH_CONTROL_GAP)
            for index, button in enumerate(self._bench_buttons):
                grid.addWidget(button, index // 2, index % 2)
            layout.addLayout(grid)
            layout.addStretch(1)
            hidden = QWidget(root)
            hidden.hide()
            for widget in widgets:
                widget.setParent(hidden)
                widget.hide()
        elif workflow_controls:
            # Section 74/76 workflow workload: the same five-control pipeline, at a
            # scale where every step's own repaint is verifiable. Like the benchmark
            # layout, the ordinary controls stay parented but hidden so none of them
            # becomes a stray top-level window.
            self._configure_workflow_controls()
            workflow_widgets = (
                self.search_icon,
                self.search_input,
                self.submit_button,
                self.result_list,
                self.play_button,
            )
            grid = QGridLayout()
            grid.setHorizontalSpacing(20)
            grid.setVerticalSpacing(WORKFLOW_CONTROL_VGAP)
            for row, widget in enumerate(workflow_widgets):
                grid.addWidget(widget, row, 0)
            layout.addLayout(grid)
            layout.addStretch(1)
            hidden = QWidget(root)
            hidden.hide()
            for widget in widgets:
                if widget in workflow_widgets:
                    continue
                widget.setParent(hidden)
                widget.hide()
        else:
            for widget in widgets:
                layout.addWidget(widget, alignment=Qt.AlignmentFlag.AlignLeft)
        self.setCentralWidget(root)

        # --- menu -------------------------------------------------------------
        menu_bar = self.menuBar()
        if menu_bar is not None:
            menu = menu_bar.addMenu("File")
            menu.setObjectName("menu_file")
            new_action = QAction("New", self)
            new_action.setObjectName("menu_new")
            menu.addAction(new_action)
            quit_action = QAction("Quit", self)
            quit_action.setObjectName("menu_quit")
            menu.addAction(quit_action)

        # --- occluding window (hidden until asked for) ------------------------
        self._occluder = QWidget(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool)
        self._occluder.setObjectName("occluder")
        self._occluder.setAccessibleName("Occluder")
        self._occluder.setStyleSheet("background: rgba(20,20,20,235);")
        self._occluder.resize(240, 140)

        self.resize(520, 760)

    # -- construction helpers -------------------------------------------------

    def _push_button(self, object_name: str, accessible_name: str) -> QPushButton:
        """Create a button with a stable object name and accessible name."""
        button = QPushButton(accessible_name)
        button.setObjectName(object_name)
        button.setAccessibleName(accessible_name)
        button.clicked.connect(lambda _checked=False, name=object_name: self._bump(name))
        return button

    def _bump(self, name: str) -> None:
        """Record a state change for a named control."""
        self._counters[name] = self._counters.get(name, 0) + 1

    def _bench_button(self, index: int) -> QPushButton:
        """A large control that toggles its own background when clicked (section 76).

        Sized so its repaint spans at least five thumbnail tiles wide and three
        tall (the section 34 ``MEANINGFUL`` thresholds), and confined to its own
        box so the change also *overlaps the target* -- which is what lets section
        60 verification succeed for a real click on a real desktop.
        """
        button = QPushButton(f"Bench Target {index}")
        button.setObjectName(f"bench_target_{index}")
        button.setAccessibleName(f"Bench Target {index}")
        button.setFixedSize(BENCH_CONTROL_WIDTH, BENCH_CONTROL_HEIGHT)
        # Non-focusable on purpose: a focus ring toggling on another control as
        # focus moves would add a *second* changed region near the target and make
        # the temporal layer treat the neighbourhood as animation.
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.setStyleSheet(self._bench_style(False))
        button.clicked.connect(lambda _checked=False, i=index: self._toggle_bench(i))
        self._bench_state[index] = False
        return button

    def _bench_style(self, on: bool) -> str:
        """Two high-contrast styles, so a toggle is a large unambiguous change."""
        if on:
            return "background-color: #f5f5f5; color: #101010; font-size: 20px;"
        return "background-color: #101418; color: #e0e0e0; font-size: 20px;"

    def _toggle_bench(self, index: int) -> None:
        """Flip one benchmark control's state and record the click."""
        self._bench_state[index] = not self._bench_state.get(index, False)
        self._bump(f"bench_target_{index}")
        widget = self.find_control(f"bench_target_{index}")
        if isinstance(widget, QPushButton):
            widget.setStyleSheet(self._bench_style(self._bench_state[index]))

    def _configure_workflow_controls(self) -> None:
        """Scale the five workflow controls so each step of the pipeline verifies.

        Two things are changed from the ordinary layout, and both are necessary:
        the clickable controls are made large and self-repainting (section 34's
        ``MEANINGFUL`` thresholds decide whether section 60 can verify a click),
        and the search field is renamed as a navigation field (section 36), since
        only such a field's editable content may be read (sections 42, 55).
        """
        self.search_input.setAccessibleName(WORKFLOW_SEARCH_FIELD_NAME)
        for button in (self.search_icon, self.submit_button, self.play_button):
            self._make_self_verifying(button)
        self.search_input.setFixedSize(WORKFLOW_CONTROL_WIDTH, WORKFLOW_CONTROL_HEIGHT)
        self.result_list.setFixedSize(WORKFLOW_CONTROL_WIDTH, WORKFLOW_CONTROL_HEIGHT * 2)
        # Selecting a row is a real, user-visible change in any application, but
        # Qt's default highlight is theme-dependent and can land below the section
        # 34 ``MEANINGFUL`` thresholds on this host -- which makes a section 60
        # verdict on the result click honestly ``UNVERIFIED`` rather than wrong.
        # Both states are pinned so the row repaints unambiguously at *its own*
        # box, which is what makes this workload test the pipeline (resolve -> lease
        # -> revalidate -> click -> verify) instead of testing the desktop theme.
        self.result_list.setStyleSheet(
            "QListWidget::item { background-color: #e8e8e8; color: #101010; }"
            "QListWidget::item:selected { background-color: #103a6b; color: #ffffff; }"
        )

    def _make_self_verifying(self, button: QPushButton) -> None:
        """Size a control so its own repaint is ``MEANINGFUL``, and make it toggle.

        Non-focusable on purpose: a focus ring appearing on a nearby control would
        add a second changed region beside the target and make the neighbourhood
        look like animation.
        """
        button.setFixedSize(WORKFLOW_CONTROL_WIDTH, WORKFLOW_CONTROL_HEIGHT)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.setStyleSheet(self._bench_style(False))
        self._highlight_state[button.objectName()] = False
        button.clicked.connect(lambda _checked=False, b=button: self._toggle_highlight(b))

    def _toggle_highlight(self, button: QPushButton) -> None:
        """Flip a self-verifying control's background so its repaint is visible."""
        name = button.objectName()
        self._highlight_state[name] = not self._highlight_state.get(name, False)
        button.setStyleSheet(self._bench_style(self._highlight_state[name]))

    def _on_focus_changed(self, _old: QWidget | None, now: QWidget | None) -> None:
        """Record the object name of the newly focused widget, if any."""
        if now is None:
            self._focus_object_name = None
        else:
            self._focus_object_name = now.objectName() or None

    def _on_search_clicked(self) -> None:
        self._bump("search_icon")
        self.search_input.setFocus()

    def _on_submit_clicked(self) -> None:
        self._bump("submit_button")
        self.result_list.clear()
        query = self.search_input.text() or "(empty)"
        for index in range(1, 4):
            item = QListWidgetItem(f"Result {index} for {query}")
            if self._workflow_controls:
                # A stable exact name (the plan targets it by description) and a
                # rect large enough that selecting it repaints MEANINGFUL-ly at
                # its own box, so a click on the result can be verified.
                item.setText(f"Result {index}")
                item.setSizeHint(QSize(WORKFLOW_CONTROL_WIDTH, WORKFLOW_CONTROL_HEIGHT))
            self.result_list.addItem(item)
        self.play_button.setEnabled(True)

    # -- lookup ---------------------------------------------------------------

    def find_control(self, object_name: str) -> QWidget | None:
        """Find a control by object name, including the occluder and dialog.

        Named ``find_control`` rather than ``find`` because ``QWidget`` already
        defines ``find(int)`` with different semantics.
        """
        if object_name == "occluder":
            return self._occluder
        for widget in self.findChildren(QWidget):
            if widget.objectName() == object_name:
                return widget
        if self._dialog is not None and self._dialog.objectName() == object_name:
            return self._dialog
        return None

    # -- operations -----------------------------------------------------------

    def inventory(self) -> list[dict[str, Any]]:
        """Describe every control the fixture exposes (state, not just existence)."""
        entries: list[dict[str, Any]] = []
        for widget in self.findChildren(QWidget):
            name = widget.objectName()
            if not name:
                continue
            entries.append(self._describe(widget, name))
        entries.append(self._describe(self._occluder, "occluder"))
        return entries

    def _describe(self, widget: QWidget, name: str) -> dict[str, Any]:
        """Build one inventory entry with local and global geometry."""
        geometry = widget.geometry()
        origin = widget.mapToGlobal(QPoint(0, 0))
        return {
            "object_name": name,
            "accessible_name": widget.accessibleName(),
            "class": type(widget).__name__,
            "password": (
                isinstance(widget, QLineEdit)
                and widget.echoMode() == QLineEdit.EchoMode.Password
            ),
            "visible": widget.isVisible(),
            "enabled": widget.isEnabled(),
            "local": {
                "x": geometry.x(),
                "y": geometry.y(),
                "width": geometry.width(),
                "height": geometry.height(),
            },
            "global": {"x": origin.x(), "y": origin.y()},
        }

    def stats(self) -> dict[str, Any]:
        """Report derived state: click counters, values, focus and destruction."""
        text_widget = self.find_control("text_input")
        search_widget = self.find_control("search_input")
        return {
            "counters": dict(self._counters),
            "text_input": text_widget.text() if isinstance(text_widget, QLineEdit) else None,
            "search_input": search_widget.text() if isinstance(search_widget, QLineEdit) else None,
            "result_count": self.result_list.count(),
            "play_enabled": self.play_button.isEnabled(),
            "checkbox_checked": self.state_checkbox.isChecked(),
            "occluder_visible": self._occluder.isVisible(),
            "workflow_present": all(
                self.find_control(name) is not None for name in WORKFLOW_ORDER
            ),
            "bench_targets": dict(self._bench_state),
            "destroyed": list(self._destroyed),
            "focused": self._focused_object_name(),
        }

    def _focused_object_name(self) -> str | None:
        """Return the object name of the focused widget *within this window*.

        Uses this window's own ``focusWidget()`` (the child that last received
        setFocus), which stays correct when another top-level window -- such as
        the fixture dialog -- holds active focus. Falls back to the value
        observed from Qt's ``focusChanged`` signal.
        """
        focused: QWidget | None = self.focusWidget()
        if focused is not None:
            return focused.objectName() or None
        return self._focus_object_name

    # -- command operations (each returns extra ack payload) ------------------

    def op_set_text(self, target: str, value: str) -> dict[str, Any]:
        widget = self.find_control(target)
        if not isinstance(widget, QLineEdit):
            raise FixtureCommandError(f"{target!r} is not a text input")
        widget.setText(value)
        return {"target": target, "value": value}

    def op_click(self, target: str) -> dict[str, Any]:
        widget = self.find_control(target)
        if not isinstance(widget, (QPushButton, QCheckBox)):
            raise FixtureCommandError(f"{target!r} is not clickable")
        widget.click()
        return {"target": target, "counters": dict(self._counters)}

    def op_toggle(self, target: str) -> dict[str, Any]:
        widget = self.find_control(target)
        if not isinstance(widget, QCheckBox):
            raise FixtureCommandError(f"{target!r} is not a checkbox")
        widget.toggle()
        return {"target": target, "checked": widget.isChecked()}

    def op_move(self, target: str, dx: int, dy: int) -> dict[str, Any]:
        widget = self.find_control(target)
        if widget is None:
            raise FixtureCommandError(f"no such target {target!r}")
        widget.move(widget.pos() + QPoint(dx, dy))
        return {"target": target, "pos": [widget.x(), widget.y()]}

    def op_destroy(self, target: str) -> dict[str, Any]:
        widget = self.find_control(target)
        if widget is None:
            raise FixtureCommandError(f"no such target {target!r}")
        widget.setParent(None)
        widget.deleteLater()
        self._destroyed.append(target)
        return {"target": target, "destroyed": list(self._destroyed)}

    def op_set_occluder(self, visible: bool) -> dict[str, Any]:
        if visible:
            anchor = self.movable.mapToGlobal(QPoint(0, 0))
            self._occluder.move(anchor - QPoint(40, 40))
            self._occluder.show()
            self._occluder.raise_()
        else:
            self._occluder.hide()
        return {"visible": self._occluder.isVisible()}

    def op_set_focus(self, target: str) -> dict[str, Any]:
        widget = self.find_control(target)
        if widget is None or not isinstance(widget, QWidget):
            raise FixtureCommandError(f"no such target {target!r}")
        self.activateWindow()
        widget.setFocus(Qt.FocusReason.OtherFocusReason)
        return {"target": target, "focused": self._focused_object_name()}

    def op_set_selection(self, target: str, index: int) -> dict[str, Any]:
        """Select a row of a list widget (no desktop input involved).

        Used to measure what a *result selection* repaints, so the section 76
        workflow's result step can be judged against a real screen change rather
        than against Qt's theme-dependent default highlight. If the row is already
        current it is cleared first, so the caller always observes a transition.
        """
        widget = self.find_control(target)
        if not isinstance(widget, QListWidget):
            raise FixtureCommandError(f"{target!r} is not a list")
        if widget.currentRow() == index:
            widget.setCurrentRow(-1)
            widget.clearSelection()
        widget.setCurrentRow(index)
        current = widget.currentItem()
        return {
            "target": target,
            "index": index,
            "selected": None if current is None else current.text(),
        }

    def op_open_dialog(self) -> dict[str, Any]:
        if self._dialog is None:
            dialog = QDialog(self)
            dialog.setObjectName("fixture_dialog")
            dialog.setAccessibleName("Fixture Dialog")
            dialog.setWindowTitle("Fixture Dialog")
            dialog_layout = QVBoxLayout(dialog)
            dialog_layout.addWidget(QLabel("Dialog body"))
            button = QPushButton("Dialog Button")
            button.setObjectName("dialog_button")
            button.setAccessibleName("Dialog Button")
            button.clicked.connect(lambda _checked=False: self._bump("dialog_button"))
            box = QDialogButtonBox()
            box.addButton(button, QDialogButtonBox.ButtonRole.AcceptRole)
            dialog_layout.addWidget(box)
            self._dialog = dialog
        self._dialog.show()
        self._dialog.raise_()
        return {"dialog_visible": self._dialog.isVisible()}

    def op_quit(self) -> dict[str, Any]:
        return {"quitting": True}


class FixtureCommandError(ValueError):
    """A control-channel command that could not be performed."""


class CommandPump:
    """Polls stdin and dispatches line-delimited JSON commands to the window."""

    def __init__(self, window: FixtureWindow, interval_ms: int = 15) -> None:
        self._window = window
        self._timer = QTimer()
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self._poll)

    def start(self) -> None:
        """Emit the ready line, then begin polling for commands."""
        self._emit("ready", title=self._window.windowTitle(), inventory=self._window.inventory())
        self._timer.start()

    def _emit(self, event: str, **payload: Any) -> None:
        print(json.dumps({"event": event, **payload}), flush=True)

    def _poll(self) -> None:
        try:
            readable, _, _ = select.select([sys.stdin], [], [], 0)
        except (OSError, ValueError):
            self._shutdown()
            return
        if not readable:
            return
        line = sys.stdin.readline()
        if line == "":
            # stdin closed: the parent went away, so exit rather than linger.
            self._shutdown()
            return
        self._dispatch(line.strip())

    def _dispatch(self, line: str) -> None:
        if not line:
            return
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            self._emit("error", cmd=None, message="malformed JSON command")
            return
        if not isinstance(request, dict):
            self._emit("error", cmd=None, message="command must be a JSON object")
            return

        cmd = request.get("cmd")
        try:
            payload = self._execute(str(cmd), request)
        except FixtureCommandError as exc:
            self._emit("error", cmd=cmd, message=str(exc))
            return
        except Exception as exc:  # never let the fixture die on a bad command
            self._emit("error", cmd=cmd, message=f"internal fixture error: {exc!r}")
            return
        self._emit("ack", cmd=cmd, **payload)
        if cmd == "quit":
            self._shutdown()

    def _execute(self, cmd: str, request: dict[str, Any]) -> dict[str, Any]:
        """Run one command, returning extra ack payload."""
        window = self._window
        if cmd == "ping":
            return {"pong": True}
        if cmd == "inventory":
            return {"inventory": window.inventory()}
        if cmd == "stats":
            return {"stats": window.stats()}
        if cmd == "set_text":
            return window.op_set_text(str(request["target"]), str(request.get("value", "")))
        if cmd == "click":
            return window.op_click(str(request["target"]))
        if cmd == "toggle":
            return window.op_toggle(str(request["target"]))
        if cmd == "move":
            return window.op_move(
                str(request["target"]), int(request.get("dx", 0)), int(request.get("dy", 0))
            )
        if cmd == "destroy":
            return window.op_destroy(str(request["target"]))
        if cmd == "set_occluder":
            return window.op_set_occluder(bool(request.get("visible", True)))
        if cmd == "set_focus":
            return window.op_set_focus(str(request["target"]))
        if cmd == "set_selection":
            return window.op_set_selection(str(request["target"]), int(request.get("index", 0)))
        if cmd == "open_dialog":
            return window.op_open_dialog()
        if cmd == "quit":
            return window.op_quit()
        raise FixtureCommandError(f"unknown command {cmd!r}")

    def _shutdown(self) -> None:
        self._timer.stop()
        app = QApplication.instance()
        if isinstance(app, QApplication):
            app.quit()


def build_window(
    title: str, *, bench_controls: bool = False, workflow_controls: bool = False
) -> FixtureWindow:
    """Create and show the fixture window (does not start the event loop)."""
    window = FixtureWindow(
        title, bench_controls=bench_controls, workflow_controls=workflow_controls
    )
    window.show()
    return window


def main(argv: list[str] | None = None) -> int:
    """Run the fixture application until told to quit or stdin closes."""
    parser = argparse.ArgumentParser(prog="fixture_app", description="BLAXCY section 74 fixture")
    parser.add_argument("--title", default="BLAXCY Test Fixture")
    parser.add_argument(
        "--offscreen",
        action="store_true",
        help="force the Qt offscreen platform (no display server needed)",
    )
    parser.add_argument(
        "--bench-controls",
        action="store_true",
        help="add the section 76 large self-verifying benchmark controls",
    )
    parser.add_argument(
        "--workflow-controls",
        action="store_true",
        help="lay the section 74 workflow controls out at self-verifying scale",
    )
    args = parser.parse_args(argv)

    if args.offscreen:
        # Must be set before QApplication is constructed.
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    app = QApplication.instance()
    if not isinstance(app, QApplication):
        app = QApplication(sys.argv[:1])

    window = build_window(
        args.title,
        bench_controls=args.bench_controls,
        workflow_controls=args.workflow_controls,
    )
    pump = CommandPump(window)
    pump.start()
    return int(app.exec())


if __name__ == "__main__":
    raise SystemExit(main())
