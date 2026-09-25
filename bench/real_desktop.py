"""Phase 14 benchmarks over the **real** desktop (specification section 76).

This module measures the numbers section 76 still owes after the fake-backend
benchmarks in ``docs/benchmark_report.md``: target resolution, lease
revalidation, accessibility query latency, an end-to-end click and keyboard
action, and an end-to-end five-step ``run_sequence`` -- all against the live X11
desktop and the section 74 fixture, with the **real** ``XtestBackend``.

It is deliberately a *measurement*, never a promise (section 4 rule 23): every
number is printed with its environment and sample count, and a target stays a
target until re-measured. Nothing here is fabricated. Where a real action cannot
be verified, the honest ``UNVERIFIED``/halt outcome is reported rather than
asserted away (section 60).

It injects real input, so it is opt-in::

    python -m bench.real_desktop --confirm-real-input

The read-only benchmarks (accessibility, resolution, revalidation) are safe and
run without the flag; the click, keyboard and sequence benchmarks are skipped
unless the flag is given, because they move the real pointer and type real keys.

Nothing here is part of the Body's runtime: it is a harness that drives the
production composition exactly as ``main.py`` would, so a number it produces is
a number the real program can be expected to produce.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ai.tool_protocol import ToolCall
from config.settings import load_settings
from control.backends import select_backend
from control.executor import revalidate_target
from core.application import BlaxcyApplication
from core.target_resolver import TargetResolver, bind_lease
from gui.app import self_excluded_settings
from schemas.actions import ToolName
from schemas.elements import ElementQuery
from schemas.enums import PolicyMode
from schemas.screen_state import ScreenState
from tests.harness import FixtureApp

#: The fixture's own controls, as the fixture names them.
FIXTURE_CONTROLS = ("Search", "Search Box", "Submit", "Text Input", "Toggle State")

#: The section 35 accessibility traversal is bounded by a deadline; the default
#: 250 ms is a performance bound that expires before the traversal reaches the
#: fixture on a busy desktop. The benchmark raises only that bound (as the section
#: 75 suite does), because a benchmark that cannot see its target measures nothing.
ACCESSIBILITY_BUDGET = {"deadline_ms": 8000, "max_nodes": 6000}

#: How long to wait for the fixture to appear in perception.
SETUP_TIMEOUT_SECONDS = 30.0

#: A five-step plan whose every step is chosen to be *verifiable* on a real
#: desktop: ``type_text`` establishes ``TEXT_PRESENT`` and ``click`` on the toggle
#: changes the control surface itself, so the section 60 postcondition is actually
#: met. A plan of plain-button clicks would be honest but unverifiable on a real
#: desktop (a button that only changes focus produces no ``MEANINGFUL`` change),
#: and section 76 asks for a *happy path*, not a halted one.
HAPPY_PATH_PLAN: list[dict[str, Any]] = [
    {"step_id": "s1", "tool": "type_text", "target": "Search Box", "text": "bench"},
    {"step_id": "s2", "tool": "type_text", "target": "Text Input", "text": "bench"},
    {"step_id": "s3", "tool": "click", "target": "Toggle State"},
    {"step_id": "s4", "tool": "type_text", "target": "Search Box", "text": "song"},
    {"step_id": "s5", "tool": "type_text", "target": "Text Input", "text": "alpha"},
]

#: The section 74 workflow, submitted *untimed* to record its honest outcome.
WORKFLOW_PLAN: list[dict[str, Any]] = [
    {"step_id": "w1", "tool": "click", "target": "Search", "role": "BUTTON"},
    {"step_id": "w2", "tool": "type_text", "target": "Search Box", "text": "song name"},
    {"step_id": "w3", "tool": "click", "target": "Submit", "role": "BUTTON"},
    {"step_id": "w4", "tool": "click", "target": "Results", "role": "LIST_ITEM"},
    {"step_id": "w5", "tool": "click", "target": "Play Button", "role": "BUTTON"},
]


@dataclass
class Samples:
    """One measured distribution, with the environment it was measured in."""

    name: str
    unit: str
    target: str | None
    values_ms: list[float] = field(default_factory=list)
    note: str = ""

    def add(self, seconds: float) -> None:
        self.values_ms.append(seconds * 1000.0)

    def summary(self) -> dict[str, Any]:
        """``p50``/``p95``/``min``/``max``/``n`` for this measurement."""
        if not self.values_ms:
            return {"name": self.name, "n": 0, "note": self.note}
        ordered = sorted(self.values_ms)
        return {
            "name": self.name,
            "unit": self.unit,
            "target": self.target,
            "n": len(ordered),
            "p50_ms": round(statistics.median(ordered), 3),
            "p95_ms": round(_percentile(ordered, 0.95), 3),
            "min_ms": round(ordered[0], 3),
            "max_ms": round(ordered[-1], 3),
            "note": self.note,
        }


def _percentile(ordered: Sequence[float], fraction: float) -> float:
    """Nearest-rank percentile over an already-sorted sequence."""
    if not ordered:
        return float("nan")
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return ordered[index]


class Environment:
    """The live fixture plus the assembled Body, with the cleanup they need."""

    def __init__(self, *, real_input: bool) -> None:
        self.real_input = real_input
        self.fixture: FixtureApp | None = None
        self.app: BlaxcyApplication | None = None
        self.backend: Any = None
        self.restore_pointer: tuple[int, int] | None = None
        self.notes: list[str] = []

    def __enter__(self) -> Environment:
        selected = select_backend()
        if selected is None:
            raise RuntimeError("no input backend passed its functional probe on this display")
        probe = selected.probe()
        if not probe.available:
            raise RuntimeError(f"no usable input backend: {probe.reason}")
        self.backend = selected
        self.restore_pointer = selected.get_pointer_position()

        if self.real_input:
            self.fixture = FixtureApp(platform="xcb", start_timeout=SETUP_TIMEOUT_SECONDS).start()

        base = self_excluded_settings(load_settings(None))
        settings = base.model_copy(
            update={
                "accessibility": base.accessibility.model_copy(update=ACCESSIBILITY_BUDGET),
            }
        )
        self.app = BlaxcyApplication(
            settings,
            backend=self.backend,
            # Never take the operator's CLIPBOARD selection, and keep the section 64
            # heartbeat out of their runtime directory.
            clipboard=None,
            watchdog_path=Path("/tmp/blaxcy-bench-watchdog.json"),
        )
        startup = self.app.start()
        if not startup.started:
            raise RuntimeError(f"the Body could not start on this host: {startup.notes}")
        self.app.set_mode(PolicyMode.ASSIST)

        if self.fixture is not None:
            self._wait_for_fixture()
            self._activate_fixture()
        return self

    def __exit__(self, *_exc: object) -> None:
        if self.restore_pointer is not None and self.backend is not None:
            try:
                self.backend.move_pointer(*self.restore_pointer)
                self.backend.flush()
            except Exception:
                pass
        if self.app is not None:
            self.app.shutdown()
        if self.fixture is not None:
            self.fixture.stop()

    # -- Fixture setup --------------------------------------------------------

    def _perceived_names(self) -> set[str]:
        assert self.app is not None
        state = self.app.cache.current
        if state is None:
            return set()
        names: set[str] = set()
        for element in state.elements:
            if element.text:
                names.add(element.text)
            if element.accessible_name:
                names.add(element.accessible_name)
        return names

    def _wait_for_fixture(self) -> None:
        assert self.app is not None
        deadline = time.monotonic() + SETUP_TIMEOUT_SECONDS
        wanted = set(FIXTURE_CONTROLS)
        while time.monotonic() < deadline:
            self.app.perceive()
            if wanted <= self._perceived_names():
                return
            time.sleep(0.25)
        raise RuntimeError(
            f"the fixture's controls were not perceived within {SETUP_TIMEOUT_SECONDS}s "
            f"(saw {len(self._perceived_names())} names)"
        )

    def ensure_active(self) -> bool:
        """Re-raise the fixture before a timed dispatch, outside the measurement.

        The fixture is a real window on a real desktop, so over a multi-minute run
        the window manager can lower it (or the operator can change focus), after
        which every target is correctly refused as occluded. Re-activation happens
        *before* the timer runs, so the measured action is still the action, not the
        setup. Returns whether the fixture is active afterwards.
        """
        if self.fixture is None:
            return True
        try:
            self._activate_fixture(timeout=5.0)
        except RuntimeError:
            return False
        return True

    def _activate_fixture(self, *, timeout: float = SETUP_TIMEOUT_SECONDS) -> None:
        """Make the fixture window active, or refuse to inject into an unknown one."""
        assert self.app is not None and self.fixture is not None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.fixture.set_focus("text_input")
            state = self.app.perceive()
            if state is None:
                time.sleep(0.25)
                continue
            element = next(
                (
                    candidate
                    for candidate in state.elements
                    if candidate.text == "Text Input" or candidate.accessible_name == "Text Input"
                ),
                None,
            )
            if element is not None and state.active_app == element.owner_app:
                return
            time.sleep(0.25)
        raise RuntimeError(
            "the fixture window could not be made active; refusing to inject real input "
            "into an unknown focused window"
        )

    def live_state(self) -> ScreenState:
        """One fresh observation, or a hard error when perception is unavailable."""
        assert self.app is not None
        state = self.app.perceive()
        if state is None:
            raise RuntimeError("perception produced no state")
        return state

    def dispatch(self, call: ToolCall) -> Any:
        assert self.app is not None
        return self.app.dispatch(call)


# -- Read-only benchmarks (no input injected) ---------------------------------


def bench_accessibility(env: Environment, samples: int) -> Samples:
    """Section 35: one live accessibility traversal, timed."""
    assert env.app is not None
    result = Samples("accessibility query (live AT-SPI refresh)", "ms", None)
    service = env.app.accessibility
    service.refresh()  # warm-up: a cold first traversal is not the steady state
    for _ in range(samples):
        started = time.perf_counter()
        service.refresh()
        result.add(time.perf_counter() - started)
    result.note = f"{len(list(env.app.cache.current.elements)) if env.app.cache.current else 0} elements in the cached state"
    return result


def bench_resolution(env: Environment, samples: int) -> Samples:
    """Section 43: resolve a real target over a frozen live observation."""
    resolver = TargetResolver(env.app.settings.resolver) if env.app else TargetResolver(load_settings(None).resolver)
    state = env.live_state()
    elements = list(state.elements)
    target = _unique_target(elements)
    label = target.text or target.accessible_name
    assert label
    query = ElementQuery(text=label)
    resolver.resolve(query, elements, occluders=elements, state=state)  # warm-up
    result = Samples("target resolution (live elements)", "ms", None)
    for _ in range(samples):
        started = time.perf_counter()
        resolver.resolve(query, elements, occluders=elements, state=state)
        result.add(time.perf_counter() - started)
    result.note = f"target {label!r} over {len(elements)} live elements, occluders=all"
    return result


def bench_revalidation(env: Environment, samples: int) -> Samples:
    """Section 45: the full revalidation checklist for a live lease."""
    assert env.app is not None
    state = env.live_state()
    elements = list(state.elements)
    target = _unique_target(elements)
    label = target.text or target.accessible_name
    resolver = TargetResolver(env.app.settings.resolver)
    resolution = resolver.resolve(ElementQuery(text=label), elements, occluders=elements, state=state)
    assert resolution.best is not None
    lease = bind_lease(resolution.best, state)
    element = resolution.best.element
    max_age = float(env.app.settings.verification.max_action_state_age_ms)
    result = Samples("lease revalidation (section 45 checklist)", "ms", "<= 25 ms")
    for _ in range(samples):
        started = time.perf_counter()
        outcome = revalidate_target(
            lease,
            state,
            max_state_age_ms=max_age,
            previous_element=element,
            occluders=elements,
        )
        result.add(time.perf_counter() - started)
    result.note = f"ok={outcome.ok} for {label!r}; checks={len(outcome.checks)}"
    return result


# -- Real-input benchmarks (opt-in) -------------------------------------------


def bench_click(env: Environment, samples: int) -> Samples:
    """Section 76: an end-to-end click on a real control, through the dispatcher."""
    result = Samples("end-to-end click (resolve -> lease -> revalidate -> click -> verify)", "ms", None)
    verified = 0
    codes: dict[str, int] = {}
    reason = ""
    skipped = 0
    for _ in range(samples):
        if not env.ensure_active():
            skipped += 1
            continue
        call = ToolCall(name=ToolName.CLICK, arguments={"target": "Toggle State"})
        started = time.perf_counter()
        envelope = env.dispatch(call)
        result.add(time.perf_counter() - started)
        if _verified(envelope):
            verified += 1
        else:
            reason = reason or str(getattr(envelope, "message", ""))
            code = _code(envelope) or "VERIFIED_FALSE"
            codes[code] = codes.get(code, 0) + 1
    result.note = f"{verified}/{samples - skipped} VERIFIED"
    if skipped:
        result.note += f"; {skipped} skipped (fixture not active)"
    if codes:
        result.note += f"; refusals: {codes}; first: {reason[:160]}"
    return result


def bench_keyboard(env: Environment, samples: int) -> Samples:
    """Section 76: an end-to-end type_text on a real field, through the dispatcher."""
    result = Samples("end-to-end keyboard (type_text + verify)", "ms", None)
    verified = 0
    codes: dict[str, int] = {}
    reason = ""
    skipped = 0
    for index in range(samples):
        if not env.ensure_active():
            skipped += 1
            continue
        call = ToolCall(
            name=ToolName.TYPE_TEXT,
            arguments={"target": "Text Input", "text": f"bench{index}"},
        )
        started = time.perf_counter()
        envelope = env.dispatch(call)
        result.add(time.perf_counter() - started)
        if _verified(envelope):
            verified += 1
        else:
            reason = reason or str(getattr(envelope, "message", ""))
            code = _code(envelope) or "VERIFIED_FALSE"
            codes[code] = codes.get(code, 0) + 1
    result.note = f"{verified}/{samples - skipped} VERIFIED"
    if skipped:
        result.note += f"; {skipped} skipped (fixture not active)"
    if codes:
        result.note += f"; refusals: {codes}; first: {reason[:160]}"
    return result


def diagnose_fixture(env: Environment) -> list[dict[str, Any]]:
    """Why each fixture control is (or is not) injectable, from the live state.

    Read-only: it resolves each target with the exact call the executor makes and
    reports the verdict, so a refused benchmark is explained rather than guessed.
    """
    assert env.app is not None
    state = env.live_state()
    rows: list[dict[str, Any]] = []
    for name in FIXTURE_CONTROLS:
        result = env.app.resolver.resolve(
            ElementQuery(text=name), state.elements, occluders=state.elements, state=state
        )
        occlusion = result.occlusion
        rows.append(
            {
                "target": name,
                "status": result.status.value,
                "actionable": result.is_actionable,
                "ratio": None if occlusion is None else round(occlusion.ratio, 3),
                "covering": [] if occlusion is None else list(occlusion.covering),
                "active_app": state.active_app,
            }
        )
    stack = []
    try:
        stack = [(w.title, w.pid) for w in env.app.windows.stacked_windows()]
    except Exception as exc:
        stack = [("<unavailable>", str(exc))]
    return [{"stack": stack}, *rows]


def bench_sequence(env: Environment, samples: int, *, halt_on_unknown: bool = False) -> Samples:
    """Section 76: the real-desktop end-to-end five-step ``run_sequence``."""
    result = Samples("end-to-end 5-step run_sequence (real desktop)", "ms", "<= 4000 ms warm")
    completed = 0
    halted: dict[str, int] = {}
    skipped = 0
    for _ in range(samples):
        if not env.ensure_active():
            skipped += 1
            continue
        call = ToolCall(name=ToolName.RUN_SEQUENCE, arguments={"steps": HAPPY_PATH_PLAN})
        started = time.perf_counter()
        envelope = env.dispatch(call)
        result.add(time.perf_counter() - started)
        steps = _step_results(envelope)
        if envelope.ok and len(steps) == len(HAPPY_PATH_PLAN) and all(
            step.get("ok") for step in steps
        ):
            completed += 1
        else:
            reason = _halt_reason(envelope, steps)
            halted[reason] = halted.get(reason, 0) + 1
    result.note = f"{completed}/{samples - skipped} completed all 5 steps"
    if skipped:
        result.note += f"; {skipped} skipped (fixture not active)"
    if halted:
        result.note += f"; halted: {halted}"
    return result


def bench_workflow_outcome(env: Environment) -> dict[str, Any]:
    """Submit the section 74 workflow once, untimed, and record how it really ends."""
    call = ToolCall(name=ToolName.RUN_SEQUENCE, arguments={"steps": WORKFLOW_PLAN})
    started = time.perf_counter()
    envelope = env.dispatch(call)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    steps = _step_results(envelope)
    return {
        "ok": bool(envelope.ok),
        "error_code": _code(envelope),
        "elapsed_ms": round(elapsed_ms, 3),
        "steps": [
            {
                "step_id": step.get("step_id"),
                "ok": step.get("ok"),
                "error_code": step.get("error_code"),
                "verification": _verification_state(step.get("verification")),
            }
            for step in steps
        ],
    }


# -- Helpers ------------------------------------------------------------------


def _unique_target(elements: Sequence[Any]) -> Any:
    """A live, uniquely labelled, actionable element with geometry."""
    from collections import Counter

    counts = Counter(
        " ".join((element.text or element.accessible_name or "").split()).casefold()
        for element in elements
    )
    for element in elements:
        label = " ".join((element.text or element.accessible_name or "").split()).casefold()
        if not label or counts[label] != 1:
            continue
        if element.is_actionable and element.bbox is not None:
            return element
    raise RuntimeError("no uniquely labelled actionable element with geometry on this desktop")


def _verified(envelope: Any) -> bool:
    """Whether an envelope reports a ``VERIFIED`` postcondition."""
    verification = getattr(envelope, "verification", None)
    if verification is None:
        return False
    if isinstance(verification, dict):
        return verification.get("state") == "VERIFIED"
    return getattr(verification, "state", None) == "VERIFIED" or getattr(verification, "value", None) == "VERIFIED"


def _step_results(envelope: Any) -> list[dict[str, Any]]:
    """The per-step result array from a ``run_sequence`` envelope."""
    data = getattr(envelope, "data", None) or {}
    steps = data.get("steps") if isinstance(data, dict) else None
    return list(steps) if steps else []


def _halt_reason(envelope: Any, steps: list[dict[str, Any]]) -> str:
    """A short reason the sequence did not complete."""
    for step in steps:
        if not step.get("ok"):
            return f"{step.get('step_id')}:{step.get('error_code') or step.get('verification')}"
    return _code(envelope) or "incomplete"


def _verification_state(value: Any) -> str | None:
    """The verification state from a step result, whichever shape it arrived in."""
    if value is None:
        return None
    if isinstance(value, dict):
        return value.get("state")
    return getattr(value, "state", None) or getattr(value, "value", None) or str(value)


def _code(envelope: Any) -> str | None:
    code = getattr(envelope, "error_code", None)
    if code is None:
        return None
    return getattr(code, "value", str(code))


# -- Entry point --------------------------------------------------------------


def run(*, samples: int, input_samples: int, sequence_samples: int, real_input: bool) -> int:
    """Run the benchmark set and print a markdown report to stdout."""
    results: list[dict[str, Any]] = []
    with Environment(real_input=real_input) as env:
        print(f"# BLAXCY real-desktop benchmarks (samples: read-only={samples}, input={input_samples})")
        print()
        print(f"- session: X11 {__import__('os').environ.get('DISPLAY')}")
        print(f"- real input: {real_input}")
        print()
        results.append(bench_accessibility(env, samples).summary())
        results.append(bench_resolution(env, samples).summary())
        results.append(bench_revalidation(env, samples).summary())
        if real_input:
            results.append(bench_click(env, input_samples).summary())
            results.append(bench_keyboard(env, input_samples).summary())
            results.append(bench_sequence(env, sequence_samples).summary())
            results.append({"name": "section 74 workflow outcome", **bench_workflow_outcome(env)})
        if real_input:
            print("## fixture target diagnosis")
            print(json.dumps(diagnose_fixture(env), indent=2))
            if env.fixture is not None:
                # Proof the input really landed: the fixture counts its own clicks
                # and holds the typed text. An honest benchmark shows the action
                # reached the desktop even when section 60 cannot verify it.
                print("## fixture state after input benchmarks")
                print(json.dumps(env.fixture.stats(), indent=2))
        print(json.dumps(results, indent=2))
        if env.notes:
            print("notes:", env.notes)
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="BLAXCY real-desktop benchmarks (section 76).")
    parser.add_argument("--samples", type=int, default=200, help="read-only sample count")
    parser.add_argument("--input-samples", type=int, default=50, help="real-input sample count")
    parser.add_argument("--sequence-samples", type=int, default=20, help="real sequence sample count")
    parser.add_argument(
        "--confirm-real-input",
        action="store_true",
        help="opt in to the click/keyboard/sequence benchmarks (they inject real input)",
    )
    args = parser.parse_args(argv)
    try:
        return run(
            samples=args.samples,
            input_samples=args.input_samples,
            sequence_samples=args.sequence_samples,
            real_input=args.confirm_real_input,
        )
    except RuntimeError as exc:
        print(f"benchmark could not run: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover - manual entry
    raise SystemExit(main())
