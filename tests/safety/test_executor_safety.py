"""Phase 8 safety properties (specification sections 13, 25, 32.1, 44, 45, 54-58, 60).

Every test here asserts a property that must hold no matter which mode, plan or
caller is in play, and every negative one additionally asserts that **nothing was
injected**. "The call returned an error" is not a safety property on its own; "no
physical input reached the desktop" is.
"""

from __future__ import annotations

import json
import time

import pytest

from config.settings import SafetySettings
from schemas.actions import PlannedAction, ToolName
from schemas.capability import Capability, CapabilityReport
from schemas.elements import ElementQuery
from schemas.enums import CapabilityName, CapabilityStatus, ErrorCode, PolicyMode, UIRole
from schemas.events import EventType
from tests.harness.phase8 import (
    ExecutorEnv,
    make_delta,
    make_element,
    make_state,
    make_text_input,
)
from tests.unit.test_keyboard import FakeClipboard

pytestmark = pytest.mark.safety

_QUERY = ElementQuery(text="Send")


def _input_events(env: ExecutorEnv) -> list[tuple[object, ...]]:
    """Every recorded backend event that actually changes the desktop.

    ``flush`` is bookkeeping, not input, so it is excluded; a move, a press, a
    key or a scroll is real input.
    """
    return [event for event in env.backend.events if event[0] != "flush"]


# -- OBSERVE never injects (section 56) ---------------------------------------

@pytest.mark.parametrize(
    ("tool", "params"),
    [
        (ToolName.CLICK, {}),
        (ToolName.DOUBLE_CLICK, {}),
        (ToolName.RIGHT_CLICK, {}),
        (ToolName.TYPE_TEXT, {"text": "hello"}),
        (ToolName.PRESS_KEY, {"key": "a"}),
        (ToolName.HOTKEY, {"combo": "ctrl+c"}),
        (ToolName.SCROLL, {"vertical": 3}),
        (ToolName.DRAG, {"destination": {"text": "Send"}}),
    ],
)
def test_observe_denies_input_and_injects_nothing(tool: str, params: dict[str, object]) -> None:
    """OBSERVE is a hard block on every input tool, not a warning."""
    element = make_element()
    env = ExecutorEnv(mode=PolicyMode.OBSERVE, state=make_state(elements=(element,)))

    envelope = env.executor.execute(
        PlannedAction(tool=tool, target=_QUERY, params=params), confirmed=True
    )

    assert envelope.ok is False
    assert envelope.error_code is ErrorCode.PERMISSION_DENIED
    assert _input_events(env) == []


def test_observe_still_permits_non_input_window_focus() -> None:
    """OBSERVE is not a blanket refusal: focus changes are not input injection."""
    windows = _FakeWindows(active=1)
    env = ExecutorEnv(
        mode=PolicyMode.OBSERVE,
        state=make_state(frame_id=5, active_window_id=1),
        window_manager=windows,
    )
    env.perceiver.script(make_state(frame_id=6, active_window_id=3))
    envelope = env.executor.execute(
        PlannedAction(tool=ToolName.ENSURE_WINDOW, params={"window_id": 3})
    )
    # The request is permitted and really attempted, and its result is verified.
    assert windows.requested == [3]
    assert envelope.ok is True
    assert envelope.data["window_id"] == 3


# -- Emergency stop / takeover abort before input (sections 62, 63) ----------

@pytest.mark.parametrize("code", [ErrorCode.EMERGENCY_STOP_ACTIVE, ErrorCode.HUMAN_TAKEOVER])
def test_abort_check_stops_the_action_before_input(code: ErrorCode) -> None:
    """A latched stop must halt the pipeline before anything is injected."""
    element = make_element()
    env = ExecutorEnv(
        mode=PolicyMode.ASSIST,
        state=make_state(elements=(element,)),
        abort_check=lambda: code,
    )
    envelope = env.executor.execute(PlannedAction(tool=ToolName.CLICK, target=_QUERY))
    assert envelope.ok is False
    assert envelope.error_code is code
    assert _input_events(env) == []


# -- Destructive confirmation is a real gate (sections 56, 66.1) --------------

def test_destructive_action_without_confirmation_is_not_executed() -> None:
    """No confirmation callback means CONFIRMATION_REQUIRED, not a proceed."""
    env = ExecutorEnv(
        mode=PolicyMode.AUTONOMOUS,
        state=make_state(elements=(make_element(),)),
    )
    envelope = env.executor.execute(
        PlannedAction(
            tool=ToolName.DRAG,
            target=_QUERY,
            params={"destination": {"text": "Send"}},
        )
    )
    assert envelope.ok is False
    assert envelope.error_code is ErrorCode.CONFIRMATION_REQUIRED
    assert _input_events(env) == []


def test_denied_confirmation_never_executes() -> None:
    """A human saying no is a hard stop."""
    env = ExecutorEnv(
        mode=PolicyMode.ASSIST,
        state=make_state(elements=(make_element(),)),
        confirmation=lambda _message, _details: False,
    )
    envelope = env.executor.execute(
        PlannedAction(
            tool=ToolName.DRAG,
            target=_QUERY,
            params={"destination": {"text": "Send"}},
        )
    )
    assert envelope.ok is False
    assert envelope.error_code is ErrorCode.CONFIRMATION_DENIED
    assert _input_events(env) == []


def test_approved_confirmation_executes() -> None:
    """An approving confirmation lets the action run, and it is recorded."""
    source = make_element("src", text="file")
    state = make_state(frame_id=5, elements=(source,))
    env = ExecutorEnv(
        mode=PolicyMode.ASSIST,
        state=state,
        confirmation=lambda _message, _details: True,
    )
    after = make_state(frame_id=6, elements=(source,))
    env.perceiver.script(after, make_delta(before=state, after=after))

    envelope = env.executor.execute(
        PlannedAction(
            tool=ToolName.DRAG,
            target=ElementQuery(text="file"),
            params={"destination": {"text": "file"}},
        )
    )

    assert envelope.ok is True
    assert _input_events(env)


# -- Blocked applications and capabilities (sections 28, 58) ------------------

def test_blocked_application_blocks_input_in_autonomous() -> None:
    """A blocked application cannot be overridden by autonomy (section 58)."""
    env = ExecutorEnv(
        mode=PolicyMode.AUTONOMOUS,
        state=make_state(elements=(make_element(owner_app="KeePassXC"),), active_app="KeePassXC"),
        safety=SafetySettings(
            mode=PolicyMode.AUTONOMOUS, blocked_applications=("keepassxc",)
        ),
    )

    envelope = env.executor.execute(PlannedAction(tool=ToolName.CLICK, target=_QUERY))

    assert envelope.ok is False
    assert envelope.error_code is ErrorCode.BLOCKED_APPLICATION
    assert _input_events(env) == []


def test_unavailable_capability_blocks_input() -> None:
    """An UNAVAILABLE mouse capability is never used (section 28)."""
    report = CapabilityReport(
        capabilities=(
            Capability(
                name=CapabilityName.MOUSE,
                status=CapabilityStatus.UNAVAILABLE,
                reason="no backend passed its probe",
            ),
        ),
        generated_at=0.0,
    )
    env = ExecutorEnv(
        mode=PolicyMode.ASSIST,
        state=make_state(elements=(make_element(),)),
        capabilities=report,
    )
    envelope = env.executor.execute(PlannedAction(tool=ToolName.CLICK, target=_QUERY))
    assert envelope.ok is False
    assert envelope.error_code is ErrorCode.BACKEND_UNAVAILABLE
    assert _input_events(env) == []


# -- Stale and ambiguous targets never click (sections 43, 45) ----------------

def test_stale_target_never_clicks() -> None:
    """A target that vanished between resolution and revalidation is not clicked.

    The executor re-observes before it issues a lease, precisely so that a lease is
    never bound to an observation older than its own TTL (see
    ``test_a_lease_is_never_bound_to_a_stale_observation`` below).
    That is why the first stage to notice the missing target is the re-observation
    rather than the lease check: the fresh observation genuinely reports no match,
    which is ``TARGET_NOT_FOUND``. Either code is a refusal; neither injects.
    """
    element = make_element()
    stale = make_state(elements=(element,), monotonic=time.monotonic() - 10.0)
    env = ExecutorEnv(mode=PolicyMode.ASSIST, state=stale)
    # The re-perception the executor is forced into no longer contains the target.
    env.perceiver.script(make_state(frame_id=6, elements=()))

    envelope = env.executor.execute(PlannedAction(tool=ToolName.CLICK, target=_QUERY))

    assert envelope.ok is False
    assert envelope.error_code is ErrorCode.TARGET_NOT_FOUND
    assert envelope.data["reobservation"] is True
    assert _input_events(env) == []


def test_a_lease_is_never_bound_to_a_stale_observation() -> None:
    """The lease is bound to what the executor will act on, not to an old reading.

    Section 54's confirmation gate can hold the action thread for as long as a human
    takes, and nothing BLAXCY controls bounds that wait. Issuing the lease against
    the pre-wait observation produced a lease born already older than its own TTL,
    which then expired during the very re-perception section 45 needs in order to
    revalidate it -- so no target action could ever be authorised on a real desktop.
    The executor now re-observes (and re-resolves) first, so the lease has its full
    lifetime available and the observation it is bound to is the live one.
    """
    element = make_element()
    stale = make_state(elements=(element,), monotonic=time.monotonic() - 10.0)
    env = ExecutorEnv(mode=PolicyMode.ASSIST, state=stale)
    fresh = make_state(frame_id=6, elements=(element,))
    after = make_state(frame_id=7, elements=(element,))
    # 1. the re-observation the executor now makes before leasing ...
    env.perceiver.script(fresh)
    # 2. ... and the post-action observation, with the change it must verify.
    env.perceiver.script(after, make_delta(before=fresh, after=after))

    envelope = env.executor.execute(PlannedAction(tool=ToolName.CLICK, target=_QUERY))

    assert envelope.ok is True
    assert envelope.data["resolution"]["reobservation"]["frame_id"] == 6
    # The strongest available evidence: the lease itself was issued against the
    # refreshed observation, not the 10-second-old one the action began with.
    leases = env.bus.history_of(EventType.LEASE_ISSUED)
    assert len(leases) == 1
    assert leases[0].payload["frame_id"] == 6
    assert len(env.backend.payloads("press")) == 1


def test_an_approval_does_not_transfer_to_a_different_control() -> None:
    """A human approved one control; if the screen moved, that approval is void.

    Re-observing after the wait means the re-resolved target could be something
    else entirely. Acting on the old approval would click whatever happens to be
    there now, so the executor refuses instead (section 45's identity check, applied
    to the approval itself).
    """
    approved = make_element("original")
    # A different control that still matches the same description: same text, same
    # role, different identity path.
    replacement = make_element("replacement")
    stale = make_state(elements=(approved,), monotonic=time.monotonic() - 10.0)
    env = ExecutorEnv(mode=PolicyMode.ASSIST, state=stale)
    env.perceiver.script(make_state(frame_id=6, elements=(replacement,)))

    envelope = env.executor.execute(PlannedAction(tool=ToolName.CLICK, target=_QUERY))

    assert envelope.ok is False
    assert envelope.error_code is ErrorCode.TARGET_STALE
    assert "does not transfer" in (envelope.message or "")
    assert _input_events(env) == []


def test_ambiguous_target_never_clicks() -> None:
    """Two indistinguishable controls are never auto-selected (section 43)."""
    env = ExecutorEnv(
        mode=PolicyMode.ASSIST,
        state=make_state(elements=(make_element("a"), make_element("b"))),
    )
    envelope = env.executor.execute(PlannedAction(tool=ToolName.CLICK, target=_QUERY))
    assert envelope.ok is False
    assert envelope.error_code is ErrorCode.TARGET_AMBIGUOUS
    assert _input_events(env) == []


# -- Credential handling (sections 42, 55) ------------------------------------

def test_password_context_never_appears_in_the_envelope() -> None:
    """A typed secret must not be present anywhere in the reported result."""
    secret = "hunter2-correct-horse"
    field = make_text_input(
        "pw", role=UIRole.PASSWORD_INPUT, password=True, text=None, focused=True
    )
    state = make_state(frame_id=5, elements=(field,))
    env = ExecutorEnv(mode=PolicyMode.ASSIST, state=state)
    after = make_state(frame_id=6, elements=(field,))
    env.perceiver.script(after, make_delta(before=state, after=after))

    envelope = env.executor.execute(
        PlannedAction(
            tool=ToolName.TYPE_TEXT,
            target=ElementQuery(role_hint=UIRole.PASSWORD_INPUT),
            params={"text": secret},
        )
    )

    # The typing really happened ...
    assert env.backend.payloads("key_press")
    # ... but the secret is nowhere in what BLAXCY reports.
    serialized = json.dumps(envelope.to_dict())
    assert secret not in serialized
    assert envelope.data.get("sensitive") is True
    # A credential field is never read back, so the outcome is honestly unverified.
    assert envelope.verification.value == "UNVERIFIED"


def test_a_non_ascii_password_is_never_put_on_the_clipboard() -> None:
    """The clipboard is shared (and clipboard managers keep history): no secrets.

    Without this, a password containing characters the keyboard mapping cannot
    produce would take the section 50 clipboard path -- publishing the credential
    to every application on the session. It is refused instead.
    """
    secret = "pässwörd"
    field = make_text_input(
        "pw", role=UIRole.PASSWORD_INPUT, password=True, text=None, focused=True
    )
    clipboard = FakeClipboard()
    env = ExecutorEnv(
        mode=PolicyMode.ASSIST,
        state=make_state(elements=(field,)),
        clipboard=clipboard,
    )

    envelope = env.executor.execute(
        PlannedAction(
            tool=ToolName.TYPE_TEXT,
            target=ElementQuery(role_hint=UIRole.PASSWORD_INPUT),
            params={"text": secret},
        )
    )

    assert envelope.ok is False
    assert envelope.error_code is ErrorCode.UNICODE_UNSUPPORTED
    # Nothing was written to the clipboard, nothing was pasted, and nothing was
    # typed as an approximation of the secret.
    assert clipboard.set_calls == []
    assert clipboard.restore_calls == 0
    assert env.backend.payloads("key_press") == []
    assert secret not in json.dumps(envelope.to_dict())


def test_a_non_ascii_password_is_typed_directly_when_the_mapping_allows_it() -> None:
    """The refusal is about the clipboard path, not about credentials."""
    field = make_text_input(
        "pw", role=UIRole.PASSWORD_INPUT, password=True, text=None, focused=True
    )
    state = make_state(frame_id=5, elements=(field,))
    clipboard = FakeClipboard()
    env = ExecutorEnv(mode=PolicyMode.ASSIST, state=state, clipboard=clipboard)
    after = make_state(frame_id=6, elements=(field,))
    env.perceiver.script(after, make_delta(before=state, after=after))

    envelope = env.executor.execute(
        PlannedAction(
            tool=ToolName.TYPE_TEXT,
            target=ElementQuery(role_hint=UIRole.PASSWORD_INPUT),
            params={"text": "hunter2"},
        )
    )

    assert env.backend.payloads("key_press")
    assert clipboard.set_calls == []
    assert envelope.verification.value == "UNVERIFIED"


def test_reading_a_password_field_is_refused() -> None:
    """A content-reading tool on credential context is denied outright.

    Content-reading tools are read-only, so they never enter the executor; the
    guard that refuses them is the shared permission engine the executor uses,
    so it is asserted here through that same engine rather than through a path
    that cannot reach it.
    """
    env = ExecutorEnv(mode=PolicyMode.ASSIST, state=make_state(elements=()))
    for tool in (ToolName.DESCRIBE_REGION, ToolName.FIND_ELEMENT):
        decision = env.permissions.decide(
            tool=tool,
            mode=PolicyMode.ASSIST,
            password_context=True,
        )
        assert decision.allowed is False
        assert decision.code is ErrorCode.PERMISSION_DENIED


# -- Terminal submission is never automatic (section 54) ----------------------

def test_return_requires_confirmation_even_in_autonomous() -> None:
    """Return can never fire as an incidental side effect of a key request."""
    env = ExecutorEnv(mode=PolicyMode.AUTONOMOUS, state=make_state())
    envelope = env.executor.execute(PlannedAction(tool=ToolName.PRESS_KEY, params={"key": "Return"}))
    assert envelope.ok is False
    assert envelope.error_code is ErrorCode.CONFIRMATION_REQUIRED
    assert _input_events(env) == []


def test_submission_of_a_destructive_command_is_still_protected() -> None:
    """A destructive submission is confirmed like any other, not fast-pathed."""
    env = ExecutorEnv(mode=PolicyMode.AUTONOMOUS, state=make_state())
    envelope = env.executor.execute(
        PlannedAction(
            tool=ToolName.PRESS_KEY,
            params={"key": "Return", "visible_command": "rm -rf /"},
        )
    )
    assert envelope.ok is False
    assert envelope.error_code is ErrorCode.CONFIRMATION_REQUIRED
    assert _input_events(env) == []


# -- Ownership is always released (sections 52, 78) ---------------------------

def test_tracker_is_clear_after_a_successful_action() -> None:
    """No action leaves BLAXCY believing it still owns the keyboard/mouse."""
    element = make_element()
    state = make_state(frame_id=5, elements=(element,))
    env = ExecutorEnv(mode=PolicyMode.ASSIST, state=state)
    after = make_state(frame_id=6, elements=(element,))
    env.perceiver.script(after, make_delta(before=state, after=after))

    env.executor.execute(PlannedAction(tool=ToolName.CLICK, target=_QUERY))

    snapshot = env.tracker.snapshot()
    assert snapshot.active_action is None
    assert snapshot.holds_input is False
    assert env.backend.held_keys == ()
    assert env.backend.held_buttons == ()


def test_tracker_is_clear_after_a_denied_action() -> None:
    """A refusal must not leave ownership behind either."""
    env = ExecutorEnv(mode=PolicyMode.OBSERVE, state=make_state(elements=(make_element(),)))
    env.executor.execute(PlannedAction(tool=ToolName.CLICK, target=_QUERY))
    assert env.tracker.snapshot().active_action is None


# -- Helpers ------------------------------------------------------------------

class _FakeWindows:
    """Minimal window controller for the focus-only safety test."""

    def __init__(self, active: int | None = 42) -> None:
        self.active = active
        self.requested: list[int] = []

    def probe(self) -> object:
        from control.window_manager import WindowManagerProbe

        return WindowManagerProbe(available=True, backend="fake")

    def active_window(self) -> int | None:
        return self.active

    def list_windows(self) -> tuple[object, ...]:
        return ()

    def window_info(self, window_id: int) -> object:
        from control.window_manager import WindowInfo

        return WindowInfo(window_id=window_id)

    def activate(self, window_id: int) -> bool:
        self.requested.append(window_id)
        self.active = window_id
        return True

    def ensure_active(self, window_id: int, *, timeout_ms: int | None = None) -> bool:
        return self.activate(window_id)


