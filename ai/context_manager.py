"""Context budget and the delta-oriented view sent to the Brain (sections 68, 68.1).

Section 68 gives a 1200-token budget and three hard content rules; section 68.1
turns the token budget into a design principle. This module implements both, and
the split between them is deliberate:

* :meth:`ContextManager.fit` owns the *conversation* side: keep the last turns
  verbatim, condense the rest into a deterministic summary, and drop the oldest
  turns when the budget is still exceeded.
* :meth:`ContextManager.state_context` owns the *perception* side: send a small
  "unchanged" marker instead of re-dumping a screen BLAXCY already reported, and
  never attach element content from a protected application.

Two content rules are enforced here rather than trusted to a caller:

1. **Unchanged state is not resent.** A state whose ``state_version``/``frame_id``
   BLAXCY already sent becomes a three-field marker, so a long session does not
   spend its budget re-transmitting the same desktop (section 68.1).
2. **Protected content never leaves.** When the active window belongs to a
   protected application, no element content is attached at all -- not just its
   text (sections 42, 68). Element summaries are built by
   :func:`ai.tool_protocol.element_summary`, which makes a credential value
   structurally impossible to include.

The 1200-token figure covers the *dynamic* context: conversation turns plus
attached state. The system instruction and the tool declarations are excluded,
because section 68 says "1200 tokens plus current model requirements as
observed" -- those are the model requirements, and counting them would make the
budget meaningless.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from ai.tool_protocol import MAX_CONTEXT_ELEMENTS, element_summary
from config.settings import Settings
from core.state_cache import StateCache
from schemas.screen_state import ScreenState

#: Turns kept verbatim before older turns are condensed (section 68).
KEEP_RECENT_TURNS: int = 8

#: Rough characters-per-token divisor used for the estimate. It is an estimate,
#: labelled as one: the real tokenizer belongs to the provider, and reporting a
#: fabricated exact count would be worse than reporting an honest approximation.
_CHARS_PER_TOKEN: int = 4


@runtime_checkable
class ContextTurn(Protocol):
    """The parts of a conversation turn this module needs (structural typing).

    Declared as read-only properties so a frozen dataclass with narrower field
    types (``tuple[ModelToolCall, ...]``) satisfies it without a cast.
    """

    @property
    def role(self) -> str: ...

    @property
    def text(self) -> str | None: ...

    @property
    def tool_calls(self) -> Sequence[Any]: ...

    @property
    def tool_results(self) -> Sequence[Any]: ...


@dataclass(frozen=True)
class ContextBudgetReport:
    """What the last :meth:`ContextManager.fit` actually did."""

    token_budget: int
    estimated_tokens: int
    turns_considered: int
    turns_kept: int
    turns_summarized: int
    turns_dropped: int
    summary_chars: int

    def to_dict(self) -> dict[str, Any]:
        """JSON-shaped budget report for logs and the GUI."""
        return {
            "token_budget": self.token_budget,
            "estimated_tokens": self.estimated_tokens,
            "turns_considered": self.turns_considered,
            "turns_kept": self.turns_kept,
            "turns_summarized": self.turns_summarized,
            "turns_dropped": self.turns_dropped,
            "summary_chars": self.summary_chars,
        }


@dataclass(frozen=True)
class FittedContext:
    """The budgeted conversation view plus the condensed prefix."""

    turns: tuple[Any, ...]
    summary: str | None
    report: ContextBudgetReport


@dataclass
class _SentState:
    """The last state BLAXCY reported, so an unchanged one can be a marker."""

    frame_id: int
    state_version: int
    generation: int


class ContextManager:
    """Builds what the Brain sees, within budget (sections 68, 68.1).

    Args:
        settings: Full configuration; ``[gemini]`` sets the budget and
            ``[safety]`` supplies the protected-application list.
        keep_recent_turns: How many recent turns stay verbatim.
        max_elements: Cap on element summaries attached to one snapshot.
        token_budget: Override for the configured budget (tests).
    """

    def __init__(
        self,
        settings: Settings,
        *,
        keep_recent_turns: int = KEEP_RECENT_TURNS,
        max_elements: int = MAX_CONTEXT_ELEMENTS,
        token_budget: int | None = None,
    ) -> None:
        self._settings = settings
        self._keep_recent = max(1, keep_recent_turns)
        self._max_elements = max(0, min(max_elements, MAX_CONTEXT_ELEMENTS))
        self._budget = token_budget if token_budget is not None else settings.gemini.context_token_budget
        self._last_report: ContextBudgetReport | None = None
        self._sent_state: _SentState | None = None
        self._last_summary: str | None = None

    # -- Conversation side (section 68) --------------------------------------

    def fit(self, turns: Sequence[ContextTurn]) -> FittedContext:
        """Trim ``turns`` to the budget, condensing the older ones.

        The newest turn is always kept: dropping it would remove the very
        information the next model call needs.
        """
        considered = len(turns)
        if considered == 0:
            report = ContextBudgetReport(self._budget, 0, 0, 0, 0, 0, 0)
            self._last_report = report
            return FittedContext((), None, report)

        older = list(turns[: max(0, considered - self._keep_recent)])
        recent = list(turns[len(older) :])
        summary = _summarize(older) if older else None

        estimated = _estimate_tokens([_turn_payload(turn) for turn in recent]) + (
            0 if summary is None else _estimate_text(summary)
        )
        dropped = 0
        while estimated > self._budget and len(recent) > 1:
            recent.pop(0)
            dropped += 1
            estimated = _estimate_tokens([_turn_payload(turn) for turn in recent]) + (
                0 if summary is None else _estimate_text(summary)
            )
        # A single turn can still exceed the budget; it is kept rather than
        # silently truncated, and the report says how big the estimate was.
        report = ContextBudgetReport(
            token_budget=self._budget,
            estimated_tokens=estimated,
            turns_considered=considered,
            turns_kept=len(recent),
            turns_summarized=len(older),
            turns_dropped=dropped,
            summary_chars=0 if summary is None else len(summary),
        )
        self._last_report = report
        self._last_summary = summary
        return FittedContext(tuple(recent), summary, report)

    @property
    def last_report(self) -> ContextBudgetReport | None:
        """The most recent budget report, or ``None``."""
        return self._last_report

    @property
    def last_summary(self) -> str | None:
        """The most recent condensed prefix, or ``None``."""
        return self._last_summary

    # -- Perception side (sections 42, 68.1) ---------------------------------

    def state_context(
        self,
        cache: StateCache,
        *,
        force: bool = False,
        include_elements: bool = True,
    ) -> dict[str, Any]:
        """Build the state attachment for the next model turn.

        Args:
            cache: The current-state authority.
            force: Attach the full snapshot even when it is unchanged (used at
                the start of a task and after a takeover resume).
            include_elements: Whether element summaries may be attached at all.
        """
        state = cache.current
        if state is None:
            return {"observed": False}

        marker = _SentState(state.frame_id, state.state_version, state.generation)
        if not force and self._sent_state is not None and self._sent_state == marker:
            return {
                "observed": True,
                "unchanged": True,
                "frame_id": state.frame_id,
                "state_version": state.state_version,
                "generation": state.generation,
                "active_window_id": state.active_window_id,
                "active_app": state.active_app,
            }

        self._sent_state = marker
        payload: dict[str, Any] = {"observed": True, "unchanged": False, **state.to_model_context()}
        protected = self._protected_reason(state)
        if protected is not None:
            # The whole screen is a protected context: attach stamps and say why,
            # never element content (sections 42, 55, 68).
            payload["protected"] = True
            payload["protected_reason"] = protected
            payload["elements"] = []
            return payload
        if not include_elements or self._max_elements == 0:
            payload["elements"] = []
            return payload
        elements = [element_summary(element) for element in state.elements[: self._max_elements]]
        payload["elements"] = elements
        payload["elements_truncated"] = len(state.elements) > self._max_elements
        delta = cache.delta
        if delta is not None:
            payload["delta"] = {
                "change_class": delta.change_class.value,
                "regions": [region.rect.to_dict() for region in delta.regions],
            }
        return payload

    def forget_state(self) -> None:
        """Drop the "already sent" marker, so the next context is a full snapshot.

        Called on human takeover resume and after a generation change: the Brain
        must not be told "unchanged" about a desktop it has never seen the new
        version of (sections 32, 62, 68.1).
        """
        self._sent_state = None

    def _protected_reason(self, state: ScreenState) -> str | None:
        """Why this observation must not have element content attached."""
        markers = self._settings.safety.protected_applications
        if not markers:
            return None
        candidates = [state.active_app]
        for element in state.elements:
            candidates.append(element.owner_app)
        for candidate in candidates:
            match = _matches_any(candidate, markers)
            if match is not None:
                return f"the application matches the protected list entry {match!r}"
        return None


#: Fields kept from a per-step verification verdict. The evidence dictionary is
#: useful when a human is debugging one action and pure repetition in a batch.
_KEPT_VERIFICATION_FIELDS: tuple[str, ...] = ("state", "postcondition", "reason")

#: Fields kept from a per-step element summary: identity, never geometry.
_KEPT_TARGET_FIELDS: tuple[str, ...] = (
    "element_id",
    "role",
    "text",
    "accessible_name",
    "source",
    "password",
)

#: Fields kept from a per-step lease. The full lease (ttl, issued_at, identity
#: fingerprint) is BLAXCY's own bookkeeping, not the Brain's business.
_KEPT_LEASE_FIELDS: tuple[str, ...] = ("lease_id", "element_id", "frame_id")


def compact_sequence_result(payload: dict[str, Any]) -> dict[str, Any]:
    """Summarise a ``run_sequence`` result array for the Brain (section 68.1).

    A batch of twelve steps would otherwise send twelve copies of the same
    heavyweight resolution payload -- up to five candidate elements each, plus
    every revalidation check, plus each verification's evidence. Section 68.1
    asks for "the delta and the verification outcome the Brain actually needs to
    decide what's next", so each step keeps its identity, its outcome, its
    verification verdict and the *shape* of its resolution/revalidation, and the
    repeated bodies are replaced by counts, ids and the failed checks only.

    Nothing safety-relevant is dropped: the error code, the verification state,
    the halt reason and the ``NOT_EXECUTED`` tail all survive intact. What is
    removed is detail that describes work BLAXCY already did, and a caller that
    genuinely needs it can call the read-only tools.
    """
    compacted: dict[str, Any] = {key: value for key, value in payload.items() if key != "steps"}
    compacted["steps"] = [_compact_step(step) for step in payload.get("steps") or ()]
    compacted["context_note"] = (
        "per-step resolution and revalidation detail is summarised; every step "
        "still ran the full policy/resolve/lease/revalidate/execute/verify pipeline"
    )
    return compacted


def _compact_step(step: dict[str, Any]) -> dict[str, Any]:
    """Compact one step's envelope without losing its outcome."""
    out = {key: value for key, value in step.items() if key != "data"}
    data = dict(step.get("data") or {})
    elided: list[str] = []

    resolution = data.pop("resolution", None)
    if isinstance(resolution, dict):
        best = resolution.get("best")
        data["resolution"] = {
            "status": resolution.get("status"),
            "matched_stage": resolution.get("matched_stage"),
            "considered": resolution.get("considered"),
            "ambiguity": resolution.get("ambiguity"),
            "candidate_count": len(resolution.get("candidates") or ()),
            "best_element_id": best.get("element_id") if isinstance(best, dict) else None,
        }
        elided.append("resolution.candidates")

    revalidation = data.pop("revalidation", None)
    if isinstance(revalidation, dict):
        checks = revalidation.get("checks") or {}
        data["revalidation"] = {
            "ok": revalidation.get("ok"),
            "moved_px": revalidation.get("moved_px"),
            "failed_checks": sorted(name for name, passed in checks.items() if not passed),
        }
        elided.append("revalidation.passing_checks")

    verification = data.get("verification")
    if isinstance(verification, dict):
        data["verification"] = {
            key: verification[key] for key in _KEPT_VERIFICATION_FIELDS if key in verification
        }
        elided.append("verification.evidence")

    lease = data.get("lease")
    if isinstance(lease, dict):
        data["lease"] = {key: lease[key] for key in _KEPT_LEASE_FIELDS if key in lease}
        elided.append("lease.bookkeeping")

    target = data.get("target")
    if isinstance(target, dict):
        data["target"] = {key: target[key] for key in _KEPT_TARGET_FIELDS if key in target}
        elided.append("target.bbox")

    hint = data.get("speculative_hint")
    if isinstance(hint, dict):
        inner = hint.get("hint")
        data["speculative_hint"] = {
            "speculative": hint.get("speculative"),
            "element_id": inner.get("element_id") if isinstance(inner, dict) else None,
            "status": inner.get("status") if isinstance(inner, dict) else None,
        }

    if elided:
        data["elided"] = sorted(set(elided))
    out["data"] = data
    return out


def _matches_any(value: str | None, markers: Sequence[str]) -> str | None:
    """Return the first protected-list entry ``value`` matches, or ``None``."""
    if not value:
        return None
    folded = value.casefold()
    for marker in markers:
        if marker and marker.casefold() in folded:
            return marker
    return None


def _turn_payload(turn: ContextTurn) -> dict[str, Any]:
    """A JSON-shaped view of one turn for the token estimate."""
    return {
        "role": getattr(turn, "role", None),
        "text": getattr(turn, "text", None),
        "tool_calls": [
            {"name": getattr(call, "name", None), "args": getattr(call, "args", None)}
            for call in getattr(turn, "tool_calls", ()) or ()
        ],
        "tool_results": [
            {
                "name": getattr(result, "name", None),
                "envelope": getattr(result, "envelope", None),
            }
            for result in getattr(turn, "tool_results", ()) or ()
        ],
    }


def _estimate_text(text: str) -> int:
    """Approximate the token count of ``text``."""
    return max(1, len(text) // _CHARS_PER_TOKEN)


def _estimate_tokens(payloads: Sequence[Any]) -> int:
    """Approximate the token count of a JSON-serialisable payload list."""
    total = 0
    for payload in payloads:
        try:
            encoded = json.dumps(payload, separators=(",", ":"), default=str)
        except (TypeError, ValueError):
            encoded = str(payload)
        total += max(1, len(encoded) // _CHARS_PER_TOKEN)
    return total


def _summarize(turns: Sequence[ContextTurn]) -> str:
    """Condense older turns into a short, deterministic, factual summary.

    Deliberately not a model call: summarising context with another model turn
    would cost exactly the tokens this is meant to save, and would introduce a
    second, unverifiable narrator of what happened. The summary records only what
    BLAXCY observed -- tool names and their outcome codes.
    """
    lines: list[str] = [f"[earlier context: {len(turns)} turn(s) condensed by BLAXCY]"]
    for turn in turns:
        text = getattr(turn, "text", None)
        if text:
            snippet = " ".join(str(text).split())
            lines.append(f"- {getattr(turn, 'role', '?')} said: {snippet[:200]}")
        for call in getattr(turn, "tool_calls", ()) or ():
            name = getattr(call, "name", None)
            args = getattr(call, "args", None) or {}
            target = args.get("target") if isinstance(args, dict) else None
            lines.append(f"- called {name}" + (f" on {target!r}" if target else ""))
        for result in getattr(turn, "tool_results", ()) or ():
            envelope = getattr(result, "envelope", None) or {}
            outcome = "ok" if envelope.get("ok") else f"failed {envelope.get('error_code')}"
            verification = envelope.get("verification")
            if verification and verification != "NOT_APPLICABLE":
                outcome += f" ({verification})"
            lines.append(f"  -> {getattr(result, 'name', '?')}: {outcome}")
    return "\n".join(lines)


__all__ = [
    "KEEP_RECENT_TURNS",
    "ContextBudgetReport",
    "ContextManager",
    "ContextTurn",
    "FittedContext",
    "compact_sequence_result",
]
