"""Section 76 benchmark-runner plumbing tests.

``bench/real_desktop.py`` is the Phase 14 measurement harness. These tests cover
the part of it that is pure selection logic -- which fixture layout and which
five-step plan a workload name maps to -- so the two workloads cannot silently
drift into each other. The measurements themselves need the real desktop and are
opt-in; they are deliberately not asserted here (section 4 rule 8 forbids
claiming a benchmark that was not actually run).
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from bench.real_desktop import (
    BENCH_CONTROL_NAMES,
    FIXTURE_CONTROLS,
    HAPPY_PATH_PLAN,
    VERIFIABLE_PLAN,
    Environment,
    bench_gui_refresh,
    bench_keyboard,
)


def test_default_workload_is_the_ordinary_fixture_controls() -> None:
    """The default workload is the honest-halt workflow, unchanged."""
    env = Environment(real_input=False)
    assert env.verifiable is False
    assert env.expected_controls == FIXTURE_CONTROLS
    assert env.sequence_plan is HAPPY_PATH_PLAN
    assert env.click_target == "Toggle State"
    assert env.keyboard_target == "Text Input"
    assert env.focus_object == "text_input"
    assert env.focus_accessible == "Text Input"


def test_verifiable_workload_selects_the_large_self_verifying_controls() -> None:
    """``workload="verifiable"`` swaps in the section 76 layout and its plan."""
    env = Environment(real_input=False, workload="verifiable")
    assert env.verifiable is True
    assert env.expected_controls == BENCH_CONTROL_NAMES
    assert env.sequence_plan is VERIFIABLE_PLAN
    assert env.click_target == "Bench Target 1"
    assert env.keyboard_target is None
    assert env.focus_object == "bench_target_1"
    assert env.focus_accessible == "Bench Target 1"


def test_verifiable_plan_is_five_distinct_clicks_on_the_bench_controls() -> None:
    """The verifiable plan is exactly the five step-clicks the fixture supports."""
    assert [step["step_id"] for step in VERIFIABLE_PLAN] == ["s1", "s2", "s3", "s4", "s5"]
    assert all(step["tool"] == "click" for step in VERIFIABLE_PLAN)
    assert [step["target"] for step in VERIFIABLE_PLAN] == list(BENCH_CONTROL_NAMES)
    # Section 66.1: a plan carries descriptions, never pre-resolved coordinates.
    assert all("element_id" not in step and "lease_id" not in step for step in VERIFIABLE_PLAN)


def test_the_two_workloads_do_not_share_controls() -> None:
    """A plan's targets must belong to the layout it is measured against."""
    assert not set(BENCH_CONTROL_NAMES) & set(FIXTURE_CONTROLS)


def test_gui_refresh_is_unmeasured_without_an_assembled_body() -> None:
    """No Body means no number: the honest path reports *why*, not a timing.

    ``bench_gui_refresh`` returns before it imports Qt, so this needs neither a
    display nor a widget stack. It asserts the property that matters for a
    measurement harness -- a benchmark that cannot measure a thing says so
    rather than inventing a value (section 4 rule 8).
    """
    env = Environment(real_input=False)
    assert env.app is None

    result = bench_gui_refresh(env, samples=5)

    assert result.values_ms == []
    assert result.summary() == {"name": result.name, "n": 0, "note": result.note}
    assert "no assembled Body" in result.note


def test_keyboard_benchmark_is_skipped_without_an_honest_target() -> None:
    """The verifiable layout has no typing target, so no number is reported for it.

    Returning an empty distribution with a reason is the honest outcome; measuring
    a target the layout does not expose would report a harness artifact as a Body
    result. This path returns before any perception, so no application is needed.
    """
    env = Environment(real_input=False, workload="verifiable")
    result = bench_keyboard(env, samples=5)
    assert result.values_ms == []
    assert "no visible text field" in result.note
