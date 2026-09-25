"""Action-class classification helpers (specification sections 32.1, 57).

The class vocabulary itself lives in :mod:`schemas.enums` and the per-tool
mapping in :mod:`schemas.actions`, because both are part of the wire contract.
This module holds the *derived* questions policy and scheduling ask about a
class:

* what class a whole ``run_sequence`` plan is gated as (the most restrictive
  class among its steps, section 57);
* whether performing a tool physically touches the desktop, which is what
  decides whether it needs the single-writer execution lock (section 32.1).

Nothing here decides *whether* an action is allowed -- that is
:mod:`policy.permissions`. Classification is descriptive, never authorising.
"""

from __future__ import annotations

from collections.abc import Iterable

from schemas.actions import (
    TOOL_ACTION_CLASS,
    ToolName,
    action_class_for,
    most_restrictive_action_class,
)
from schemas.enums import ActionClass

#: Tools that inject real input into the desktop: pointer motion, button
#: presses of any kind, scroll wheel events, key events and drags. These are the
#: actions that must serialise behind the single-writer execution lock, and that
#: OBSERVE refuses outright (sections 32.1, 56).
_PHYSICAL_INPUT_TOOLS: frozenset[str] = frozenset(
    {
        ToolName.CLICK,
        ToolName.DOUBLE_CLICK,
        ToolName.RIGHT_CLICK,
        ToolName.TYPE_TEXT,
        ToolName.PRESS_KEY,
        ToolName.HOTKEY,
        ToolName.SCROLL,
        ToolName.DRAG,
    }
)

#: Tools that change the desktop without injecting input: they move focus or
#: invoke an application-level action. They mutate, so they are not read-only,
#: but they do not need the physical-input lock (section 32.1).
_NON_INPUT_MUTATING_TOOLS: frozenset[str] = frozenset(
    {
        ToolName.ENSURE_WINDOW,
        ToolName.ACTIVATE_ELEMENT,
    }
)


def performs_physical_input(tool: str) -> bool:
    """True when ``tool`` injects real mouse/keyboard input (section 32.1).

    Raises:
        ValueError: For an unknown tool.
    """
    if tool not in TOOL_ACTION_CLASS and tool != ToolName.RUN_SEQUENCE:
        raise ValueError(f"unknown tool {tool!r}")
    return tool in _PHYSICAL_INPUT_TOOLS


def requires_exclusive_execution(tool: str) -> bool:
    """True when ``tool`` must hold the single-writer execution lock (section 32.1).

    The section 32 exclusion rule guards ``MUTATING``/``DESTRUCTIVE`` actions and
    any ``NAVIGATIONAL`` action that performs physical input. Read-only tools and
    non-input navigational tools (focus changes through the window manager) are
    deliberately excluded, which is exactly what makes concurrent read-only
    dispatch safe.
    """
    if tool == ToolName.RUN_SEQUENCE:
        return True
    action_class = action_class_for(tool)
    if action_class in (ActionClass.MUTATING, ActionClass.DESTRUCTIVE):
        return True
    return action_class is ActionClass.NAVIGATIONAL and performs_physical_input(tool)


def sequence_gate_class(steps: Iterable[str]) -> ActionClass:
    """Return the class a whole ``run_sequence`` plan is gated as (section 57).

    The gate is the most restrictive class among the declared steps: a plan with
    one destructive step is confirmed as destructive even when every other step
    is navigational. ``run_sequence`` is never a class of its own.
    """
    classes = tuple(action_class_for(step) for step in steps)
    if not classes:
        return ActionClass.READ_ONLY
    return most_restrictive_action_class(classes)
