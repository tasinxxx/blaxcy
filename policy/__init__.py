"""BLAXCY policy: modes, action classes, permissions and guards.

Phase 8 owns the decision layer that sits between a Brain request and the
executor (specification sections 13, 56-58):

* :mod:`policy.modes` -- the current mode and the AUTONOMOUS session ceiling;
* :mod:`policy.action_classes` -- what class an action is, and whether it needs
  the single-writer execution lock;
* :mod:`policy.guards` -- blocked applications, credential context, calibration
  and capability verdicts;
* :mod:`policy.terminal_guard` -- section 54's separation of typing a command
  from submitting it;
* :mod:`policy.permissions` -- the one place that answers "may this proceed?".

Policy reads state and returns verdicts. It never injects input and never
performs an action, so a policy bug can refuse work but can never authorise a
bypass of the executor's lease and revalidation checks.
"""

from policy.action_classes import (
    performs_physical_input,
    requires_exclusive_execution,
    sequence_gate_class,
)
from policy.guards import (
    VISUAL_FALLBACK_MODES,
    GuardOutcome,
    check_blocked_application,
    check_calibration,
    check_capability,
    check_password_context,
    check_protected_application,
    check_visual_fallback,
    match_application,
)
from policy.modes import ModeController, ModeStatus
from policy.permissions import (
    PermissionEngine,
    PolicyDecision,
    allowed_tools_for,
    is_terminal_submit,
    mode_allows,
)
from policy.terminal_guard import (
    TerminalAssessment,
    TerminalDecision,
    TerminalGuard,
    TerminalRisk,
    classify_command,
    is_submission_hotkey,
    is_submission_key,
)

__all__ = [
    "VISUAL_FALLBACK_MODES",
    "GuardOutcome",
    "ModeController",
    "ModeStatus",
    "PermissionEngine",
    "PolicyDecision",
    "TerminalAssessment",
    "TerminalDecision",
    "TerminalGuard",
    "TerminalRisk",
    "allowed_tools_for",
    "check_blocked_application",
    "check_calibration",
    "check_capability",
    "check_password_context",
    "check_protected_application",
    "check_visual_fallback",
    "classify_command",
    "is_submission_hotkey",
    "is_submission_key",
    "is_terminal_submit",
    "match_application",
    "mode_allows",
    "performs_physical_input",
    "requires_exclusive_execution",
    "sequence_gate_class",
]
