"""The Phase 15 soak analysis and its opt-in guard (specification section 84).

The live run needs a real desktop and injects real input, so the *decision*
surface is tested here against synthetic samples: stability, drift, and the
resource invariants (held input, live leases, orphan threads, memory growth).
"""

from __future__ import annotations

import pytest

from bench.soak import SoakReport, SoakSample, analyze, main


def _sample(
    index: int,
    *,
    completed: bool = True,
    halt_reason: str | None = None,
    ms: float = 100.0,
    rss_mb: float = 100.0,
    held_keys: int = 0,
    held_buttons: int = 0,
    leases: int = 0,
) -> SoakSample:
    """One synthetic iteration sample."""
    return SoakSample(
        index=index,
        completed=completed,
        steps_completed=5 if completed else 2,
        total_steps=5,
        halt_reason=halt_reason,
        wall_clock_ms=ms,
        rss_mb=rss_mb,
        threads=8,
        held_keys=held_keys,
        held_buttons=held_buttons,
        leases=leases,
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
