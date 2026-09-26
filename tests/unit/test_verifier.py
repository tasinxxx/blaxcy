"""Verification semantics (specification section 60).

The boundary between ``UNVERIFIED`` and ``CONTRADICTED`` is the whole point of
this module, so most of these tests are about which one an ambiguous-looking
situation actually is.
"""

from __future__ import annotations

from config.settings import VerificationSettings
from control.verifier import Postcondition, Verifier
from schemas.enums import ActionClass, ChangeClass, CoordinateSpace, UIRole, VerificationState
from schemas.geometry import Rect
from schemas.screen_state import ChangeRegion, ScreenDelta
from tests.harness.phase8 import DEFAULT_BOX, make_delta, make_element, make_state, make_text_input


def _verifier(**overrides: object) -> Verifier:
    """A verifier over default settings with optional overrides."""
    return Verifier(VerificationSettings.model_validate(overrides))


# -- Applicability ------------------------------------------------------------

def test_not_applicable_is_its_own_state() -> None:
    """Read-only work has no postcondition and says so."""
    outcome = _verifier().not_applicable()
    assert outcome.state is VerificationState.NOT_APPLICABLE
    assert outcome.postcondition is Postcondition.NONE


def test_verification_requirement_by_class() -> None:
    """Mutating verification follows configuration; destructive is always required."""
    assert _verifier().requires_verification(ActionClass.READ_ONLY) is False
    assert _verifier().requires_verification(ActionClass.MUTATING) is True
    assert _verifier().requires_verification(ActionClass.DESTRUCTIVE) is True
    relaxed = _verifier(require_verification_for_mutating=False)
    assert relaxed.requires_verification(ActionClass.MUTATING) is False
    assert relaxed.requires_verification(ActionClass.DESTRUCTIVE) is True


# -- Screen-change postcondition ---------------------------------------------

def test_no_observation_is_unverified_never_verified() -> None:
    """Without an observation nothing can be established."""
    outcome = _verifier().verify_screen_change(before=None, after=None, delta=None)
    assert outcome.state is VerificationState.UNVERIFIED


def test_no_delta_is_unverified() -> None:
    """An unclassified frame step does not establish a user-visible outcome."""
    before = make_state(frame_id=1)
    after = make_state(frame_id=2)
    outcome = _verifier().verify_screen_change(before=before, after=after, delta=None)
    assert outcome.state is VerificationState.UNVERIFIED


def test_trivial_change_does_not_verify() -> None:
    """Cursor blink and animation are noise, not outcomes."""
    before = make_state(frame_id=1)
    after = make_state(frame_id=2)
    delta = make_delta(before=before, after=after, change_class=ChangeClass.TRIVIAL)
    outcome = _verifier().verify_screen_change(before=before, after=after, delta=delta)
    assert outcome.state is VerificationState.UNVERIFIED


def test_meaningful_change_verifies() -> None:
    """A structural change establishes the postcondition."""
    before = make_state(frame_id=1)
    after = make_state(frame_id=2)
    delta = make_delta(before=before, after=after, change_class=ChangeClass.MEANINGFUL)
    outcome = _verifier().verify_screen_change(before=before, after=after, delta=delta)
    assert outcome.state is VerificationState.VERIFIED
    assert outcome.postcondition is Postcondition.SCREEN_CHANGED


def test_change_elsewhere_does_not_verify_a_targeted_action() -> None:
    """A repaint away from the target is not this action's result."""
    before = make_state(frame_id=1)
    after = make_state(frame_id=2)
    far_away = Rect(x=1500.0, y=900.0, width=100.0, height=50.0, space=CoordinateSpace.DESKTOP)
    delta = ScreenDelta(
        from_frame_id=1,
        to_frame_id=2,
        from_state_version=before.state_version,
        to_state_version=after.state_version,
        generation=after.generation,
        regions=(ChangeRegion(rect=far_away, change_class=ChangeClass.MEANINGFUL),),
        computed_at=1000.0,
    )
    outcome = _verifier().verify_screen_change(
        before=before, after=after, delta=delta, target=make_element()
    )
    assert outcome.state is VerificationState.UNVERIFIED
    assert outcome.evidence["target_overlap"] is False


def test_active_window_change_counts_as_evidence() -> None:
    """A focus/window transition is reported as evidence, not as a pixel change."""
    before = make_state(frame_id=1, active_window_id=42)
    after = make_state(frame_id=2, active_window_id=99)
    delta = ScreenDelta(
        from_frame_id=1,
        to_frame_id=2,
        from_state_version=before.state_version,
        to_state_version=after.state_version,
        generation=after.generation,
        regions=(),
        computed_at=1000.0,
    )
    outcome = _verifier().verify_screen_change(
        before=before, after=after, delta=delta, target=make_element(bbox=DEFAULT_BOX)
    )
    # An empty-region delta is a NONE change, so there is no structural evidence;
    # the window transition alone is reported inside the evidence, not as success.
    assert outcome.state is VerificationState.UNVERIFIED
    assert outcome.evidence["active_window_changed"] is True


# -- Text postcondition -------------------------------------------------------

def test_readable_field_containing_the_text_verifies() -> None:
    """A readable field that shows the text establishes the postcondition."""
    typed = make_text_input()
    after = make_state(elements=(make_text_input(text="hello world"),))
    outcome = _verifier().verify_text(after=after, target=typed, text="hello world")
    assert outcome.state is VerificationState.VERIFIED
    assert outcome.postcondition is Postcondition.TEXT_PRESENT


def test_readable_field_missing_the_text_contradicts() -> None:
    """Positive contradicting evidence is not the same as missing evidence."""
    typed = make_text_input()
    after = make_state(elements=(make_text_input(text="something else"),))
    outcome = _verifier().verify_text(after=after, target=typed, text="hello")
    assert outcome.state is VerificationState.CONTRADICTED


def test_unreadable_field_is_unverified_not_contradicted() -> None:
    """A field that reports no text cannot contradict anything."""
    typed = make_text_input(text=None)
    after = make_state(elements=(make_text_input(text=None),))
    outcome = _verifier().verify_text(after=after, target=typed, text="hello")
    assert outcome.state is VerificationState.UNVERIFIED


def test_password_typing_is_unverifiable() -> None:
    """Credential fields are never read back (section 55)."""
    field = make_text_input("pw", role=UIRole.PASSWORD_INPUT, password=True, text=None)
    outcome = _verifier().verify_text(after=make_state(elements=(field,)), target=field, text="secret")
    assert outcome.state is VerificationState.UNVERIFIED
    assert "password" in outcome.reason or "credential" in outcome.reason


def test_missing_typed_target_is_unverified() -> None:
    """A vanished field gives no evidence either way about the text."""
    outcome = _verifier().verify_text(after=make_state(), target=make_text_input(), text="hello")
    assert outcome.state is VerificationState.UNVERIFIED


def test_no_observation_for_text_is_unverified() -> None:
    """Without an observation typing cannot be confirmed."""
    outcome = _verifier().verify_text(after=None, target=make_text_input(), text="hello")
    assert outcome.state is VerificationState.UNVERIFIED# -- Selection postcondition --------------------------------------------------


def test_a_selected_target_is_positive_selection_evidence() -> None:
    """Section 60: a selectable control's own state settles a click.

    This is stronger evidence than the pixels the control repainted, and it is
    immune to the section 34 temporal layer reclassifying a repeated region as
    ``ANIMATION`` -- which is what made clicking a result row unverifiable when the
    previous step had already changed that region as a side effect.
    """
    item = make_element("row", role=UIRole.LIST_ITEM, text="Result 1", selected=True)
    outcome = _verifier().verify_selection(after=make_state(elements=(item,)), target=item)

    assert outcome is not None
    assert outcome.state is VerificationState.VERIFIED
    assert outcome.postcondition is Postcondition.SELECTED


def test_unreported_or_negative_selection_is_not_a_verdict() -> None:
    """Positive-only: the selection path never manufactures or contradicts a result."""
    unselected = make_element("row", role=UIRole.LIST_ITEM, text="Result 1", selected=False)
    unknown = make_element("row", role=UIRole.LIST_ITEM, text="Result 1")
    assert unknown.selected is None

    for element in (unselected, unknown):
        assert (
            _verifier().verify_selection(after=make_state(elements=(element,)), target=element)
            is None
        )

    assert _verifier().verify_selection(after=None, target=unselected) is None


# -- Window postcondition -----------------------------------------------------


def test_active_window_matching_verifies() -> None:
    """The requested window being active establishes the postcondition."""
    outcome = _verifier().verify_window(after=make_state(active_window_id=7), window_id=7)
    assert outcome.state is VerificationState.VERIFIED
    assert outcome.postcondition is Postcondition.WINDOW_ACTIVE


def test_different_active_window_contradicts() -> None:
    """A different active window is positive contradicting evidence."""
    outcome = _verifier().verify_window(after=make_state(active_window_id=8), window_id=7)
    assert outcome.state is VerificationState.CONTRADICTED


def test_unknown_active_window_is_unverified() -> None:
    """An unobservable active window cannot confirm or deny activation."""
    outcome = _verifier().verify_window(after=make_state(active_window_id=None), window_id=7)
    assert outcome.state is VerificationState.UNVERIFIED
