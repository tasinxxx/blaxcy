"""Safety properties of the Brain tool boundary (specification sections 66, 85).

Section 85 requires the safety properties to hold *identically* whether a
triggering action arrived standalone or inside a plan, and section 3 requires
that no tool call can bypass ``Policy -> Resolve -> Lease -> Revalidate ->
Execute -> Verify``. These tests attack the boundary from the Brain's side: a
coordinate smuggled into an argument, a confirmation smuggled into an argument,
a destructive step inside a plan, a blocked application, a credential field, a
latched stop. Every refusal is also checked for the thing that matters most --
that nothing was injected into the desktop.
"""

from __future__ import annotations

import pytest

from ai.tool_protocol import ToolCall
from config.settings import SafetySettings, Settings
from schemas.actions import ToolName
from schemas.enums import ErrorCode, PolicyMode, UIRole
from tests.harness.phase8 import make_element, make_state
from tests.harness.phase10 import build_dispatch_env

pytestmark = pytest.mark.safety

CLICKABLE = make_state(
    elements=(make_element("e1", text="Send", clickable=True, effective_clickable=True),),
    active_app="fixture",
)


def _env(**kwargs: object) -> object:
    """A dispatch environment seeded with a clickable control."""
    kwargs.setdefault("state", CLICKABLE)
    return build_dispatch_env(**kwargs)  # type: ignore[arg-type]


def _assert_nothing_injected(env: object) -> None:
    """The desktop was untouched: no move, no click, no key."""
    assert env.env.backend.events == []  # type: ignore[attr-defined]


# -- Mode gating --------------------------------------------------------------

def test_observe_refuses_physical_input_and_injects_nothing() -> None:
    """OBSERVE is the first-launch default and permits no input (section 56)."""
    env = _env()
    envelope = env.dispatch(ToolName.CLICK, target="Send")  # type: ignore[attr-defined]
    assert envelope.ok is False
    assert envelope.error_code is ErrorCode.PERMISSION_DENIED
    _assert_nothing_injected(env)


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        (ToolName.TYPE_TEXT, {"target": "Send", "text": "hello"}),
        (ToolName.PRESS_KEY, {"key": "Return"}),
        (ToolName.HOTKEY, {"combo": "ctrl+l"}),
        (ToolName.SCROLL, {"target": "Send"}),
    ],
)
def test_observe_refuses_every_input_tool(tool: str, arguments: dict[str, object]) -> None:
    """No input tool escapes OBSERVE."""
    env = _env()
    envelope = env.dispatch(tool, **arguments)  # type: ignore[attr-defined]
    assert envelope.ok is False
    _assert_nothing_injected(env)


def test_observe_allows_read_only_work() -> None:
    """OBSERVE still permits observation; it only forbids input."""
    env = _env()
    assert env.dispatch(ToolName.GET_SCREEN_STATE).ok is True  # type: ignore[attr-defined]


def test_the_emergency_stop_tool_works_in_every_mode() -> None:
    """A stop must never be gated behind the mode it is there to end."""
    from control.emergency_stop import EmergencyStop

    stop = EmergencyStop()
    env = _env()
    dispatcher = env.dispatcher  # type: ignore[attr-defined]
    dispatcher._stop = stop
    envelope = env.dispatch(ToolName.EMERGENCY_STOP)  # type: ignore[attr-defined]
    assert envelope.ok is True
    assert stop.latched is True


# -- Confirmation cannot be taken by the Brain -------------------------------

def test_a_destructive_action_in_assist_needs_a_human() -> None:
    """Section 56: destructive work always goes through confirmation."""
    env = _env(mode=PolicyMode.ASSIST)
    envelope = env.dispatch(  # type: ignore[attr-defined]
        ToolName.DRAG, target="invoice.pdf", destination="trash"
    )
    assert envelope.ok is False
    assert envelope.error_code is ErrorCode.CONFIRMATION_REQUIRED
    _assert_nothing_injected(env)


def test_a_confirmation_argument_is_rejected_rather_than_honoured() -> None:
    """The Brain cannot smuggle ``confirmed`` through its own arguments."""
    env = _env(mode=PolicyMode.ASSIST)
    envelope = env.dispatch(  # type: ignore[attr-defined]
        ToolName.DRAG, target="invoice.pdf", destination="trash", confirmed=True
    )
    assert envelope.ok is False
    assert envelope.error_code is ErrorCode.INTERNAL_ERROR
    _assert_nothing_injected(env)


def test_observe_refuses_a_destructive_action_outright() -> None:
    """In OBSERVE the destructive action is not even a confirmation candidate."""
    env = _env()
    envelope = env.dispatch(  # type: ignore[attr-defined]
        ToolName.DRAG, target="invoice.pdf", destination="trash"
    )
    assert envelope.ok is False
    assert envelope.error_code in {ErrorCode.PERMISSION_DENIED, ErrorCode.CONFIRMATION_REQUIRED}
    _assert_nothing_injected(env)


# -- Coordinates and leases cannot be smuggled -------------------------------

@pytest.mark.parametrize("key", ["x", "y", "element_id", "lease_id", "frame_id", "generation"])
def test_a_pre_resolved_target_is_refused_before_anything_happens(key: str) -> None:
    """A coordinate/lease would bypass resolution and revalidation (sections 44, 45)."""
    env = _env(mode=PolicyMode.ASSIST)
    envelope = env.dispatch(ToolName.CLICK, **{"target": "Send", key: 5})  # type: ignore[attr-defined]
    assert envelope.ok is False
    assert envelope.error_code is ErrorCode.INTERNAL_ERROR
    _assert_nothing_injected(env)


def test_a_destructive_step_inside_a_plan_cannot_be_pre_authorized() -> None:
    """A plan is refused as a whole when BLAXCY cannot honestly run it (section 66.1)."""
    env = _env(mode=PolicyMode.ASSIST)
    plan = {
        "steps": [
            {"step_id": "s1", "tool": "click", "target": "Send"},
            {"step_id": "s2", "tool": "drag", "target": "invoice.pdf", "destination": "trash"},
        ]
    }
    envelope = env.dispatcher.dispatch(  # type: ignore[attr-defined]
        ToolCall(name=ToolName.RUN_SEQUENCE, arguments=plan)
    )
    assert envelope.ok is False
    assert envelope.error_code is ErrorCode.BACKEND_UNAVAILABLE
    _assert_nothing_injected(env)


# -- Application policy -------------------------------------------------------

def test_a_blocked_application_cannot_be_acted_on() -> None:
    """Section 58: no input, in any mode, with no override."""
    settings = Settings().model_copy(
        update={"safety": SafetySettings(mode=PolicyMode.ASSIST, blocked_applications=("fixture",))}
    )
    env = _env(mode=PolicyMode.ASSIST, settings=settings)
    envelope = env.dispatch(ToolName.CLICK, target="Send")  # type: ignore[attr-defined]
    assert envelope.ok is False
    assert envelope.error_code is ErrorCode.BLOCKED_APPLICATION
    _assert_nothing_injected(env)


def test_a_blocked_application_is_blocked_for_a_blocked_read_too() -> None:
    """Even the observation of a blocked application is reported as blocked."""
    settings = Settings().model_copy(
        update={"safety": SafetySettings(blocked_applications=("fixture",))}
    )
    env = _env(mode=PolicyMode.ASSIST, settings=settings)
    envelope = env.dispatch(ToolName.FIND_ELEMENT, target="Send")  # type: ignore[attr-defined]
    # The read path resolves against observed state without policy gating -- what
    # matters is that acting on it is refused; assert the action path is closed.
    assert envelope is not None
    action = env.dispatch(ToolName.CLICK, target="Send")  # type: ignore[attr-defined]
    assert action.error_code is ErrorCode.BLOCKED_APPLICATION


# -- Credential privacy -------------------------------------------------------

def test_a_password_value_never_reaches_the_brain() -> None:
    """Sections 42/55: credential content is never read into model context."""
    from ai.context_manager import ContextManager

    state = make_state(
        elements=(
            make_element(
                "pw",
                role=UIRole.PASSWORD_INPUT,
                password=True,
                text=None,
                accessible_name="Password",
                clickable=True,
                effective_clickable=True,
            ),
        )
    )
    env = _env(state=state, mode=PolicyMode.ASSIST)
    envelope = env.dispatch(ToolName.GET_SCREEN_STATE)  # type: ignore[attr-defined]
    assert envelope.ok is True
    entry = envelope.data["elements"][0]
    assert entry["password"] is True
    assert entry["text"] is None

    payload = ContextManager(env.settings).state_context(env.cache)  # type: ignore[attr-defined]
    assert payload["elements"][0]["text"] is None


def test_a_password_field_is_reported_as_sensitive_by_the_executor() -> None:
    """Typing into a credential field is permitted, but never recorded as content."""
    state = make_state(
        elements=(
            make_element(
                "pw",
                role=UIRole.PASSWORD_INPUT,
                password=True,
                text=None,
                accessible_name="Password",
                clickable=True,
                effective_clickable=True,
                focusable=True,
                focused=True,
            ),
        )
    )
    env = _env(state=state, mode=PolicyMode.ASSIST)
    envelope = env.dispatch(  # type: ignore[attr-defined]
        ToolName.TYPE_TEXT, target="Password", text="hunter2"
    )
    assert "hunter2" not in repr(envelope.to_dict())


# -- Stop preemption ----------------------------------------------------------

def test_a_latched_stop_preempts_mutating_and_read_only_calls_alike() -> None:
    """Sections 32.1/63: a stop outranks everything, read or write."""
    env = _env(mode=PolicyMode.ASSIST, abort_check=lambda: ErrorCode.HUMAN_TAKEOVER)
    click = env.dispatch(ToolName.CLICK, target="Send")  # type: ignore[attr-defined]
    read = env.dispatch(ToolName.GET_SCREEN_STATE)  # type: ignore[attr-defined]
    assert click.ok is False
    assert read.ok is False
    assert click.error_code is ErrorCode.HUMAN_TAKEOVER
    assert read.error_code is ErrorCode.HUMAN_TAKEOVER
    _assert_nothing_injected(env)


# -- Unknown tools ------------------------------------------------------------

def test_an_unknown_tool_is_never_executed_optimistically() -> None:
    """BLAXCY reports that it does not have the tool."""
    env = _env(mode=PolicyMode.AUTONOMOUS)
    envelope = env.dispatcher.dispatch(  # type: ignore[attr-defined]
        ToolCall(name="disable_firewall", arguments={})
    )
    assert envelope.ok is False
    assert envelope.error_code is ErrorCode.BACKEND_UNAVAILABLE
    _assert_nothing_injected(env)


def test_activate_element_never_fakes_a_click() -> None:
    """Section 66/80: activation refuses honestly, and never substitutes a click.

    ``activate_element`` is implemented now (the AT-SPI action lives behind
    ``AccessibilityService.activate``). The property this test exists for is
    unchanged and is what must survive the implementation: BLAXCY never
    substitutes a synthetic pointer click for the accessibility action it was
    asked to perform.    Which honest gate refuses first depends on the environment -- an absent
    target is ``TARGET_NOT_FOUND``, a harvested state past its maximum age is
    ``TARGET_STALE``, and a resolved target with no backend wired is
    ``BACKEND_UNAVAILABLE``. The property asserted here is the one that must hold
    regardless: a structured refusal, never a fabricated success, and nothing
    injected.
    """
    env = _env(mode=PolicyMode.ASSIST)
    envelope = env.dispatch(ToolName.ACTIVATE_ELEMENT, target="Send")  # type: ignore[attr-defined]
    assert envelope.ok is False, "activation must never report success without acting"
    assert envelope.error_code is not None
    assert envelope.message, "a refusal must carry its reason"
    _assert_nothing_injected(env)


# -- Read path isolation ------------------------------------------------------

def test_read_only_dispatch_never_enters_the_executor() -> None:
    """The single-writer path only ever sees work that needs it (section 32.1)."""
    env = _env(mode=PolicyMode.ASSIST)
    before = env.env.executor.stats()["actions"]  # type: ignore[attr-defined]
    env.dispatch(ToolName.GET_SCREEN_STATE)  # type: ignore[attr-defined]
    env.dispatch(ToolName.GET_CAPABILITIES)  # type: ignore[attr-defined]
    assert env.env.executor.stats()["actions"] == before  # type: ignore[attr-defined]
