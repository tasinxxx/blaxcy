"""Target resolution, occlusion and lease binding (specification sections 43-46).

The resolver is deterministic and pure, so these tests assert exact stages,
scores and decisions rather than "it returned something". The two properties
that matter most here are the section 43 **absolute ambiguity rule** (never
out-voted by confidence) and the section 46 **occlusion rule** (never click
through another window).
"""

from __future__ import annotations

import pytest

from config.settings import ResolverSettings
from core.target_resolver import (
    DEFAULT_OCCLUSION_THRESHOLD,
    ResolutionStatus,
    TargetResolver,
    assess_occlusion,
    bind_lease,
)
from schemas.elements import ElementQuery, UIElement
from schemas.enums import CoordinateSpace, PerceptionSource, UIRole
from schemas.geometry import MonitorGeometry, MonitorLayout, Rect
from schemas.screen_state import ScreenState

_BOX = Rect(x=0.0, y=0.0, width=100.0, height=40.0, space=CoordinateSpace.DESKTOP)


def _element(element_id: str = "e1", **overrides: object) -> UIElement:
    """Build a clickable AT-SPI button, overriding fields as needed."""
    base: dict[str, object] = {
        "element_id": element_id,
        "role": UIRole.BUTTON,
        "text": "Send",
        "source": PerceptionSource.ATSPI,
        "confidence": 0.95,
        "clickable": True,
        "effective_clickable": True,
        "bbox": _BOX,
        "atspi_path": f"/p/{element_id}",
    }
    base.update(overrides)
    return UIElement.model_validate(base)


def _layout() -> MonitorLayout:
    """A single-monitor layout for state fixtures."""
    return MonitorLayout(
        monitors=(MonitorGeometry(monitor_id=0, width=1920, height=1080, is_primary=True),)
    )


def _state(frame_id: int = 5, *, state_version: int = 3, generation: int = 2) -> ScreenState:
    """A minimal perceived state carrying the stamps a lease binds to."""
    return ScreenState(
        frame_id=frame_id,
        state_version=state_version,
        generation=generation,
        timestamp=1000.0,
        monotonic=float(frame_id),
        layout=_layout(),
    )


def _resolver(**overrides: object) -> TargetResolver:
    """A resolver over default (specification) settings, with overrides."""
    return TargetResolver(ResolverSettings.model_validate(overrides))


# -- Resolution stages and scoring (section 43) -------------------------------


def test_exact_text_resolves_at_exact_stage() -> None:
    """An exact visible-text match is the strongest, earliest cascade stage."""
    result = _resolver().resolve(ElementQuery(text="Send"), [_element()])
    assert result.status is ResolutionStatus.RESOLVED
    assert result.matched_stage == "exact_text"
    assert result.best is not None
    assert result.best.breakdown["text"] == pytest.approx(1.0)
    # 0.40*1.0 + 0.20*0.95 + 0.15 + 0.15 + 0.10
    assert result.best.score == pytest.approx(0.99)
    assert result.candidates[0].element_id == "e1"


def test_case_insensitive_and_normalized_stages_are_distinct() -> None:
    """Case-only and whitespace-only differences are weaker than exact text."""
    case = _resolver().resolve(ElementQuery(text="Send"), [_element(text="send")])
    assert case.matched_stage == "case_insensitive_text"
    normalized = _resolver().resolve(
        ElementQuery(text="Send Message"), [_element(text="Send   Message")]
    )
    assert normalized.matched_stage == "normalized_text"


def test_accessible_name_is_a_later_weaker_stage() -> None:
    """With no visible text, the accessible name still resolves (section 37)."""
    element = _element(text=None, accessible_name="Send icon")
    result = _resolver().resolve(ElementQuery(text="Send icon"), [element])
    assert result.status is ResolutionStatus.RESOLVED
    assert result.matched_stage == "accessible_name"
    assert result.best is not None
    assert result.best.breakdown["text"] == pytest.approx(0.95)


def test_role_only_query_is_neutral_on_text() -> None:
    """A query that constrains only the role is not penalised on text."""
    result = _resolver().resolve(ElementQuery(role_hint=UIRole.BUTTON), [_element()])
    assert result.status is ResolutionStatus.RESOLVED
    assert result.matched_stage == "role"
    assert result.best is not None
    assert result.best.breakdown["text"] == pytest.approx(1.0)
    assert result.best.breakdown["role"] == pytest.approx(1.0)


def test_role_hint_mismatch_lowers_score_below_threshold() -> None:
    """A weak substring match plus a role mismatch falls under act_threshold."""
    element = _element(
        role=UIRole.LINK,
        text="Send message",
        source=PerceptionSource.OCR,
        confidence=0.75,
        atspi_path=None,
        owner_app="chat",
    )
    query = ElementQuery(text="Send", role_hint=UIRole.BUTTON, context="email")
    result = _resolver().resolve(query, [element])
    assert result.status is ResolutionStatus.BELOW_THRESHOLD
    assert result.best is None
    assert result.candidates


def test_act_threshold_is_enforced() -> None:
    """A candidate under a raised threshold is never auto-selected."""
    result = _resolver(act_threshold=0.999).resolve(ElementQuery(text="Send"), [_element()])
    assert result.status is ResolutionStatus.BELOW_THRESHOLD


def test_not_found_when_nothing_matches() -> None:
    """No textual/role/context match means honest absence, never a guess."""
    result = _resolver().resolve(ElementQuery(text="Nonexistent"), [_element()])
    assert result.status is ResolutionStatus.NOT_FOUND
    assert result.element is None
    assert result.candidates == ()


def test_require_actionable_filters_non_clickable_elements() -> None:
    """Non-clickable perception is not a target unless the caller asks for it."""
    element = _element(text="Note", clickable=False, effective_clickable=False)
    assert (
        _resolver().resolve(ElementQuery(text="Note"), [element]).status
        is ResolutionStatus.NOT_FOUND
    )
    relaxed = _resolver().resolve(
        ElementQuery(text="Note", require_actionable=False), [element]
    )
    assert relaxed.status is ResolutionStatus.RESOLVED


def test_window_id_filter_excludes_other_windows() -> None:
    """A window-scoped query considers only elements in that window."""
    mine = _element("mine", owner_window_id=42)
    theirs = _element("theirs", text="Other", owner_window_id=7, atspi_path="/p/theirs")
    result = _resolver().resolve(ElementQuery(text="Send", window_id=42), [mine, theirs])
    assert result.status is ResolutionStatus.RESOLVED
    assert result.element is not None
    assert result.element.element_id == "mine"


def test_password_target_is_resolvable_without_leaking_content() -> None:
    """A password field may be located; its content is never exposed (section 55)."""
    element = _element(
        role=UIRole.PASSWORD_INPUT,
        password=True,
        text=None,
        accessible_name="Password",
    )
    result = _resolver().resolve(ElementQuery(text="Password", role_hint=UIRole.PASSWORD_INPUT), [element])
    assert result.status is ResolutionStatus.RESOLVED
    assert result.best is not None
    assert result.best.to_dict()["text"] is None


def test_max_candidates_caps_the_returned_list() -> None:
    """At most ``max_candidates`` candidates are returned (section 43)."""
    elements = [
        _element(f"b{i}", text="Send", role=UIRole.HEADING, atspi_path=f"/p/b{i}") for i in range(10)
    ]
    result = _resolver(max_candidates=3).resolve(
        ElementQuery(text="Send", require_actionable=False), elements
    )
    assert len(result.candidates) == 3


# -- Absolute ambiguity rule (section 43) -------------------------------------


def test_duplicate_normalized_text_and_role_is_ambiguous() -> None:
    """Same normalized text plus same role is ambiguous, always (section 43)."""
    a = _element("a", text="Alpha", atspi_path="/p/a")
    b = _element("b", text="alpha", atspi_path="/p/b")
    result = _resolver().resolve(ElementQuery(text="Alpha"), [a, b])
    assert result.status is ResolutionStatus.AMBIGUOUS
    assert result.ambiguity is True
    assert result.best is None
    assert "normalized text 'alpha'" in result.reason


def test_ambiguity_is_never_out_voted_by_confidence() -> None:
    """Maximum confidence does not defeat the absolute ambiguity rule."""
    a = _element("a", text="Alpha", confidence=1.0, atspi_path="/p/a")
    b = _element("b", text="Alpha", confidence=1.0, atspi_path="/p/b")
    result = _resolver().resolve(ElementQuery(text="Alpha"), [a, b])
    assert result.status is ResolutionStatus.AMBIGUOUS


def test_near_tie_top_two_is_ambiguous() -> None:
    """A gap <= ambiguity_gap is ambiguous even with different roles/labels."""
    save = _element("save", role=UIRole.BUTTON, text="Save", atspi_path="/p/save")
    also = _element("also", role=UIRole.LINK, text="Save", atspi_path="/p/also")
    result = _resolver().resolve(ElementQuery(text="Save"), [save, also])
    assert result.status is ResolutionStatus.AMBIGUOUS
    assert "within" in result.reason


def test_clear_winner_is_not_ambiguous() -> None:
    """A decisive score gap resolves normally."""
    exact = _element("exact", text="Save", atspi_path="/p/exact")
    weak = _element("weak", text="Save file as", role=UIRole.LABEL, atspi_path="/p/weak")
    result = _resolver().resolve(ElementQuery(text="Save"), [exact, weak])
    assert result.status is ResolutionStatus.RESOLVED
    assert result.element is not None
    assert result.element.element_id == "exact"


# -- Occlusion (section 46) ---------------------------------------------------


def _cover(fraction: float) -> Rect:
    """A DESKTOP rect covering ``fraction`` of a 100x40 target's width."""
    return Rect(x=0.0, y=0.0, width=100.0 * fraction, height=40.0, space=CoordinateSpace.DESKTOP)


def test_occlusion_over_threshold_is_not_actionable() -> None:
    """More than 50% coverage blocks the target."""
    assessment = assess_occlusion(_element(), [_cover(0.6)])
    assert assessment.ratio == pytest.approx(0.6)
    assert assessment.blocked is True
    assert assessment.is_actionable is False


def test_occlusion_at_or_below_threshold_is_actionable() -> None:
    """Coverage at or under the threshold does not block."""
    assert assess_occlusion(_element(), [_cover(0.5)]).is_actionable is True
    assert assess_occlusion(_element(), [_cover(0.4)]).is_actionable is True
    assert assess_occlusion(_element(), []).is_actionable is True


def test_occlusion_uses_max_not_sum() -> None:
    """Overlapping occluders are not double-counted into a false block."""
    two_partials = [_cover(0.4), _cover(0.45)]
    assessment = assess_occlusion(_element(), two_partials)
    assert assessment.ratio == pytest.approx(0.45)
    assert assessment.blocked is False


def test_occlusion_respects_declared_flag_and_missing_geometry() -> None:
    """Perception's own occlusion flag blocks; no geometry blocks assessment."""
    declared = assess_occlusion(_element(occluded=True), [])
    assert declared.declared_occluded is True
    assert declared.is_actionable is False

    no_geometry = _element(bbox=None)
    unassessable = assess_occlusion(no_geometry, [_cover(1.0)])
    assert unassessable.assessed is False
    assert unassessable.is_actionable is False


def test_occlusion_ignores_cross_space_occluders() -> None:
    """A FRAME-space occluder is ignored, never silently converted (section 31)."""
    frame_cover = Rect(x=0.0, y=0.0, width=100.0, height=40.0, space=CoordinateSpace.FRAME)
    assessment = assess_occlusion(_element(), [frame_cover])
    assert assessment.ratio == 0.0
    assert assessment.blocked is False


def test_resolution_attaches_occlusion_to_best_candidate() -> None:
    """A resolved-but-covered target reports resolved yet not actionable."""
    result = _resolver().resolve(ElementQuery(text="Send"), [_element()], occluders=[_cover(0.9)])
    assert result.status is ResolutionStatus.RESOLVED
    assert result.occlusion is not None
    assert result.occlusion.blocked is True
    assert result.is_actionable is False


def test_default_occlusion_threshold_matches_specification() -> None:
    """The section 46 default rule is ``> 0.50 => not actionable``."""
    assert DEFAULT_OCCLUSION_THRESHOLD == 0.50


# -- Leases (section 44) ------------------------------------------------------


def test_bind_lease_uses_the_resolved_state_stamps() -> None:
    """A lease is bound to one observation's frame/version/generation."""
    state = _state(frame_id=7, state_version=3, generation=2)
    result = _resolver().resolve(ElementQuery(text="Send"), [_element()])
    assert result.best is not None
    lease = bind_lease(result.best, state, ttl_ms=500, now_monotonic=50.0, lease_id="L1")
    assert lease.lease_id == "L1"
    assert lease.element_id == "e1"
    assert (lease.frame_id, lease.state_version, lease.generation) == (7, 3, 2)
    assert lease.ttl_ms == 500
    assert lease.identity == result.best.element.identity


# -- Determinism and the disabled 43.1 hook -----------------------------------


def test_resolution_is_deterministic() -> None:
    """Identical input yields identical candidate order and scores."""
    elements = [
        _element("a", text="Alpha", atspi_path="/p/a"),
        _element("b", text="Alpha", atspi_path="/p/b"),
        _element("c", text="Alphabet", role=UIRole.HEADING, atspi_path="/p/c"),
    ]
    first = _resolver().resolve(ElementQuery(text="Alpha", require_actionable=False), elements)
    second = _resolver().resolve(ElementQuery(text="Alpha", require_actionable=False), elements)
    assert first.status is second.status
    assert [c.element_id for c in first.candidates] == [c.element_id for c in second.candidates]
    assert [c.score for c in first.candidates] == [c.score for c in second.candidates]


def test_semantic_scorer_is_inert_by_default() -> None:
    """The section 43.1 fuzzy hook does nothing unless explicitly injected."""
    element = _element(text="Send message")
    assert (
        _resolver().resolve(ElementQuery(text="Transmit"), [element]).status
        is ResolutionStatus.NOT_FOUND
    )

    def scorer(_query: str, _label: str) -> float:
        return 90.0

    inert_floor = TargetResolver(ResolverSettings(), semantic_scorer=scorer, semantic_match_floor=100.0)
    assert (
        inert_floor.resolve(ElementQuery(text="Transmit"), [element]).status
        is ResolutionStatus.NOT_FOUND
    )

    enabled = TargetResolver(ResolverSettings(), semantic_scorer=scorer, semantic_match_floor=70.0)
    result = enabled.resolve(ElementQuery(text="Transmit"), [element])
    assert result.status is ResolutionStatus.RESOLVED
    assert result.matched_stage == "normalized_text"


def test_stats_reports_configuration_and_counters() -> None:
    """The resolver exposes honest counters for logs and benchmarks."""
    resolver = _resolver()
    resolver.resolve(ElementQuery(text="Send"), [_element()])
    resolver.resolve(ElementQuery(text="nope"), [_element()])
    stats = resolver.stats()
    assert stats["resolutions"] == 2
    assert stats["not_found"] == 1
    assert stats["semantic_matching"] is False
