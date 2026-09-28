"""Permission decisions (specification sections 13, 56, 57, 66.1).

One place answers "may this action proceed, and does it need a human first?" so
that no caller has to reassemble the answer from the mode, the action class, the
application, the calibration and the terminal state on its own. The engine is
pure: it reads configuration and inspects values it is handed, performs no
input, and returns a value rather than raising for an expected denial.

The decision order is fixed and fail-closed:

1. **Blocked application** (section 58) -- refuse outright, in every mode.
2. **Terminal** (section 54) -- typing and submission are judged separately, and
   submission always needs confirmation.
3. **Credential context** (section 55) -- interaction is allowed, reading the
   content back is refused.
4. **Calibration** (section 31) -- a disarmed calibration refuses all input.
5. **Mode and action class** (sections 56-57) -- OBSERVE refuses every action
   that injects input.
6. **Confirmation** -- destructive actions always need an explicit human
   confirmation, and a confirmation that has not been given yet is reported as
   ``CONFIRMATION_REQUIRED`` rather than as a denial, so the caller can ask.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from config.settings import Settings
from core.calibration import Calibration
from policy.action_classes import performs_physical_input
from policy.guards import (
    GuardOutcome,
    check_blocked_application,
    check_calibration,
    check_capability,
    check_password_context,
)
from policy.terminal_guard import TerminalGuard, is_submission_hotkey, is_submission_key
from schemas.actions import TOOL_ACTION_CLASS, ToolName, action_class_for
from schemas.capability import CapabilityReport
from schemas.enums import ActionClass, CapabilityName, ErrorCode, PolicyMode


def mode_allows(mode: PolicyMode, *, tool: str, action_class: ActionClass) -> bool:
    """True when ``mode`` permits performing ``tool`` at all (section 56).

    * ``READ_ONLY`` work is always permitted.
    * ``OBSERVE`` additionally permits navigational work that does **not**
      inject input (moving focus through the window manager), and refuses
      everything else: mouse/keyboard injection, scroll, drag, terminal
      submission and ``run_sequence`` execution.
    * ``ASSIST``/``AUTONOMOUS`` permit the remaining action classes, with
      destructive actions still routed through confirmation.
    """
    if action_class is ActionClass.READ_ONLY:
        return True
    if mode is PolicyMode.OBSERVE:
        return action_class is ActionClass.NAVIGATIONAL and not performs_physical_input(tool)
    return True


@dataclass(frozen=True)
class PolicyDecision:
    """The complete verdict on one action.

    ``allowed`` always means "may proceed *now*": when a confirmation is needed
    and has not been given, ``allowed`` is False and ``code`` is
    ``CONFIRMATION_REQUIRED``, with ``requires_confirmation`` set so the caller
    knows an ask -- not a hard denial -- is what stands in the way.
    """

    allowed: bool
    action_class: ActionClass | None = None
    mode: PolicyMode | None = None
    requires_confirmation: bool = False
    code: ErrorCode | None = None
    message: str | None = None
    sensitive: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def permit(
        cls,
        *,
        action_class: ActionClass,
        mode: PolicyMode,
        sensitive: bool = False,
        message: str | None = None,
    ) -> PolicyDecision:
        """An unconditional permission."""
        return cls(True, action_class, mode, sensitive=sensitive, message=message)

    @classmethod
    def deny(
        cls,
        code: ErrorCode,
        message: str,
        *,
        action_class: ActionClass | None = None,
        mode: PolicyMode | None = None,
        details: dict[str, Any] | None = None,
    ) -> PolicyDecision:
        """A refusal."""
        return cls(False, action_class, mode, code=code, message=message, details=details or {})

    @classmethod
    def needs_confirmation(
        cls,
        *,
        action_class: ActionClass,
        mode: PolicyMode,
        message: str,
        sensitive: bool = False,
    ) -> PolicyDecision:
        """A permitted action that must be confirmed before it proceeds."""
        return cls(
            allowed=False,
            action_class=action_class,
            mode=mode,
            requires_confirmation=True,
            code=ErrorCode.CONFIRMATION_REQUIRED,
            message=message,
            sensitive=sensitive,
        )


class PermissionEngine:
    """Owns the single permission policy (sections 56-58, 66.1).

    Args:
        settings: The full configuration. The ``[safety]``/``[terminal]``
            sections are the ones consulted; the whole object is accepted so
            application lists, mode ceilings and terminal policy stay together.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._terminal = TerminalGuard(settings.terminal)

    @property
    def terminal(self) -> TerminalGuard:
        """The terminal guard shared with the executor (section 54)."""
        return self._terminal

    def decide(
        self,
        *,
        tool: str,
        mode: PolicyMode,
        action_class: ActionClass | None = None,
        application: Sequence[str | None] = (),
        password_context: bool = False,
        calibration: Calibration | None = None,
        capability_report: CapabilityReport | None = None,
        capability: CapabilityName | None = None,
        terminal_command: str | None = None,
        terminal_submit: bool = False,
        confirmed: bool = False,
    ) -> PolicyDecision:
        """Decide whether ``tool`` may run right now.

        Args:
            tool: The tool being considered. ``run_sequence`` must be passed with
                an explicit ``action_class`` (its own gate is the most
                restrictive class among its steps, section 57).
            mode: The *effective* policy mode (already expired if AUTONOMOUS
                lapsed).
            action_class: Override for a container tool such as ``run_sequence``.
            application: Candidate application strings (name, window title,
                window class) for the section 58 deny-list.
            password_context: Whether a credential field is the target.
            calibration: The active calibration, when calibration is required.
            capability_report: A capability report, when the caller has one.
            capability: The capability ``tool`` depends on, if any.
            terminal_command: The literal command line, when the target is a
                terminal text entry.
            terminal_submit: True when this action would submit the line.
            confirmed: Whether a human confirmation has already been given.
        """
        resolved_class = action_class if action_class is not None else self._resolve_class(tool)

        blocked = check_blocked_application(application, self._settings.safety.blocked_applications)
        if not blocked.ok:
            return self._from_guard(blocked, resolved_class, mode)

        terminal = self._terminal_decision(
            tool=tool,
            mode=mode,
            terminal_command=terminal_command,
            terminal_submit=terminal_submit,
            action_class=resolved_class,
        )
        if terminal is not None:
            return terminal

        password = check_password_context(tool=tool, is_password=password_context)
        if not password.ok:
            return self._from_guard(password, resolved_class, mode)

        calibration_outcome = check_calibration(calibration)
        if not calibration_outcome.ok:
            return self._from_guard(calibration_outcome, resolved_class, mode)

        if capability is not None:
            capability_outcome = check_capability(capability_report, capability)
            if not capability_outcome.ok:
                return self._from_guard(capability_outcome, resolved_class, mode)

        if not mode_allows(mode, tool=tool, action_class=resolved_class):
            return PolicyDecision.deny(
                ErrorCode.PERMISSION_DENIED,
                f"{mode.value} does not permit {tool} ({resolved_class.value})",
                action_class=resolved_class,
                mode=mode,
                details={"tool": tool, "action_class": resolved_class.value},
            )

        requires_confirmation = self._requires_confirmation(resolved_class)
        if requires_confirmation and not confirmed:
            return PolicyDecision.needs_confirmation(
                action_class=resolved_class,
                mode=mode,
                message=self._confirmation_message(tool, resolved_class),
                sensitive=password.sensitive,
            )
        return PolicyDecision.permit(
            action_class=resolved_class,
            mode=mode,
            sensitive=password.sensitive,
            message="destructive action confirmed" if requires_confirmation else None,
        )

    # -- Internals ------------------------------------------------------------

    def _resolve_class(self, tool: str) -> ActionClass:
        """Resolve a tool's action class, refusing a container tool."""
        if tool == ToolName.RUN_SEQUENCE:
            raise ValueError("run_sequence has no fixed class; pass action_class (section 57)")
        return action_class_for(tool)

    def _requires_confirmation(self, action_class: ActionClass) -> bool:
        """Destructive actions always need a human confirmation.

        This is not configurable. Section 56 requires it for ASSIST and
        AUTONOMOUS alike, and section 54 keeps destructive work protected in
        every mode; a switch here would be a way to silently disable it later.
        """
        return action_class is ActionClass.DESTRUCTIVE

    def _confirmation_message(self, tool: str, action_class: ActionClass) -> str:
        """The human-facing reason a confirmation is required."""
        return f"{tool} is a {action_class.value} action and requires explicit confirmation"

    def _terminal_decision(
        self,
        *,
        tool: str,
        mode: PolicyMode,
        terminal_command: str | None,
        terminal_submit: bool,
        action_class: ActionClass,
    ) -> PolicyDecision | None:
        """Apply section 54 when the action touches a terminal, else ``None``."""
        if terminal_command is None and not terminal_submit:
            return None

        if terminal_submit:
            # A submit is judged on the literal text being submitted. The key or
            # hotkey that triggers it is irrelevant to the decision.
            submission = self._terminal.submission_decision(mode, terminal_command or "")
            if not submission.allowed:
                return PolicyDecision.deny(
                    submission.code or ErrorCode.PERMISSION_DENIED,
                    submission.message or "terminal submission is not permitted",
                    action_class=action_class,
                    mode=mode,
                    details={"risk": submission.assessment.risk.value} if submission.assessment else {},
                )
            if submission.requires_confirmation and not confirmed:
                return PolicyDecision.needs_confirmation(
                    action_class=action_class,
                    mode=mode,
                    message=submission.message or "terminal submission requires confirmation",
                )
            return None

        # Typing path: the terminal command is merely being typed.
        typing = self._terminal.typing_decision(mode, terminal_command or "")
        if not typing.allowed:
            return PolicyDecision.deny(
                typing.code or ErrorCode.PERMISSION_DENIED,
                typing.message or "terminal typing is not permitted",
                action_class=action_class,
                mode=mode,
            )
        return None

    def _from_guard(
        self,
        outcome: GuardOutcome,
        action_class: ActionClass,
        mode: PolicyMode,
    ) -> PolicyDecision:
        """Turn a failing guard outcome into a denial."""
        return PolicyDecision.deny(
            outcome.code or ErrorCode.PERMISSION_DENIED,
            outcome.message or "blocked by policy guard",
            action_class=action_class,
            mode=mode,
            details=outcome.details,
        )


def is_terminal_submit(tool: str, params: dict[str, Any] | None = None) -> bool:
    """True when ``tool`` with ``params`` would submit a terminal line (section 54).

    Used by the executor to route a key press through the *submission* policy
    rather than the typing one, so ``Return`` can never fire as an incidental
    side effect of a "just press a key" request.
    """
    values = params or {}
    if tool == ToolName.HOTKEY:
        combo = values.get("combo") or values.get("keys") or ""
        return is_submission_hotkey(str(combo))
    if tool == ToolName.PRESS_KEY:
        key = values.get("key") or ""
        modifiers = values.get("modifiers") or ()
        if modifiers:
            return is_submission_hotkey("+".join([*[str(m) for m in modifiers], str(key)]))
        return is_submission_key(str(key))
    return False


def allowed_tools_for(mode: PolicyMode) -> tuple[str, ...]:
    """Every tool ``mode`` permits at all, for the capability report and the GUI."""
    allowed: list[str] = []
    for tool in TOOL_ACTION_CLASS:
        if mode_allows(mode, tool=tool, action_class=TOOL_ACTION_CLASS[tool]):
            allowed.append(tool)
    return tuple(allowed)
