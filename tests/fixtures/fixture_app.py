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

from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPaintEvent, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
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

    def __init__(self, title: str) -> None:
        super().__init__()
        self.setWindowTitle(title)
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
        for widget in (
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
        ):
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
            self.result_list.addItem(QListWidgetItem(f"Result {index} for {query}"))
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


def build_window(title: str) -> FixtureWindow:
    """Create and show the fixture window (does not start the event loop)."""
    window = FixtureWindow(title)
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
    args = parser.parse_args(argv)

    if args.offscreen:
        # Must be set before QApplication is constructed.
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    app = QApplication.instance()
    if not isinstance(app, QApplication):
        app = QApplication(sys.argv[:1])

    window = build_window(args.title)
    pump = CommandPump(window)
    pump.start()
    return int(app.exec())


if __name__ == "__main__":
    raise SystemExit(main())
