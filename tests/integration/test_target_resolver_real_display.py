"""Real-display resolver integration test (specification sections 43, 44, 46).

This is an *integration* test: it feeds the **live** AT-SPI elements the
accessibility service actually observed into the Phase 6
:class:`~core.target_resolver.TargetResolver`, and asserts the section 43
ambiguity and section 46 occlusion rules against real desktop geometry.

Because a live desktop is not deterministic, the test never hardcodes an element
name. Instead it derives its fixtures from what is really on screen:

* a uniquely labelled, actionable element with real geometry for the resolution
  and occlusion cases,
* a genuinely duplicated ``(normalized label, role)`` pair for the absolute
  ambiguity rule -- a real duplicate, not a synthetic one, when one exists.

Every case skips with an honest reason when the live desktop cannot supply the
shape it needs; nothing is fabricated to make a test pass. The test injects no
input at all: resolution is pure arithmetic over observations and this module
never calls the mouse or keyboard layer.
"""

from __future__ import annotations

import os
import time
from collections import Counter

import pytest

from config.settings import AccessibilitySettings, ResolverSettings
from core.accessibility import AccessibilityService
from core.target_resolver import (
    DEFAULT_OCCLUSION_THRESHOLD,
    ResolutionStatus,
    TargetResolver,
    assess_occlusion,
    bind_lease,
)
from schemas.elements import ElementQuery, UIElement
from schemas.enums import CoordinateSpace, UIRole
from schemas.errors import BlaxcyError
from schemas.geometry import MonitorGeometry, MonitorLayout, Rect
from schemas.screen_state import ScreenState

pytestmark = pytest.mark.skipif(
    not os.environ.get("DISPLAY"),
    reason="real-display resolver test requires an X display (DISPLAY unset)",
)

#: The cascade stages that represent a real textual match.
_TEXT_STAGES = frozenset(
    {"exact_text", "case_insensitive_text", "normalized_text", "accessible_name"}
)


def _service() -> AccessibilityService:
    """A started real accessibility service, or a skip when AT-SPI is unavailable."""
    service = AccessibilityService(AccessibilitySettings())
    try:
        service.start()
    except BlaxcyError as exc:
        pytest.skip(f"live AT-SPI accessibility is unavailable: {exc.message}")
    return service


def _live_elements() -> list[UIElement]:
    """The live AT-SPI observations, or a skip when there are none."""
    service = _service()
    try:
        elements = list(service.refresh())
    finally:
        service.stop()
    if not elements:
        pytest.skip("the live accessibility tree produced no elements")
    return elements


def _label(element: UIElement) -> str:
    """The normalized label the resolver uses for ambiguity checks."""
    return " ".join((element.text or element.accessible_name or "").split()).casefold()


def _unique_actionable_target(elements: list[UIElement]) -> UIElement:
    """An actionable element whose label is unique across every observation."""
    counts = Counter(_label(element) for element in elements)
    for element in elements:
        label = _label(element)
        if not label or counts[label] != 1:
            continue
        if element.is_actionable and element.bbox is not None:
            return element
    pytest.skip("no uniquely labelled actionable element with geometry on this desktop")


def _duplicate_group(
    elements: list[UIElement],
) -> tuple[str, UIRole, list[UIElement], bool]:
    """A real duplicated ``(label, role)`` group, preferring actionable duplicates.

    Returns ``(label, role, group, require_actionable)``. The actionable pool is
    tried first: if two *actionable* elements share a label and role, the
    ambiguity is proven with the default actionability requirement. Otherwise a
    pool of visible elements is tried with ``require_actionable=False`` -- still
    the section 43 absolute rule, on elements that really exist.
    """
    for require_actionable in (True, False):
        groups: dict[tuple[str, UIRole], list[UIElement]] = {}
        for element in elements:
            if not element.visible:
                continue
            if require_actionable and not element.is_actionable:
                continue
            label = _label(element)
            if not label:
                continue
            groups.setdefault((label, element.role), []).append(element)
        for (label, role), group in groups.items():
            if len(group) >= 2:
                return label, role, group, require_actionable
    pytest.skip("no two visible elements share a normalized label and role on this desktop")


def _labelled_with_geometry(elements: list[UIElement]) -> UIElement:
    """A labelled, not-yet-occluded element with real DESKTOP geometry."""
    for element in elements:
        if element.bbox is not None and _label(element) and not element.occluded:
            return element
    pytest.skip("no labelled element with geometry on this desktop")


def _live_layout() -> MonitorLayout:
    """Build a :class:`MonitorLayout` from the real physical monitors."""
    import mss

    try:
        with mss.MSS() as capture:
            monitors = [dict(monitor) for monitor in capture.monitors]
    except Exception as exc:  # pragma: no cover - depends on the host
        pytest.skip(f"live screen capture is unavailable: {exc!r}")
    physical = monitors[1:]
    if not physical:
        pytest.skip("the live display backend reported no physical monitor")
    return MonitorLayout(
        monitors=tuple(
            MonitorGeometry(
                monitor_id=index,
                name=monitor.get("output") or None,
                origin_x=float(monitor["left"]),
                origin_y=float(monitor["top"]),
                width=int(monitor["width"]),
                height=int(monitor["height"]),
            )
            for index, monitor in enumerate(physical)
        )
    )


# -- Section 43: resolution over live elements --------------------------------


def test_live_element_resolves_through_the_cascade() -> None:
    """A real, uniquely labelled element resolves to itself, deterministically."""
    elements = _live_elements()
    target = _unique_actionable_target(elements)
    label = target.text or target.accessible_name
    assert label

    resolver = TargetResolver(ResolverSettings())
    # Text-only on purpose: the section 43 ambiguity rule treats any two
    # candidates sharing a label *and* role as ambiguous, and this desktop really
    # does have a duplicated panel control. Narrowing by text is what makes the
    # target unique, so that is the honest way to assert a resolution.
    query = ElementQuery(text=label)
    result = resolver.resolve(query, elements)

    assert result.status is ResolutionStatus.RESOLVED
    assert result.element is not None
    assert result.element.element_id == target.element_id
    assert result.element.identity == target.identity
    assert result.matched_stage in _TEXT_STAGES
    assert result.best is not None and result.best.breakdown["text"] > 0.0
    assert result.ambiguity is False
    # No known occluders, and the target has geometry, so it is actionable.
    assert result.occlusion is not None and result.occlusion.assessed is True
    assert result.is_actionable is True

    # Determinism: the same live observations resolve identically.
    repeat = TargetResolver(ResolverSettings()).resolve(query, elements)
    assert repeat.status is result.status
    assert repeat.matched_stage == result.matched_stage
    assert repeat.element is not None and repeat.element.element_id == target.element_id


def test_live_absence_of_a_target_is_reported_not_guessed() -> None:
    """A label nothing on screen carries is an honest NOT_FOUND (section 43)."""
    elements = _live_elements()
    labels = {_label(element) for element in elements}
    missing = "blaxcy-no-element-has-this-label"
    assert missing not in labels

    result = TargetResolver(ResolverSettings()).resolve(
        ElementQuery(text=missing, require_actionable=False), elements
    )
    assert result.status is ResolutionStatus.NOT_FOUND
    assert result.element is None
    assert result.candidates == ()


# -- Section 43: absolute ambiguity rule over live elements -------------------


def test_live_duplicate_label_and_role_is_ambiguous() -> None:
    """Two live elements sharing a normalized label and role are AMBIGUOUS."""
    elements = _live_elements()
    label, role, group, require_actionable = _duplicate_group(elements)

    resolver = TargetResolver(ResolverSettings())
    result = resolver.resolve(
        ElementQuery(text=label, require_actionable=require_actionable), elements
    )

    assert result.status is ResolutionStatus.AMBIGUOUS
    assert result.ambiguity is True
    assert result.best is None
    assert result.element is None
    assert "normalized text" in result.reason
    # The ambiguous candidates are the real duplicates, not some other element.
    candidate_ids = {candidate.element_id for candidate in result.candidates}
    assert len(candidate_ids & {element.element_id for element in group}) >= 2
    # Every reported candidate really does carry the shared label and role.
    for candidate in result.candidates:
        if candidate.element_id in {element.element_id for element in group}:
            assert _label(candidate.element) == label
            assert candidate.element.role is role


def test_live_ambiguity_is_independent_of_confidence() -> None:
    """The live duplicates stay ambiguous even with the strongest perception signal."""
    elements = _live_elements()
    label, _role, group, require_actionable = _duplicate_group(elements)
    boosted = [
        element.model_copy(update={"confidence": 1.0})
        if element.element_id in {member.element_id for member in group}
        else element
        for element in elements
    ]

    result = TargetResolver(ResolverSettings()).resolve(
        ElementQuery(text=label, require_actionable=require_actionable), boosted
    )
    assert result.status is ResolutionStatus.AMBIGUOUS
    assert result.best is None


# -- Section 46: occlusion against real desktop geometry ----------------------


def test_live_occlusion_uses_max_not_sum_over_real_geometry() -> None:
    """Coverage is the max single occluder, measured on a real element's box."""
    elements = _live_elements()
    target = _labelled_with_geometry(elements)
    bbox = target.bbox
    assert bbox is not None and bbox.space is CoordinateSpace.DESKTOP

    clear = assess_occlusion(target, [])
    assert clear.assessed is True
    assert clear.ratio == 0.0
    assert clear.blocked is False and clear.is_actionable is True

    # A rectangle taken from the element's own live box fully covers it.
    cover = Rect(x=bbox.x, y=bbox.y, width=bbox.width, height=bbox.height, space=bbox.space)
    covered = assess_occlusion(target, [cover])
    assert covered.ratio > DEFAULT_OCCLUSION_THRESHOLD
    assert covered.blocked is True
    assert covered.is_actionable is False

    # Two partial occluders (each ~40% of the real width) must not sum to a block.
    left = Rect(x=bbox.x, y=bbox.y, width=bbox.width * 0.4, height=bbox.height, space=bbox.space)
    right = Rect(
        x=bbox.x + bbox.width * 0.6,
        y=bbox.y,
        width=bbox.width * 0.4,
        height=bbox.height,
        space=bbox.space,
    )
    partial = assess_occlusion(target, [left, right])
    assert partial.ratio == pytest.approx(0.4, abs=0.02)
    assert partial.blocked is False
    assert partial.is_actionable is True


def test_live_declared_occlusion_blocks_when_the_desktop_reports_it() -> None:
    """When perception marks a live element occluded, it is never actionable."""
    elements = _live_elements()
    occluded = [element for element in elements if element.occluded]
    if not occluded:
        pytest.skip("no element on this desktop is currently reported as occluded")

    assessment = assess_occlusion(occluded[0], [])
    assert assessment.declared_occluded is True
    assert assessment.blocked is True
    assert assessment.is_actionable is False


def test_live_resolution_reports_occlusion_but_is_not_actionable() -> None:
    """A live target that resolves cleanly is still not actionable when covered."""
    elements = _live_elements()
    target = _unique_actionable_target(elements)
    label = target.text or target.accessible_name
    assert label and target.bbox is not None
    bbox = target.bbox

    cover = Rect(x=bbox.x, y=bbox.y, width=bbox.width, height=bbox.height, space=bbox.space)
    result = TargetResolver(ResolverSettings()).resolve(
        ElementQuery(text=label), elements, occluders=[cover]
    )

    assert result.status is ResolutionStatus.RESOLVED
    assert result.occlusion is not None
    assert result.occlusion.blocked is True
    assert result.is_actionable is False


def test_bind_lease_stamps_a_live_resolution() -> None:
    """Section 44: a lease bound to a live resolution carries that observation's stamps."""
    elements = _live_elements()
    target = _unique_actionable_target(elements)
    label = target.text or target.accessible_name
    assert label

    result = TargetResolver(ResolverSettings()).resolve(ElementQuery(text=label), elements)
    assert result.best is not None

    state = ScreenState(
        frame_id=41,
        state_version=7,
        generation=3,
        timestamp=time.time(),
        monotonic=time.monotonic(),
        layout=_live_layout(),
    )
    lease = bind_lease(result.best, state, ttl_ms=1234, lease_id="live-lease")

    assert (lease.frame_id, lease.state_version, lease.generation) == (41, 7, 3)
    assert lease.ttl_ms == 1234
    assert lease.element_id == target.element_id
    assert lease.identity == target.identity
    assert lease.matches(target) is True
