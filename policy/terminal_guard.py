"""Terminal safety (specification section 54).

Typing a command and submitting it are two separate security decisions, and
BLAXCY keeps them separate:

* **Typing** a command into a terminal is permitted only when the policy mode
  allows terminal typing at all.
* **Submitting** it is independently authorised. Nothing in this module ever
  submits automatically: a submission needs an explicit human confirmation
  unless the mode and policy explicitly say otherwise, and the configured
  security invariant (``terminal.submit_requires_confirmation``) can never be
  turned off (section 4 rule 25).
* Destructive commands stay protected in **every** mode, including AUTONOMOUS,
  and a submitted command is never an automatic retry candidate (sections 4
  rule 21, 54, 56).
* ``Return`` must never fire accidentally, and a paste or a hotkey must not be
  usable as a way around the submission policy -- both are routed through
  :func:`is_submission_key` / :func:`is_submission_hotkey`.

The destructive classifier is intentionally *conservative*: it prefers a false
positive (a benign command that needs confirmation) over a false negative (a
destructive command that runs unconfirmed). A `sudo` prefix is treated as
sensitive rather than safe.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from config.settings import TerminalSettings
from schemas.enums import ErrorCode, PolicyMode

#: Keys that submit a command line. ``KP_Enter`` is the keypad Return, and
#: ``ctrl+m``/``ctrl+j`` are the carriage-return/line-feed equivalents that a
#: shell also treats as "run this line".
SUBMIT_KEYS: frozenset[str] = frozenset({"return", "enter", "kp_enter"})
SUBMIT_HOTKEYS: frozenset[str] = frozenset({"ctrl+m", "ctrl+j", "control+m", "control+j"})


class TerminalRisk(StrEnum):
    """How dangerous a command line is judged to be (section 54)."""

    SAFE = "SAFE"
    SENSITIVE = "SENSITIVE"
    DESTRUCTIVE = "DESTRUCTIVE"


#: Patterns that mark a command line destructive. Each is a search, not a match,
#: so a destructive verb nested in a pipeline is still caught.
_DESTRUCTIVE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\brm\s+(-[a-z]*[rf]|--recursive|--force|--no-preserve-root)"),
    re.compile(r"\bmkfs(\.\w+)?\b"),
    re.compile(r"\b(fdisk|parted|sgdisk|wipefs)\b"),
    re.compile(r"\bdd\b[^\n|;]*\bof\s*="),
    re.compile(r"\bshred\b"),
    re.compile(r"\b(shutdown|reboot|poweroff|halt)\b"),
    re.compile(r"\binit\s+0\b"),
    re.compile(r"\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"),  # fork bomb
    re.compile(r"\bchmod\s+(-[a-zA-Z]*\s+)*777\s+/\s*$"),
    re.compile(r"\bchown\s+-R\b"),
    re.compile(r">\s*/dev/(sd|nvme|hd)\w*"),
    re.compile(r"\bgit\s+push\b[^\n]*--force"),
    re.compile(r"\bgit\s+reset\s+--hard\b"),
    re.compile(r"\bgit\s+clean\s+-[a-z]*f"),
    re.compile(r"\bkill(all)?\s+-9\s+1\b"),
    re.compile(r"\b(curl|wget)\b[^\n|;]*\|\s*(sudo\s+)?(ba|z|d)?sh\b"),
    re.compile(r"\bmv\b[^\n|;]*\s/dev/null\b"),
    re.compile(r"\btruncate\s+-s\s*0\b"),
    re.compile(r"\bformat\b"),
)

#: Patterns that are not destructive yet still deserve care -- privilege
#: escalation in particular, because a sudo line's blast radius is not visible
#: from the command itself.
_SENSITIVE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bsudo\b"),
    re.compile(r"\bsu\s"),
    re.compile(r"\bdoas\b"),
    re.compile(r"\bapt(-get)?\s+(install|remove|purge|upgrade|dist-upgrade)\b"),
    re.compile(r"\bpip\s+install\b"),
    re.compile(r"\bkill(all)?\b"),
    re.compile(r"\bsystemctl\b"),
    re.compile(r"\bchmod\b"),
    re.compile(r"\bgit\s+(push|reset|rebase)\b"),
)


@dataclass(frozen=True)
class TerminalAssessment:
    """What kind of command line this is (section 54)."""

    risk: TerminalRisk
    matched: tuple[str, ...] = ()
    sensitive_patterns: tuple[str, ...] = ()

    @property
    def destructive(self) -> bool:
        """True when the command is classed destructive."""
        return self.risk is TerminalRisk.DESTRUCTIVE

    @property
    def requires_confirmation(self) -> bool:
        """True when submission must be confirmed by a human."""
        return self.risk is not TerminalRisk.SAFE

    def to_dict(self) -> dict[str, object]:
        """JSON-shaped assessment for the confirmation dialog and the envelope."""
        return {
            "risk": self.risk.value,
            "matched": list(self.matched),
            "requires_confirmation": self.requires_confirmation,
        }


@dataclass(frozen=True)
class TerminalDecision:
    """A terminal-specific policy verdict."""

    allowed: bool
    requires_confirmation: bool = False
    code: ErrorCode | None = None
    message: str | None = None
    assessment: TerminalAssessment | None = None
    details: dict[str, object] = field(default_factory=dict)


def classify_command(command: str) -> TerminalAssessment:
    """Classify ``command`` as safe, sensitive or destructive (section 54)."""
    matched = tuple(pattern.pattern for pattern in _DESTRUCTIVE_PATTERNS if pattern.search(command))
    if matched:
        return TerminalAssessment(TerminalRisk.DESTRUCTIVE, matched=matched)
    sensitive = tuple(pattern.pattern for pattern in _SENSITIVE_PATTERNS if pattern.search(command))
    if sensitive:
        return TerminalAssessment(TerminalRisk.SENSITIVE, sensitive_patterns=sensitive)
    return TerminalAssessment(TerminalRisk.SAFE)


def canonical_key(key: str) -> str:
    """Normalise a key name or hotkey combo for comparison."""
    return "+".join(part.strip().casefold() for part in key.split("+") if part.strip())


def is_submission_key(key: str) -> bool:
    """True when pressing ``key`` would submit the current line (section 54)."""
    return canonical_key(key) in SUBMIT_KEYS


def is_submission_hotkey(combo: str) -> bool:
    """True when ``combo`` would submit the current line (section 54).

    A combo whose final component is Return -- ``shift+Return``, ``ctrl+Return``
    -- counts, because a terminal's behaviour for those is not something BLAXCY
    may assume. Treating them as submission is the conservative direction.
    """
    normalised = canonical_key(combo)
    if not normalised:
        return False
    if normalised in SUBMIT_HOTKEYS:
        return True
    parts = normalised.split("+")
    return parts[-1] in SUBMIT_KEYS


class TerminalGuard:
    """Terminal typing and submission policy (section 54).

    Args:
        settings: The ``[terminal]`` configuration. ``submit_requires_confirmation``
            and ``destructive_always_protected`` are security invariants and are
            validated at config load time; this class additionally refuses to
            behave as though they were off.
        typing_modes: Modes in which typing into a terminal is permitted at all.
            Defaults to ASSIST and AUTONOMOUS; OBSERVE never types.
    """

    def __init__(
        self,
        settings: TerminalSettings | None = None,
        *,
        typing_modes: Sequence[PolicyMode] = (PolicyMode.ASSIST, PolicyMode.AUTONOMOUS),
    ) -> None:
        self._settings = settings if settings is not None else TerminalSettings()
        self._typing_modes = frozenset(typing_modes)

    @property
    def submit_requires_confirmation(self) -> bool:
        """The (invariant) requirement that a submission be confirmed."""
        return True

    def assess(self, command: str) -> TerminalAssessment:
        """Classify a command line."""
        return classify_command(command)

    def typing_decision(self, mode: PolicyMode, command: str = "") -> TerminalDecision:
        """Decide whether typing ``command`` is permitted in ``mode``.

        Destructive commands may still be *typed* (the text is inert until
        submitted) but are reported as requiring confirmation, so the caller can
        surface the literal text before anything is submitted.
        """
        assessment = classify_command(command) if command else TerminalAssessment(TerminalRisk.SAFE)
        if mode not in self._typing_modes:
            return TerminalDecision(
                allowed=False,
                code=ErrorCode.PERMISSION_DENIED,
                message=f"terminal typing is not permitted in {mode.value}",
                assessment=assessment,
            )
        if assessment.destructive:
            return TerminalDecision(
                allowed=True,
                requires_confirmation=True,
                assessment=assessment,
                message="destructive command text: submission will require confirmation",
            )
        return TerminalDecision(allowed=True, assessment=assessment)

    def submission_decision(self, mode: PolicyMode, command: str) -> TerminalDecision:
        """Decide whether submitting ``command`` is permitted in ``mode``.

        This is the decision section 54 keeps independent of typing. It never
        auto-approves: even a benign command requires an explicit human
        confirmation while ``submit_requires_confirmation`` holds, and a
        destructive command is protected in every mode.
        """
        assessment = classify_command(command)
        if self._settings.destructive_always_protected and assessment.destructive:
            return TerminalDecision(
                allowed=True,
                requires_confirmation=True,
                assessment=assessment,
                message="destructive terminal command: explicit confirmation required",
            )
        if self._settings.submit_requires_confirmation:
            return TerminalDecision(
                allowed=True,
                requires_confirmation=True,
                assessment=assessment,
                message="terminal submission requires explicit confirmation",
            )
        # Unreachable while the invariants hold; kept explicit so the behaviour
        # is never an implicit default if the invariant is ever weakened.
        return TerminalDecision(allowed=False, code=ErrorCode.PERMISSION_DENIED, message="submission policy is disabled")
