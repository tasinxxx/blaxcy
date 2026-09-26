"""Phase 15 soak run (specification section 84, "full matrix/soak/documentation").

The Phase 14 benchmarks measure *how fast* a single action is. This module asks
the harder question a soak run exists to answer: **does the Body stay correct and
bounded when the same workflow is run over and over?** It repeats the section 74
``run_sequence`` end to end on the live desktop and reports three things:

* **stability** -- how often the whole workflow completes, and the halt reasons
  when it does not (the same honest vocabulary the workflow itself reports);
* **drift** -- whether latency, process memory or thread count trends upward over
  the run, rather than staying flat; and
* **resource invariants** -- that no input is left held, no lease is left live, no
  stale state update is accepted, and no worker thread outlives shutdown.

Each iteration also reports its **per-step** latency, so a drift is attributed to
the step that caused it rather than to "the workflow" as a whole.

It injects real input, so it is opt-in and refuses to run without
``--confirm-real-input``::

    python -m bench.soak --confirm-real-input --iterations 30

Nothing is fabricated (section 4 rule 8): every number is computed from the
samples the run actually produced, a target stays a target until re-measured, and
a metric the host cannot supply is reported as unavailable rather than guessed.
The analysis (:func:`analyze`) is deliberately pure, so it is unit-tested against
synthetic samples and does not need a desktop.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from ai.tool_protocol import ToolCall
from bench.real_desktop import Environment, _halt_reason, _percentile, _step_results
from schemas.actions import ToolName

#: A latency drift above this fraction (second half vs first half) is reported as
#: a real trend rather than noise. It is a *reporting* threshold, not a safety bound.
DRIFT_REPORT_FRACTION = 0.25

#: Steady-state per-iteration RSS growth above this (MiB) is reported as unbounded
#: growth. A desktop Body warms up (allocators, caches, GI/AT-SPI proxies), so the
#: verdict is taken from the *second half* of the run; the whole-run figure is still
#: reported so a warm-up cannot hide a truck-sized leak.
RSS_GROWTH_LIMIT_MB_PER_ITERATION = 2.0

#: Threads allowed to outlive shutdown. The Body stops and joins its workers; a
#: small tolerance keeps the check about orphans, not about unrelated host threads.
ORPHAN_THREAD_TOLERANCE = 0


@dataclass
class SoakSample:
    """One iteration's outcome and resource reading."""

    index: int
    completed: bool
    steps_completed: int
    total_steps: int
    halt_reason: str | None
    wall_clock_ms: float
    rss_mb: float | None
    threads: int
    held_keys: int
    held_buttons: int
    leases: int
    #: Executed steps' own ``elapsed_ms``, keyed by ``step_id``. Only steps the
    #: runner actually executed appear; a ``NOT_EXECUTED`` step carries no
    #: duration and must not be counted as a zero, which would invent a fast step.
    step_timings_ms: dict[str, float] = field(default_factory=dict)


@dataclass
class SoakReport:
    """The analysed result of a soak run."""

    iterations: int
    skipped: int
    completed: int
    stability: float
    halts: dict[str, int] = field(default_factory=dict)
    latency_ms: dict[str, float] = field(default_factory=dict)
    drift: dict[str, Any] = field(default_factory=dict)
    steps: dict[str, Any] = field(default_factory=dict)
    resources: dict[str, Any] = field(default_factory=dict)
    verdicts: dict[str, bool] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """A JSON-shaped report."""
        return {
            "iterations": self.iterations,
            "skipped": self.skipped,
            "completed": self.completed,
            "stability": round(self.stability, 4),
            "halts": dict(sorted(self.halts.items())),
            "latency_ms": {key: round(value, 3) for key, value in self.latency_ms.items()},
            "drift": self.drift,
            "steps": self.steps,
            "resources": self.resources,
            "verdicts": self.verdicts,
        }


def _rss_mb() -> float | None:
    """This process's resident set size in MiB, or ``None`` when unavailable."""
    try:
        import psutil
    except ImportError:  # pragma: no cover - psutil is a declared dependency
        return None
    try:
        return psutil.Process().memory_info().rss / (1024 * 1024)
    except Exception:  # pragma: no cover - defensive: psutil on an exotic host
        return None


def _thread_count() -> int:
    return threading.active_count()


def _held_input(env: Environment) -> tuple[int, int]:
    """How many keys and buttons the backend still holds, honestly reported."""
    backend = env.backend
    if backend is None:
        return 0, 0
    try:
        return len(backend.held_keys), len(backend.held_buttons)
    except Exception:  # pragma: no cover - a backend without the ownership API
        return 0, 0


def _lease_count(env: Environment) -> int:
    """Live leases in the state cache, or 0 when there is no Body."""
    if env.app is None:
        return 0
    try:
        return len(env.app.cache.leases)
    except Exception:  # pragma: no cover - defensive
        return 0


def _latency_drift(values: Sequence[float]) -> dict[str, Any]:
    """Second-half vs first-half ``p50`` drift for one series, or an honest null.

    Returns an empty dict when there are too few samples to split, so a series that
    cannot support a drift claim reports nothing rather than a made-up number.
    """
    if len(values) < 4:
        return {}
    half = len(values) // 2
    first_p50 = statistics.median(sorted(values[:half]))
    second_p50 = statistics.median(sorted(values[half:]))
    baseline = first_p50 if first_p50 > 0 else 1.0
    fraction = (second_p50 - first_p50) / baseline
    return {
        "first_half_p50_ms": round(first_p50, 3),
        "second_half_p50_ms": round(second_p50, 3),
        "drift_fraction": round(fraction, 4),
        "drift_within_bound": abs(fraction) <= DRIFT_REPORT_FRACTION,
    }


def _step_breakdown(samples: Sequence[SoakSample]) -> dict[str, Any]:
    """Per-step latency distribution and drift, so a drift names its step.

    Steps are reported in plan order (first seen), and a step contributes only on
    iterations where it actually executed -- a step that was ``NOT_EXECUTED`` after
    a halt has no duration and is not counted as a fast zero. The slowest step by
    ``p50`` is called out, which is the one to look at first when the workflow as a
    whole drifts.
    """
    order: list[str] = []
    timings: dict[str, list[float]] = {}
    for sample in samples:
        for step_id, value in sample.step_timings_ms.items():
            if step_id not in timings:
                timings[step_id] = []
                order.append(step_id)
            timings[step_id].append(float(value))

    rows: dict[str, Any] = {}
    for step_id in order:
        values = timings[step_id]
        ordered = sorted(values)
        row: dict[str, Any] = {
            "n": len(ordered),
            "p50_ms": round(statistics.median(ordered), 3),
            "p95_ms": round(_percentile(ordered, 0.95), 3),
            "min_ms": round(ordered[0], 3),
            "max_ms": round(ordered[-1], 3),
            "sum_p50_ms": round(sum(ordered), 3),
        }
        row.update(_latency_drift(values))
        rows[step_id] = row

    slowest = None
    if rows:
        slowest = max(rows, key=lambda step_id: rows[step_id]["p50_ms"])
    return {
        "order": order,
        "steps": rows,
        "slowest_step_by_p50": slowest,
    }


def analyze(
    samples: Sequence[SoakSample],
    *,
    skipped: int,
    rss_start_mb: float | None,
    rss_end_mb: float | None,
    threads_start: int,
    threads_end: int,
    threads_after_shutdown: int | None,
    runner_stats: dict[str, Any] | None = None,
) -> SoakReport:
    """Turn raw soak samples into a stability / drift / resource report.

    Pure and deterministic: it reads the samples and the start/end resource
    readings, and computes every number. It decides nothing about the desktop.
    """
    ran = len(samples)
    completed = sum(1 for sample in samples if sample.completed)
    halts: dict[str, int] = {}
    for sample in samples:
        if sample.halt_reason is not None:
            halts[sample.halt_reason] = halts.get(sample.halt_reason, 0) + 1

    latencies = [sample.wall_clock_ms for sample in samples]
    latency_ms: dict[str, float] = {}
    if latencies:
        latency_ms = {
            "p50": _percentile(sorted(latencies), 0.50),
            "p95": _percentile(sorted(latencies), 0.95),
            "min": min(latencies),
            "max": max(latencies),
        }

    drift: dict[str, Any] = {}
    if ran >= 4:
        half = ran // 2
        first_p50 = statistics.median(sorted(latencies[:half]))
        second_p50 = statistics.median(sorted(latencies[half:]))
        baseline = first_p50 if first_p50 > 0 else 1.0
        fraction = (second_p50 - first_p50) / baseline
        drift["latency_first_half_p50_ms"] = round(first_p50, 3)
        drift["latency_second_half_p50_ms"] = round(second_p50, 3)
        drift["latency_drift_fraction"] = round(fraction, 4)
        drift["latency_drift_within_bound"] = abs(fraction) <= DRIFT_REPORT_FRACTION
    else:
        drift["latency_drift_within_bound"] = None
    steps = _step_breakdown(samples)

    rss_series = [sample.rss_mb for sample in samples if sample.rss_mb is not None]
    if rss_start_mb is not None and rss_end_mb is not None:
        growth = rss_end_mb - rss_start_mb
        per_iteration = growth / ran if ran else 0.0
        drift["rss_start_mb"] = round(rss_start_mb, 1)
        drift["rss_end_mb"] = round(rss_end_mb, 1)
        drift["rss_growth_mb"] = round(growth, 1)
        drift["rss_growth_mb_per_iteration"] = round(per_iteration, 3)
    # The steady-state figure: does memory keep climbing after warm-up? A leak
    # shows as late-phase growth; a warm-up flattens. This is the verdict basis.
    if len(rss_series) >= 4:
        half = len(rss_series) // 2
        late_growth = (rss_series[-1] - rss_series[half]) / max(1, len(rss_series) - half - 1)
        drift["rss_first_half_median_mb"] = round(statistics.median(rss_series[:half]), 1)
        drift["rss_second_half_median_mb"] = round(statistics.median(rss_series[half:]), 1)
        drift["rss_late_growth_mb_per_iteration"] = round(late_growth, 3)
        drift["rss_late_growth_within_bound"] = late_growth <= RSS_GROWTH_LIMIT_MB_PER_ITERATION
        if rss_start_mb is not None and rss_end_mb is not None:
            drift["rss_growth_within_bound"] = per_iteration <= RSS_GROWTH_LIMIT_MB_PER_ITERATION
    else:
        drift["rss_late_growth_within_bound"] = None
        drift["rss_growth_within_bound"] = None

    resources: dict[str, Any] = {
        "threads_start": threads_start,
        "threads_end": threads_end,
        "threads_after_shutdown": threads_after_shutdown,
        "max_held_keys": max((sample.held_keys for sample in samples), default=0),
        "max_held_buttons": max((sample.held_buttons for sample in samples), default=0),
        "max_leases_after_iteration": max((sample.leases for sample in samples), default=0),
        "rss_mb_series": [round(value, 1) for value in rss_series],
        "runner_stats": runner_stats,
    }
    orphan_threads = None
    if threads_after_shutdown is not None:
        orphan_threads = max(0, threads_after_shutdown - threads_start)
        resources["orphan_threads"] = orphan_threads

    verdicts = {
        "stable": ran > 0 and completed == ran,
        "no_stuck_input": resources["max_held_keys"] == 0 and resources["max_held_buttons"] == 0,
        "no_live_lease_retained": resources["max_leases_after_iteration"] == 0,
        "no_orphan_threads": orphan_threads is None or orphan_threads <= ORPHAN_THREAD_TOLERANCE,
    }
    if drift.get("latency_drift_within_bound") is not None:
        verdicts["latency_stable"] = bool(drift["latency_drift_within_bound"])
    if drift.get("rss_late_growth_within_bound") is not None:
        # Steady-state growth is the verdict: a warm-up must not be reported as a leak.
        verdicts["memory_bounded"] = bool(drift["rss_late_growth_within_bound"])

    return SoakReport(
        iterations=ran,
        skipped=skipped,
        completed=completed,
        stability=(completed / ran) if ran else 0.0,
        halts=halts,
        latency_ms=latency_ms,
        drift=drift,
        steps=steps,
        resources=resources,
        verdicts=verdicts,
    )


def _step_timings(steps: Sequence[dict[str, Any]]) -> dict[str, float]:
    """Executed steps' own ``elapsed_ms``, keyed by ``step_id``.

    A ``NOT_EXECUTED`` step (the tail of a halted sequence) carries no duration and
    is omitted, so it cannot be counted as a suspiciously fast step.
    """
    timings: dict[str, float] = {}
    for step in steps:
        step_id = step.get("step_id")
        value = step.get("elapsed_ms")
        if not step_id or value is None or step.get("error_code") == "NOT_EXECUTED":
            continue
        timings[str(step_id)] = float(value)
    return timings


def _runner_stats(env: Environment) -> dict[str, Any] | None:
    """The sequence runner's health snapshot, or ``None`` when there is no Body."""
    if env.app is None:
        return None
    try:
        return env.app.sequence_runner.stats()
    except Exception:  # pragma: no cover - defensive
        return None


def run(*, iterations: int, workload: str = "workflow-verifiable", pause_ms: int = 0) -> int:
    """Run the soak and print its report. Requires real input by construction."""
    samples: list[SoakSample] = []
    skipped = 0
    threads_start = _thread_count()
    rss_start = _rss_mb()
    threads_after_shutdown: int | None = None
    runner_stats: dict[str, Any] | None = None

    with Environment(real_input=True, workload=workload) as env:
        plan = env.sequence_plan
        total_steps = len(plan)
        print(f"# BLAXCY soak run (iterations={iterations}, workload={workload})")
        print()
        for index in range(iterations):
            if not env.ensure_active():
                skipped += 1
                continue
            call = ToolCall(name=ToolName.RUN_SEQUENCE, arguments={"steps": plan})
            started = time.perf_counter()
            envelope = env.dispatch(call)
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            steps = _step_results(envelope)
            completed = bool(
                envelope.ok and len(steps) == total_steps and all(step.get("ok") for step in steps)
            )
            held_keys, held_buttons = _held_input(env)
            samples.append(
                SoakSample(
                    index=index,
                    completed=completed,
                    steps_completed=sum(1 for step in steps if step.get("ok")),
                    total_steps=total_steps,
                    halt_reason=None if completed else _halt_reason(envelope, steps),
                    wall_clock_ms=elapsed_ms,
                    rss_mb=_rss_mb(),
                    threads=_thread_count(),
                    held_keys=held_keys,
                    held_buttons=held_buttons,
                    leases=_lease_count(env),
                    step_timings_ms=_step_timings(steps),
                )
            )
            print(
                f"  iteration {index + 1}/{iterations}: "
                f"{'completed' if completed else 'halted'} "
                f"[{samples[-1].steps_completed}/{total_steps} steps] "
                f"{elapsed_ms:.1f} ms"
            )
            if pause_ms > 0 and index + 1 < iterations:
                time.sleep(pause_ms / 1000.0)
        runner_stats = _runner_stats(env)
        # Measured while the Body is still up, so a thread leak is visible; the
        # post-shutdown count below is what catches an orphan worker.
        rss_end = _rss_mb()
        threads_end = _thread_count()

    # The Body has now shut down (Environment.__exit__). Give its workers a moment
    # to exit before counting, then measure what is left.
    time.sleep(0.5)
    threads_after_shutdown = _thread_count()

    report = analyze(
        samples,
        skipped=skipped,
        rss_start_mb=rss_start,
        rss_end_mb=rss_end,
        threads_start=threads_start,
        threads_end=threads_end,
        threads_after_shutdown=threads_after_shutdown,
        runner_stats=runner_stats,
    )
    print()
    if report.steps.get("steps"):
        print("per-step latency (ms):")
        for step_id in report.steps["order"]:
            row = report.steps["steps"][step_id]
            drift = row.get("drift_fraction")
            drift_text = "n/a" if drift is None else f"{drift:+.1%}"
            print(
                f"  {step_id}: n={row['n']} p50={row['p50_ms']:.1f} "
                f"p95={row['p95_ms']:.1f} max={row['max_ms']:.1f} drift={drift_text}"
            )
        print(f"  slowest step by p50: {report.steps['slowest_step_by_p50']}")
        print()
    print(json.dumps(report.to_dict(), indent=2))
    print()
    failed = [name for name, ok in report.verdicts.items() if not ok]
    if failed:
        print(f"soak verdict: FAILED on {', '.join(failed)}")
        return 1
    print("soak verdict: all recorded invariants hold")
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="BLAXCY Phase 15 soak run (repeats the workflow and reports drift)."
    )
    parser.add_argument("--iterations", type=int, default=30, help="workflow repetitions")
    parser.add_argument(
        "--workload",
        choices=("workflow", "verifiable", "workflow-verifiable"),
        default="workflow-verifiable",
        help="which fixture layout / plan to soak (default: the self-verifying section 74 workflow)",
    )
    parser.add_argument("--pause-ms", type=int, default=0, help="pause between iterations")
    parser.add_argument(
        "--confirm-real-input",
        action="store_true",
        help="required: this run moves the real pointer and types real keys",
    )
    args = parser.parse_args(argv)
    if args.iterations < 1:
        print("iterations must be at least 1", file=sys.stderr)
        return 2
    if not args.confirm_real_input:
        print(
            "refusing to run: a soak injects real input on the live desktop. "
            "Re-run with --confirm-real-input.",
            file=sys.stderr,
        )
        return 2
    try:
        return run(
            iterations=args.iterations,
            workload=args.workload,
            pause_ms=args.pause_ms,
        )
    except RuntimeError as exc:
        print(f"soak could not run: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover - manual entry
    raise SystemExit(main())
