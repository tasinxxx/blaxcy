"""UI elements, identity and confidence (specification sections 37-39)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from schemas.elements import (
    ElementCandidate,
    ElementQuery,
    UIElement,
    identity_fingerprint,
    perception_confidence,
)
from schemas.enums import (
    SOURCE_BASE_CONFIDENCE,
    CoordinateSpace,
    PerceptionSource,
    UIRole,
)
from schemas.geometry import Point, Rect


def _element(**overrides: object) -> UIElement:
    """Build a valid button element, overriding fields as needed."""
    base: dict[str, object] = {
        "element_id": "e1",
        "role": UIRole.BUTTON,
        "text": "Send",
        "source": PerceptionSource.ATSPI,
        "confidence": 0.95,
        "atspi_path": "/desktop/frame[0]/button[3]",
    }
    base.update(overrides)
    return UIElement.model_validate(base)


def test_password_element_cannot_carry_text() -> None:
    """Credential content must never be stored (sections 42, 55)."""
    with pytest.raises(ValidationError):
        _element(role=UIRole.PASSWORD_INPUT, password=True, text="hunter2")


def test_password_role_requires_password_flag() -> None:
    """A PASSWORD_INPUT element must be explicitly flagged as a password."""
    with pytest.raises(ValidationError):
        _element(role=UIRole.PASSWORD_INPUT, password=False, text=None)
    element = _element(role=UIRole.PASSWORD_INPUT, password=True, text=None)
    assert element.is_password is True


def test_bbox_and_center_must_share_space() -> None:
    """Geometry is single-space; a mismatch is rejected."""
    with pytest.raises(ValidationError):
        _element(
            bbox=Rect(x=0, y=0, width=10, height=10, space=CoordinateSpace.DESKTOP),
            center=Point(x=5, y=5, space=CoordinateSpace.FRAME),
        )


def test_coordinate_space_must_agree_with_geometry() -> None:
    """The explicit space field cannot contradict the geometry it describes."""
    with pytest.raises(ValidationError):
        _element(
            bbox=Rect(x=0, y=0, width=10, height=10, space=CoordinateSpace.DESKTOP),
            coordinate_space=CoordinateSpace.MONITOR,
        )


def test_is_actionable_requires_visible_enabled_clickable() -> None:
    """Actionability is necessary but never sufficient (sections 44-46)."""
    actionable = _element(clickable=True, effective_clickable=True)
    assert actionable.is_actionable is True
    assert _element(clickable=True, occluded=True).is_actionable is False
    assert _element(clickable=True, enabled=False).is_actionable is False
    assert _element(clickable=True, visible=False).is_actionable is False
    assert _element(clickable=False, effective_clickable=False).is_actionable is False


def test_text_entry_role_classification() -> None:
    """Text-entry roles drive the focus guard (section 51)."""
    assert _element(role=UIRole.TEXT_INPUT, text=None).is_text_entry is True
    assert _element(role=UIRole.BUTTON).is_text_entry is False


def test_identity_prefers_accessibility_path() -> None:
    """An identity path survives moves and restyles, so it is preferred."""
    element = _element()
    assert identity_fingerprint(element) == "atspi:/desktop/frame[0]/button[3]"
    dom = _element(atspi_path=None, dom_path="/html/body/button[1]")
    assert identity_fingerprint(dom) == "dom:/html/body/button[1]"


def test_name_based_identity_is_stable_and_role_aware() -> None:
    """Without a path, identity hashes role + label + window."""
    a = _element(atspi_path=None, owner_window_id=42)
    b = _element(element_id="e2", atspi_path=None, owner_window_id=42)
    c = _element(element_id="e3", atspi_path=None, owner_window_id=99)
    assert identity_fingerprint(a) == identity_fingerprint(b)
    assert identity_fingerprint(a) != identity_fingerprint(c)
    assert a.matches_identity(b) is True


def test_perception_confidence_uses_source_base_rates() -> None:
    """Section 39: source base rate times the three multipliers."""
    assert perception_confidence(PerceptionSource.ATSPI) == pytest.approx(
        SOURCE_BASE_CONFIDENCE[PerceptionSource.ATSPI]
    )
    assert perception_confidence(PerceptionSource.OCR) == pytest.approx(0.75)
    assert perception_confidence(PerceptionSource.VISUAL, geometry_sanity=0.5) == pytest.approx(0.40)


def test_perception_confidence_rejects_bad_multipliers() -> None:
    """Multipliers outside [0, 1] are not meaningful."""
    with pytest.raises(ValueError):
        perception_confidence(PerceptionSource.ATSPI, freshness=1.5)


def test_element_query_needs_a_selector() -> None:
    """A query with neither text nor role cannot be resolved."""
    with pytest.raises(ValidationError):
        ElementQuery()
    assert ElementQuery(text="Send").normalized_text == "send"
    assert ElementQuery(text="  Send   Message ").normalized_text == "send message"


def test_candidate_reports_score_and_element() -> None:
    """A candidate carries the element and its score breakdown."""
    element = _element(bbox=Rect(x=0, y=0, width=10, height=10, space=CoordinateSpace.DESKTOP))
    candidate = ElementCandidate(element=element, score=0.9, breakdown={"text": 0.4})
    assert candidate.element_id == "e1"
    payload = candidate.to_dict()
    assert payload["role"] == "BUTTON"
    assert payload["score"] == 0.9
    assert payload["bbox"] is not None
