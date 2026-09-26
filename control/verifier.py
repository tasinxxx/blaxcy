"""Verification semantics (specification section 60).

Section 60 defines three outcomes and BLAXCY is not allowed to blur them:

* ``VERIFIED`` -- the action executed **and** the expected postcondition was
  positively established from evidence.
* ``UNVERIFIED`` -- the action executed, but there was not enough evidence to
  establish the postcondition. This is the honest default, never a soft
  version of success.
* ``CONTRADICTED`` -- evidence indicates the action did not produce the expected
  outcome. This requires *positive* contradicting evidence, not merely the
  absence of confirmation.

The distinction that matters most is between the last two. "I could not read the
text field" is ``UNVERIFIED``; "I read the text field and my text is not in it"
is ``CONTRADICTED``. Confusing those is how a body ends up reporting success it
cannot prove, or a failure that did not happen.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from config.settings import VerificationSettings
from schemas.elements import UIElement
from schemas.enums import (
    CHANGE_CLASS_SEVERITY,
    ActionClass,
    ChangeClass,
    UIRole,
    VerificationState,
)
from schemas.screen_state import ScreenDelta, ScreenState


class Postcondition(StrEnum):
    """The kind of evidence an action's success is judged on (section 60)."""

    NONE = "NONE"
    SCREEN_CHANGED = "SCREEN_CHANGED"
    TEXT_PRESENT = "TEXT_PRESENT"
    WINDOW_ACTIVE = "WINDOW_ACTIVE"
    SELECTED = "SELECTED"


#: Roles whose click postcondition is "this control is now selected". The
#: accessibility tree reports that directly, which is stronger evidence than the
#: pixels the control repainted (section 60).
SELECTABLE_ROLES: frozenset[UIRole] = frozenset(
    {UIRole.LIST_ITEM, UIRole.TREE_ITEM, UIRole.TAB, UIRole.MENU_ITEM}
)


@dataclass(frozen=True)
class VerificationOutcome:
    """One verification verdict, with the evidence behind it."""

    state: VerificationState
    postcondition: Postcondition
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """True only for ``VERIFIED``."""
        return self.state is VerificationState.VERIFIED

    def to_dict(self) -> dict[str, Any]:
        """JSON-shaped verdict for the tool envelope and the GUI."""
        return {
            "state": self.state.value,
            "postcondition": self.postcondition.value,
            "reason": self.reason,
            "evidence": self.evidence,
        }


def _find_element(state: ScreenState | None, element: UIElement) -> UIElement | None:
    """Find ``element`` in ``state`` by identity first, then by element id."""
    if state is None:
        return None
    identity = element.identity
    for candidate in state.elements:
        if candidate.identity == identity:
            return candidate
    return state.element_by_id(element.element_id)


def _normalize_text(value: str) -> str:
    """Case-folded, whitespace-collapsed text for a containment check."""
    return " ".join(value.split()).casefold()


class Verifier:
    """Establishes postconditions from real observations (section 60).

    Args:
        settings: The ``[verification]`` configuration. ``require_verification_for_mutating``
            decides whether an unverified mutating action is a failure or merely
            an unverified success; either way the reported state is honest.
    """

    def __init__(self, settings: VerificationSettings | None = None) -> None:
        self._settings = settings if settings is not None else VerificationSettings()

    @property
    def require_for_mutating(self) -> bool:
        """Whether a mutating action's verification is mandatory."""
        return self._settings.require_verification_for_mutating

    def requires_verification(self, action_class: ActionClass) -> bool:
        """Default verification requirement for ``action_class`` (section 66.1)."""
        if action_class is ActionClass.READ_ONLY:
            return False
        if action_class is ActionClass.DESTRUCTIVE:
            return True
        if action_class is ActionClass.MUTATING:
            return self._settings.require_verification_for_mutating
        return False

    # -- Verdicts -------------------------------------------------------------

    def not_applicable(self, reason: str = "no postcondition applies to this action") -> VerificationOutcome:
        """A read-only action with nothing to verify."""
        return VerificationOutcome(VerificationState.NOT_APPLICABLE, Postcondition.NONE, reason)

    def unverified(self, reason: str, **evidence: Any) -> VerificationOutcome:
        """Evidence was insufficient to establish the postcondition."""
        return VerificationOutcome(VerificationState.UNVERIFIED, Postcondition.NONE, reason, evidence)

    def verify_screen_change(
        self,
        *,
        before: ScreenState | None,
        after: ScreenState | None,
        delta: ScreenDelta | None,
        target: UIElement | None = None,
        reason_context: str = "action",
    ) -> VerificationOutcome:
        """Judge an action whose postcondition is "the desktop responded".

        Used for click, scroll and key actions where the expected evidence is a
        structural change to the screen. A change that is not at least
        ``MEANINGFUL`` does not establish anything: cursor blink and animations
        are noise, not outcomes.

        When ``target`` is supplied the change must overlap the target's region
        (or the active window must have changed), so an unrelated repaint
        elsewhere cannot be counted as this action's result.
        """
        if after is None:
            return self.unverified(f"no post-action observation available to verify the {reason_context}")
        if delta is None:
            return self.unverified(
                f"no classified delta available to verify the {reason_context}",
                after_frame_id=after.frame_id,
            )

        change_class = delta.change_class
        severity = CHANGE_CLASS_SEVERITY[change_class]
        window_changed = (
            before is not None and after.active_window_id != before.active_window_id
        )
        evidence: dict[str, Any] = {
            "change_class": change_class.value,
            "after_frame_id": after.frame_id,
            "active_window_changed": window_changed,
        }

        if severity < CHANGE_CLASS_SEVERITY[ChangeClass.MEANINGFUL]:
            return self.unverified(
                f"only a {change_class.value} change followed the {reason_context}; "
                "that does not establish a user-visible outcome",
                **evidence,
            )

        if target is not None and target.bbox is not None:
            overlaps = delta.affects(target.bbox)
            evidence["target_overlap"] = overlaps
            if not overlaps and not window_changed:
                return self.unverified(
                    f"a {change_class.value} change occurred, but not at the {reason_context} target",
                    **evidence,
                )

        return VerificationOutcome(
            VerificationState.VERIFIED,
            Postcondition.SCREEN_CHANGED,
            f"a {change_class.value} change followed the {reason_context}",
            evidence,
        )

    def verify_text(
        self,
        *,
        after: ScreenState | None,
        target: UIElement,
        text: str,
    ) -> VerificationOutcome:
        """Judge a typing action against the field's observed content.

        Only readable content can establish anything. A password field is never
        readable (section 55) and reports ``UNVERIFIED`` -- BLAXCY does not get
        to claim a credential was entered correctly by inspecting it.
        """
        if after is None:
            return self.unverified("no post-action observation available to verify typing")
        if target.is_password:
            return self.unverified(
                "credential fields are never read back; typing into a password field is unverifiable"
            )
        observed = _find_element(after, target)
        if observed is None:
            return self.unverified(
                "the typed-into element is no longer observable, so its content cannot be confirmed",
                element_id=target.element_id,
            )
        if observed.is_password:
            return self.unverified("the target is now a credential field and must not be read")
        if observed.text is None:
            return self.unverified(
                "the target reports no readable text, so the typed content cannot be confirmed",
                element_id=observed.element_id,
            )
        if _normalize_text(text) in _normalize_text(observed.text):
            return VerificationOutcome(
                VerificationState.VERIFIED,
                Postcondition.TEXT_PRESENT,
                "the typed text is present in the target's observed content",
                {"element_id": observed.element_id, "characters": len(text)},
            )
        return VerificationOutcome(
            VerificationState.CONTRADICTED,
            Postcondition.TEXT_PRESENT,
            "the target is readable and does not contain the typed text",
            {"element_id": observed.element_id, "characters": len(text)},
        )

    def verify_selection(
        self,
        *,
        after: ScreenState | None,
        target: UIElement,
    ) -> VerificationOutcome | None:
        """Positive-only evidence that a click selected ``target`` (section 60).

        Clicking a selectable control's postcondition is "this control is now
        selected", and the accessibility tree reports that directly. That is
        *stronger* evidence than the pixels the control repainted, and it is what
        makes a result-list click verifiable when its repaint is reclassified
        ``ANIMATION`` by the section 34 temporal layer -- which happens whenever the
        same region also changed as a side effect of the previous step.

        Deliberately positive-only: ``None`` means "this observation says nothing
        about selection", and the caller falls back to the ordinary screen-change
        check. A selection that is unreported, or reported ``False``, is never taken
        as a contradiction, because a click may legitimately do something else (and
        an unreadable state is missing evidence, not contradicting evidence).
        """
        if after is None:
            return None
        observed = _find_element(after, target)
        if observed is None or observed.selected is not True:
            return None
        return VerificationOutcome(
            VerificationState.VERIFIED,
            Postcondition.SELECTED,
            "the target reports itself selected after the click",
            {"element_id": observed.element_id},
        )

    def verify_window(self, *, after: ScreenState | None, window_id: int) -> VerificationOutcome:
        """Judge a window-activation action against the observed active window."""
        if after is None:
            return self.unverified("no post-action observation available to verify window activation")
        if after.active_window_id is None:
            return self.unverified(
                "the active window is not observable, so activation cannot be confirmed"
            )
        if after.active_window_id == window_id:
            return VerificationOutcome(
                VerificationState.VERIFIED,
                Postcondition.WINDOW_ACTIVE,
                "the requested window is the active window",
                {"window_id": window_id},
            )
        return VerificationOutcome(
            VerificationState.CONTRADICTED,
            Postcondition.WINDOW_ACTIVE,
            "a different window is active after the activation attempt",
            {"window_id": window_id, "active_window_id": after.active_window_id},
        )
