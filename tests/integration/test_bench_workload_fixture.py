"""Section 76 verifiable-workload fixture tests.

The Phase 14 real-desktop benchmark needs a workload whose controls repaint at
their *own* regions, so a real click produces a section 34 ``MEANINGFUL`` change
overlapping the target and section 60 can verify it. That workload is the
fixture's opt-in ``--bench-controls`` layout, and these tests prove the layout is
real -- the five controls exist, are the size the thumbnail change detector
needs, and a click genuinely toggles each one -- without a display server or any
injected input (the fixture runs on the Qt offscreen platform and is driven over
its own JSON control channel).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from tests.fixtures.fixture_app import (
    BENCH_CONTROL_HEIGHT,
    BENCH_CONTROL_WIDTH,
    WORKFLOW_CONTROL_HEIGHT,
    WORKFLOW_CONTROL_WIDTH,
    WORKFLOW_SEARCH_FIELD_NAME,
)
from tests.harness import FixtureApp

#: The five verifiable controls, in the order ``bench/real_desktop.py`` clicks them.
BENCH_TARGETS = [f"bench_target_{index}" for index in range(1, 6)]


@pytest.fixture(scope="module")
def bench_fixture() -> Iterator[FixtureApp]:
    """One bench-controls fixture process shared by the module."""
    with FixtureApp(bench_controls=True) as app:
        yield app


@pytest.fixture(scope="module")
def ordinary_fixture() -> Iterator[FixtureApp]:
    """One ordinary fixture process, to prove the layout is opt-in."""
    with FixtureApp() as app:
        yield app


def _entries_by_name(app: FixtureApp) -> dict[str, dict[str, Any]]:
    return {str(entry["object_name"]): dict(entry) for entry in app.inventory()}


def test_bench_targets_are_present_only_when_requested(bench_fixture: FixtureApp) -> None:
    """The five verifiable controls exist in the bench layout and nowhere else."""
    assert set(BENCH_TARGETS) <= set(_entries_by_name(bench_fixture))


def test_the_ordinary_layout_has_no_bench_targets(ordinary_fixture: FixtureApp) -> None:
    """``bench_controls=False`` (the default) must be unchanged for every other test."""
    names = set(_entries_by_name(ordinary_fixture))
    assert not names & set(BENCH_TARGETS)


def test_bench_targets_are_large_enough_to_change_meaningfully(bench_fixture: FixtureApp) -> None:
    """Each target is big enough for its repaint to clear the section 34 thresholds.

    A change must span enough of the 480x270 thumbnail grid to register as
    ``MEANINGFUL`` (>= 80x24 px of the desktop, >= 1.5% area). The fixed size here
    is what makes that true, so it is asserted rather than assumed.
    """
    entries = _entries_by_name(bench_fixture)
    for name in BENCH_TARGETS:
        local = entries[name]["local"]
        assert local["width"] == BENCH_CONTROL_WIDTH
        assert local["height"] == BENCH_CONTROL_HEIGHT
        assert entries[name]["visible"] is True


def test_every_bench_target_has_a_distinct_accessible_name(bench_fixture: FixtureApp) -> None:
    """Distinct names are what the resolver targets by; duplicates would be ambiguous."""
    entries = _entries_by_name(bench_fixture)
    names = [entries[name]["accessible_name"] for name in BENCH_TARGETS]
    assert names == [f"Bench Target {index}" for index in range(1, 6)]
    assert len(set(names)) == len(names)


def test_a_click_toggles_a_bench_target(bench_fixture: FixtureApp) -> None:
    """A click must really change the control's own state, not just bump a counter.

    The benchmark claims a click is verifiable because the control repaints at its
    own box; that is only true if the control's state actually flips.
    """
    before = dict(bench_fixture.stats()["bench_targets"])
    assert before["1"] is False

    bench_fixture.click("bench_target_1")
    after = bench_fixture.stats()["bench_targets"]
    assert after["1"] is True
    assert after["2"] == before["2"]

    # Second click flips it back, so the workload is repeatable across samples.
    bench_fixture.click("bench_target_1")
    assert bench_fixture.stats()["bench_targets"]["1"] is False


def test_bench_controls_hide_the_ordinary_widgets(bench_fixture: FixtureApp) -> None:
    """The ordinary controls are hidden in the bench layout, never stray windows.

    They must remain *findable* (their state is still readable) but not visible,
    or they would overlap the grid and merge their changes into the target's.
    """
    entries = _entries_by_name(bench_fixture)
    assert "text_input" in entries
    assert entries["text_input"]["visible"] is False
    # Their state is still real and readable, so the harness keeps working.
    bench_fixture.set_text("text_input", "still readable")
    assert bench_fixture.stats()["text_input"] == "still readable"


# ---------------------------------------------------------------------------
# Section 74 workflow at a self-verifying scale.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def workflow_fixture() -> Iterator[FixtureApp]:
    """One workflow-controls fixture process shared by the module."""
    with FixtureApp(workflow_controls=True) as app:
        yield app


def test_the_workflow_layout_is_the_pipeline_at_a_self_verifying_scale(
    workflow_fixture: FixtureApp,
) -> None:
    """Each clickable step is large enough for its own repaint to be MEANINGFUL.

    The section 34 thresholds decide whether section 60 can verify a click, so the
    layout -- not the plan -- is what makes the workflow completable.
    """
    entries = _entries_by_name(workflow_fixture)
    for name in ("search_icon", "submit_button", "play_button"):
        local = entries[name]["local"]
        assert local["width"] == WORKFLOW_CONTROL_WIDTH
        assert local["height"] == WORKFLOW_CONTROL_HEIGHT
        assert entries[name]["visible"] is True
    assert entries["result_list"]["local"]["width"] == WORKFLOW_CONTROL_WIDTH


def test_the_workflow_search_field_is_named_as_a_navigation_field(
    workflow_fixture: FixtureApp,
) -> None:
    """Sections 36/55: only a navigation field's content is readable.

    A plain "Search Box" would make the typed step honestly ``UNVERIFIED``, so the
    self-verifying layout must name the field as a navigation one.
    """
    entries = _entries_by_name(workflow_fixture)
    field = entries["search_input"]
    assert field["accessible_name"] == WORKFLOW_SEARCH_FIELD_NAME
    assert field["visible"] is True
    assert field["local"]["width"] == WORKFLOW_CONTROL_WIDTH


def test_the_workflow_layout_hides_the_ordinary_controls(workflow_fixture: FixtureApp) -> None:
    """The ordinary widgets stay parented but hidden, as in the benchmark layout."""
    entries = _entries_by_name(workflow_fixture)
    assert entries["text_input"]["visible"] is False
    assert entries["password_input"]["visible"] is False
    assert entries["btn_alpha"]["visible"] is False


def test_a_click_reaches_the_workflow_control_it_targets(workflow_fixture: FixtureApp) -> None:
    """A click on a workflow control is recorded by that control, not another."""
    before = dict(workflow_fixture.stats()["counters"])

    workflow_fixture.click("search_icon")

    after = workflow_fixture.stats()["counters"]
    assert after["search_icon"] == before.get("search_icon", 0) + 1


def test_submit_produces_three_results_and_enables_play(workflow_fixture: FixtureApp) -> None:
    """The submit step really populates the list and arms the play step."""
    workflow_fixture.set_text("search_input", "song name")

    workflow_fixture.click("submit_button")

    stats = workflow_fixture.stats()
    assert stats["result_count"] == 3
    assert stats["play_enabled"] is True
