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

- `core/` — perception (Eye) and shared state: session detection, capability
  probes, **calibration** (`calibration.py`), **capture** (`frame_engine.py`),
  **change detection** (`change_detector.py`), **accessibility**
  (`accessibility.py`, the single AT-SPI owner), **browser accessibility**
  (`browser_accessibility.py`), **OCR** (`ocr.py`, fallback-only), **target
  resolution / occlusion / lease binding** (`target_resolver.py`), the **event
  bus** (`event_bus.py`) and the **state cache** (`state_cache.py`) — all
  implemented; the resolver cache and speculative perceiver arrive with the
  Phase 10.1 batching layer (and ship disabled)
- `control/` — **input layer implemented (Phase 7)**: `backends/` (`base.py` the
  narrow backend contract, `keys.py` keysym helpers, `xtest.py` the preferred
  in-process XTEST backend, selected only after a functional probe), `mouse.py`
  (§48 sequence + §49 drag) and `keyboard.py` (§50 typing + §51 focus guard + §52
  modifier hygiene). Still to come: clipboard, window management, executor,
  sequence runner, verifier, recovery, emergency stop, watchdog (Phase 8+)
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
- `tests/integration/` — real-display tests that skip honestly without a display:
  calibration, frame engine, accessibility, input/XTest, and
  `test_target_resolver_real_display.py`, which runs the **live** AT-SPI tree
  through the Phase 6 resolver to check §43 ambiguity and §46 occlusion against
  real desktop geometry.

## Conventions

- Python 3.12+, strict typing on `schemas/`, `core/`, `control/`, `policy/`.
- No fake tools, fake status, or fake verification.
- Every capability reports `AVAILABLE` / `DEGRADED` / `UNAVAILABLE`, never assumed.
- Capabilities are established by functional probes, not by the presence of an
  executable on `PATH`.
- AT-SPI has exactly one owner thread (`T-A11Y`); the browser layer consumes the
  accessibility service's observations rather than calling AT-SPI itself, so
  there is never a second accessibility owner.
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
  or uploaded (§42, §55). OCR additionally never runs over a password element's
  region (`core/ocr.py`), and OCR text is never assumed clickable (§38).
- The state cache (`core/state_cache.py`) is the sole authority on an accepted
  state's `state_version`; it rejects stale updates (§32) and clears leases
  fail-closed on a generation, layout or structural change. All events go through
  the one bus (`core/event_bus.py`) — there is never a second delivery path.
- Target resolution (`core/target_resolver.py`) is pure and deterministic: it
  scores observed elements and never injects input. Two visible candidates with
  the same normalized label and role are `AMBIGUOUS` regardless of confidence
  (§43). Occlusion is the **max** single-occluder coverage, `> 0.50` blocks, and
  cross-space occluders are ignored, never converted (§31/§46). `bind_lease`
  stamps a lease with the exact frame/version/generation it resolved against
  (§44). The §43.1 fuzzy/semantic hook is inert unless a scorer is injected.
- Input backends (`control/backends/`) are the *how* only: they perform no
  policy, lease or revalidation check and never decide to act (§13/§30). A
  backend is selected only after a **functional** probe (XTEST version query);
  only XTEST is implemented, and the xdotool path is explicitly not implemented
  (no dead stub). Every held key/button is tracked so `release_all` can undo it
  (§52/§63).
- §43 ambiguity is deliberately conservative: the duplicate `label + role` rule
  counts every scored candidate, so a **role-hinted** query on a desktop with
  duplicated role-matching controls reports `AMBIGUOUS` even when a text match is
  decisive. It fails closed (safe), but is over-broad; narrowing it is a §43
  safety-rule change that needs an explicit decision.
- The §48 mouse sequence is ordered and fail-closed: transform+clamp → inject →
  flush → bounded readback/correction. An off-target readback after corrections
  makes the move **fail**, so a click can never follow a known-bad position;
  without readback support the move is honestly `verified=False`. Typing resolves
  every character before injecting any, so a mid-string failure never leaves half
  the text, and text the keyboard cannot produce is either clipboard-pasted or
  refused with `UNICODE_UNSUPPORTED` — never approximated.

## Benchmarks

Measured numbers live in `docs/benchmark_report.md`, always in the section 76
form `target, actual, machine, desktop/session, backend, sample count`. Numbers
are only valid for the environment recorded beside them; a target stays a target
until re-measured (section 4 rule 23).

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
