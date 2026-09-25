"""Clipboard paster tests (specification sections 30, 50, 52).

These run without a usable X display, on purpose: they pin the *honest failure*
path, which is the behaviour the keyboard controller depends on when the
clipboard is unusable. Nothing here may report success it cannot back up, and
nothing here may leave BLAXCY's text sitting on the user's clipboard.

The real transfer -- taking the X CLIPBOARD selection, serving it to another
client, and handing it back -- is verified against the live display in
``tests/integration/test_clipboard_real_display.py``.
"""

from __future__ import annotations

import pytest

from config.settings import ClipboardSettings, InputSettings
from control.backends.keys import keysym_for_char
from control.clipboard import (
    X11ClipboardPaster,
    probe_x11_clipboard,
    select_clipboard_paster,
)
from control.keyboard import KeyboardController
from schemas.enums import ErrorCode
from schemas.errors import BlaxcyError
from tests.harness.fake_input_backend import FakeInputBackend
from tests.unit.test_keyboard import FakeClipboard, _backend

#: A display that cannot exist, so the failure is real and fast rather than faked.
NO_SUCH_DISPLAY = ":12345"


def _unusable_paster(**overrides: object) -> X11ClipboardPaster:
    """A paster pointed at a display that does not exist."""
    settings = ClipboardSettings.model_validate(overrides) if overrides else ClipboardSettings()
    return X11ClipboardPaster(settings, display_name=NO_SUCH_DISPLAY, sleep=lambda _s: None)


def _injected_keys(backend: FakeInputBackend) -> list[tuple[object, ...]]:
    """Every real key event, ignoring bookkeeping flushes."""
    return [event for event in backend.events if event[0] in ("key_press", "key_release", "move", "press")]


# -- Honest failure -----------------------------------------------------------

def test_the_probe_of_a_missing_display_is_unavailable_with_a_reason() -> None:
    """The probe distinguishes 'cannot serve the clipboard' from 'not asked'."""
    probe = X11ClipboardPaster(display_name=NO_SUCH_DISPLAY, sleep=lambda _s: None).probe()
    assert probe.available is False
    assert probe.name == "xlib-selection"
    assert probe.reason
    assert probe.latency_ms is not None
    assert probe.details.get("display") == NO_SUCH_DISPLAY


def test_probe_x11_clipboard_reports_the_live_display_honestly() -> None:
    """A probe always answers, and only claims availability it can back up."""
    probe = probe_x11_clipboard(ClipboardSettings())
    if probe.available:
        # Availability has to carry the evidence that produced it.
        assert probe.details["targets"] == ["UTF8_STRING", "STRING", "TEXT"]
        assert probe.details["takes_ownership"] is False
    else:
        assert probe.reason


def test_a_paster_on_a_missing_display_reports_the_real_reason() -> None:
    """A refused clipboard says why, so the caller can act on it."""
    paster = _unusable_paster()
    try:
        assert paster.set_text("café") is False
        status = paster.status()
        assert status["owns_selection"] is False
        assert status["last_error"]
        assert NO_SUCH_DISPLAY in status["last_error"]
    finally:
        paster.close()


# -- Refusals that must not touch the clipboard ------------------------------

def test_oversized_text_is_refused_before_any_x_interaction() -> None:
    """The size cap is enforced before the display is even contacted."""
    paster = _unusable_paster(max_bytes=8)
    try:
        assert paster.set_text("x" * 9) is False
        assert "9 bytes" in (paster.status()["last_error"] or "")
        assert paster.status()["set_calls"] == 1
        assert paster.status()["connected"] is False
    finally:
        paster.close()


def test_disabling_clipboard_typing_refuses_honestly_and_selects_nothing() -> None:
    """``enabled = false`` removes a capability; it never fakes one."""
    paster = _unusable_paster(enabled=False)
    try:
        assert paster.set_text("café") is False
        assert "disabled" in (paster.status()["last_error"] or "")
    finally:
        paster.close()
    # A disabled paster is never handed to the typing layer, so the controller
    # takes its own honest "cannot produce this text" path.
    assert select_clipboard_paster(ClipboardSettings(enabled=False)) is None


def test_restore_with_nothing_served_is_a_no_op_that_says_so() -> None:
    """Restoring an untouched clipboard must not claim it restored anything."""
    paster = _unusable_paster()
    try:
        paster.restore()
        first = paster.last_report
        assert first is not None
        assert first.restored is False
        assert first.handed_back is False
        assert "nothing was being served" in first.reasons
        # Idempotent: a second restore is still a no-op, not an error.
        paster.restore()
        second = paster.last_report
        assert second is not None
        assert second.restored is False
    finally:
        paster.close()


def test_close_is_safe_without_a_connection_and_is_idempotent() -> None:
    """Teardown paths (crash cleanup) must never raise."""
    paster = _unusable_paster()
    paster.close()
    paster.close()
    status = paster.status()
    assert status["connected"] is False
    assert status["owns_selection"] is False
    assert status["serving_chars"] is None


def test_a_failed_set_text_never_claims_to_be_serving() -> None:
    """There is no path returning True without really owning the selection."""
    paster = _unusable_paster()
    try:
        assert paster.set_text("anything") is False
        assert paster.status()["owns_selection"] is False
        assert paster.status()["serving_chars"] is None
        assert paster.status()["previous_text_chars"] is None
    finally:
        paster.close()


# -- The controller's contract with a real paster ----------------------------

def test_the_real_paster_plugs_into_the_keyboard_controller() -> None:
    """The controller drives the real implementation through the protocol alone."""
    paster = _unusable_paster()
    backend = _backend()
    controller = KeyboardController(backend, InputSettings(), clipboard=paster, sleep=lambda _s: None)
    try:
        with pytest.raises(BlaxcyError) as excinfo:
            controller.type_text("café")
        # The clipboard was reached but unusable: an honest failure, and not one
        # character of the text was approximated.
        assert excinfo.value.code is ErrorCode.CLIPBOARD_FAILED
        assert excinfo.value.details["characters"] == 4
        assert _injected_keys(backend) == []
        assert paster.status()["owns_selection"] is False
    finally:
        controller.close()


def test_the_clipboard_is_restored_when_the_paste_keystroke_fails() -> None:
    """Sections 50/52: a failed paste must not leave BLAXCY's text on the clipboard."""
    clipboard = FakeClipboard()
    v_keysym = keysym_for_char("v")
    assert v_keysym is not None
    backend = _backend(unresolvable={v_keysym})
    controller = KeyboardController(
        backend, InputSettings(), clipboard=clipboard, sleep=lambda _s: None
    )
    try:
        with pytest.raises(BlaxcyError) as excinfo:
            controller.type_text("café")
        assert excinfo.value.code is ErrorCode.BACKEND_UNAVAILABLE
        assert clipboard.set_calls == ["café"]
        # The text went on, so it must come back off, and nothing was injected.
        assert clipboard.restore_calls == 1
        assert backend.payloads("key_press") == []
    finally:
        controller.close()
