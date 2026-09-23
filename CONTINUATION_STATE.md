# BLAXCY — Continuation State

> This file is an index, not a source of truth by itself. Before trusting
> anything below, reconcile it against git and the actual repository
> (Part II §8, step 4). If this file and the repository disagree, the
> repository wins and this file gets corrected.

## Project identity
- Name: BLAXCY
- Project root: `/home/tsn/blaxcy` (confirmed via `pwd` this session)
- Repository: **git present** (`.git` exists; branch `master` with **zero commits**;
  every file is untracked). A previous revision of this file claimed git was
  absent — that was wrong; corrected against the repository per §8 step 4.
  `git init` was not needed (the repo already exists) and no commit has been made.
- Last verified commit: none (no commits exist yet)

## Current phase
- Phase: Phase 1 — Schemas / calibration
- Phase status: **COMPLETE** (implemented + tests + verification all run this session)

## Completed milestones
- **Phase 0 — Repository / harness / probes: COMPLETE**, verified 2026-09-23 (prior session)
  and **re-verified this session** (`pytest`/`ruff`/`mypy` all re-run).
  Deliverables: repo scaffold (§26), typed schemas (§28/§57/§77), TOML config with
  enforced security invariants (§72), session detection (§29/30), functional
  capability probes (§28), CLI (`main.py probe|session|config`), §74 fixture
  application + harness (`tests/fixtures/fixture_app.py`, `tests/harness/fixture_client.py`).
- **Phase 1 — Schemas / calibration: COMPLETE**, verified 2026-09-23.
  Evidence: `pytest` -> `144 passed, 1 skipped`; `mypy .` -> `Success: no issues
  found in 52 source files`; `ruff check .` -> `All checks passed!`.
  Deliverables (all new, none pre-existing):
  - `schemas/enums.py` (extended): `UIRole` (§37), `PerceptionSource` (§37/38),
    `PASSWORD_ROLES`, `TEXT_ENTRY_ROLES`, `SOURCE_BASE_CONFIDENCE` (§39),
    `CHANGE_CLASS_SEVERITY` (§34).
  - `schemas/geometry.py` (§31): `Point`/`Rect`/`Size` (space-tagged), `MonitorGeometry`,
    `MonitorLayout` (validated topology), `Affine2D`, `MonitorInputTransform`,
    `GeometryMap` implementing FRAME->MONITOR->DESKTOP->INPUT with clamping.
  - `core/calibration.py` (§31): `CalibrationSample`, `MonitorCalibration`,
    `Calibration` (per-monitor, disarmed when incomplete), least-squares affine
    solve with rank (non-collinearity) + residual checks, `identity_geometry_map`.
  - `schemas/screen_state.py` (§32/34/45/65): `ScreenState` (versioned, monotonic
    age), `ScreenDelta`/`ChangeRegion`, `classify_delta`.
  - `schemas/elements.py` (§37/38/39): `UIElement` (password content invariant),
    `identity_fingerprint`, `perception_confidence`, `ElementQuery`, `ElementCandidate`.
  - `schemas/leases.py` (§44): `ElementLease`, `issue_lease`.
  - `schemas/actions.py` (§57/66): tool vocabulary + `TOOL_ACTION_CLASS`,
    `REQUIRED_TOOLS`, `PlannedAction`, uniform `ToolEnvelope`.
  - `schemas/events.py` (§65): `EventType`, `Event`, sequence/safety event models.
  - `schemas/sequences.py` (§66.1): `HaltCondition`, `SequenceStep` (rejects
    coordinates/leases/pre-resolved ids), `RunSequenceRequest` (halt set may only
    narrow), `SequenceStepResult`, `SequenceResult`, `SequenceProgress`.
  - Unit tests: `tests/unit/test_geometry.py`, `test_calibration.py`,
    `test_screen_state.py`, `test_elements.py`, `test_leases.py`, `test_actions.py`,
    `test_events.py`, `test_sequences.py`.
  - Integration test: `tests/integration/test_calibration_real_display.py` — 5 tests,
    executed against the real X11 display this session (all passed): live monitor
    layout matches the X root geometry; a full calibration solve over the real
    topology is complete + calibrated; DESKTOP->INPUT agrees with live pointer
    readback; FRAME->DESKTOP round-trips; off-desktop points clamp. It injects
    **no** input (that is Phase 7, per §4 rule 13); the X11 XTEST identity
    precondition is asserted and cross-checked against readback, not assumed.

## Current task
- Task: Start Phase 2 — frame engine (`core/frame_engine.py`) and change detector (`core/change_detector.py`).
- Subtask: none

## Interruption recovery
- Last session state: normal
- Last operation started: `.venv/bin/pytest` (full suite)
- Last operation status: completed
- Recovery action: reconciled the file against git — found the "no git" claim was
  stale/incorrect and corrected it; re-ran the Phase 0 gate before editing anything.

## Last known blocker
- none

## Files being actively modified
- (none mid-change)

## Last test results (verbatim, not paraphrased as "passed")
- Command: `. .venv/bin/activate && python -m pytest`
- Result: `144 passed, 1 skipped, 2 warnings in 3.97s` (1 skip = opt-in real-display
  fixture test; warnings are third-party DeprecationWarnings from GI and google-genai)
- Command: `. .venv/bin/activate && python -m pytest tests/integration/test_calibration_real_display.py -v`
- Result: `5 passed in 0.46s` (real X11 display: 1 physical monitor eDP-1 1366x768,
  root geometry 1366x768, pointer readback (443, 523))
- Command: `. .venv/bin/activate && python -m ruff check .`
- Result: `All checks passed!`
- Command: `. .venv/bin/activate && python -m mypy .`
- Result: `Success: no issues found in 52 source files`
- Date/commit: 2026-09-23 (no git; no commit hash)

## Known failures
*(structure per §12.1)*
- none

## Next concrete action
- Start Phase 2: add `core/frame_engine.py` (mss capture, 2 full frames + 8 thumbnails
  memory cap, adaptive idle/normal/active profiles, 3-error backend reinit -> `CAPTURE_FAILED`)
  and `core/change_detector.py` (480x270 grayscale, absdiff -> tiles 30x30 over 16x9,
  `NONE/TRIVIAL/ANIMATION/MEANINGFUL/MAJOR`, debounce), with unit tests and a first
  measured benchmark note (§76: record `target, actual, machine, session, backend, n`).

## Architectural decisions made so far
- Python 3.14.6 is installed; spec targets 3.12+, so this is satisfied.
- venv created with `--system-site-packages` so GI/AT-SPI resolve (§27).
- Capability probes perform **no physical input** (§28 rule 12).
- Capabilities not yet implemented (browser accessibility, visual grounding, sequence
  execution) report `UNAVAILABLE` with an honest reason rather than a guess.
- Batching layer features (`resolver_cache.enabled`, `sequence.enabled`, `capture_boost`,
  `speculative_perception`) all default **off** (§4 rule 31), pending Phase 9 gate +
  Phase 10.1 tests.
- **Coordinates are always space-tagged** (`Point`/`Rect` carry `CoordinateSpace`);
  comparisons across spaces raise rather than silently converting (§31).
- **Calibration is per-monitor**, is never assumed, and a failed/incomplete calibration
  leaves input **disarmed** (`CALIBRATION_FAILED`). `identity_geometry_map()` (calibration
  not required under X11 XTest) is deliberately distinct from a failed fit degrading to
  identity.
- **A resolver cache/speculative hint is never a confidence source** (§39) — confidence
  is computed only from real perception sources.
- **`run_sequence` steps may only describe targets** — coordinates, `element_id`,
  `lease_id`, `frame_id`, `state_version`, `generation` are rejected at parse time (§66.1).
- **`halt_on` may only narrow within the default safety set** — it can never add a halt
  that removes a safety stop (§66.1).

## Migration status
- Config `schema_version = 1`; no migrations registered yet (registry exists in
  `config/migration.py`).

## Open TODOs
- §84 Phase 2: frame engine + change detector (capture, change classes, debounce).
- §84 Phase 3: accessibility + browser accessibility.
- §84 Phase 4: state cache + event bus.
- §84 Phase 5: OCR. §84 Phase 6: resolver/leases/occlusion. Phase 7: mouse/keyboard.
  Phase 8: policy/executor. Phase 9: safety gate. Phase 10: Brain/tools/context.
  Phase 10.1: batching layer (disabled until Phase 9 passes). Phases 11–15.
- Full reproducibility: consider committing the current tree for recovery
  checkpoints (needs user confirmation, §11).

## Verification status snapshot
- Build: N/A (interpreted Python; no build step yet)
- Lint/typecheck: PASS (`ruff check .` clean; `mypy .` clean, 52 files)
- Unit tests: PASS
- Integration tests: PASS (fixture app via harness; real-display calibration 5/5; 1 opt-in skip)
- Safety tests: NOT_RUN (arrive with Phase 8/9)
- E2E tests: NOT_RUN
- Sequence/batching tests (§66.1, §75): SCHEMA-LEVEL PASS (validation only);
  RUNTIME NOT_RUN (batching layer is Phase 10.1)
- Benchmark: NOT_RUN

## Environment facts (verified this session)
- Host: Kali GNU/Linux Rolling, kernel 7.1.5+kali-amd64, XFCE, X11 (`DISPLAY=:0.0`)
- Python 3.14.6; venv at `.venv` created with `--system-site-packages`
- XTEST 2.2 confirmed live via python-xlib; AT-SPI bus present; XDG portal present
- Missing binaries (not required yet): `wmctrl`, `ydotool`
- `xxhash` and `numpy` import successfully (used by `schemas/elements.py` and
  `core/calibration.py` respectively)

## Session log (append-only, informational only — never load-bearing)
- 2026-09-23 — session started; root confirmed empty; Phase 0 scaffold + probes implemented and verified.
- 2026-09-23 — §74 fixture application + tests/harness built; Phase 0 marked COMPLETE (47 passed, 1 skipped).
- 2026-09-23 — bootstrap re-run: corrected the git claim; re-verified Phase 0; implemented
  and verified Phase 1 (schemas/geometry/screen-state/elements/leases/actions/events/sequences
  + calibration) — 139 passed, 1 skipped; ruff + mypy clean.
- 2026-09-23 — added real-display calibration integration test (5 tests, all passing on
  the live X11 display) — 144 passed, 1 skipped; ruff + mypy clean (52 files).
- 2026-09-23 — committed the Phase 0 + Phase 1 tree as recovery checkpoint `<commit>`.
- 2026-09-23 — Phase 2 (frame engine + change detector) requested but **not started**
  (no Phase 2 code exists yet); interrupted to do the two follow-up requests above.
