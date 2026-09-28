"""Verification semantics (specification section 60).

The boundary between ``UNVERIFIED`` and ``CONTRADICTED`` is the whole point of
this module, so most of these tests are about which one an ambiguous-looking
situation actually is.
"""

from __future__ import annotations

from config.settings import VerificationSettings
from control.verifier import Postcondition, Verifier
from schemas.elements import UIElement
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


def test_recreated_text_field_with_same_identity_hints_can_verify() -> None:
    """Transient AT-SPI node recreation must not erase positive typing evidence."""
    target = make_text_input(
        "old-field",
        accessible_name="Search with Google or enter address",
        atspi_path="/old",
        owner_window_id=42,
        bbox=DEFAULT_BOX,
    )
    recreated = make_text_input(
        "new-field",
        accessible_name="Search with Google or enter address",
        atspi_path="/new",
        owner_window_id=42,
        bbox=DEFAULT_BOX,
        text="https://www.youtube.com",
    )
    outcome = _verifier().verify_text(
        after=make_state(elements=(recreated,)),
        target=target,
        text="https://www.youtube.com",
    )
    assert outcome.state is VerificationState.VERIFIED
    assert outcome.postcondition is Postcondition.TEXT_PRESENT


def test_recreated_text_field_without_shared_application_identity_stays_unverified() -> None:
    """A matching label alone is not enough to attribute another field."""
    target = make_text_input(
        "old-field",
        accessible_name="Search with Google or enter address",
        atspi_path="/old",
        owner_window_id=42,
        bbox=DEFAULT_BOX,
    )
    unrelated = make_text_input(
        "other-field",
        accessible_name="Search with Google or enter address",
        atspi_path="/other",
        owner_window_id=99,
        bbox=DEFAULT_BOX,
        text="https://www.youtube.com",
    )
    outcome = _verifier().verify_text(
        after=make_state(elements=(unrelated,)),
        target=target,
        text="https://www.youtube.com",
    )
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


# -- Element-state postcondition ----------------------------------------------


def _click_target(**overrides: object) -> UIElement:
    """A plain push button to attribute a state transition to."""
    return make_element("submit", text="Submit", atspi_path="/p/submit", **overrides)


def _play_button(element_id: str, **overrides: object) -> UIElement:
    """A control that the click enables, sharing the target's window."""
    base: dict[str, object] = {
        "text": "Play Button",
        "atspi_path": "/p/play",
        "enabled": False,
        "owner_window_id": 42,
    }
    base.update(overrides)
    return make_element(element_id, **base)


def test_a_reported_enable_transition_verifies() -> None:
    """Section 60: a same-application control becoming enabled is real evidence.

    This is independent of the pixel delta, so it survives the section 34 temporal
    layer reclassifying a repeated region and a delta that does not line up with
    the observation -- both of which leave a genuine click ``UNVERIFIED``.
    """
    before = make_state(elements=(_click_target(), _play_button("play_before")))
    after = make_state(elements=(_click_target(), _play_button("play_after", enabled=True)))
    outcome = _verifier().verify_element_state_change(
        before=before, after=after, target=_click_target()
    )

    assert outcome is not None
    assert outcome.state is VerificationState.VERIFIED
    assert outcome.postcondition is Postcondition.ELEMENT_STATE
    assert outcome.evidence["state"] == "enabled"


def test_element_state_evidence_needs_both_observations_and_a_target() -> None:
    """Without a before/after pair and a target there is no transition to judge."""
    after = make_state(elements=(_play_button("play_after", enabled=True),))
    verifier = _verifier()
    assert verifier.verify_element_state_change(
        before=None, after=after, target=_click_target()
    ) is None
    assert verifier.verify_element_state_change(
        before=after, after=None, target=_click_target()
    ) is None
    assert verifier.verify_element_state_change(before=after, after=after) is None


def test_a_newly_appearing_control_is_never_evidence() -> None:
    """Positive-only and truncation-robust: only a control seen in both counts."""
    before = make_state(elements=(_click_target(),))
    after = make_state(elements=(_click_target(), _play_button("play_after", enabled=True)))
    assert (
        _verifier().verify_element_state_change(
            before=before, after=after, target=_click_target()
        )
        is None
    )


def test_a_transition_in_another_application_is_not_evidence() -> None:
    """The same causality bound the screen-change overlap rule enforces."""
    before = make_state(elements=(_click_target(), _play_button("play_before", owner_window_id=99)))
    after = make_state(
        elements=(
            _click_target(),
            _play_button("play_after", enabled=True, owner_window_id=99),
        )
    )
    assert (
        _verifier().verify_element_state_change(
            before=before, after=after, target=_click_target()
        )
        is None
    )


def test_unchanged_or_reverse_transitions_are_not_evidence() -> None:
    """Only false-to-true is a response; nothing else is turned into a verdict."""
    verifier = _verifier()
    for earlier_enabled, later_enabled in ((True, True), (True, False), (False, False)):
        before = make_state(
            elements=(_click_target(), _play_button("play_before", enabled=earlier_enabled))
        )
        after = make_state(
            elements=(_click_target(), _play_button("play_after", enabled=later_enabled))
        )
        assert (
            verifier.verify_element_state_change(
                before=before, after=after, target=_click_target()
            )
            is None
        )


def test_the_bound_falls_back_to_the_application_name() -> None:
    """When no window id is reported, a matching application name still bounds it."""
    verifier = _verifier()
    target = _click_target(owner_window_id=None, owner_app="fixture")
    before = make_state(
        elements=(target, _play_button("play_before", owner_window_id=None, owner_app="fixture"))
    )
    after = make_state(
        elements=(
            target,
            _play_button("play_after", enabled=True, owner_window_id=None, owner_app="fixture"),
        )
    )
    outcome = verifier.verify_element_state_change(before=before, after=after, target=target)
    assert outcome is not None and outcome.state is VerificationState.VERIFIED

    # A different application, or no bound at all, yields nothing.
    other_app = make_state(
        elements=(
            target,
            _play_button("play_after", enabled=True, owner_window_id=None, owner_app="other"),
        )
    )
    assert verifier.verify_element_state_change(before=before, after=other_app, target=target) is None
    unbounded = make_state(
        elements=(
            target,
            _play_button("play_after", enabled=True, owner_window_id=None, owner_app=None),
        )
    )
    assert verifier.verify_element_state_change(before=before, after=unbounded, target=target) is None


def test_an_invisible_control_transition_is_not_evidence() -> None:
    """A control that was not really shown cannot have become usable."""
    before = make_state(
        elements=(_click_target(), _play_button("play_before", visible=False))
    )
    after = make_state(elements=(_click_target(), _play_button("play_after", enabled=True)))
    assert (
        _verifier().verify_element_state_change(
            before=before, after=after, target=_click_target()
        )
        is None
    )


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
