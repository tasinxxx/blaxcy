"""Keyboard tests (specification sections 50, 51, 52).

The central safety property here is the section 51 gate: when the caller names
the element it means to type into, nothing is injected unless that exact element
is present, still accepts text, and is actually focused. The section 50 tests
pin the direct/clipboard decision and the honest ``UNICODE_UNSUPPORTED`` refusal.
"""

from __future__ import annotations

import pytest

from config.settings import InputSettings
from control.backends.keys import keysym_for_char, keysym_for_name
from control.keyboard import KeyboardController, check_focus
from schemas.elements import UIElement
from schemas.enums import ErrorCode, PerceptionSource, UIRole
from schemas.errors import BlaxcyError
from schemas.geometry import MonitorGeometry, MonitorLayout
from schemas.screen_state import ScreenState
from tests.harness.fake_input_backend import FakeInputBackend

SHIFT_KEYCODE = 65505
CTRL_KEYCODE = 65507
V_KEYCODE = ord("v")


class FakeClipboard:
    """A clipboard that records what it was asked to hold."""

    def __init__(self, *, works: bool = True) -> None:
        self.works = works
        self.set_calls: list[str] = []
        self.restore_calls = 0

    def set_text(self, text: str) -> bool:
        self.set_calls.append(text)
        return self.works

    def restore(self) -> None:
        self.restore_calls += 1


def _layout() -> MonitorLayout:
    return MonitorLayout(
        monitors=(MonitorGeometry(monitor_id=0, width=1920, height=1080, is_primary=True),)
    )


def _field(element_id: str = "f1", **overrides: object) -> UIElement:
    """A focused text field (or an override) for focus-guard fixtures."""
    base: dict[str, object] = {
        "element_id": element_id,
        "role": UIRole.TEXT_INPUT,
        "source": PerceptionSource.ATSPI,
        "confidence": 0.95,
        "focused": True,
        "atspi_path": f"/p/{element_id}",
    }
    base.update(overrides)
    return UIElement.model_validate(base)


def _state(*elements: UIElement) -> ScreenState:
    return ScreenState(
        frame_id=1,
        state_version=1,
        generation=0,
        timestamp=1000.0,
        monotonic=1.0,
        layout=_layout(),
        elements=tuple(elements),
    )


def _backend(
    *,
    shifted: set[int] | None = None,
    unresolvable: set[int] | None = None,
    raise_on_key_press: int | None = None,
) -> FakeInputBackend:
    return FakeInputBackend(
        shifted={ord("H")} if shifted is None else shifted,
        unresolvable=unresolvable,
        raise_on_key_press=raise_on_key_press,
    )


def _keyboard(
    backend: FakeInputBackend,
    *,
    settings: InputSettings | None = None,
    clipboard: FakeClipboard | None = None,
) -> KeyboardController:
    return KeyboardController(backend, settings, clipboard=clipboard, sleep=lambda _s: None)


# -- Section 50: direct typing ------------------------------------------------


def test_ascii_text_is_typed_directly_key_by_key() -> None:
    """Plain ASCII uses XTEST with a press/release pair per character."""
    backend = _backend()
    result = _keyboard(backend).type_text("ab")
    assert result.ok is True
    assert result.method == "xtest_direct"
    assert result.characters == 2
    assert backend.event_names() == [
        "key_press",
        "key_release",
        "flush",
        "key_press",
        "key_release",
        "flush",
    ]
    assert backend.payloads("key_press") == [("key_press", ord("a")), ("key_press", ord("b"))]


def test_shift_is_pressed_and_released_around_a_shifted_character() -> None:
    """An uppercase letter holds and then releases Shift (section 52 hygiene)."""
    backend = _backend()
    result = _keyboard(backend).type_text("H")
    assert result.ok is True
    assert backend.payloads("key_press") == [
        ("key_press", SHIFT_KEYCODE),
        ("key_press", ord("H")),
    ]
    assert backend.payloads("key_release") == [
        ("key_release", ord("H")),
        ("key_release", SHIFT_KEYCODE),
    ]
    assert backend.held_keys == ()


def test_key_interval_sleeps_between_characters_only() -> None:
    """The section 50 inter-key delay is applied *between* keys, not after the last."""
    sleeps: list[float] = []
    backend = _backend()
    controller = KeyboardController(
        backend, InputSettings(key_interval_ms=10), clipboard=None, sleep=sleeps.append
    )
    controller.type_text("abc")
    assert sleeps == [0.01, 0.01]


def test_non_ascii_without_clipboard_is_refused_honestly() -> None:
    """Text the keyboard cannot produce fails with UNICODE_UNSUPPORTED (section 50)."""
    backend = _backend()
    with pytest.raises(BlaxcyError) as excinfo:
        _keyboard(backend).type_text("café")
    assert excinfo.value.code is ErrorCode.UNICODE_UNSUPPORTED
    assert "key_" not in backend.event_names()


def test_unresolvable_character_is_refused_honestly() -> None:
    """A character with no keycode on this mapping is not approximated."""
    backend = _backend(unresolvable={ord("q")})
    with pytest.raises(BlaxcyError) as excinfo:
        _keyboard(backend).type_text("q")
    assert excinfo.value.code is ErrorCode.UNICODE_UNSUPPORTED


def test_non_ascii_uses_the_clipboard_when_available() -> None:
    """A clipboard turns unsupported text into a real Ctrl+V paste (section 50)."""
    backend = _backend()
    clipboard = FakeClipboard()
    result = _keyboard(backend, clipboard=clipboard).type_text("café")
    assert result.method == "clipboard"
    assert clipboard.set_calls == ["café"]
    assert clipboard.restore_calls == 1
    assert backend.payloads("key_press") == [
        ("key_press", CTRL_KEYCODE),
        ("key_press", V_KEYCODE),
    ]
    assert backend.held_keys == ()


def test_long_ascii_prefers_the_clipboard_but_still_works_without_one() -> None:
    """Length alone switches to the clipboard only when a clipboard exists."""
    text = "abcdef"
    with_clipboard = _keyboard(
        _backend(), settings=InputSettings(ascii_direct_max=2), clipboard=FakeClipboard()
    ).type_text(text)
    assert with_clipboard.method == "clipboard"

    without = _keyboard(_backend(), settings=InputSettings(ascii_direct_max=2)).type_text(text)
    assert without.method == "xtest_direct"


def test_a_failed_clipboard_paste_is_reported() -> None:
    """A clipboard that cannot hold the text yields CLIPBOARD_FAILED."""
    backend = _backend()
    clipboard = FakeClipboard(works=False)
    with pytest.raises(BlaxcyError) as excinfo:
        _keyboard(backend, clipboard=clipboard).type_text("café")
    assert excinfo.value.code is ErrorCode.CLIPBOARD_FAILED
    assert clipboard.restore_calls == 0


# -- Sections 42/55: credential text never reaches the clipboard --------------


def test_sensitive_text_never_uses_the_clipboard() -> None:
    """A password is not published to the clipboard just to make it typable."""
    backend = _backend()
    clipboard = FakeClipboard()
    with pytest.raises(BlaxcyError) as excinfo:
        _keyboard(backend, clipboard=clipboard).type_text("pässwörd", sensitive=True)
    # Refused honestly rather than pasted: the clipboard is readable by every
    # application, and a clipboard manager keeps it in history.
    assert excinfo.value.code is ErrorCode.UNICODE_UNSUPPORTED
    assert excinfo.value.details["sensitive"] is True
    assert clipboard.set_calls == []
    assert clipboard.restore_calls == 0
    assert backend.payloads("key_press") == []


def test_sensitive_ascii_text_is_still_typed_directly() -> None:
    """The refusal is about the clipboard, not about typing credentials at all."""
    backend = _backend()
    clipboard = FakeClipboard()
    result = _keyboard(backend, clipboard=clipboard).type_text("hunter2", sensitive=True)
    assert result.ok is True
    assert result.method == "xtest_direct"
    assert clipboard.set_calls == []
    assert len(backend.payloads("key_press")) == len("hunter2")


def test_sensitive_text_ignores_the_length_shortcut_too() -> None:
    """A long credential is typed directly; length alone never means 'paste it'."""
    backend = _backend()
    clipboard = FakeClipboard()
    long_secret = "abcde" * 10
    result = _keyboard(
        backend,
        settings=InputSettings(ascii_direct_max=2),
        clipboard=clipboard,
    ).type_text(long_secret, sensitive=True)
    assert result.method == "xtest_direct"
    assert clipboard.set_calls == []


def test_a_mid_typing_failure_releases_every_held_key() -> None:
    """Section 52: an unexpected failure never leaves a key held."""
    backend = _backend(raise_on_key_press=2)
    with pytest.raises(BlaxcyError) as excinfo:
        _keyboard(backend).type_text("ab")
    assert excinfo.value.code is ErrorCode.INTERNAL_ERROR
    assert backend.held_keys == ()
    assert "release_all" in backend.event_names()


# -- Section 51: focus guard --------------------------------------------------


def test_focus_guard_blocks_typing_into_an_unfocused_target() -> None:
    """Nothing is injected when the named target is not focused (section 51)."""
    target = _field("f1")
    state = _state(_field("f1", focused=False))
    backend = _backend()
    with pytest.raises(BlaxcyError) as excinfo:
        _keyboard(backend).type_text("a", expected_focus=target, state=state)
    assert excinfo.value.code is ErrorCode.FOCUS_MISMATCH
    assert backend.events == []


def test_focus_guard_blocks_typing_when_the_target_is_absent() -> None:
    """A target missing from the current state blocks typing."""
    target = _field("f1", atspi_path="/p/gone")
    state = _state(_field("f2"))
    with pytest.raises(BlaxcyError) as excinfo:
        _keyboard(_backend()).type_text("a", expected_focus=target, state=state)
    assert excinfo.value.code is ErrorCode.FOCUS_MISMATCH


def test_focus_guard_allows_typing_when_the_target_is_focused() -> None:
    """A present, focused, text-entry target lets typing through."""
    target = _field("f1")
    state = _state(_field("f1"))
    backend = _backend()
    result = _keyboard(backend).type_text("a", expected_focus=target, state=state)
    assert result.ok is True
    assert result.focus is not None and result.focus.ok is True
    assert backend.payloads("key_press") == [("key_press", ord("a"))]


def test_focus_guard_cannot_be_satisfied_without_a_state() -> None:
    """The guard fails closed when there is no observation to check against."""
    check = check_focus(_field("f1"), None)
    assert check.ok is False
    assert check.reason is not None and "ScreenState" in check.reason


def test_focus_guard_rejects_a_non_text_entry_target() -> None:
    """Typing is not silently allowed into a non-text-entry element."""
    button = _field("b1", role=UIRole.BUTTON, focused=True)
    check = check_focus(button, _state(button))
    assert check.ok is False
    assert check.reason is not None and "text-entry" in check.reason


def test_focus_guard_is_skipped_when_no_target_is_named() -> None:
    """Without an expected target there is nothing to guard (caller's choice)."""
    result = _keyboard(_backend()).type_text("a")
    assert result.ok is True
    assert result.focus is None


def test_focus_guard_can_be_disabled_by_configuration() -> None:
    """Disabling the guard only removes the extra check, it adds no bypass."""
    settings = InputSettings(guard_focus=False)
    result = _keyboard(_backend(), settings=settings).type_text(
        "a", expected_focus=_field("f1"), state=None
    )
    assert result.ok is True
    assert result.focus is None


# -- Section 50: named keys and hotkeys ---------------------------------------


def test_named_key_presses_the_named_keysym() -> None:
    """A named key resolves through its X keysym to a keycode."""
    backend = _backend()
    result = _keyboard(backend).press_key("Return")
    assert result.ok is True
    assert backend.payloads("key_press") == [("key_press", keysym_for_name("Return"))]


def test_hotkey_holds_modifiers_around_the_key_and_releases_in_reverse() -> None:
    """Modifier order is deterministic and every modifier is released."""
    backend = _backend()
    result = _keyboard(backend).hotkey("ctrl+shift+t")
    assert result.ok is True
    assert backend.payloads("key_press") == [
        ("key_press", CTRL_KEYCODE),
        ("key_press", SHIFT_KEYCODE),
        ("key_press", ord("t")),
    ]
    assert backend.payloads("key_release") == [
        ("key_release", ord("t")),
        ("key_release", SHIFT_KEYCODE),
        ("key_release", CTRL_KEYCODE),
    ]
    assert backend.held_keys == ()


def test_unknown_key_name_is_reported_not_ignored() -> None:
    """An unknown key name is an honest BACKEND_UNAVAILABLE failure."""
    with pytest.raises(BlaxcyError) as excinfo:
        _keyboard(_backend()).press_key("DefinitelyNotAKey")
    assert excinfo.value.code is ErrorCode.BACKEND_UNAVAILABLE


def test_empty_hotkey_is_a_caller_error() -> None:
    """An empty combination cannot be pressed."""
    with pytest.raises(ValueError):
        _keyboard(_backend()).hotkey("  ")


# -- Hygiene ------------------------------------------------------------------


def test_release_all_and_close_reach_the_backend() -> None:
    """The section 52/63 hooks propagate to the backend."""
    backend = _backend()
    controller = _keyboard(backend)
    controller.release_all()
    controller.close()
    assert backend.event_names() == ["release_all", "release_all", "close"]


def test_keysym_for_char_covers_ascii_and_control_characters() -> None:
    """The keysym helper maps printable ASCII and newline/tab, and nothing else."""
    assert keysym_for_char("a") == ord("a")
    assert keysym_for_char("!") == ord("!")
    assert keysym_for_char("\n") == 0xFF0D
    assert keysym_for_char("\t") == 0xFF09
    assert keysym_for_char("é") is None
    assert keysym_for_char("ab") is None
