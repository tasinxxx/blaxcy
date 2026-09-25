"""Section 75: the real confirmation dialog gating a real destructive action.

The gap this file closes
------------------------
The GUI's confirmation path is covered in three cheaper ways: the bridge's
*decision* logic against a fake dialog (``tests/unit/test_gui_confirmation.py``),
the dialog's own widget behaviour, and the composition check that the executor was
handed a confirmation callback at all
(``tests/integration/test_gui_real_body.py``). None of those answers the question
this suite exists to answer, which was recorded as an honest limitation when the
GUI landed: **does a real human answer, through the real dialog, actually gate a
real destructive action on a real desktop?**

What is real here
-----------------
* the section 74 fixture application, as a real child process with a real window
  on the real display, driven over its own control channel;
* the real Body, assembled by the production composition path
  (``gui.app.build_application``): real capture, real AT-SPI, real policy engine,
  real executor, real verifier, and the real ``XtestBackend``;
* the real :class:`~gui.confirmation.ConfirmationDialog`, genuinely modal, with its
  real countdown floor, answered by the test pressing its real buttons from inside
  its own event loop;
* real input: the pointer really moves and keys are really injected.

The one instrument
------------------
The backend is the real XTEST backend wrapped in :class:`RecordingBackend`, which
forwards every call unchanged and records it. Nothing is stubbed out; the recorder
exists so this file can assert the property the whole suite asserts elsewhere --
that a **refused** action injected nothing at all.

How to run it
-------------
It is opt-in, because it injects real input and opens a real window::

    BLAXCY_E2E_REAL_DISPLAY=1 pytest tests/e2e/test_fixture_confirmation_e2e.py

Run it **on its own**. Qt permits exactly one ``QApplication`` per process and the
rest of the GUI suite needs the offscreen one, so this file skips with that reason
when an offscreen application already exists. It also restores your pointer
position and closes the fixture on the way out.

What is asserted about the outcome
----------------------------------
The gate, not a fabricated success. A drag onto a plain ``QPushButton`` performs
real input and then the verifier honestly reports that no user-visible change
followed -- which is exactly what section 60 requires of it. The assertions are
therefore about *what the human's answer allowed*: nothing injected when denied,
and real injection after approval.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from ai.tool_protocol import ToolCall
from config.settings import load_settings
from control.backends import select_backend
from control.backends.base import BackendProbe, InputBackend, KeyResolution, PointerButton
from gui.app import build_application, self_excluded_settings
from gui.confirmation import (
    MIN_COUNTDOWN_SECONDS,
    ConfirmationBridge,
    ConfirmationDialog,
)
from schemas.actions import ToolName
from schemas.elements import ElementQuery
from schemas.enums import ErrorCode, PolicyMode
from tests.harness import FixtureApp

#: The opt-in gate. This suite injects real input into the live desktop.
REAL_DISPLAY_ENV = "BLAXCY_E2E_REAL_DISPLAY"

#: A command section 54's classifier calls destructive, chosen so that even if
#: something unexpected ever executed it, it would delete nothing that exists.
DESTRUCTIVE_COMMAND = "rm -rf /tmp/blaxcy-e2e-does-not-exist"

#: Fixture controls, named the way the fixture itself names them.
DRAG_SOURCE = "Movable Target"
DRAG_DESTINATION = "Toggle State"
TEXT_FIELD = "Text Input"

#: How long to wait for the fixture's window to become perceivable and active.
SETUP_TIMEOUT = 25.0

#: Section 35 bounds the accessibility traversal by a deadline. The default 250 ms
#: is a *performance* bound, and on a real desktop with a browser, a panel and a
#: terminal running it expires before the traversal reaches the last application in
#: the registry -- which is where the fixture registers. The suite therefore asks
#: for the patience to walk the whole tree. This is the only setting this file
#: changes, it weakens no check, and it is exactly the kind of bound section 4 rule
#: 23 says must be re-measured for the machine it runs on.
ACCESSIBILITY_BUDGET = {"deadline_ms": 8000, "max_nodes": 6000}

#: How long to wait for a dispatch (including the human answering the dialog).
DISPATCH_TIMEOUT = 90.0

#: Event kinds that put something into the desktop. Releases and flushes are not
#: injections, and ``release_all`` on shutdown is cleanup.
_INJECTION_KINDS = frozenset({"move", "press", "scroll", "key_press"})

pytestmark = [
    pytest.mark.gui,
    pytest.mark.skipif(
        not os.environ.get(REAL_DISPLAY_ENV),
        reason=(
            f"set {REAL_DISPLAY_ENV}=1 to run the opt-in real-desktop suite "
            "(it injects real input and opens a real window)"
        ),
    ),
]


class RecordingBackend(InputBackend):
    """The real backend, plus a record of what it was asked to inject.

    Every method forwards to ``inner`` unchanged, so the injection is genuinely
    physical. The record is what lets a *refusal* be asserted to have injected
    nothing -- an assertion that would otherwise need the real desktop to be
    inspected for absence, which is not something a test can do honestly.
    """

    def __init__(self, inner: InputBackend) -> None:
        self._inner = inner
        self.events: list[tuple[object, ...]] = []

    @property
    def inner(self) -> InputBackend:
        """The real backend this wraps."""
        return self._inner

    @property
    def name(self) -> str:
        return f"recording({self._inner.name})"

    def probe(self) -> BackendProbe:
        return self._inner.probe()

    def get_pointer_position(self) -> tuple[int, int] | None:
        return self._inner.get_pointer_position()

    @property
    def supports_pointer_readback(self) -> bool:
        return self._inner.supports_pointer_readback

    def move_pointer(self, x: int, y: int) -> None:
        self.events.append(("move", x, y))
        self._inner.move_pointer(x, y)

    def press_button(self, button: PointerButton) -> None:
        self.events.append(("press", button.value))
        self._inner.press_button(button)

    def release_button(self, button: PointerButton) -> None:
        self.events.append(("release", button.value))
        self._inner.release_button(button)

    def scroll(self, *, vertical: int = 0, horizontal: int = 0) -> None:
        self.events.append(("scroll", vertical, horizontal))
        self._inner.scroll(vertical=vertical, horizontal=horizontal)

    def resolve_key(self, keysym: int) -> KeyResolution | None:
        return self._inner.resolve_key(keysym)

    def key_press(self, keycode: int) -> None:
        self.events.append(("key_press", keycode))
        self._inner.key_press(keycode)

    def key_release(self, keycode: int) -> None:
        self.events.append(("key_release", keycode))
        self._inner.key_release(keycode)

    @property
    def held_keys(self) -> tuple[int, ...]:
        return self._inner.held_keys

    @property
    def held_buttons(self) -> tuple[PointerButton, ...]:
        return self._inner.held_buttons

    def flush(self) -> None:
        self._inner.flush()

    def release_all(self) -> None:
        self._inner.release_all()

    def close(self) -> None:
        self._inner.close()

    # -- What the assertions read ---------------------------------------------

    def injections(self) -> list[tuple[object, ...]]:
        """Only the recorded events that put input into the desktop."""
        return [event for event in self.events if event[0] in _INJECTION_KINDS]

    def event_kinds(self) -> list[str]:
        """The injection kinds, in order."""
        return [str(event[0]) for event in self.injections()]


class DialogAnswerer:
    """Answers the real modal dialog from inside its own event loop.

    The dialog is shown from a nested ``exec()`` running on the GUI thread, so the
    only code that can press its buttons is a timer running *inside* that loop.
    That is what makes this an end-to-end answer rather than a stub: the real
    button on the real dialog is really pressed, after the real countdown.

    It also *observes* the gate: whether ``Confirm`` was ever seen disabled, and
    how long the countdown still had to run when the dialog first appeared.
    """

    def __init__(self, *, approve: bool) -> None:
        from PySide6.QtCore import QObject, QTimer

        self.approve = approve
        self.dialogs: list[Any] = []
        self.first_remaining_seconds: float | None = None
        self.saw_disabled = False
        self.confirm_enabled_seen = False
        self.early_attempts = 0
        self.errors: list[str] = []
        self._answered: list[int] = []
        # The QTimer must be owned by a live QObject, or Qt collects the timer out
        # from under the nested loop.
        self._owner = QObject()
        self._timer = QTimer(self._owner)
        self._timer.setInterval(20)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def stop(self) -> None:
        """Stop answering. Safe to call more than once."""
        self._timer.stop()

    @property
    def confirmations_answered(self) -> int:
        """How many dialogs a decision was delivered to."""
        return len(self._answered)

    def _active_dialog(self) -> Any:
        from PySide6.QtWidgets import QApplication

        dialog = QApplication.activeModalWidget()
        return dialog if isinstance(dialog, ConfirmationDialog) else None

    def _tick(self) -> None:
        """Observe the open dialog and, when appropriate, press its button."""
        try:
            self._observe_and_answer()
        except Exception as exc:  # never let a timer callback break the loop
            self.errors.append(repr(exc))
            self._timer.stop()

    def _observe_and_answer(self) -> None:
        dialog = self._active_dialog()
        if dialog is None:
            return
        if not any(dialog is seen for seen in self.dialogs):
            self.dialogs.append(dialog)
            if self.first_remaining_seconds is None:
                self.first_remaining_seconds = dialog.remaining_seconds
        if dialog.confirm_enabled:
            self.confirm_enabled_seen = True
        else:
            self.saw_disabled = True

        if not self.approve:
            # Deny: the cancel button, pressed for real.
            self._timer.stop()
            self._answered.append(id(dialog))
            dialog.cancel_button.click()
            return

        if dialog.confirm_enabled:
            self._timer.stop()
            self._answered.append(id(dialog))
            dialog.confirm_button.click()
            return

        # Not yet unlocked: press anyway, to record that the floor really held.
        # A disabled button ignores the click, so this cannot shortcut the wait --
        # which is the property being demonstrated.
        self.early_attempts += 1
        dialog.confirm_button.click()


@dataclass
class ConfirmationE2E:
    """Everything one case needs, already wired to the real desktop."""

    fixture: FixtureApp
    app: Any
    bridge: ConfirmationBridge
    backend: RecordingBackend
    restore_pointer: tuple[int, int] | None = None
    notes: list[str] = field(default_factory=list)

    # -- Convenience ----------------------------------------------------------

    def element(self, name: str) -> Any:
        """The perceived element whose text or accessible name is ``name``."""
        state = self.app.cache.current
        if state is None:
            return None
        for element in state.elements:
            if element.text == name or element.accessible_name == name:
                return element
        return None

    def perceived_names(self) -> set[str]:
        """Every text/accessible name the Body really perceived."""
        state = self.app.cache.current
        if state is None:
            return set()
        names: set[str] = set()
        for element in state.elements:
            if element.text:
                names.add(element.text)
            if element.accessible_name:
                names.add(element.accessible_name)
        return names

    def movable_position(self) -> tuple[int, int] | None:
        """The fixture's own reported global position of its movable control."""
        for entry in self.fixture.inventory():
            if entry["object_name"] == "movable":
                return int(entry["global"]["x"]), int(entry["global"]["y"])
        return None


@pytest.fixture(scope="module")
def real_qt() -> Any:
    """A ``QApplication`` on the real display, or an honest skip.

    The rest of the GUI suite runs offscreen, and Qt allows one ``QApplication``
    per process, so this refuses to reuse an offscreen one rather than silently
    testing nothing.
    """
    if not os.environ.get("DISPLAY"):
        pytest.skip("no X display (DISPLAY unset), so there is no real desktop to act on")
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QApplication

    existing = QApplication.instance()
    if existing is not None:
        platform = QGuiApplication.platformName()
        if platform != "xcb":
            pytest.skip(
                f"a {platform!r} QApplication already exists in this process; run this "
                "file on its own (Qt allows exactly one QApplication per process)"
            )
        return existing
    # A real display is the entire point of this file, so never inherit offscreen.
    os.environ.pop("QT_QPA_PLATFORM", None)
    application = QApplication([])
    platform = QGuiApplication.platformName()
    if platform != "xcb":  # pragma: no cover - depends on the host
        pytest.skip(f"Qt selected the {platform!r} platform rather than the X display")
    return application


@pytest.fixture
def confirmation_e2e(real_qt: Any, tmp_path: Any) -> Iterator[ConfirmationE2E]:
    """The real fixture, the real Body, the real dialog -- wired and cleaned up."""
    # The fixture must be on the real display too: the default is offscreen, which
    # would leave nothing for real input to reach.
    try:
        fixture = FixtureApp(platform="xcb", start_timeout=30.0).start()
    except Exception as exc:  # pragma: no cover - depends on the host
        pytest.skip(f"the section 74 fixture could not start on this display: {exc!r}")

    selected = select_backend()
    if selected is None:  # pragma: no cover - depends on the host
        fixture.stop()
        pytest.skip("no input backend passed its functional probe on this display")
    inner = selected
    probe = inner.probe()
    if not probe.available:  # pragma: no cover - depends on the host
        fixture.stop()
        pytest.skip(f"no usable input backend to inject with: {probe.reason}")

    backend = RecordingBackend(inner)
    restore_pointer = inner.get_pointer_position()

    bridge = ConfirmationBridge()
    base = self_excluded_settings(load_settings(None))
    settings = base.model_copy(
        update={
            "accessibility": base.accessibility.model_copy(update=ACCESSIBILITY_BUDGET),
        }
    )
    app = build_application(
        settings,
        bridge=bridge,
        backend=backend,
        # Never take the user's CLIPBOARD selection for this suite, and keep the
        # section 64 heartbeat out of the user's runtime directory.
        clipboard=None,
        watchdog_path=tmp_path / "watchdog.json",
    )
    context = ConfirmationE2E(
        fixture=fixture, app=app, bridge=bridge, backend=backend, restore_pointer=restore_pointer
    )
    try:
        startup = app.start()
        if not startup.started:  # pragma: no cover - depends on the host
            pytest.skip(f"the Body could not start on this host: {startup.notes}")
        app.set_mode(PolicyMode.ASSIST)
        yield context
    finally:
        # Put the pointer back before the Body is torn down, so the user's desktop
        # is left where it was found.
        if restore_pointer is not None:
            try:
                inner.move_pointer(*restore_pointer)
                inner.flush()
            except Exception:  # pragma: no cover - the display may already be gone
                pass
        app.shutdown()
        bridge.close()
        fixture.stop()


# -- Helpers -------------------------------------------------------------------


def _perceive_until(context: ConfirmationE2E, wanted: set[str]) -> set[str]:
    """Perceive until the fixture's controls are visible, or the timeout expires."""
    deadline = time.monotonic() + SETUP_TIMEOUT
    names: set[str] = set()
    while time.monotonic() < deadline:
        context.app.perceive()
        names = context.perceived_names()
        if wanted <= names:
            return names
        time.sleep(0.25)
    return names


def _activate_fixture(context: ConfirmationE2E, control: str) -> None:
    """Make the fixture's window the active one, or skip rather than type blind.

    This is the guard that matters most in this file: the destructive command is
    typed with real keystrokes, so if the fixture cannot be shown to be the active
    window the test must not type at all -- those keys would land in whatever the
    user actually has focused.
    """
    deadline = time.monotonic() + SETUP_TIMEOUT
    while time.monotonic() < deadline:
        context.fixture.set_focus(control)
        state = context.app.perceive()
        if state is None:
            time.sleep(0.25)
            continue
        element = context.element(TEXT_FIELD)
        # The perceived element's own ``owner_app`` is the application the fixture's
        # controls belong to, so requiring it to be the active application is a real
        # reading of the live desktop rather than a guess at a process name.
        if element is not None and state.active_app == element.owner_app:
            return
        time.sleep(0.25)
    pytest.skip(
        "the fixture window could not be made the active window on this desktop; refusing "
        "to inject real keystrokes into an unknown focused window"
    )


def _require_fixture_targets(context: ConfirmationE2E) -> None:
    """Skip unless the fixture's real controls are in the perceived tree."""
    names = _perceive_until(context, {DRAG_SOURCE, DRAG_DESTINATION, TEXT_FIELD})
    missing = {DRAG_SOURCE, DRAG_DESTINATION, TEXT_FIELD} - names
    if missing:
        pytest.skip(
            "the fixture's controls are not in the Body's AT-SPI perception on this "
            f"desktop (missing: {sorted(missing)}); {len(names)} names were perceived"
        )


def _require_injectable(context: ConfirmationE2E, name: str) -> None:
    """Skip when BLAXCY itself would refuse this target as occluded.

    Section 46 blocks a target that is more than half covered by *something in
    front of it*. This helper used to skip almost every path on a real desktop,
    because the occlusion assessment was purely geometric and counted the desktop
    background and every window **behind** the target as covering it. That defect
    is fixed (window stacking: ancestors excluded by ``atspi_path``, ownership
    joined by pid), so the fixture's controls are now normally actionable and the
    injection paths below really run.

    The guard is kept because it is still the honest thing to do when the desktop
    genuinely has a window in front of the fixture: injecting at a target the Body
    has refused would be testing something the Body does not do. When it does
    trigger, it names the coverage so the operator can move the window.

    Note this is deliberately *not* applied to the deny paths: a refusal at the
    confirmation gate happens before revalidation ever consults occlusion, so
    those cases are meaningful on a cluttered desktop.
    """
    state = context.app.cache.current
    assert state is not None, "no perceived state to resolve against"
    result = context.app.resolver.resolve(
        ElementQuery(text=name), state.elements, occluders=state.elements, state=state
    )
    if not result.is_resolved:
        pytest.skip(f"{name!r} did not resolve on this desktop: {result.reason}")
    if result.is_actionable:
        return
    assessment = result.occlusion
    detail = (
        f"coverage {assessment.ratio:.2f} > {assessment.threshold:.2f}, covering "
        f"{list(assessment.covering)}"
        if assessment is not None
        else "occlusion was not assessed"
    )
    pytest.skip(
        f"BLAXCY refuses {name!r} as occluded on this desktop ({detail}); the rule is "
        "geometric and counts windows behind the target, so this needs an uncluttered "
        "desktop. Minimise the overlapping windows and run this file again."
    )


def _dispatch_while_answering(
    context: ConfirmationE2E, call: ToolCall, answerer: DialogAnswerer
) -> tuple[Any, BaseException | None]:
    """Dispatch on a worker thread while the GUI thread answers the dialog.

    The executor calls its confirmation callback from the thread that is about to
    inject input, and the bridge blocks that thread until the human answers. That
    is exactly the real arrangement: the action thread waits, the GUI thread shows
    the dialog, and this loop is the GUI thread.
    """
    from PySide6.QtWidgets import QApplication

    outcome: dict[str, Any] = {}

    def work() -> None:
        try:
            outcome["envelope"] = context.app.dispatch(call)
        except BaseException as exc:  # surfaced to the test rather than swallowed
            outcome["error"] = exc

    worker = threading.Thread(target=work, name="blaxcy-e2e-dispatch", daemon=True)
    worker.start()
    deadline = time.monotonic() + DISPATCH_TIMEOUT
    while worker.is_alive() and time.monotonic() < deadline:
        # This is the call that delivers the queued request to the GUI thread; the
        # modal dialog then runs its own loop inside it, where the answerer's timer
        # fires and the real button is pressed.
        QApplication.processEvents()
        time.sleep(0.01)
    worker.join(timeout=5.0)
    answerer.stop()
    assert not answerer.errors, f"the dialog answerer raised: {answerer.errors}"
    assert not worker.is_alive(), "the dispatch did not finish within the timeout"
    return outcome.get("envelope"), outcome.get("error")


def _drag_call() -> ToolCall:
    """The destructive action: a drag, classified DESTRUCTIVE by section 49."""
    return ToolCall(
        name=ToolName.DRAG,
        arguments={"target": DRAG_SOURCE, "destination": DRAG_DESTINATION},
    )


def _submission_call() -> ToolCall:
    """The destructive submission: the literal command is visible to section 54."""
    return ToolCall(
        name=ToolName.PRESS_KEY,
        arguments={"key": "Return", "visible_command": DESTRUCTIVE_COMMAND},
    )


# -- The confirmation gate over a real destructive action ----------------------


def test_a_real_dialog_denies_a_real_destructive_drag(
    confirmation_e2e: ConfirmationE2E,
) -> None:
    """A human pressing Cancel injects nothing at all into the desktop."""
    context = confirmation_e2e
    _require_fixture_targets(context)
    position_before = context.movable_position()

    answerer = DialogAnswerer(approve=False)
    envelope, error = _dispatch_while_answering(context, _drag_call(), answerer)

    assert error is None, f"the dispatch raised: {error!r}"
    # The real dialog really appeared, and the countdown floor was really closed.
    assert answerer.dialogs, "no confirmation dialog was ever shown"
    assert answerer.first_remaining_seconds is not None
    assert answerer.first_remaining_seconds > 0.0, (
        "the dialog was already unlocked when it first appeared, so the countdown "
        f"floor ({MIN_COUNTDOWN_SECONDS}s) is not being applied"
    )
    assert answerer.saw_disabled, "Confirm was never observed disabled"
    assert answerer.confirmations_answered >= 1, "no decision was delivered to the dialog"

    # The human said no, and the answer was honoured.
    assert context.bridge.stats()["denied"] >= 1
    assert envelope.ok is False
    assert envelope.error_code in {
        ErrorCode.CONFIRMATION_DENIED,
        ErrorCode.CONFIRMATION_REQUIRED,
    }, f"expected a confirmation refusal, got {envelope.error_code}: {envelope.message}"

    # The property the whole suite asserts: a refusal injected nothing.
    assert context.backend.injections() == [], (
        f"a denied destructive action still injected input: {context.backend.injections()}"
    )
    # And the target application did not change.
    assert context.movable_position() == position_before


def test_a_real_dialog_approves_a_real_destructive_drag(
    confirmation_e2e: ConfirmationE2E,
) -> None:
    """A human pressing Confirm after the countdown is what lets the input out."""
    context = confirmation_e2e
    _require_fixture_targets(context)
    _require_injectable(context, DRAG_SOURCE)
    _require_injectable(context, DRAG_DESTINATION)

    answerer = DialogAnswerer(approve=True)
    envelope, error = _dispatch_while_answering(context, _drag_call(), answerer)

    assert error is None, f"the dispatch raised: {error!r}"
    assert answerer.dialogs, "no confirmation dialog was ever shown"
    assert answerer.saw_disabled, "Confirm was never observed disabled, so it was not gated"
    assert answerer.early_attempts > 0, (
        "the test never pressed Confirm before the countdown elapsed, so it did not "
        "demonstrate that the floor cannot be shortcut"
    )
    assert answerer.confirm_enabled_seen, "Confirm was never observed unlocked"
    assert context.bridge.stats()["granted"] >= 1, "the approval never reached the bridge"

    # The approval is what allowed the input out: real moves and a real button press.
    kinds = context.backend.event_kinds()
    assert "move" in kinds, f"the approved drag never moved the pointer: {kinds}"
    assert "press" in kinds, f"the approved drag never pressed a button: {kinds}"

    # Nothing about the outcome is invented: the verifier reports what actually
    # happened, which for a drag onto a plain button is honest non-verification.
    assert envelope.error_code not in {
        ErrorCode.CONFIRMATION_REQUIRED,
        ErrorCode.CONFIRMATION_DENIED,
    }, f"the approved action was still refused for confirmation: {envelope.message}"


def test_a_real_dialog_denies_a_real_destructive_submission(
    confirmation_e2e: ConfirmationE2E,
) -> None:
    """Typing the command proceeds; submitting it is gated, and the refusal injects nothing."""
    context = confirmation_e2e
    _require_fixture_targets(context)
    _require_injectable(context, TEXT_FIELD)
    _activate_fixture(context, "text_input")

    # Typing a destructive line is allowed and flagged (section 54): the gate is on
    # the submission. This types for real, into the fixture's own text field.
    typed = context.app.dispatch(
        ToolCall(
            name=ToolName.TYPE_TEXT,
            arguments={
                "target": TEXT_FIELD,
                "text": DESTRUCTIVE_COMMAND,
                "visible_command": DESTRUCTIVE_COMMAND,
            },
        )
    )
    if context.fixture.stats()["text_input"] != DESTRUCTIVE_COMMAND:
        pytest.skip(
            "real typing did not reach the fixture's text field on this desktop "
            f"(envelope said {typed.error_code}: {typed.message}); no submission was attempted"
        )
    typed_events = len(context.backend.injections())
    assert typed_events > 0, "the typing reported success without injecting any key"

    answerer = DialogAnswerer(approve=False)
    envelope, error = _dispatch_while_answering(context, _submission_call(), answerer)

    assert error is None, f"the dispatch raised: {error!r}"
    assert answerer.dialogs, "the destructive submission was not gated by a dialog at all"
    assert context.bridge.stats()["denied"] >= 1
    assert envelope.ok is False
    assert envelope.error_code in {
        ErrorCode.CONFIRMATION_DENIED,
        ErrorCode.CONFIRMATION_REQUIRED,
    }, f"expected a confirmation refusal, got {envelope.error_code}: {envelope.message}"

    # Section 54's whole point: the Return key was never injected.
    assert len(context.backend.injections()) == typed_events, (
        "a denied submission still injected input: "
        f"{context.backend.event_kinds()[typed_events:]}"
    )


def test_a_real_dialog_approves_a_real_destructive_submission(
    confirmation_e2e: ConfirmationE2E,
) -> None:
    """After the human approves, the Return key is really injected."""
    context = confirmation_e2e
    _require_fixture_targets(context)
    _require_injectable(context, TEXT_FIELD)
    _activate_fixture(context, "text_input")

    context.app.dispatch(
        ToolCall(
            name=ToolName.TYPE_TEXT,
            arguments={
                "target": TEXT_FIELD,
                "text": DESTRUCTIVE_COMMAND,
                "visible_command": DESTRUCTIVE_COMMAND,
            },
        )
    )
    if context.fixture.stats()["text_input"] != DESTRUCTIVE_COMMAND:
        pytest.skip("real typing did not reach the fixture's text field on this desktop")
    before = len(context.backend.injections())

    answerer = DialogAnswerer(approve=True)
    envelope, error = _dispatch_while_answering(context, _submission_call(), answerer)

    assert error is None, f"the dispatch raised: {error!r}"
    assert answerer.dialogs, "the destructive submission was not gated by a dialog at all"
    assert answerer.saw_disabled, "Confirm was never observed disabled, so it was not gated"
    assert answerer.confirm_enabled_seen, "Confirm was never observed unlocked"
    assert context.bridge.stats()["granted"] >= 1, "the approval never reached the bridge"

    new_kinds = context.backend.event_kinds()[before:]
    assert "key_press" in new_kinds, (
        f"the approved submission never injected the Return key: {new_kinds}"
    )
    # The submission is genuinely judged on its own outcome, and that outcome is
    # reported honestly rather than asserted into a success.
    assert envelope.error_code not in {
        ErrorCode.CONFIRMATION_REQUIRED,
        ErrorCode.CONFIRMATION_DENIED,
    }, f"the approved submission was still refused for confirmation: {envelope.message}"
