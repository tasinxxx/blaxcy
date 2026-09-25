"""Tool declaration and argument parsing (specification section 66).

The parser is a safety boundary, not a convenience: these tests are mostly about
what it *refuses*. A request carrying a coordinate, a lease, an unexpected
argument or an unknown tool must be rejected as written rather than partially
honoured, because a partially honoured request is how a Brain would end up
clicking something it did not ask for.
"""

from __future__ import annotations

import pytest

from ai.tool_protocol import (
    READ_ONLY_TOOLS,
    TOOL_PARAMETER_SCHEMAS,
    ToolProtocolError,
    build_planned_action,
    element_query,
    parse_tool_arguments,
    protocol_snapshot,
    tool_declarations,
)
from schemas.actions import REQUIRED_TOOLS, ToolName, action_class_for
from schemas.enums import ActionClass, ErrorCode, UIRole

# -- Declarations -------------------------------------------------------------

def test_every_required_tool_is_declared_with_a_schema() -> None:
    """The advertised set and the accepted set are the same set."""
    declared = {declaration.name for declaration in tool_declarations()}
    assert declared == set(REQUIRED_TOOLS)
    assert set(TOOL_PARAMETER_SCHEMAS) == set(REQUIRED_TOOLS)


def test_declarations_are_ordered_and_described() -> None:
    """Declarations are stable, unique and carry real guidance."""
    declarations = tool_declarations()
    names = [declaration.name for declaration in declarations]
    assert len(names) == len(set(names))
    assert names == [declaration.name for declaration in declarations]
    for declaration in declarations:
        assert declaration.description
        assert declaration.parameters["type"] == "object"
        assert declaration.parameters["additionalProperties"] is False
        # A no-argument tool (get_capabilities) declares an empty property set;
        # that is still a declaration, and still rejects any argument.
        assert "properties" in declaration.parameters


def test_run_sequence_is_declared_but_is_not_a_step_tool() -> None:
    """Sequences do not nest, so ``run_sequence`` is absent from the step set."""
    from schemas.actions import SEQUENCE_STEP_TOOLS

    assert ToolName.RUN_SEQUENCE in TOOL_PARAMETER_SCHEMAS
    assert ToolName.RUN_SEQUENCE not in SEQUENCE_STEP_TOOLS


def test_protocol_snapshot_lists_tools_without_secrets() -> None:
    """The snapshot is loggable: names and limits only."""
    snapshot = protocol_snapshot()
    assert set(snapshot["tools"]) == set(REQUIRED_TOOLS)
    assert set(snapshot["read_only_tools"]) == set(READ_ONLY_TOOLS)
    assert set(snapshot) == {"tools", "read_only_tools", "max_context_elements"}


# -- Action parsing -----------------------------------------------------------

def test_click_parses_into_a_resolvable_planned_action() -> None:
    """A click target is a description, and its class is derived."""
    planned = build_planned_action(ToolName.CLICK, {"target": "Send", "role": "BUTTON"})
    assert planned.tool is ToolName.CLICK
    assert planned.action_class is ActionClass.MUTATING
    assert planned.target is not None
    assert planned.target.text == "Send"
    assert planned.target.role_hint is UIRole.BUTTON


def test_type_text_requires_and_carries_literal_text() -> None:
    """The typed text travels in params, never merged into the target query."""
    planned = build_planned_action(
        ToolName.TYPE_TEXT, {"target": "Search", "text": "hello world"}
    )
    assert planned.params["text"] == "hello world"
    assert planned.target is not None
    assert planned.target.text == "Search"


def test_type_text_without_text_is_refused() -> None:
    """Typing nothing is not a no-op request, it is a malformed one."""
    with pytest.raises(ToolProtocolError) as excinfo:
        build_planned_action(ToolName.TYPE_TEXT, {"target": "Search"})
    assert "text" in excinfo.value.message


def test_press_key_accepts_a_list_or_a_plus_string_of_modifiers() -> None:
    """Both shapes the Brain may emit mean the same key press."""
    from_list = build_planned_action(
        ToolName.PRESS_KEY, {"key": "a", "modifiers": ["ctrl", "shift"]}
    )
    from_string = build_planned_action(
        ToolName.PRESS_KEY, {"key": "a", "modifiers": "ctrl+shift"}
    )
    assert from_list.params["modifiers"] == ["ctrl", "shift"]
    assert from_string.params["modifiers"] == ["ctrl", "shift"]


def test_hotkey_normalizes_a_list_into_a_combo() -> None:
    """A list combo becomes the canonical '+'-joined string."""
    planned = build_planned_action(ToolName.HOTKEY, {"combo": ["ctrl", "l"]})
    assert planned.params["combo"] == "ctrl+l"


def test_scroll_defaults_are_explicit() -> None:
    """Unset scroll amounts become the documented defaults, not zero."""
    planned = build_planned_action(ToolName.SCROLL, {"target": "page"})
    assert planned.params["vertical"] == 3
    assert planned.params["horizontal"] == 0


def test_drag_requires_a_destination_description() -> None:
    """A drag without a destination could not be resolved at drop time."""
    with pytest.raises(ToolProtocolError):
        build_planned_action(ToolName.DRAG, {"target": "file"})
    planned = build_planned_action(ToolName.DRAG, {"target": "file", "destination": "trash"})
    assert planned.action_class is ActionClass.DESTRUCTIVE
    assert planned.params["destination"] == "trash"


def test_ensure_window_requires_a_window_id() -> None:
    """Focus needs a target window; there is no 'current window' default."""
    with pytest.raises(ToolProtocolError):
        build_planned_action(ToolName.ENSURE_WINDOW, {})
    planned = build_planned_action(ToolName.ENSURE_WINDOW, {"window_id": 7})
    assert planned.params["window_id"] == 7


def test_visible_command_is_forwarded_for_terminal_policy() -> None:
    """Section 54 needs the literal visible line, so it must survive parsing."""
    planned = build_planned_action(
        ToolName.PRESS_KEY,
        {"key": "Return", "visible_command": "rm -rf /tmp/x"},
    )
    assert planned.params["visible_command"] == "rm -rf /tmp/x"


# -- Refusals -----------------------------------------------------------------

@pytest.mark.parametrize("key", ["x", "y", "element_id", "lease_id", "frame_id"])
def test_pre_resolved_values_are_refused_for_an_action_tool(key: str) -> None:
    """A coordinate/lease would bypass resolution and revalidation (sections 44, 45)."""
    with pytest.raises(ToolProtocolError) as excinfo:
        build_planned_action(ToolName.CLICK, {"target": "Send", key: 10})
    assert "pre-resolved" in excinfo.value.message


def test_an_unknown_argument_is_refused_rather_than_ignored() -> None:
    """Silently dropping an argument would change what the Brain asked for."""
    with pytest.raises(ToolProtocolError) as excinfo:
        build_planned_action(ToolName.CLICK, {"target": "Send", "double": True})
    assert "unsupported argument" in excinfo.value.message
    assert excinfo.value.details["unknown"] == ["double"]


def test_an_unknown_role_is_refused() -> None:
    """A role hint that is not in the section 37 vocabulary is a malformed call."""
    with pytest.raises(ToolProtocolError) as excinfo:
        build_planned_action(ToolName.CLICK, {"target": "Send", "role": "WIDGET"})
    assert excinfo.value.code is ErrorCode.INTERNAL_ERROR


def test_an_unknown_tool_is_reported_as_unavailable() -> None:
    """BLAXCY says it does not have the tool rather than inventing behaviour."""
    with pytest.raises(ToolProtocolError) as excinfo:
        build_planned_action("teleport_mouse", {})
    assert excinfo.value.code is ErrorCode.BACKEND_UNAVAILABLE


def test_read_only_tools_have_no_planned_action() -> None:
    """Read-only work is dispatched on the read path, never the executor."""
    for tool in sorted(READ_ONLY_TOOLS):
        with pytest.raises(ToolProtocolError) as excinfo:
            build_planned_action(tool, {})
        assert "read-only" in excinfo.value.message


def test_run_sequence_is_not_parsed_as_an_action() -> None:
    """The container is parsed by its own schema, not as one more action."""
    with pytest.raises(ToolProtocolError) as excinfo:
        build_planned_action(ToolName.RUN_SEQUENCE, {"steps": []})
    assert "container" in excinfo.value.message


def test_arguments_must_be_an_object() -> None:
    """A non-object argument payload is malformed, not coerced."""
    with pytest.raises(ToolProtocolError):
        element_query(ToolName.CLICK, {"target": ""})


def test_missing_target_is_refused() -> None:
    """An empty description is not a target."""
    with pytest.raises(ToolProtocolError):
        build_planned_action(ToolName.CLICK, {})


def test_read_only_arguments_are_validated_too() -> None:
    """Strictness applies on the read path as well."""
    assert parse_tool_arguments(ToolName.GET_SCREEN_STATE, {}) == {}
    with pytest.raises(ToolProtocolError):
        parse_tool_arguments(ToolName.GET_SCREEN_STATE, {"x": 1})


def test_every_action_tool_exposes_a_declared_class() -> None:
    """Every non-container tool has a class; nothing inherits a default."""
    for tool in TOOL_PARAMETER_SCHEMAS:
        if tool == ToolName.RUN_SEQUENCE:
            continue
        assert action_class_for(tool) in set(ActionClass)
