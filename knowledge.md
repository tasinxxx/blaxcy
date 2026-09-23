# BLAXCY — Project Knowledge

## What this is

Linux-native AI computer-control BODY. The authoritative build contract is the
BLAXCY Master Engineering Specification; this file is a quick orientation
index, not a source of truth.

## Canonical project root

`/home/tsn/blaxcy` — always verify with `pwd`. Never create a duplicate
project to solve context loss. (Note: `/home/tsn/AGENTS.md` describes a
*different* project, `/home/tsn/ai-agent/`; it is not authoritative for BLAXCY.)

## Continuation

Before doing anything else this session, read `CONTINUATION_STATE.md` at the
project root and reconcile it against git/the filesystem. See Part II of the
master specification for the full bootstrap procedure.

## Quickstart

- Setup: `python3 -m venv --system-site-packages .venv && . .venv/bin/activate && pip install -r requirements-dev.txt`
- Probe capabilities (no input injected): `python main.py probe`
- Session facts: `python main.py session`
- Resolved config: `python main.py config`
- Run: `python main.py` (currently defaults to `probe`; the Qt GUI arrives in Phase 12)
- Test: `pytest`
- Full gate: `make check`

## Architecture

- `core/` — perception (Eye): session detection, capability probes, and
  **calibration** (`calibration.py`, implemented); later capture, change detection,
  accessibility, OCR, target resolution, resolver cache, speculative perceiver,
  state cache
- `control/` — mouse, keyboard, clipboard, window management, executor, sequence
  runner, verifier, recovery, emergency stop, watchdog (Phase 7+)
- `policy/` — modes, action classes, guards (Phase 8)
- `ai/` — Brain adapter (Gemini), tool protocol, context manager (Phase 10)
- `gui/` — PySide6 interface (Phase 12)
- `schemas/` — typed geometry, screen state, elements, leases, actions, errors,
  events, sequences (all implemented as of Phase 1). Every coordinate is
  space-tagged (`CoordinateSpace`); `ToolEnvelope` is the one result shape (§66).
- `config/` — TOML settings, schema migration
- `security/` — keyring, redaction
- `tests/harness/` — launcher/client for fixture apps
- `tests/fixtures/fixture_app.py` — the §74 fixture application, launched as a real
  child process; drive it with `tests.harness.FixtureApp` (Qt offscreen by default,
  no window appears). It exposes every control §74 requires plus the 5-control
  workflow fixture used for `run_sequence` happy-path and halt-path tests.

## Conventions

- Python 3.12+, strict typing on `schemas/`, `core/`, `control/`, `policy/`.
- No fake tools, fake status, or fake verification.
- Every capability reports `AVAILABLE` / `DEGRADED` / `UNAVAILABLE`, never assumed.
- Capabilities are established by functional probes, not by the presence of an
  executable on `PATH`.
- Security invariants are enforced in code (`config/settings.py`) and cannot be
  configured into an unsafe state.
- Every performance optimization (batching, caching, speculation, concurrency)
  is a scheduling/hint layer only — it never removes a Policy / Resolve / Lease /
  Revalidate / Execute / Verify step. The batching layer ships disabled.
- Coordinates are always tagged with their space; cross-space comparison raises.
- Calibration is per-monitor and is never assumed: an incomplete/failed calibration
  leaves input disarmed (`CALIBRATION_FAILED`), and is distinct from the deliberate
  identity map used when calibration is not required (X11 XTest).
- A `run_sequence` step carries a target *description* only — coordinates,
  `element_id`, `lease_id`, `frame_id`, `state_version`, `generation` are rejected
  at parse time (§66.1). `halt_on` may only narrow within the default safety set.
- Password elements may not carry text; credential content is never stored, logged
  or uploaded (§42, §55).

## Verifying changes

After any code change, run:

- `.venv/bin/ruff check .`
- `.venv/bin/mypy .`
- `.venv/bin/pytest`

For anything touching Phase 9 safety code (policy/executor/verifier/recovery/
takeover/emergency_stop/watchdog): also run `pytest tests/safety/`.

For anything touching the batching layer (sequence_runner/resolver_cache/
speculative_perceiver): also run `pytest tests/safety/test_sequence_halts.py`
and confirm disabling the feature flag reproduces identical (only slower)
behavior.

## Source verification

For third-party APIs: inspect the installed version, inspect installed source
when required, consult official documentation, and implement only behavior
actually verified. Do not treat examples from old documentation as guaranteed
current APIs.
