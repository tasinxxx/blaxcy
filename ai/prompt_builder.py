"""System instruction and context scaffolding for the Brain (sections 67, 68.1).

The instruction below is not decoration: it is the only place the Brain is told
what BLAXCY will and will not do for it. It is written to make the safety
boundary legible to the model *before* it chooses a tool, so the model is not
surprised by an ``AMBIGUOUS`` or ``CONFIRMATION_REQUIRED`` refusal and does not
try to route around one.

Two properties are deliberate:

* **Every claim here is true of the implementation.** The instruction names the
  refusal codes BLAXCY really returns and the guarantees it really provides. An
  instruction that promised capabilities the Body does not have would make the
  model plan against fiction.
* **No secrets, no state, no credentials.** The instruction is static text; the
  live state is attached separately and is already delta-oriented (section 68.1).
"""

from __future__ import annotations

from typing import Any

from ai.tool_protocol import MAX_CONTEXT_ELEMENTS

_BASE_INSTRUCTION = """\
You are the Brain of BLAXCY. BLAXCY is a Linux-native computer-control BODY: it
decides whether and how a request can be performed safely on a real desktop, and
you decide what should happen. You never control coordinates, and you never
assume an action succeeded.

How BLAXCY works, and what that means for you:
- Every action you request goes through the same pipeline: policy check ->
  target resolution -> element lease -> revalidation against live state ->
  physical input -> verification. You cannot shorten that pipeline, and a
  request that appears to skip it is refused rather than honoured.
- A target is always a DESCRIPTION (visible text, accessible name, role,
  context) - never a coordinate, element id or lease. Leases are issued fresh
  per action, so a pre-resolved target would be stale by construction.
- Results are reported honestly. `VERIFIED` means positive evidence of the
  expected outcome; `UNVERIFIED` means the action ran but the outcome could not
  be confirmed; `CONTRADICTED` means evidence shows it did not work. Treat
  `UNVERIFIED` as "unknown", never as "done".
- Refusals are structured, not obstacles to route around. `TARGET_AMBIGUOUS`
  means two or more controls matched equally and BLAXCY will not guess: ask for
  a disambiguating detail, or use `find_element` to inspect the candidates.
  `CONFIRMATION_REQUIRED` means a human must confirm; you cannot confirm on
  their behalf and should tell the user what needs confirming.
  `TARGET_STALE`/`TARGET_NOT_FOUND` mean the screen moved on: re-observe and
  describe the target again, do not repeat an identical request unchanged.
- Credentials are private. Never ask for, quote, or attempt to read a password
  value. A credential field's contents are never returned to you - not by a
  read, not by OCR, not by verification. If a task needs a password typed, ask
  the user to provide it to BLAXCY directly.
- Protected and blocked applications stay protected. Content from a protected
  application is never sent to you, and a blocked application cannot be acted
  on, in any mode, with no override.

How to work economically (this does not relax any safety rule):
- Prefer one well-specified request over many small ones when you already know
  the ordered workflow (for example: focus a search field, type a query, submit,
  open the first result). Use `run_sequence` for that, but ONLY when you are
  confident at every step. If a step may need a decision based on what the
  screen looks like after the previous step, issue it as a separate call
  instead: a sequence halts on ambiguity or failed verification rather than
  letting you branch mid-plan.
- Observe before you act when you are unsure. `get_screen_state` and
  `find_element` are read-only and cheap; a wrong click is neither.
- Do not re-observe what you already know has not changed, and do not ask for a
  full screen dump when a target description will do. BLAXCY sends you state
  deltas, not whole screens, for the same reason.
- Keep your own replies short: say what you did, what the result was, and what
  you will do next.
"""


def system_instruction(
    *,
    tool_names: list[str] | None = None,
    mode: str | None = None,
    extra_rules: list[str] | None = None,
) -> str:
    """Build the system instruction for a session (section 67).

    Args:
        tool_names: The tools actually advertised this session. Listing them
            keeps the instruction truthful when a tool is withheld (for example
            ``run_sequence`` before Phase 10.1).
        mode: The current policy mode, so the Brain knows what is permitted now
            (section 56). Modes can change underneath a session; the caller is
            expected to call this again when that happens.
        extra_rules: Additional operator-supplied rules, appended verbatim.
    """
    parts = [_BASE_INSTRUCTION.rstrip()]
    if tool_names:
        parts.append("Tools available this session: " + ", ".join(sorted(tool_names)) + ".")
    if mode:
        parts.append(f"Current policy mode: {mode}. Work within it; it can be changed by the user.")
    parts.append(
        "Reminder: at most "
        f"{MAX_CONTEXT_ELEMENTS} elements are attached to any state snapshot you receive."
    )
    for rule in extra_rules or ():
        parts.append(str(rule))
    return "\n".join(parts)


def tool_guidance(tool: str) -> str:
    """Per-tool guidance text, for a caller assembling its own prompt."""
    from ai.tool_protocol import TOOL_DESCRIPTIONS

    return TOOL_DESCRIPTIONS.get(tool, f"{tool}: no declared guidance")


def context_preamble(state: dict[str, Any] | None) -> str:
    """A short, human-readable preamble for an attached state snapshot.

    Kept as text (not JSON) because the Brain already receives the machine
    readable form; this is the one-line orientation that makes a delta snapshot
    readable without re-dumping the screen (section 68.1).
    """
    if not state:
        return "No screen state has been observed yet."
    if state.get("unchanged"):
        return (
            f"Screen unchanged since state_version {state.get('state_version')} "
            f"(generation {state.get('generation')})."
        )
    app = state.get("active_app") or "unknown"
    return (
        f"Screen state_version {state.get('state_version')} "
        f"(frame {state.get('frame_id')}, generation {state.get('generation')}); "
        f"active window {state.get('active_window_id')} in {app}."
    )


__all__ = ["context_preamble", "system_instruction", "tool_guidance"]
