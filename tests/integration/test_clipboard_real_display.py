"""Real-display clipboard integration tests (specification sections 30, 50).

Unlike the other real-display suites, this one **does** change shared desktop
state: verifying that long and non-ASCII text can be typed requires really taking
the X CLIPBOARD selection and really serving it to another client. That is the
only way to know the feature works rather than merely that it did not raise.

The discipline that makes that acceptable:

* every test restores the clipboard in a ``finally`` block, so the selection
  ownership is handed back;
* the previous content is captured by the paster before it takes ownership and
  re-served during the restore grace, which is what lets a clipboard manager keep
  it -- so the user's clipboard survives on a desktop that runs one, and the test
  asserts that capture really happened;
* nothing here injects *input*: the paste keystroke (Ctrl+V) is exercised only on
  the fake backend in the unit tests, never against the live desktop.

If there is no display, or the selection cannot be served, the tests skip with an
honest reason rather than fabricating a result.
"""

from __future__ import annotations

import os
import threading
import time

import pytest

from config.settings import ClipboardSettings
from control.clipboard import (
    TARGET_STRING,
    TARGET_TARGETS,
    TARGET_UTF8_STRING,
    X11ClipboardPaster,
    select_clipboard_paster,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("DISPLAY"),
    reason="real-display clipboard test requires an X display (DISPLAY unset)",
)

#: Deliberately recognisable test content, so a leftover entry in a clipboard
#: manager's history is obviously a test artifact.
TEST_TEXT = "BLAXCY clipboard test — héllo wörld ✓ 日本語"
PRIOR_TEXT = "BLAXCY prior-content test ✓"


def _read_clipboard(target: str = TARGET_UTF8_STRING, timeout: float = 2.0) -> tuple[int | None, str | None]:
    """Ask the selection owner for ``target``, as a paste target would.

    Returns ``(property_atom, text)``: the property the owner answered with (or
    ``None`` when it refused the target) and the decoded text when there was one.
    A refusal is reported as a refusal, not as empty text.
    """
    from Xlib import X, display
    from Xlib.protocol import request as req

    connection = display.Display()
    screen = connection.screen()
    window = screen.root.create_window(
        0,
        0,
        1,
        1,
        0,
        screen.root_depth,
        X.InputOutput,
        X.CopyFromParent,
        background_pixel=screen.white_pixel,
        event_mask=X.PropertyChangeMask,
    )
    prop = connection.intern_atom("BLAXCY_TEST_READ")
    utf8_atom = connection.intern_atom(TARGET_UTF8_STRING)
    try:
        req.ConvertSelection(
            connection.display,
            requestor=window,
            selection=connection.intern_atom("CLIPBOARD"),
            target=connection.intern_atom(target),
            property=prop,
            time=X.CurrentTime,
        )
        connection.sync()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not connection.pending_events():
                time.sleep(0.005)
                continue
            event = connection.next_event()
            if event.type != X.SelectionNotify:
                continue
            if event.property == X.NONE:
                return None, None
            data = window.get_full_property(prop, X.AnyPropertyType)
            if data is None:
                return None, None
            value = data.value
            if isinstance(value, bytes):
                # Decode according to the type the owner actually set: STRING is
                # Latin-1 by convention, UTF8_STRING is UTF-8. python-xlib reports
                # it as ``property_type`` (``type`` is the event type).
                property_type = int(getattr(data, "property_type", utf8_atom) or 0)
                if property_type != utf8_atom:
                    return int(event.property), value.decode("latin-1")
                try:
                    return int(event.property), value.decode("utf-8")
                except UnicodeDecodeError:
                    return int(event.property), None
            return int(event.property), str(value)
        # The owner never answered within the timeout: an honest "no data".
        return None, None
    finally:
        window.destroy()
        connection.sync()
        connection.close()


def _current_owner() -> int | None:
    """The window id that owns CLIPBOARD right now, or ``None``."""
    from Xlib import display

    connection = display.Display()
    try:
        owner = connection.get_selection_owner(connection.intern_atom("CLIPBOARD"))
        # python-xlib reports "nobody owns it" as a window whose id is 0 (verified
        # against the installed version), not as ``None``.
        return int(getattr(owner, "id", owner) or 0) or None
    finally:
        connection.close()


def _paster(**overrides: object) -> X11ClipboardPaster:
    """A paster over explicit clipboard settings (short graces where possible)."""
    settings = ClipboardSettings.model_validate(overrides) if overrides else ClipboardSettings()
    paster = X11ClipboardPaster(settings)
    if not paster.probe().available:
        paster.close()
        pytest.skip(f"the clipboard selection cannot be served: {paster.status()['last_error']}")
    return paster


# -- The probe ----------------------------------------------------------------

def test_the_probe_is_functional_and_takes_nothing() -> None:
    """A passing probe must mean the mechanism exists, without stealing the clipboard."""
    owner_before = _current_owner()
    probe = _paster().probe()

    assert probe.available is True
    assert probe.name == "xlib-selection"
    assert probe.details["takes_ownership"] is False
    assert probe.details["targets"] == [TARGET_UTF8_STRING, TARGET_STRING, "TEXT"]
    assert probe.details["owner_window"]
    # The probe really read the live selection, and really left it alone.
    assert probe.details["selection_owned"] is (owner_before is not None)
    assert _current_owner() == owner_before


def test_select_returns_a_usable_paster_on_a_live_display() -> None:
    """The selection helper hands back a paster that can really serve text."""
    paster = select_clipboard_paster(ClipboardSettings(paste_grace_ms=0, restore_grace_ms=0))
    assert paster is not None
    try:
        assert paster.set_text(TEST_TEXT) is True
        prop, text = _read_clipboard()
        assert prop is not None
        assert text == TEST_TEXT
    finally:
        paster.restore()
        paster.close()


# -- The real transfer --------------------------------------------------------

def test_non_ascii_text_really_reaches_another_client() -> None:
    """The section 50 promise, verified: a second client reads back the exact text."""
    paster = _paster(restore_grace_ms=0)
    try:
        assert paster.set_text(TEST_TEXT) is True
        status = paster.status()
        assert status["owns_selection"] is True
        assert status["serving_chars"] == len(TEST_TEXT)

        prop, text = _read_clipboard()
        assert text == TEST_TEXT
        assert prop is not None
        # The pause between asking and reading must not change the answer: the
        # data is served on demand, not handed over once.
        prop_again, text_again = _read_clipboard()
        assert text_again == TEST_TEXT
        assert prop_again == prop
        assert paster.status()["served_requests"] >= 2
        assert TARGET_UTF8_STRING in paster.status()["served_targets"]
    finally:
        paster.restore()
        paster.close()


def test_targets_are_advertised_truthfully() -> None:
    """A TARGETS request gets the real list, not a claim of arbitrary formats."""
    paster = _paster(restore_grace_ms=0)
    try:
        assert paster.set_text(TEST_TEXT) is True
        prop, _text = _read_clipboard(TARGET_TARGETS)
        assert prop is not None
        assert TARGET_TARGETS in paster.status()["served_targets"]
    finally:
        paster.restore()
        paster.close()


def test_string_is_refused_rather_than_mangled_for_non_latin1_text() -> None:
    """A format that cannot represent the text is refused, never approximated."""
    paster = _paster(restore_grace_ms=0)
    try:
        assert paster.set_text(TEST_TEXT) is True
        # TEST_TEXT contains characters Latin-1 cannot encode.
        prop, _text = _read_clipboard(TARGET_STRING)
        assert prop is None, "STRING was answered for text Latin-1 cannot represent"
    finally:
        paster.restore()
        paster.close()


def test_string_is_served_when_the_text_really_is_latin1() -> None:
    """The refusal above is about representability, not about refusing STRING."""
    paster = _paster(restore_grace_ms=0)
    try:
        assert paster.set_text("cafe creme brulée") is True
        prop, text = _read_clipboard(TARGET_STRING)
        assert prop is not None
        assert text == "cafe creme brulée"
    finally:
        paster.restore()
        paster.close()


# -- Restoring the user's clipboard -------------------------------------------

def test_restore_captures_the_previous_content_and_hands_ownership_back() -> None:
    """The previous owner and its text are captured, and both are restored."""
    first = _paster(restore_grace_ms=0)
    second = _paster(paste_grace_ms=0, restore_grace_ms=0)
    try:
        assert first.set_text(PRIOR_TEXT) is True
        first_owner = _current_owner()
        assert first_owner is not None

        assert second.set_text(TEST_TEXT) is True
        assert _read_clipboard()[1] == TEST_TEXT

        second.restore()
        report = second.last_report
        assert report is not None
        assert report.restored is True
        assert report.handed_back is True
        # The previous content was really read off the selection before BLAXCY
        # took it over -- this is the evidence, not an assumption.
        assert report.previous_text == PRIOR_TEXT
        assert report.previous_owner == first_owner
        assert _current_owner() == first_owner
    finally:
        second.close()
        first.close()


def test_the_previous_content_is_served_during_the_restore_grace() -> None:
    """The grace window is what lets a clipboard manager keep the user's content."""
    first = _paster(restore_grace_ms=0)
    second = _paster(paste_grace_ms=0, restore_grace_ms=1500)
    try:
        assert first.set_text(PRIOR_TEXT) is True
        assert second.set_text(TEST_TEXT) is True

        seen: list[str | None] = []
        restore_thread = threading.Thread(target=second.restore)
        restore_thread.start()
        try:
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline and PRIOR_TEXT not in seen:
                seen.append(_read_clipboard(timeout=0.3)[1])
        finally:
            restore_thread.join(timeout=5.0)

        assert PRIOR_TEXT in seen, f"the previous content was never re-served: {seen}"
        assert second.last_report is not None
        assert second.last_report.handed_back is True
    finally:
        second.close()
        first.close()


def test_restore_releases_the_selection_when_there_was_no_previous_owner() -> None:
    """With nothing to hand back, BLAXCY releases the selection instead of holding it."""
    paster = _paster(paste_grace_ms=0, restore_grace_ms=0)
    try:
        if _current_owner() is not None:
            pytest.skip("another client owns the clipboard, so 'no previous owner' is untestable")
        assert paster.set_text(TEST_TEXT) is True
        paster.restore()
        report = paster.last_report
        assert report is not None
        assert report.handed_back is True
        assert report.previous_owner is None
        assert _current_owner() is None
        assert paster.status()["owns_selection"] is False
    finally:
        paster.close()


def test_a_reused_paster_serves_every_round() -> None:
    """A stale SelectionClear must not stop the next transfer (regression)."""
    paster = _paster(paste_grace_ms=0, restore_grace_ms=0)
    try:
        for index in range(3):
            text = f"{TEST_TEXT} #{index}"
            assert paster.set_text(text) is True
            assert _read_clipboard()[1] == text
            paster.restore()
    finally:
        paster.close()


def test_repeated_set_text_reuses_one_serve_thread() -> None:
    """Two threads cannot pump one X connection: the second borrow reuses the loop."""
    paster = _paster(paste_grace_ms=0, restore_grace_ms=0)
    try:
        assert paster.set_text("first value") is True
        assert paster.set_text(TEST_TEXT) is True
        assert _read_clipboard()[1] == TEST_TEXT
        loops = [
            thread
            for thread in threading.enumerate()
            if thread.name == "blaxcy-clipboard" and thread.is_alive()
        ]
        assert len(loops) == 1, f"expected one serve loop, found {len(loops)}"
    finally:
        paster.restore()
        paster.close()


def test_close_releases_the_selection_it_was_holding() -> None:
    """Teardown cannot leave BLAXCY owning the clipboard."""
    paster = _paster(paste_grace_ms=0, restore_grace_ms=0)
    assert paster.set_text(TEST_TEXT) is True
    paster.close()
    status = paster.status()
    assert status["owns_selection"] is False
    assert status["connected"] is False
    assert status["serving_chars"] is None
