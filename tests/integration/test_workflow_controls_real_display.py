"""The section 74 workflow at self-verifying scale, over the *live* desktop.

The benchmark workload only works if the fixture's pipeline is really perceivable
the way the plan describes it, and three of those facts are invisible to the
offscreen fixture tests:

* Qt announces an editable text field with the AT-SPI role ``text``, so it must
  still be recognized as a ``TEXT_INPUT`` (the ``EDITABLE`` state decides,
  section 37) for section 51's focus guard to allow typing at all;
* only a *navigation* field's editable content may be read (sections 36/42/55),
  which is what makes the typed step verifiable instead of honestly
  ``UNVERIFIED``;
* the results must appear as ``LIST_ITEM`` nodes with usable geometry, or the
  plan's result step can never resolve.

Every observation polls, because the fixture registers last in the desktop's
application list and a single bounded traversal (section 35) can finish before
reaching it on a loaded desktop -- a naive single pass made this test flaky.

Read-only: the Body is assembled without dispatching any action, and the fixture
is driven over its own JSON control channel, which is not BLAXCY input.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator

import pytest

from bench.real_desktop import WORKFLOW_SEARCH_CONTROL, WORKFLOW_SEARCH_FIELD
from config.settings import load_settings
from control.verifier import Postcondition, Verifier
from core.application import BlaxcyApplication
from gui.app import self_excluded_settings
from schemas.elements import UIElement
from schemas.enums import CoordinateSpace, UIRole, VerificationState
from schemas.geometry import Point, Rect
from schemas.screen_state import ScreenState
from tests.harness import FixtureApp

#: The traversal budget the benchmark uses; the default 250 ms expires before it
#: reaches the fixture on a busy desktop (section 35).
ACCESSIBILITY_BUDGET = {"deadline_ms": 8000, "max_nodes": 6000}

pytestmark = pytest.mark.skipif(
    not os.environ.get("DISPLAY"),
    reason="real-display workflow test requires an X display (DISPLAY unset)",
)


@pytest.fixture(scope="module")
def env(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[BlaxcyApplication, FixtureApp]]:
    """A read-only Body plus the workflow-controls fixture, or an honest skip."""
    base = self_excluded_settings(load_settings(None))
    settings = base.model_copy(
        update={"accessibility": base.accessibility.model_copy(update=ACCESSIBILITY_BUDGET)}
    )
    application = BlaxcyApplication(
        settings,
        clipboard=None,
        watchdog_path=tmp_path_factory.mktemp("workflow") / "watchdog.json",
    )
    startup = application.start()
    if not startup.started:
        application.shutdown()
        pytest.skip(f"the Body could not start on this host: {startup.notes}")
    fixture = FixtureApp(platform="xcb", start_timeout=30.0, workflow_controls=True).start()
    try:
        yield application, fixture
    finally:
        fixture.stop()
        application.shutdown()


def _perceive_until(
    application: BlaxcyApplication,
    fixture: FixtureApp,
    predicate: Callable[[ScreenState], bool],
    *,
    describe: str,
    timeout: float = 30.0,
) -> ScreenState:
    """Poll fresh observations until ``predicate`` holds (read-only).

    The fixture registers last in the desktop's application list, so a single
    bounded traversal can finish before reaching it. Polling is what the benchmark
    harness itself does, and waiting for exactly what the caller asserts is what
    makes the assertion independent of traversal order.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        fixture.set_focus("search_input")
        application.accessibility.refresh()
        state = application.perceive()
        if state is not None and predicate(state):
            return state
        time.sleep(0.25)
    raise AssertionError(f"{describe} was not perceived within {timeout:.0f}s")


def _fixture_bounds(fixture: FixtureApp) -> Rect:
    """The live bounding box of the fixture's visible controls, in DESKTOP space."""
    xs: list[int] = []
    ys: list[int] = []
    for entry in fixture.inventory():
        if not entry["visible"]:
            continue
        origin = entry["global"]
        local = entry["local"]
        xs.extend([int(origin["x"]), int(origin["x"]) + int(local["width"])])
        ys.extend([int(origin["y"]), int(origin["y"]) + int(local["height"])])
    return Rect(
        x=float(min(xs)),
        y=float(min(ys)),
        width=float(max(xs) - min(xs)),
        height=float(max(ys) - min(ys)),
        space=CoordinateSpace.DESKTOP,
    )


def _fixture_elements(state: ScreenState, fixture: FixtureApp) -> list[UIElement]:
    """The perceived elements that lie inside the fixture window's live box.

    A real desktop may hold other windows whose controls share a name ("Search"
    is not unique to the fixture), so the assertion filters by geometry rather
    than by name alone.
    """
    bounds = _fixture_bounds(fixture)
    inside: list[UIElement] = []
    for element in state.elements:
        if element.bbox is None:
            continue
        centre = Point(
            x=element.bbox.x + element.bbox.width / 2.0,
            y=element.bbox.y + element.bbox.height / 2.0,
            space=CoordinateSpace.DESKTOP,
        )
        if bounds.contains(centre):
            inside.append(element)
    return inside


def test_the_live_search_field_is_an_editable_input_with_readable_content(
    env: tuple[BlaxcyApplication, FixtureApp],
) -> None:
    """Sections 36/37/51: the Qt role fix and the navigation-field read compose."""
    application, fixture = env
    fixture.set_text("search_input", "song name")

    def typed(state: ScreenState) -> bool:
        return any(
            element.accessible_name == WORKFLOW_SEARCH_FIELD and element.text == "song name"
            for element in state.elements
        )

    state = _perceive_until(
        application, fixture, typed, describe="the search field with its typed content"
    )
    field = next(
        element
        for element in state.elements
        if element.accessible_name == WORKFLOW_SEARCH_FIELD and element.text == "song name"
    )

    assert field.role is UIRole.TEXT_INPUT, "Qt's editable 'text' role was not refined"
    assert field.is_text_entry is True, "the section 51 focus guard would refuse to type"


def test_the_live_clickable_controls_are_large_perceivable_buttons(
    env: tuple[BlaxcyApplication, FixtureApp],
) -> None:
    """Section 34/60: each clickable step must be a real, large, clickable control."""
    application, fixture = env

    state = _perceive_until(
        application,
        fixture,
        lambda s: any(e.accessible_name == "Submit" for e in s.elements),
        describe="the Submit control",
    )
    inside = _fixture_elements(state, fixture)
    by_name: dict[str | None, list[UIElement]] = {}
    for element in inside:
        by_name.setdefault(element.accessible_name, []).append(element)

    for name in (WORKFLOW_SEARCH_CONTROL, "Submit", "Play Button"):
        matches = [e for e in by_name.get(name, []) if e.role is UIRole.BUTTON]
        assert matches, f"{name!r} is not perceived as a BUTTON inside the fixture window"
        assert matches[0].clickable, f"{name!r} is not clickable"
        assert matches[0].bbox is not None and matches[0].bbox.width > 0.0


def test_the_live_submit_click_reports_a_same_window_enable_transition(
    env: tuple[BlaxcyApplication, FixtureApp],
) -> None:
    """Section 60: the stronger postcondition the section 74 ``s3`` click needs.

    The Submit click leaves the button's own state unchanged and repaints nothing
    at its own box, which is exactly the 1-in-150 case where the pixel check left
    the step ``UNVERIFIED``. The live desktop does report a directly observable
    postcondition: a control in the same application becomes enabled. This proves
    the evidence the new verifier consumes really exists on a live Qt window. The
    change is driven over the fixture's own control channel (no desktop input), and
    the transition is read from real observations, so it is a functional probe of
    the evidence rather than of the verifier in isolation.

    Defined before the other tests that click Submit, because the fixture is
    module-scoped and its Play Button stays enabled once Submit has run.
    """
    application, fixture = env
    fixture.set_text("search_input", "song name")

    def both_present(state: ScreenState) -> bool:
        return any(e.accessible_name == "Submit" for e in state.elements) and any(
            e.accessible_name == "Play Button" for e in state.elements
        )

    before = _perceive_until(application, fixture, both_present, describe="Submit and Play Button")
    play_before = next(e for e in before.elements if e.accessible_name == "Play Button")
    submit = next(e for e in before.elements if e.accessible_name == "Submit")
    assert play_before.enabled is False, "the Play Button must start disabled"

    fixture.click("submit_button")

    def play_enabled(state: ScreenState) -> bool:
        return any(
            e.accessible_name == "Play Button" and e.enabled is True for e in state.elements
        )

    after = _perceive_until(application, fixture, play_enabled, describe="the enabled Play Button")
    play_after = next(e for e in after.elements if e.accessible_name == "Play Button")
    assert play_after.enabled is True

    outcome = Verifier().verify_element_state_change(
        before=before, after=after, target=submit, reason_context="click"
    )

    assert outcome is not None, "the live enable transition was not reported as evidence"
    assert outcome.state is VerificationState.VERIFIED
    assert outcome.postcondition is Postcondition.ELEMENT_STATE


def test_the_live_results_are_named_list_items_with_geometry(
    env: tuple[BlaxcyApplication, FixtureApp],
) -> None:
    """The result step targets a ``LIST_ITEM``; without one it can never resolve."""
    application, fixture = env
    fixture.set_text("search_input", "song name")
    fixture.click("submit_button")

    def results(state: ScreenState) -> bool:
        return (
            sum(
                1
                for element in state.elements
                if element.role is UIRole.LIST_ITEM
                and (element.text or "").startswith("Result ")
            )
            >= 3
        )

    state = _perceive_until(application, fixture, results, describe="the three result items")
    inside = _fixture_elements(state, fixture)
    items = {e.text: e for e in inside if e.role is UIRole.LIST_ITEM}

    assert {"Result 1", "Result 2", "Result 3"} <= set(items), (
        f"the result items are not perceived as LIST_ITEMs: {sorted(k for k in items if k)}"
    )
    item = items["Result 1"]
    assert item.bbox is not None
    assert item.bbox.width > 0.0 and item.bbox.height > 0.0
    assert item.clickable


def test_the_live_submit_control_is_really_activated_through_atspi(
    env: tuple[BlaxcyApplication, FixtureApp],
) -> None:
    """Section 66: ``activate_element`` really performs the application's action.

    The unit tests prove the wiring over a fake hook; this proves the other half --
    that ``Atspi.Action`` is genuinely invoked and genuinely drives the
    application. No pointer or key input is injected, so the observable effect
    below can only have come from the application performing its own action. That
    is exactly why the tool is worth having: it needs no coordinates and cannot be
    defeated by pointer occlusion, while still passing the same policy, lease and
    revalidation pipeline the executor enforces.
    """
    application, fixture = env
    fixture.set_text("search_input", "song name")
    before = int(fixture.stats()["counters"].get("submit_button", 0))

    def has_submit(state: ScreenState) -> bool:
        return any(
            element.accessible_name == "Submit" and element.atspi_path
            for element in state.elements
        )

    state = _perceive_until(application, fixture, has_submit, describe="the Submit control")
    submit = next(
        element
        for element in state.elements
        if element.accessible_name == "Submit" and element.atspi_path
    )
    submit_path = submit.atspi_path
    assert submit_path, "the Submit control must carry an accessibility path to activate"

    outcome = application.accessibility.activate(submit_path)

    assert outcome.ok is True, outcome.reason
    assert outcome.action, "the invoked action must be reported, not assumed"
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        if int(fixture.stats()["counters"].get("submit_button", 0)) > before:
            break
        time.sleep(0.25)
    assert int(fixture.stats()["counters"].get("submit_button", 0)) > before, (
        "the accessibility action reported success but the application never acted"
    )


def test_a_result_selection_is_observable_as_positive_evidence(
    env: tuple[BlaxcyApplication, FixtureApp],
) -> None:
    """Section 60: a result click's postcondition is readable from the item itself.

    Clicking a selectable control's postcondition is "this control is now
    selected", and the accessibility tree reports that directly -- stronger
    evidence than the pixels the control repainted, and immune to the section 34
    temporal layer reclassifying a repeated region as ``ANIMATION`` (which is what
    made the benchmark's result step ``UNVERIFIED``: the previous step had already
    changed that region as a side effect). This test proves the evidence the
    verifier consumes actually exists on a live Qt list. The row is selected over
    the fixture's own control channel, so no desktop input is injected.
    """
    application, fixture = env
    fixture.set_text("search_input", "song name")
    fixture.click("submit_button")

    def results(state: ScreenState) -> bool:
        return (
            sum(
                1
                for element in state.elements
                if element.role is UIRole.LIST_ITEM
                and (element.text or "").startswith("Result ")
            )
            >= 3
        )

    _perceive_until(application, fixture, results, describe="the result items")
    fixture.set_selection("result_list", 0)

    def selected(state: ScreenState) -> bool:
        return any(
            element.role is UIRole.LIST_ITEM
            and element.text == "Result 1"
            and element.selected is True
            for element in state.elements
        )

    state = _perceive_until(
        application, fixture, selected, describe="the selected result being reported"
    )
    item = next(
        element
        for element in state.elements
        if element.role is UIRole.LIST_ITEM and element.text == "Result 1"
    )
    assert item.selected is True
    # The unselected siblings must not be reported as selected, or the evidence
    # would be worthless.
    sibling = next(
        element
        for element in state.elements
        if element.role is UIRole.LIST_ITEM and element.text == "Result 2"
    )
    assert sibling.selected is False
