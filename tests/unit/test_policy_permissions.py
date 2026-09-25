"""Permission decisions (specification sections 13, 54-58).

These tests pin the mode/class matrix, the destructive-confirmation rule and the
guard ordering. The most important property is that OBSERVE cannot inject input
by any route, including one the caller forgot to consider.
"""

from __future__ import annotations

import pytest

from config.settings import SafetySettings, Settings
from core.calibration import Calibration, MonitorCalibration
from policy.action_classes import (
    performs_physical_input,
    requires_exclusive_execution,
    sequence_gate_class,
)
from policy.permissions import PermissionEngine, allowed_tools_for, is_terminal_submit, mode_allows
from schemas.actions import ToolName
from schemas.enums import ActionClass, ErrorCode, PolicyMode
from schemas.geometry import Affine2D, MonitorGeometry, MonitorLayout


def _engine(**safety: object) -> PermissionEngine:
    """A permission engine over a configuration with the given safety overrides."""
    settings = Settings(safety=SafetySettings.model_validate(safety))
    return PermissionEngine(settings)


def _disarmed_calibration() -> Calibration:
    """A calibration too thin to allow input (section 31)."""
    return Calibration(
        layout=MonitorLayout(
            monitors=(MonitorGeometry(monitor_id=0, width=1920, height=1080, is_primary=True),)
        ),
        monitors=(
            MonitorCalibration(
                monitor_id=0,
                transform=Affine2D.identity(),
                sample_count=1,
                max_residual_px=0.0,
                tolerance_px=1.0,
            ),
        ),
    )


# -- Mode / action-class matrix (sections 56, 57) -----------------------------

@pytest.mark.parametrize("tool", [ToolName.CLICK, ToolName.TYPE_TEXT, ToolName.PRESS_KEY, ToolName.DRAG])
def test_observe_refuses_input_tools(tool: str) -> None:
    """OBSERVE denies mouse/keyboard injection and drag (section 56)."""
    decision = _engine(mode=PolicyMode.OBSERVE).decide(tool=tool, mode=PolicyMode.OBSERVE)
    assert decision.allowed is False
    assert decision.code is ErrorCode.PERMISSION_DENIED


def test_observe_refuses_scroll_because_it_moves_the_pointer() -> None:
    """Scroll is navigational but still injects mouse input."""
    decision = _engine(mode=PolicyMode.OBSERVE).decide(tool=ToolName.SCROLL, mode=PolicyMode.OBSERVE)
    assert decision.allowed is False


def test_observe_permits_non_input_navigation_and_read_only() -> None:
    """Focus changes and read-only work are not input injection."""
    engine = _engine(mode=PolicyMode.OBSERVE)
    assert engine.decide(tool=ToolName.ENSURE_WINDOW, mode=PolicyMode.OBSERVE).allowed is True
    assert engine.decide(tool=ToolName.FIND_ELEMENT, mode=PolicyMode.OBSERVE).allowed is True


def test_assist_and_autonomous_allow_mutating_input() -> None:
    """ASSIST and AUTONOMOUS permit ordinary navigation and input."""
    for mode in (PolicyMode.ASSIST, PolicyMode.AUTONOMOUS):
        decision = _engine(mode=mode).decide(tool=ToolName.CLICK, mode=mode)
        assert decision.allowed is True, mode


def test_drag_requires_confirmation_because_it_is_destructive() -> None:
    """An unresolved drop target defaults to destructive (sections 49, 56)."""
    engine = _engine(mode=PolicyMode.ASSIST)
    decision = engine.decide(tool=ToolName.DRAG, mode=PolicyMode.ASSIST)
    assert decision.allowed is False
    assert decision.requires_confirmation is True
    assert decision.code is ErrorCode.CONFIRMATION_REQUIRED


def test_drag_proceeds_once_confirmed() -> None:
    """A confirmation turns the ask into a permission."""
    engine = _engine(mode=PolicyMode.ASSIST)
    decision = engine.decide(tool=ToolName.DRAG, mode=PolicyMode.ASSIST, confirmed=True)
    assert decision.allowed is True


def test_destructive_confirmation_is_required_in_autonomous_too() -> None:
    """Section 56 does not exempt AUTONOMOUS from destructive confirmation."""
    decision = _engine(mode=PolicyMode.AUTONOMOUS).decide(tool=ToolName.DRAG, mode=PolicyMode.AUTONOMOUS)
    assert decision.requires_confirmation is True


def test_mode_allows_is_consistent_with_the_engine() -> None:
    """The standalone helper matches the engine's own view."""
    assert mode_allows(PolicyMode.OBSERVE, tool=ToolName.CLICK, action_class=ActionClass.MUTATING) is False
    assert mode_allows(PolicyMode.ASSIST, tool=ToolName.CLICK, action_class=ActionClass.MUTATING) is True


def test_run_sequence_needs_an_explicit_gate_class() -> None:
    """A container tool has no class of its own (section 57)."""
    engine = _engine(mode=PolicyMode.ASSIST)
    with pytest.raises(ValueError):
        engine.decide(tool=ToolName.RUN_SEQUENCE, mode=PolicyMode.ASSIST)
    decision = engine.decide(
        tool=ToolName.RUN_SEQUENCE,
        mode=PolicyMode.ASSIST,
        action_class=ActionClass.DESTRUCTIVE,
    )
    assert decision.requires_confirmation is True


# -- Guards inside the engine (sections 31, 42, 55, 58) -----------------------

def test_blocked_application_is_refused_in_every_mode() -> None:
    """Section 58 blocks regardless of mode, including AUTONOMOUS."""
    engine = _engine(mode=PolicyMode.AUTONOMOUS, blocked_applications=("keepassxc",))
    for mode in (PolicyMode.OBSERVE, PolicyMode.ASSIST, PolicyMode.AUTONOMOUS):
        decision = engine.decide(tool=ToolName.CLICK, mode=mode, application=("KeePassXC",))
        assert decision.allowed is False
        assert decision.code is ErrorCode.BLOCKED_APPLICATION


def test_reading_a_password_field_is_denied_by_the_engine() -> None:
    """The credential guard runs before the mode gate."""
    decision = _engine().decide(
        tool=ToolName.DESCRIBE_REGION, mode=PolicyMode.ASSIST, password_context=True
    )
    assert decision.allowed is False
    assert decision.code is ErrorCode.PERMISSION_DENIED


def test_typing_into_a_password_field_is_permitted_and_marked_sensitive() -> None:
    """Typing a credential is allowed; its content is flagged for redaction."""
    decision = _engine(mode=PolicyMode.ASSIST).decide(
        tool=ToolName.TYPE_TEXT, mode=PolicyMode.ASSIST, password_context=True
    )
    assert decision.allowed is True
    assert decision.sensitive is True


def test_disarmed_calibration_blocks_input() -> None:
    """Section 31: input stays disarmed while calibration is incomplete."""
    decision = _engine(mode=PolicyMode.ASSIST).decide(
        tool=ToolName.CLICK, mode=PolicyMode.ASSIST, calibration=_disarmed_calibration()
    )
    assert decision.allowed is False
    assert decision.code is ErrorCode.CALIBRATION_FAILED


# -- Terminal separation inside the engine (section 54) -----------------------

def test_terminal_submission_requires_confirmation() -> None:
    """A submission is never auto-approved, whatever the mode."""
    engine = _engine(mode=PolicyMode.AUTONOMOUS)
    decision = engine.decide(
        tool=ToolName.PRESS_KEY,
        mode=PolicyMode.AUTONOMOUS,
        terminal_submit=True,
        terminal_command="ls",
    )
    assert decision.allowed is False
    assert decision.requires_confirmation is True


def test_terminal_typing_is_refused_in_observe() -> None:
    """OBSERVE does not type into a terminal."""
    decision = _engine().decide(
        tool=ToolName.TYPE_TEXT,
        mode=PolicyMode.OBSERVE,
        terminal_command="ls",
    )
    assert decision.allowed is False


def test_is_terminal_submit_recognises_keys_and_hotkeys() -> None:
    """Return and Return-bearing hotkeys route through the submission policy."""
    assert is_terminal_submit(ToolName.PRESS_KEY, {"key": "Return"}) is True
    assert is_terminal_submit(ToolName.PRESS_KEY, {"key": "a"}) is False
    assert is_terminal_submit(ToolName.HOTKEY, {"combo": "ctrl+m"}) is True
    assert is_terminal_submit(ToolName.HOTKEY, {"combo": "ctrl+c"}) is False
    assert is_terminal_submit(ToolName.PRESS_KEY, {"key": "Enter", "modifiers": ["shift"]}) is True


# -- Reporting ----------------------------------------------------------------

def test_allowed_tools_for_observe_excludes_input() -> None:
    """The capability/GUI view of OBSERVE matches what the engine enforces."""
    allowed = allowed_tools_for(PolicyMode.OBSERVE)
    assert ToolName.CLICK not in allowed
    assert ToolName.TYPE_TEXT not in allowed
    assert ToolName.DRAG not in allowed
    assert ToolName.SCROLL not in allowed
    assert ToolName.FIND_ELEMENT in allowed
    assert ToolName.ENSURE_WINDOW in allowed


def test_allowed_tools_for_assist_includes_input() -> None:
    """ASSIST exposes the input tools (destructive ones still confirm)."""
    allowed = allowed_tools_for(PolicyMode.ASSIST)
    assert ToolName.CLICK in allowed
    assert ToolName.TYPE_TEXT in allowed
    assert ToolName.DRAG in allowed


# -- Classification (sections 32.1, 57) ---------------------------------------

def test_requires_exclusive_execution_follows_the_section_32_1_rule() -> None:
    """Only input-injecting work needs the single-writer lock."""
    # Mutating/destructive work always does.
    assert requires_exclusive_execution(ToolName.CLICK) is True
    assert requires_exclusive_execution(ToolName.DRAG) is True
    # A navigational action that injects input does too.
    assert requires_exclusive_execution(ToolName.SCROLL) is True
    # A container is always exclusive while it runs.
    assert requires_exclusive_execution(ToolName.RUN_SEQUENCE) is True
    # Focus changes and read-only work do not.
    assert requires_exclusive_execution(ToolName.ENSURE_WINDOW) is False
    assert requires_exclusive_execution(ToolName.GET_SCREEN_STATE) is False
    assert requires_exclusive_execution(ToolName.FIND_ELEMENT) is False


def test_performs_physical_input_is_about_injection_only() -> None:
    """Focus changes are not physical input; scroll is."""
    assert performs_physical_input(ToolName.CLICK) is True
    assert performs_physical_input(ToolName.SCROLL) is True
    assert performs_physical_input(ToolName.ENSURE_WINDOW) is False
    assert performs_physical_input(ToolName.GET_CAPABILITIES) is False


def test_unknown_tool_is_refused_rather_than_defaulted() -> None:
    """A new tool must declare its class; it never inherits a permissive guess."""
    with pytest.raises(ValueError):
        performs_physical_input("not_a_real_tool")


def test_sequence_gate_class_is_the_most_restrictive_step() -> None:
    """A plan is gated as its most dangerous step (section 57)."""
    assert sequence_gate_class([ToolName.CLICK, ToolName.PRESS_KEY]) is ActionClass.MUTATING
    assert sequence_gate_class([ToolName.CLICK, ToolName.DRAG]) is ActionClass.DESTRUCTIVE
    assert sequence_gate_class([ToolName.SCROLL, ToolName.DRAG]) is ActionClass.DESTRUCTIVE
    assert sequence_gate_class([ToolName.GET_SCREEN_STATE]) is ActionClass.READ_ONLY
    assert sequence_gate_class([]) is ActionClass.READ_ONLY


def test_sequence_gate_class_ignores_run_sequence_recursion() -> None:
    """A sequence cannot contain a nested sequence (section 66.1)."""
    with pytest.raises(ValueError):
        sequence_gate_class([ToolName.RUN_SEQUENCE])
