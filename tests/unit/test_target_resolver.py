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
    occluders_above,
)
from schemas.elements import ElementQuery, UIElement
from schemas.enums import CoordinateSpace, PerceptionSource, UIRole
from schemas.geometry import MonitorGeometry, MonitorLayout, Rect
from schemas.screen_state import ScreenState, WindowStackEntry

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


def test_role_only_matches_cannot_veto_a_decisive_text_match() -> None:
    """A unique text target wins even when many role-only controls tie (section 43).

    This is the real-desktop shape the narrowing exists for: every unnamed panel
    button matches a role hint and ties with its siblings at the same score. Before
    the narrowing that tie made a uniquely-named control ``AMBIGUOUS``; a role-only
    match scores at most 0.60 while any textual match scores at least 0.81, so the
    text match is necessarily the winner.
    """
    target = _element("target", text="Search", atspi_path="/p/target")
    panel = [_element(f"panel{i}", text=None, atspi_path=f"/p/panel{i}") for i in range(4)]

    result = _resolver().resolve(
        ElementQuery(text="Search", role_hint=UIRole.BUTTON), [target, *panel]
    )

    assert result.status is ResolutionStatus.RESOLVED
    assert result.ambiguity is False
    assert result.element is not None
    assert result.element.element_id == "target"
    assert result.matched_stage == "exact_text"


def test_two_text_matches_with_a_role_hint_stay_ambiguous() -> None:
    """The absolute rule still fires for two real matches of the requested text."""
    a = _element("a", text="Search", atspi_path="/p/a")
    b = _element("b", text="search", atspi_path="/p/b")

    result = _resolver().resolve(ElementQuery(text="Search", role_hint=UIRole.BUTTON), [a, b])

    assert result.status is ResolutionStatus.AMBIGUOUS
    assert result.ambiguity is True
    assert result.best is None


def test_a_weaker_text_match_still_beats_a_role_only_tie() -> None:
    """A weak (substring) text match still out-scores role-only candidates.

    The narrowing is gated on a *decisive* text match, so this case is decided by
    the ordinary scoring and gap rule -- which must still prefer the text match.
    """
    weak = _element("weak", text="Search settings", atspi_path="/p/weak")
    panel = [_element(f"panel{i}", text=None, atspi_path=f"/p/panel{i}") for i in range(3)]

    result = _resolver().resolve(
        ElementQuery(text="Search", role_hint=UIRole.BUTTON), [weak, *panel]
    )

    assert result.status is ResolutionStatus.RESOLVED
    assert result.element is not None
    assert result.element.element_id == "weak"


def test_a_decisive_match_is_not_vetoed_by_a_fuzzy_subset_competitor() -> None:
    """Section 43.1: a fuzzy subset hit must not make a decisive target ambiguous.

    The live case this pins: a query for "Search Address Bar" on a desktop that
    also holds a button named "Search". ``token_set_ratio`` scores that button 100
    (the query's tokens are a superset of its label), which used to put its total
    inside the 0.08 gap of the decisive accessible-name match and refuse the
    action. The fuzzy signal still scores; it no longer creates ambiguity against
    a real text match.
    """
    field = _element(
        "field", role=UIRole.TEXT_INPUT, text=None, accessible_name="Search Address Bar"
    )
    button = _element("button", text="Search", accessible_name="Search")
    resolver = TargetResolver(
        ResolverSettings(), semantic_scorer=lambda _query, _label: 100.0, semantic_match_floor=70.0
    )

    result = resolver.resolve(ElementQuery(text="Search Address Bar"), [field, button])

    assert result.status is ResolutionStatus.RESOLVED
    assert result.element is not None
    assert result.element.element_id == "field"
    assert result.matched_stage == "accessible_name"
    # The weak competitor is still scored and still reported as a candidate.
    assert any(c.element_id == "button" for c in result.candidates)


def test_a_decisive_match_is_not_vetoed_by_a_fuzzy_duplicate_label() -> None:
    """Two fuzzy competitors sharing a label must not veto a decisive target."""
    field = _element(
        "field", role=UIRole.TEXT_INPUT, text=None, accessible_name="Search Address Bar"
    )
    first = _element("first", text="Search", accessible_name="Search")
    second = _element("second", text="Search", accessible_name="Search")
    resolver = TargetResolver(
        ResolverSettings(), semantic_scorer=lambda _query, _label: 100.0, semantic_match_floor=70.0
    )

    result = resolver.resolve(ElementQuery(text="Search Address Bar"), [field, first, second])

    assert result.status is ResolutionStatus.RESOLVED
    assert result.element is not None
    assert result.element.element_id == "field"


def test_a_fuzzy_only_query_keeps_the_conservative_ambiguity_rule() -> None:
    """With no decisive match, weak signals still create ambiguity (unchanged)."""
    first = _element("first", text="Send note", accessible_name="Send note")
    second = _element("second", text="Send note", accessible_name="Send note")
    resolver = TargetResolver(
        ResolverSettings(), semantic_scorer=lambda _query, _label: 90.0, semantic_match_floor=70.0
    )

    result = resolver.resolve(ElementQuery(text="Transmit"), [first, second])

    assert result.status is ResolutionStatus.AMBIGUOUS
    assert result.best is None


def test_role_only_query_with_duplicate_controls_stays_ambiguous() -> None:
    """A text-less query keeps the original conservative behaviour."""
    a = _element("a", text="Launcher", role=UIRole.TOGGLE, atspi_path="/p/a")
    b = _element("b", text="Launcher", role=UIRole.TOGGLE, atspi_path="/p/b")

    result = _resolver().resolve(ElementQuery(role_hint=UIRole.TOGGLE), [a, b])

    assert result.status is ResolutionStatus.AMBIGUOUS
    assert result.ambiguity is True


def test_a_text_query_matching_nothing_keeps_the_conservative_rule() -> None:
    """With no textual match at all, role-only ties are still ambiguous."""
    panel = [_element(f"panel{i}", text=None, atspi_path=f"/p/panel{i}") for i in range(3)]

    result = _resolver().resolve(
        ElementQuery(text="Nonexistent", role_hint=UIRole.BUTTON), panel
    )

    assert result.status is ResolutionStatus.AMBIGUOUS
    assert result.ambiguity is True


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
    assert result.matched_stage == "fuzzy_text"


def test_stats_reports_configuration_and_counters() -> None:
    """The resolver exposes honest counters for logs and benchmarks."""
    resolver = _resolver()
    resolver.resolve(ElementQuery(text="Send"), [_element()])
    resolver.resolve(ElementQuery(text="nope"), [_element()])
    stats = resolver.stats()
    assert stats["resolutions"] == 2
    assert stats["not_found"] == 1
    assert stats["semantic_matching"] is False


# -- Occluder narrowing by window stacking (section 46) -----------------------

#: A full-screen background element, the shape that made every real target read
#: as fully covered before the stacking rule existed.
_SCREEN = Rect(x=0.0, y=0.0, width=1920.0, height=1080.0, space=CoordinateSpace.DESKTOP)


def _windowed(element_id: str, window: str | None, **overrides: object) -> UIElement:
    """An element that knows which window it belongs to, by title."""
    return _element(element_id, owner_window_title=window, **overrides)


def _pid_windowed(element_id: str, pid: int, **overrides: object) -> UIElement:
    """An element that knows which process owns its window."""
    return _element(element_id, owner_app_pid=pid, **overrides)


def _stack(*titles: str | None) -> tuple[WindowStackEntry, ...]:
    """A stacking order, bottom to top, matched by title only."""
    return tuple(
        WindowStackEntry(window_id=index + 1, title=title) for index, title in enumerate(titles)
    )


def _pid_stack(*windows: tuple[str | None, int]) -> tuple[WindowStackEntry, ...]:
    """A stacking order, bottom to top, in which every window reports its pid."""
    return tuple(
        WindowStackEntry(window_id=index + 1, title=title, pid=pid)
        for index, (title, pid) in enumerate(windows)
    )


def test_occluders_below_the_target_are_dropped() -> None:
    """Section 46 is about what is *in front*: a window behind cannot hide a target.

    This is the regression the rule exists for. On a real desktop the desktop
    background and every other running window overlap the target, so counting them
    made every target unclickable.
    """
    stack = _stack("Desktop", "Editor", "Target Window")
    target = _windowed("t", "Target Window")
    background = _windowed("bg", "Desktop", bbox=_SCREEN)
    behind = _windowed("behind", "Editor", bbox=_SCREEN)
    same_window = _windowed("overlay", "Target Window", bbox=_SCREEN)

    kept = occluders_above(target, [background, behind, same_window], stack)

    ids = {element.element_id for element in kept if isinstance(element, UIElement)}
    assert "bg" not in ids, "the desktop background is behind the target and cannot occlude it"
    assert "behind" not in ids, "a window below the target cannot occlude it"
    assert "overlay" in ids, "an overlay in the target's own window really can cover it"


def test_an_occluder_above_the_target_is_kept_and_still_blocks() -> None:
    """A window genuinely in front must keep blocking, or this rule would be a bypass."""
    stack = _stack("Desktop", "Target Window", "Dialog On Top")
    target = _windowed("t", "Target Window")
    in_front = _windowed("front", "Dialog On Top", bbox=_SCREEN)

    kept = occluders_above(target, [in_front], stack)
    assert [element.element_id for element in kept if isinstance(element, UIElement)] == ["front"]
    assessment = assess_occlusion(target, kept)
    assert assessment.blocked is True
    assert assessment.is_actionable is False


def test_unknown_stacking_order_narrows_nothing() -> None:
    """A missing stacking list must never become a permission."""
    target = _windowed("t", "Target Window")
    guesses = [_windowed("bg", "Desktop", bbox=_SCREEN)]

    # No stack at all: the candidate list comes back exactly as it was.
    assert occluders_above(target, guesses, ()) == tuple(guesses)
    # A stack that does not mention the target's window: same, still conservative.
    assert occluders_above(target, guesses, _stack("Some Other Window")) == tuple(guesses)


def test_an_occluder_with_no_window_identity_is_kept() -> None:
    """An element we cannot place is treated as if it could be in front."""
    target = _windowed("t", "Target Window")
    anonymous = _element("anon", bbox=_SCREEN)
    rect_only = _SCREEN

    kept = occluders_above(target, [anonymous, rect_only], _stack("Desktop", "Target Window"))
    assert len(kept) == 2, "an unplaceable occluder must be kept, not assumed harmless"


def test_browser_style_title_suffixes_still_join() -> None:
    """Titles come from two sources and are not always byte-identical."""
    stack = _stack("Desktop", "Example - Google Chrome", "Target Window")
    target = _windowed("t", "Target Window")
    # The accessibility tree publishes the base title; the window manager gets the
    # browser's decorated version.
    chrome = _windowed("chrome", "Example - Google Chrome - Audio playing", bbox=_SCREEN)

    assert occluders_above(target, [chrome], stack) == ()


def test_a_duplicated_title_is_resolved_conservatively() -> None:
    """Two windows may share a title, and we cannot tell which one an element is in.

    The topmost reading is used, so the occluder is *kept*: an ambiguity about which
    window an element belongs to must widen the check, never narrow it.
    """
    stack = _stack("Untitled", "Target Window", "Untitled")
    target = _windowed("t", "Target Window")
    twin = _windowed("twin", "Untitled", bbox=_SCREEN)

    kept = occluders_above(target, [twin], stack)
    assert [element.element_id for element in kept if isinstance(element, UIElement)] == ["twin"]


def test_resolution_uses_the_states_window_stack() -> None:
    """The wiring: a caller that passes every element no longer blocks itself."""
    target = _windowed("t", "Target Window", text="Send")
    background = _windowed("bg", "Desktop", text="something else", bbox=_SCREEN)
    stack = _stack("Desktop", "Target Window")
    elements = [target, background]

    blocked = _resolver().resolve(ElementQuery(text="Send"), elements, occluders=elements)
    assert blocked.is_actionable is False, "without stacking the rule stays conservative"

    state = ScreenState(
        frame_id=1,
        state_version=1,
        generation=1,
        timestamp=1000.0,
        monotonic=1000.0,
        layout=MonitorLayout(
            monitors=(MonitorGeometry(monitor_id=0, width=1920, height=1080, is_primary=True),)
        ),
        elements=tuple(elements),
        window_stack=stack,
    )
    resolved = _resolver().resolve(ElementQuery(text="Send"), elements, occluders=elements, state=state)
    assert resolved.is_resolved is True
    assert resolved.is_actionable is True
    assert resolved.occlusion is not None
    assert resolved.occlusion.ratio == 0.0


def test_an_ancestor_container_is_never_an_occluder() -> None:
    """A container cannot hide what it contains (section 46).

    On a real desktop the target's own window frame and the ``desktop[0]`` root are
    handed over as occluders, and both completely cover the target. They are its
    *ancestors* in the accessibility tree, which is the structural fact that makes
    them not occluders -- and unlike window identity it needs no stacking order.
    """
    target = _windowed("t", "Target Window", atspi_path="/desktop[0]/25/0/1")
    frame = _windowed("frame", "Target Window", bbox=_SCREEN, atspi_path="/desktop[0]/25")
    root = _element("root", bbox=_SCREEN, atspi_path="/desktop[0]")

    kept = occluders_above(target, [frame, root], _stack("Target Window"))

    assert kept == (), "the target's own frame and the desktop root cannot occlude it"


def test_ancestry_is_compared_segment_by_segment() -> None:
    """``desktop[0]/2`` is not an ancestor of ``desktop[0]/25``.

    A plain string-prefix test calls those related and silently drops a real
    occluder, so the comparison walks path segments rather than characters.
    """
    target = _windowed("t", "Target Window", atspi_path="/desktop[0]/25/0/1")
    sibling = _windowed("sib", "Target Window", bbox=_SCREEN, atspi_path="/desktop[0]/2")

    kept = occluders_above(target, [sibling], _stack("Target Window"))

    assert [element.element_id for element in kept if isinstance(element, UIElement)] == ["sib"]


def test_a_matching_pid_places_a_window_whose_title_does_not_match() -> None:
    """The pid is the exact join; the title is only the fallback.

    A browser publishes one decorated title to the window manager and a different
    one to accessibility, so title matching alone fails to place its window -- and
    an unplaceable occluder is conservatively *kept*, which is what left every
    target on a real desktop reading as fully covered.
    """
    target = _pid_windowed("t", 100, owner_window_title="Target")
    chrome = _pid_windowed("chrome", 200, owner_window_title="Example - Google Chrome - Audio")
    stack = _pid_stack(("Desktop", 1), ("Example - Google Chrome", 200), ("Target", 100))

    kept = occluders_above(target, [chrome], stack)

    assert kept == (), "pid 200 is below pid 100, so that window cannot occlude the target"


def test_a_pid_owning_several_windows_still_uses_titles_to_break_the_tie() -> None:
    """One process may own several windows, and the title then decides which one."""
    target = _pid_windowed("t", 100, owner_window_title="Panel")
    twin = _pid_windowed("twin", 100, owner_window_title="Dialog", bbox=_SCREEN)
    stack = _pid_stack(("Panel", 100), ("Dialog", 100))

    kept = occluders_above(target, [twin], stack)

    assert [element.element_id for element in kept if isinstance(element, UIElement)] == ["twin"]


def test_an_ambiguous_pid_without_titles_widens_the_check() -> None:
    """Two untitled windows from one pid: the target reads lowest, the occluder highest.

    Both directions widen the occluder set, which is the only direction a safety
    check may err in.
    """
    target = _pid_windowed("t", 100)
    twin = _pid_windowed("twin", 100, bbox=_SCREEN)
    stack = _pid_stack((None, 100), (None, 100))

    kept = occluders_above(target, [twin], stack)

    assert [element.element_id for element in kept if isinstance(element, UIElement)] == ["twin"]
