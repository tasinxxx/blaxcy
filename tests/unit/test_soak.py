"""The Phase 15 soak analysis and its opt-in guard (specification section 84).

The live run needs a real desktop and injects real input, so the *decision*
surface is tested here against synthetic samples: stability, drift, and the
resource invariants (held input, live leases, orphan threads, memory growth).
"""

from __future__ import annotations

from typing import Any

import pytest

from bench.real_desktop import _halt_detail
from bench.soak import SoakReport, SoakSample, _step_timings, analyze, main


def _sample(
    index: int,
    *,
    completed: bool = True,
    halt_reason: str | None = None,
    halt_detail: str | None = None,
    ms: float = 100.0,
    rss_mb: float = 100.0,
    held_keys: int = 0,
    held_buttons: int = 0,
    leases: int = 0,
    steps: dict[str, float] | None = None,
) -> SoakSample:
    """One synthetic iteration sample."""
    return SoakSample(
        index=index,
        completed=completed,
        steps_completed=5 if completed else 2,
        total_steps=5,
        halt_reason=halt_reason,
        halt_detail=halt_detail,
        wall_clock_ms=ms,
        rss_mb=rss_mb,
        threads=8,
        held_keys=held_keys,
        held_buttons=held_buttons,
        leases=leases,
        step_timings_ms=steps or {},
    )


def _base_report(**overrides: object) -> SoakReport:
    """Analyze ten clean samples, with any overrides a test needs."""
    readings: dict[str, object] = {
        "skipped": 0,
        "rss_start_mb": 100.0,
        "rss_end_mb": 100.5,
        "threads_start": 8,
        "threads_end": 8,
        "threads_after_shutdown": 7,
    }
    readings.update(overrides)
    return analyze([_sample(index) for index in range(10)], **readings)  # type: ignore[arg-type]


def test_a_clean_soak_reports_every_invariant_holding() -> None:
    """The happy path: full stability, flat resources, nothing left held."""
    report = _base_report()

    assert report.stability == 1.0
    assert report.halts == {}
    assert all(report.verdicts.values()), report.verdicts
    assert report.resources["orphan_threads"] == 0


def test_a_single_halt_reduces_stability_and_is_named() -> None:
    """Stability is the honest completion fraction, and halts keep their reason."""
    samples = [_sample(index) for index in range(9)]
    samples.append(_sample(9, completed=False, halt_reason="s2:FOCUS_MISMATCH"))
    report = analyze(
        samples,
        skipped=0,
        rss_start_mb=100.0,
        rss_end_mb=100.1,
        threads_start=8,
        threads_end=8,
        threads_after_shutdown=8,
    )

    assert report.completed == 9
    assert report.stability == 0.9
    assert report.halts == {"s2:FOCUS_MISMATCH": 1}
    assert report.verdicts["stable"] is False


def test_stuck_input_and_a_live_lease_are_both_caught() -> None:
    """A held key, a held button or a retained lease fails the run (sections 52/63)."""
    samples = [_sample(index) for index in range(9)]
    samples.append(_sample(9, held_buttons=1, leases=1))
    report = analyze(
        samples,
        skipped=0,
        rss_start_mb=100.0,
        rss_end_mb=100.1,
        threads_start=8,
        threads_end=8,
        threads_after_shutdown=8,
    )

    assert report.verdicts["no_stuck_input"] is False
    assert report.verdicts["no_live_lease_retained"] is False
    assert report.resources["max_held_buttons"] == 1
    assert report.resources["max_leases_after_iteration"] == 1


def test_latency_drift_over_the_run_is_measured() -> None:
    """The second half is compared with the first, so a creep is visible."""
    samples = [_sample(index, ms=100.0) for index in range(5)]
    samples += [_sample(index, ms=300.0) for index in range(5, 10)]
    report = analyze(
        samples,
        skipped=0,
        rss_start_mb=100.0,
        rss_end_mb=100.1,
        threads_start=8,
        threads_end=8,
        threads_after_shutdown=8,
    )

    assert report.drift["latency_first_half_p50_ms"] == 100.0
    assert report.drift["latency_second_half_p50_ms"] == 300.0
    assert report.drift["latency_drift_within_bound"] is False
    assert report.verdicts["latency_stable"] is False


def _report_with_series(series: list[float], **overrides: object) -> SoakReport:
    """Analyze one sample per RSS reading, so the steady-state trend is visible."""
    readings: dict[str, object] = {
        "skipped": 0,
        "rss_start_mb": series[0],
        "rss_end_mb": series[-1],
        "threads_start": 8,
        "threads_end": 8,
        "threads_after_shutdown": 8,
    }
    readings.update(overrides)
    samples = [_sample(index, rss_mb=rss) for index, rss in enumerate(series)]
    return analyze(samples, **readings)  # type: ignore[arg-type]


def test_continued_memory_growth_fails_the_run() -> None:
    """A steady-state climb that never flattens is reported as unbounded growth."""
    report = _report_with_series([100.0, 100.0, 100.0, 100.0, 120.0, 140.0, 160.0, 180.0, 200.0, 220.0])

    assert report.drift["rss_growth_mb_per_iteration"] == 12.0
    assert report.drift["rss_late_growth_mb_per_iteration"] == 20.0
    assert report.drift["rss_late_growth_within_bound"] is False
    assert report.verdicts["memory_bounded"] is False


def test_a_startup_warmup_is_not_reported_as_a_leak() -> None:
    """The verdict is steady-state growth, so a flattening warm-up is not a leak.

    Memory that climbs early (allocators, caches, GI/AT-SPI proxies) and then goes
    flat is a warm-up, not unbounded growth; only continued late-phase climbing is.
    """
    report = _report_with_series([100.0, 130.0, 150.0, 160.0, 165.0] + [165.0] * 5)

    assert report.drift["rss_late_growth_mb_per_iteration"] == 0.0
    assert report.drift["rss_growth_within_bound"] is False  # the whole-run figure is reported
    assert report.verdicts["memory_bounded"] is True  # but the steady state is flat


def test_orphan_threads_after_shutdown_are_caught() -> None:
    """A worker that outlives shutdown is an orphan (section 64/85)."""
    report = _base_report(threads_start=8, threads_after_shutdown=11)

    assert report.resources["orphan_threads"] == 3
    assert report.verdicts["no_orphan_threads"] is False


def test_skipped_iterations_are_not_counted_as_samples() -> None:
    """A fixture that could not be activated is reported, not silently averaged in."""
    report = _base_report(skipped=4)

    assert report.iterations == 10
    assert report.skipped == 4


def _breakdown_report(samples: list[SoakSample]) -> SoakReport:
    """Analyze the samples with the fixed resource readings the other tests use."""
    return analyze(
        samples,
        skipped=0,
        rss_start_mb=100.0,
        rss_end_mb=100.1,
        threads_start=8,
        threads_end=8,
        threads_after_shutdown=8,
    )


def test_per_step_timings_are_reported_in_plan_order() -> None:
    """Each step's own latency is reported, with the slowest one called out."""
    samples = [_sample(index, steps={"s1": 10.0, "s2": 20.0, "s3": 30.0}) for index in range(10)]
    report = _breakdown_report(samples)

    assert report.steps["order"] == ["s1", "s2", "s3"]
    assert report.steps["steps"]["s3"]["p50_ms"] == 30.0
    assert report.steps["steps"]["s1"]["n"] == 10
    assert report.steps["slowest_step_by_p50"] == "s3"


def test_a_drifting_step_is_attributed_by_name() -> None:
    """A drift in the workflow is attributed to the step that caused it."""
    samples: list[SoakSample] = []
    for index in range(10):
        slow = 30.0 if index < 5 else 300.0
        samples.append(_sample(index, steps={"s1": 10.0, "s3": slow}))
    report = _breakdown_report(samples)

    assert report.steps["steps"]["s1"]["drift_within_bound"] is True
    assert report.steps["steps"]["s3"]["drift_within_bound"] is False
    assert report.steps["steps"]["s3"]["drift_fraction"] == 9.0
    assert report.steps["slowest_step_by_p50"] == "s3"


def test_a_step_that_did_not_execute_contributes_no_timing() -> None:
    """A ``NOT_EXECUTED`` step carries no duration and is not counted as a fast one."""
    steps: list[dict[str, Any]] = [
        {"step_id": "s1", "elapsed_ms": 12.0},
        {"step_id": "s2", "elapsed_ms": 0.0, "error_code": "NOT_EXECUTED"},
        {"step_id": "s3"},
    ]
    assert _step_timings(steps) == {"s1": 12.0}

    # A step that ran in only some iterations reports the count it really has, so
    # a halted run cannot make a step look uniformly fast.
    samples = [_sample(index, steps={"s1": 10.0, "s2": 20.0}) for index in range(5)]
    samples += [_sample(index, steps={"s1": 10.0}) for index in range(5, 10)]
    report = _breakdown_report(samples)
    assert report.steps["steps"]["s1"]["n"] == 10
    assert report.steps["steps"]["s2"]["n"] == 5


def test_a_run_without_step_timings_reports_none() -> None:
    """Without per-step data the breakdown is empty, never fabricated."""
    report = _breakdown_report([_sample(index) for index in range(4)])
    assert report.steps["steps"] == {}
    assert report.steps["slowest_step_by_p50"] is None


def test_a_halt_detail_names_the_occluding_evidence() -> None:
    """A section 46 refusal reports the coverage ratio and the covering objects.

    This is the diagnostic that separates an environmental occlusion (something
    really is on top of the target) from a code fault, without the soak having to
    weaken the refusal to find out.
    """
    steps: list[dict[str, Any]] = [
        {"step_id": "s1", "ok": True, "error_code": None, "elapsed_ms": 10.0},
        {
            "step_id": "s3",
            "ok": False,
            "error_code": "TARGET_OCCLUDED",
            "data": {
                "revalidation": {
                    "occlusion": {
                        "blocked": True,
                        "declared_occluded": False,
                        "ratio": 0.78,
                        "covering": ["firefox-esr:window-2"],
                    }
                }
            },
        },
        {"step_id": "s4", "ok": False, "error_code": "NOT_EXECUTED"},
    ]

    detail = _halt_detail(steps)

    assert detail is not None
    assert detail.startswith("s3:TARGET_OCCLUDED")
    assert "ratio=0.78" in detail
    assert "firefox-esr:window-2" in detail


def test_a_halt_detail_is_none_when_nothing_failed() -> None:
    """A clean run reports no halt detail rather than an invented one."""
    assert _halt_detail([{"step_id": "s1", "ok": True}]) is None


def test_analyze_aggregates_halt_details() -> None:
    """Halt details are counted per distinct reason, so a pattern is visible."""
    samples = [_sample(index) for index in range(8)]
    samples.append(
        _sample(8, completed=False, halt_reason="s3:TARGET_OCCLUDED", halt_detail="s3:occluded")
    )
    samples.append(
        _sample(9, completed=False, halt_reason="s3:TARGET_OCCLUDED", halt_detail="s3:occluded")
    )
    report = analyze(
        samples,
        skipped=0,
        rss_start_mb=100.0,
        rss_end_mb=100.1,
        threads_start=8,
        threads_end=8,
        threads_after_shutdown=8,
    )

    assert report.halt_details == {"s3:occluded": 2}
    assert report.to_dict()["halt_details"] == {"s3:occluded": 2}


def test_the_cli_refuses_without_explicit_real_input_consent(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A soak moves the real pointer; it must never start by accident (section 13)."""
    code = main(["--iterations", "5"])

    assert code == 2
    captured = capsys.readouterr()
    assert "confirm-real-input" in captured.err


def test_the_cli_rejects_a_non_positive_iteration_count() -> None:
    """A soak of zero iterations is a mistake, not a valid run."""
    assert main(["--iterations", "0", "--confirm-real-input"]) == 2
