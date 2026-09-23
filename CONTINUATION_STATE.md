# BLAXCY — Continuation State

> This file is an index, not a source of truth by itself. Before trusting
> anything below, reconcile it against git and the actual repository
> (Part II §8, step 4). If this file and the repository disagree, the
> repository wins and this file gets corrected.

## Project identity
- Name: BLAXCY
- Project root: `/home/tsn/blaxcy` (confirmed via `pwd` this session)
- Repository: **git present**, branch `master`. The whole tree is committed as
  `a56d74a` (see below); Phase 2 through Phase 6 work is currently **uncommitted**
  in the working tree, so future sessions can use commits as recovery
  checkpoints only up to `a56d74a` (§11).
- Last verified commit: `a56d74a` — "Establish the BLAXCY baseline: Phase 0 scaffold
  and typed Phase 1 contracts" (the first commit)

## Current phase
- Phase: Phase 7 — Mouse / keyboard input layer
- Phase status: **COMPLETE** (implemented + 50 new tests, all run this session)

## Completed milestones
- **Phase 0 — Repository / harness / probes: COMPLETE**, verified 2026-09-23 (prior session)
  and re-verified this session (`pytest`/`ruff`/`mypy` all re-run).
  Deliverables: repo scaffold (§26), typed schemas (§28/§57/§77), TOML config with
  enforced security invariants (§72), session detection (§29/30), functional
  capability probes (§28), CLI (`main.py probe|session|config`), §74 fixture
  application + harness (`tests/fixtures/fixture_app.py`, `tests/harness/fixture_client.py`).
- **Phase 1 — Schemas / calibration: COMPLETE**, verified 2026-09-23.
  Deliverables: `schemas/enums.py`, `schemas/geometry.py`, `core/calibration.py`,
  `schemas/screen_state.py`, `schemas/elements.py`, `schemas/leases.py`,
  `schemas/actions.py`, `schemas/events.py`, `schemas/sequences.py`, plus unit
  tests and the real-display calibration integration test.
- **Phase 2 — Frame engine / change detector: COMPLETE**, verified 2026-09-23.
  Deliverables: `core/frame_engine.py` (§33/§33.1), `core/change_detector.py`
  (§34), unit tests, a real-display integration test, and the first benchmark
  numbers in `docs/benchmark_report.md`.
- **Phase 3 — Accessibility / browser accessibility: COMPLETE**, verified 2026-09-23.
  Evidence: `pytest` -> `220 passed, 1 skipped`; `mypy .` -> `Success: no issues
  found in 62 source files`; `ruff check .` -> `All checks passed!`;
  `pytest tests/integration/test_accessibility_real_display.py -v` -> `4 passed`
  against the live X11/AT-SPI bus.
  Deliverables:
  - `core/accessibility.py` (§35/§37/§38) — the single owner of AT-SPI:
    * a dedicated `T-A11Y` thread owns all GI/GLib interaction; every cross-thread
      request is marshalled through one queue and reports `A11Y_TIMEOUT` rather
      than hanging;
    * bounded traversal (`max_depth=12`, `max_nodes=1200`, `deadline_ms=250`);
      `Atspi.Collection` is probed and used when present, with a bounded BFS as
      the verified fallback (the common case on this host);
    * `NodeBlacklist` quarantines a repeatedly-failing path for a cooldown;
    * an event-driven element cache (TTL `2 s`) that any observed AT-SPI event
      conservatively invalidates;
    * AT-SPI role → BLAXCY `UIRole` mapping; `PASSWORD_INPUT` elements are
      reported with `text=None`/`password=True` and credential content is never
      read; only a navigation field (address/URL/location bar) has its editable
      text read (§36/§42/§55);
    * geometry reported as DESKTOP-space `Rect`, with non-positive extents
      reported as "no geometry" rather than a bogus rectangle;
    * an evidence-carrying `capability()` (UNAVAILABLE / DEGRADED / AVAILABLE).
  - `core/browser_accessibility.py` (§36) — derives one `BrowserContext` per
    observed browser, with URL discovery in section-36 order (document
    attributes → address bar → window title → OCR, the last reported honestly as
    not-yet-implemented). It never relaunches or mutates a browser and never
    fabricates a URL.
  - `core/capability_probe.py` (updated) — `probe_accessibility` and
    `probe_browser_accessibility` are now real *functional* probes that share one
    `AccessibilityService` (one AT-SPI owner thread, §35), rather than a
    `gi`-import check and a hardcoded "not implemented until Phase 3" verdict.
  - Bug fixed this session: `NodeBlacklist.blocked_paths` iterated the live
    `_blocked_until` mapping while `is_blocked` removed expired entries, raising
    "dictionary changed size during iteration". It now iterates a snapshot; a
    regression test covers the expiry-during-iteration case.
  - Tests: `tests/unit/test_accessibility.py` (21), `tests/unit/test_browser_accessibility.py`
    (12), `tests/integration/test_accessibility_real_display.py` (4), and the
    updated `tests/unit/test_capability_probe.py`.
- **Phase 4 — State cache / event bus: COMPLETE**, verified 2026-09-23.
  Evidence: `pytest` -> `253 passed, 1 skipped`; `mypy .` -> `Success: no issues
  found in 66 source files`; `ruff check .` -> `All checks passed!`.
  Deliverables:
  - `core/event_bus.py` (§65) — the single typed event bus every consumer shares:
    * thread-safe subscribe/publish with per-type and wildcard subscriptions, and
      a cancellable `Subscription` handle (`subscribe`, `subscribe_many`, `cancel`);
    * dispatch never holds the lock, so a subscriber may publish re-entrantly (the
      emergency-stop path) without deadlocking, and a subscription cancelled by an
      earlier callback in the same dispatch receives nothing further;
    * a subscriber exception is recorded (`error_count` / `last_error`) and
      isolated — it never blocks other subscribers and is never silently swallowed;
    * bounded rolling history (`history`, `history_of`, `latest`) for the GUI log.
  - `core/state_cache.py` (§32/§65) — the locked, versioned current-state view:
    * holds current/previous `ScreenState`, the accepted `ScreenDelta`, live
      `ElementLease`s, the capability report, active task/action, verification,
      and the active `SequenceProgress` record (§65);
    * **rejects stale updates** by the §32 rules (`frame_id <= current` or
      `generation < current`), counted in `rejected_updates`, never mutating state;
    * is the **sole authority on `state_version`** — it restamps the accepted state
      and its delta, so a skipped frame shows as a version gap rather than being
      silently renumbered;
    * **fail-closed invalidation**: a generation change, a monitor-layout change, or
      a structural (`MEANINGFUL`/`MAJOR`) delta clears every live lease and emits one
      `LEASE_REJECTED` per lease; clearing an authorization can only prevent input,
      never authorise it (§32/§34/§44). Ordinary updates leave leases to expire on
      their own TTL;
    * emits `STATE_UPDATED` (always on acceptance), `SCREEN_CHANGED` (non-`NONE`
      delta), `LAYOUT_CHANGED`, `CAPABILITY_CHANGED`, `LEASE_REJECTED`; a rejected
      update emits nothing;
    * a `StateSnapshot` dataclass gives a consistent, complete read for the GUI.
  - Tests: `tests/unit/test_event_bus.py` (12), `tests/unit/test_state_cache.py` (21).
  - `core/change_detector.py` comment corrected: the frame id is only a
    *provisional* state version; the cache restamps it (comment-only change).
- **Phase 5 — OCR: COMPLETE**, verified 2026-09-23.
  Evidence: `pytest` -> `273 passed, 1 skipped`; `mypy .` -> `Success: no issues
  found in 68 source files`; `ruff check .` -> `All checks passed!`; real-backend
  functional test (`test_real_pytesseract_backend_reads_rendered_text`) ran and
  passed against Tesseract 5.5.0; cold OCR p50 116.63 ms / p95 162.30 ms vs the
  §76 target of ≤ 300 ms (n=200, recorded in `docs/benchmark_report.md`).
  Deliverables:
  - `core/ocr.py` (§39/§40/§42/§55) — OCR as a **fallback only**, never primary:
    * `OcrEngine.select_regions` enforces the §40 limits in one deterministic pass
      — aims to drop sub-`40x14` regions, regions whose area exceeds
      `max_area_ratio` (1.5%) of the desktop, and everything past `max_regions` (6),
      ordering largest-first then top-to-bottom/left-to-right so the same input
      always does the same work and reports the same counts;
    * the whole call is bounded by `deadline_ms` (300): once spent the engine stops
      and returns `truncated_by_deadline=True` with what it actually produced;
    * **password fields are never OCR'd** (§42/§55) — candidates overlapping a
      password element's bbox are dropped before the backend is invoked and the
      drop is counted (`password_regions` helper extracts the boxes);
    * results are cached by an xxhash of the **cropped pixels plus geometry**
      (bounded TTL LRU, `OcrCache`), so an unchanged region skips Tesseract;
    * confidence is always computed via `perception_confidence(OCR, ...)` — the
      0.75 base rate times geometry/freshness/source-consistency (word agreement),
      never an assumed value (§39);
    * `as_elements` emits `TEXT_FRAGMENT` elements with `clickable=False`: OCR
      proves text is *visible*, never that it is *clickable* (§38);
    * a real functional `capability()` (AVAILABLE/DEGRADED/UNAVAILABLE) and
      `diagnostics()` for logs/benchmarks;
    * partial failure returns real partial evidence with `failures` set; only a
      total failure raises `OCR_FAILED` (§80).
  - Tests: `tests/unit/test_ocr.py` (20) — region selection/limits/determinism,
    DESKTOP word mapping, confidence derivation, cache hit and TTL/LRU eviction,
    deadline truncation, all-fail `OCR_FAILED`, password exclusion, element
    construction, capability evidence, and one real-backend rendered-text test
    (skipped only if Tesseract is absent).
- **Phase 6 — Resolver / leases / occlusion: COMPLETE**, verified 2026-09-23.
  Evidence: `pytest` -> `299 passed, 1 skipped`; `mypy .` -> `Success: no issues
  found in 70 source files`; `ruff check .` -> `All checks passed!`;
  `pytest tests/unit/test_target_resolver.py` -> `26 passed`.
  Deliverables:
  - `core/target_resolver.py` (§43/§44/§46) — the deterministic targeting layer
    over the elements perception already observed; pure arithmetic, never
    injects input, never mutates the state it is given:
    * `TargetResolver.resolve(query, elements, occluders=...)` implements the
      §43 text cascade (exact → case-insensitive → normalized → accessible name
      → role → context → DOM path), the §43 weights (`text .40 / source .20 /
      role .15 / context .15 / geometry .10`), `act_threshold` 0.65 and up to
      `max_candidates` (5); neutral dimensions the query does not constrain
      score 1.0 so a role-only query is not penalised on text;
    * **absolute ambiguity rule** enforced independently of confidence: two
      visible candidates sharing a normalized label *and* role are `AMBIGUOUS`,
      and a top-two gap `<= ambiguity_gap` (0.08) is ambiguous too — `best` is
      `None` in both cases, never a silent pick;
    * `assess_occlusion(target, occluders, threshold=0.50)` (§46) — coverage is
      the **max** single-occluder ratio (never summed), cross-space occluders
      are ignored rather than converted (§31), an element with no geometry is
      reported `assessed=False` (not silently clickable), and perception's own
      `occluded` flag blocks outright;
    * `bind_lease(candidate, state, ...)` (§44) stamps a lease with the exact
      `frame_id`/`state_version`/`generation` of the observation it resolved
      against, so a lease can never outlive its perception;
    * the §43.1 fuzzy hook is present (`semantic_scorer`/`semantic_match_floor`)
      but **inert by default** (section 4 rule 31): with no scorer injected the
      resolver is exact/normalized-only, and a `semantic_match_floor` of 100
      keeps even an injected scorer disabled;
    * `ResolutionResult`/`OcclusionAssessment`/`stats()` carry the candidate
      list, scores, stage and reason the Brain needs (§43), and never element
      secrets (a password candidate serialises with `text: null`).
  - Tests: `tests/unit/test_target_resolver.py` (26) — every cascade stage,
    scoring arithmetic, `act_threshold`, `max_candidates`, role-only neutrality,
    window filtering, the absolute + gap ambiguity rules (including
    confidence-proof), all occlusion cases (over/at/below threshold, max-not-sum,
    declared flag, missing geometry, cross-space), resolution-attached occlusion,
    lease binding, determinism, the inert semantic hook, and `stats()`.
- **Phase 7 — Mouse / keyboard input layer: COMPLETE**, verified 2026-09-23.
  Evidence: `pytest` -> `349 passed, 1 skipped`; `mypy .` -> `Success: no issues
  found in 80 source files`; `ruff check .` -> `All checks passed!`; the new
  input suites -> `50 passed` (26 mouse + 20 keyboard + 8 backend unit tests, plus
  4 real-display integration tests that ran against the live X11/XTest 2.2
  display). Live capability report now reports `mouse AVAILABLE xtest
  {'xtest_version': '2.2'}` and `keyboard AVAILABLE xtest {'xtest_version':
  '2.2'}`.
  Deliverables:
  - `config/settings.py` + `config/default_settings.toml` (§48-52) — new `[input]`
    section: `mouse_settle_ms=40`, `post_click_settle_ms=120`,
    `pointer_readback_tolerance_px=4`, `max_pointer_corrections=1`,
    `ascii_direct_max=200`, `key_interval_ms=10`, `guard_focus=true`. These are
    behavioural knobs, not safety switches; disabling the focus guard removes an
    extra pre-typing check and opens no other path to unverified typing.
  - `control/backends/base.py` (§30/§48/§50) — the narrow input contract:
    `InputBackend` (probe, readback, move, button press/release, scroll,
    `resolve_key`, key press/release, flush, `release_all`, close),
    `PointerButton`, `BackendProbe`, `KeyResolution`. A backend is the *how* only:
    it performs no policy, lease or revalidation check (those are §13/§44/§45,
    Phase 8), and it tracks every key/button it holds so §52/§63 can undo it.
  - `control/backends/keys.py` (§50/§52) — keysym helpers: printable ASCII maps by
    code point (X11 Latin-1 keysyms), newline/tab map to named keysyms, friendly
    modifier/key aliases (`ctrl`->`Control_L`, `enter`->`Return`), and
    non-Latin-1/multi-character input returns `None` (never approximated). `Xlib`
    is imported lazily so the package stays probeable without it.
  - `control/backends/xtest.py` (§30/§48-52) — the preferred in-process XTEST
    backend: a functional `probe()` (XTEST version query, no input injected),
    absolute XTEST motion, button/wheel injection, keycode+Shift resolution from
    the live keyboard mapping (levels 0/1 only; an AltGr-only keysym resolves to
    `None` rather than being faked), lazy connection, thread-safe, and a
    `release_all()` that safely releases tracked keys/buttons (including from the
    emergency-stop thread). Bug fixed this session: the probe previously reported
    AVAILABLE with **no** version detail because python-xlib stores the parsed
    reply in the private `_data` dict (verified against the installed version);
    it now reads that defensively and reports `xtest_version`.
  - `control/backends/__init__.py` — `select_backend()` returns a backend only when
    its functional probe passed (§28 rule 12). Only XTEST is implemented; the
    xdotool path is explicitly **not** implemented (no dead stub), and the
    capability report says so honestly.
  - `control/mouse.py` (§31/§48/§49) — the ordered mouse sequence:
    `move_to` transforms through `GeometryMap.prepare_input_point` (FRAME/MONITOR/
    DESKTOP -> INPUT, clamped), injects, flushes, then reads back and corrects a
    bounded number of times (§48) — an off-target readback after corrections is a
    **failure**, so a click can never follow a known-bad move; `click`/`double_click`/
    `right_click`/`scroll` settle `mouse_settle_ms` before and `post_click_settle_ms`
    after; `drag` (§49) interpolates between press and release and guarantees the
    button is released on any unexpected failure. Coordinate injection is
    integers-in-bounds (a known `input_bounds` is kept strictly inside, avoiding a
    half-open-rectangle rounding overshoot).
  - `control/keyboard.py` (§50/§51/§52) — typing with an explicit decision:
    ASCII up to `ascii_direct_max` is typed directly (key press/release, Shift
    held only for shifted keysyms, `key_interval_ms` between characters); longer or
    non-ASCII text uses a pluggable `ClipboardPaster` + injected Ctrl+V, and when
    neither path is possible it raises `UNICODE_UNSUPPORTED` instead of typing an
    approximation; all characters are resolved **before** any is typed, so a
    mid-string failure never leaves half the text. `check_focus` is the §51 guard
    (fails closed: no state, absent target, non-text-entry target or unfocused
    target all block before a single key is injected); `press_key`/`hotkey` release
    every modifier in reverse order and `release_all` is the §52/§63 hygiene hook.
  - `core/capability_probe.py` (updated) — `probe_mouse`/`probe_keyboard` now share
    the backend's functional probe (one source of truth), and the xdotool fallback
    verdict no longer implies an implemented fallback.
  - Tests: `tests/unit/test_mouse.py` (16), `tests/unit/test_keyboard.py` (20),
    `tests/unit/test_input_backends.py` (8), `tests/harness/fake_input_backend.py`
    (recording fake), and `tests/integration/test_input_real_display.py` (4).
- **Real-display resolver integration coverage: ADDED**, verified 2026-09-23.
  `tests/integration/test_target_resolver_real_display.py` (7 passed, 1 honest
  skip) feeds the **live** AT-SPI observations into the Phase 6 `TargetResolver`
  and verifies §43/§44/§46 against real desktop geometry: a uniquely labelled
  live element resolves to itself (deterministically, with a real bbox), an
  absent label is an honest `NOT_FOUND`, a **real** duplicated
  `(normalized label, role)` group (e.g. 11 `open launcher menu`/TOGGLE controls,
  2 of them visible+actionable on this host) is `AMBIGUOUS` with `best=None` and
  stays ambiguous under maximum confidence, occlusion is max-not-sum over real
  boxes, a covered live target resolves yet is not actionable, and `bind_lease`
  stamps a live resolution with the observation's frame/version/generation. No
  input is injected. The declared-occlusion test skips honestly because no
  element on this host is currently reported occluded.

## Current task
- Task: Phase 7 is complete and verified. Next: Phase 8 — policy / executor.
- Subtask: none

## Interruption recovery
- Last session state: normal (clean bootstrap; the tree matched the recorded Phase 6 state)
- Last operation started: bootstrap re-verification, then Phase 7 (mouse/keyboard input layer) implementation
- Last operation status: completed — full gate re-run, this file updated
- Recovery action: none needed; Phase 6 was re-verified green (299 passed, 1 skipped) before Phase 7 began.

## Last known blocker
- none

## Files being actively modified
- (none mid-change)

## Last test results (verbatim, not paraphrased as "passed")
- Command: `. .venv/bin/activate && python -m pytest -o addopts="" -q`
- Result: `356 passed, 2 skipped, 4 warnings in 9.12s` (skips = the opt-in
  fixture test and the declared-live-occlusion condition; Phase 7 added 50 tests,
  and the live resolver integration suite added 7 more)
- Command: `. .venv/bin/activate && python -m pytest tests/integration/test_target_resolver_real_display.py -o addopts="" -q`
- Result: `7 passed, 1 skipped in 2.46s` (ran against the live AT-SPI tree, no input injected)
- Command: `. .venv/bin/activate && python -m pytest tests/unit/test_mouse.py tests/unit/test_keyboard.py tests/unit/test_input_backends.py tests/integration/test_input_real_display.py -o addopts="" -q`
- Result: `50 passed in 0.44s` (the 4 real-display input tests ran, not skipped)
- Command: `. .venv/bin/activate && python -m ruff check .`
- Result: `All checks passed!`
- Command: `. .venv/bin/activate && python -m mypy .`
- Result: `Success: no issues found in 81 source files`
- Note: this pytest (9.1.1) prints no final summary line under the project's
  default `addopts = "-q"`; the honest count above is with `-o addopts=""`.
- Date/commit: 2026-09-23 (working tree based on `a56d74a`; Phase 2-7 changes uncommitted)

## Known failures
*(structure per §12.1)*
- none

## Next concrete action
- Start Phase 8 (§84: policy / executor). Build `policy/` (modes, action classes,
  permissions, guards, terminal guard) and `control/executor.py`, which is the
  layer that finally owns the §44 lease issue + the §45 revalidation before any
  input, and is the only caller allowed to drive `control/mouse.py` and
  `control/keyboard.py`. Add `control/verifier.py` (§60) and wire the §66 tool
  envelope. Do **not** enable the batching layer (§66.1) before the Phase 9
  safety gate. Keep the invariant: no physical input without policy -> resolve ->
  lease -> revalidate -> execute -> verify.

## Architectural decisions made so far
- Python 3.14.6 is installed; spec targets 3.12+, so this is satisfied.
- venv created with `--system-site-packages` so GI/AT-SPI resolve (§27).
- Capability probes perform **no physical input** (§28 rule 12).
- Capabilities not yet implemented (visual grounding, sequence execution) report
  `UNAVAILABLE` with an honest reason rather than a guess.
- **AT-SPI has exactly one owner thread** (`T-A11Y`); the browser layer consumes the
  accessibility service's observations rather than calling AT-SPI itself, so there is
  never a second accessibility owner (§35).
- **Credential content is never read by perception**: password elements carry no text,
  and only navigation fields have editable text read (§42/§55).
- Batching layer features (`resolver_cache.enabled`, `sequence.enabled`, `capture_boost`,
  `speculative_perception`) all default **off** (§4 rule 31), pending Phase 9 gate +
  Phase 10.1 tests.
- **The state cache is the sole authority on an accepted state's `state_version`**
  (§32/§65): perception builds a `ScreenState` and the cache restamps it, so
  `state_version` advances per accepted update independently of `frame_id`.
- **State invalidation is fail-closed** (§32/§34/§44): generation, layout or
  structural change clears leases; removal of an authorization can never enable input.
- **One event bus, no second delivery path** (§65/§66.1): the sequence lifecycle
  events, state events and safety events all use `core/event_bus.py`; subscriber
  failures are recorded, never silently swallowed.
- **Coordinates are always space-tagged** (`Point`/`Rect` carry `CoordinateSpace`);
  comparisons across spaces raise rather than silently converting (§31).
- **Calibration is per-monitor**, is never assumed, and a failed/incomplete calibration
  leaves input **disarmed** (`CALIBRATION_FAILED`).
- **A resolver cache/speculative hint is never a confidence source** (§39).
- **Target resolution is deterministic, pure and confidence-independent on
  ambiguity** (§43): the resolver scores observed elements and never injects
  input or mutates state; two visible candidates sharing a normalized label and
  role are `AMBIGUOUS` regardless of confidence, and `best` is `None` rather than
  a silent pick.
- **Occlusion is max, not sum, and never click-through** (§46): > 0.50 coverage
  blocks; cross-space occluders are ignored (never converted, §31); a target with
  no geometry is `assessed=False` (not silently actionable).
- **A lease is bound to one observation** (§44): `bind_lease` stamps the lease
  with the state's exact frame/version/generation; no path reissues or extends a
  lease.
- **The §43.1 fuzzy/semantic hook is inert by default** (§4 rule 31): resolution
  is exact/normalized-only unless a scorer is explicitly injected, and
  `semantic_match_floor=100` disables even an injected one.
- **Input backends are the "how" only** (§13/§30/§48): `control/backends/*`
  performs no policy, lease or revalidation check and never decides to act; a
  backend is selected only after a passing functional probe, and only XTEST is
  implemented (no xdotool dead stub).
- **A pointer move that is provably off-target is a failure** (§48): the readback/
  correction loop is bounded, and after corrections an off-target readback makes
  `move_to` fail, so a click can never follow a known-bad position. Without
  readback support the move is honestly `verified=False`.
- **Text that cannot be produced is never approximated** (§50): direct XTEST for
  ASCII within `ascii_direct_max`, clipboard paste for long/non-ASCII only when a
  `ClipboardPaster` exists, otherwise `UNICODE_UNSUPPORTED`. All characters are
  resolved before any is typed.
- **The §51 focus guard fails closed**: with no current state, an absent target,
  a non-text-entry target, or an unfocused target, no key is injected
  (`FOCUS_MISMATCH`).
- **`run_sequence` steps may only describe targets** — coordinates, `element_id`,
  `lease_id`, `frame_id`, `state_version`, `generation` are rejected at parse time (§66.1).
- **`halt_on` may only narrow within the default safety set** — it can never add a halt
  that removes a safety stop (§66.1).
- **OCR is a fallback source with a hard privacy boundary** (§40/§55): only
  caller-supplied regions are OCR'd (never the desktop), the call is bounded by
  `deadline_ms`, results are cached by crop-pixel hash, and any region overlapping
  a password field is dropped before the backend runs.
- **OCR text is never assumed clickable** (§38): fragments are `TEXT_FRAGMENT`
  elements with `clickable=False`; confidence is always `perception_confidence(OCR)`
  (0.75 base), never an assumed value.

## Migration status
- Config `schema_version = 1`; no migrations registered yet (registry exists in
  `config/migration.py`).

## Open TODOs
- §84 Phase 8: policy/executor (owns §44 lease issue, §45 revalidation, and the
  wire-up of `core/target_resolver.py` + `bind_lease` + the Phase 7 input layer).
  Phase 9: safety gate. Phase 10: Brain/tools/context. Phase 10.1: batching layer
  (disabled until Phase 9 passes; `resolver_cache.py`/`speculative_perceiver.py`/
  `sequence_runner.py` still unbuilt). Phases 11–15.
- §50 clipboard-assisted typing needs a real `control/clipboard.py`
  `ClipboardPaster`; until then the controller reports `UNICODE_UNSUPPORTED`
  honestly instead of pasting.
- §43 ambiguity **scope** (finding from the live resolver test, 2026-09-23): the
  duplicate `label + role` rule currently counts *every* scored candidate, so a
  **role-hinted** query on a desktop with duplicated role-matching controls
  (this host has 11 `open launcher menu`/TOGGLE elements) returns `AMBIGUOUS`
  even when the text match is decisive and unique. It fails closed, so it is safe,
  but it is over-broad. Revisit whether the rule should consider only candidates
  competitive with the top (or that matched the query's own text) — needs a human
  decision, since narrowing it changes a §43 safety rule.
- Full reproducibility: consider committing the current tree (Phases 2–3) for recovery
  checkpoints (needs user confirmation, §11).

## Verification status snapshot
- Build: N/A (interpreted Python; no build step yet)
- Lint/typecheck: PASS (`ruff check .` clean; `mypy .` clean, 80 files)
- Unit tests: PASS (incl. accessibility 21 + browser accessibility 12; event bus 12 +
  state cache 21; OCR 20 incl. one real-backend functional test; target resolver 26;
  mouse 16 + keyboard 20 + input backends 8)
- Integration tests: PASS (real-display calibration 5/5; frame engine 4/4;
  accessibility 4/4; input/XTest 4/4; resolver-over-live-AT-SPI 7/7 with 1 honest
  conditional skip; fixture app via harness; 1 opt-in skip)
- Safety tests: NOT_RUN (arrive with Phase 8/9)
- E2E tests: NOT_RUN
- Sequence/batching tests (§66.1, §75): SCHEMA-LEVEL PASS (validation only);
  RUNTIME NOT_RUN (batching layer is Phase 10.1)
- Benchmark: PARTIAL (Phase 2 capture + change detection and Phase 5 OCR measured
  and recorded in `docs/benchmark_report.md`; accessibility probe latency measured
  live (251.2 ms first traversal) but not yet an n>=200 benchmark; all later-phase
  benchmarks NOT_RUN)

## Environment facts (verified this session)
- Host: Kali GNU/Linux Rolling, kernel 7.1.5+kali-amd64, XFCE, X11 (`DISPLAY=:0.0`)
- Python 3.14.6; venv at `.venv` created with `--system-site-packages`
- XTEST 2.2 confirmed live via python-xlib; AT-SPI bus present and **perception
  functionally verified this session** (live traversal produced AT-SPI elements);
  XDG portal present
- Missing binaries (not required yet): `wmctrl`, `ydotool`
- OCR stack verified this session: Tesseract 5.5.0 (`/usr/bin/tesseract`),
  `pytesseract` 0.3.13 (`image_to_data`/`Output.DICT` confirmed), Pillow 12.3.0
- Versions verified this session: `rapidfuzz`, `xxhash`, `numpy`, `mss` 10.2.0,
  `opencv-python-headless` 5.0.0 all import successfully
- `Atspi.Action.get_action_name` emits a DeprecationWarning on this host (the call
  still works); noted, not yet actioned
- XTEST 2.2 present; the input layer's functional probe reports
  `xtest_version: 2.2` (python-xlib stores the parsed version reply in the private
  `_data` dict — verified this session)

## Session log (append-only, informational only — never load-bearing)
- 2026-09-23 — session started; root confirmed empty; Phase 0 scaffold + probes implemented and verified.
- 2026-09-23 — §74 fixture application + tests/harness built; Phase 0 marked COMPLETE (47 passed, 1 skipped).
- 2026-09-23 — bootstrap re-run: corrected the git claim; re-verified Phase 0; implemented
  and verified Phase 1 — 139 passed, 1 skipped; ruff + mypy clean.
- 2026-09-23 — added real-display calibration integration test (5 tests) — 144 passed,
  1 skipped; ruff + mypy clean (52 files).
- 2026-09-23 — committed the Phase 0 + Phase 1 tree as recovery checkpoint `a56d74a`.
- 2026-09-23 — Phase 2 (frame engine + change detector) requested but **not started**;
  interrupted for two follow-up requests.
- 2026-09-23 — bootstrap re-run: re-verified Phase 0/1; implemented and verified Phase 2
  — 180 passed, 1 skipped; ruff + mypy clean (57 files).
- 2026-09-23 — Phase 3 started (accessibility + browser accessibility + functional probes)
  but the session ended before tests were written or this file was updated.
- 2026-09-23 — bootstrap re-run: detected the interrupted Phase 3; fixed the stale
  capability test, added 37 accessibility/browser tests + updated the capability test,
  fixed a real `NodeBlacklist.blocked_paths` iteration bug, and verified the whole gate
  — 220 passed, 1 skipped; ruff + mypy clean (62 files); live AT-SPI probe AVAILABLE.
- 2026-09-23 — bootstrap re-run (clean, no interruption); re-verified the Phase 3 gate,
  then implemented and verified Phase 4 (state cache + event bus) — 253 passed,
  1 skipped; ruff + mypy clean (66 files).
- 2026-09-23 — bootstrap re-run: re-verified Phase 4 green, then implemented and
  verified Phase 5 (OCR, fallback-only) — 273 passed, 1 skipped (real-backend OCR
  test ran); ruff + mypy clean (68 files); OCR benchmark recorded in
  `docs/benchmark_report.md` (cold p50 116.63 ms / p95 162.30 ms, target ≤ 300 ms).
- 2026-09-23 — bootstrap re-run (clean; re-verified Phase 5 at 273 passed, 1
  skipped; ruff + mypy clean), then implemented and verified Phase 6
  (resolver/leases/occlusion) — 299 passed, 1 skipped; ruff + mypy clean (70
  files); 26 new resolver tests.
- 2026-09-23 — bootstrap re-run (clean; re-verified Phase 6 at 299 passed, 1
  skipped; ruff + mypy clean, 70 files), then implemented and verified Phase 7
  (mouse/keyboard input layer: XTEST backend, §48 sequence, §49 drag, §50
  keyboard, §51 focus guard, §52 hygiene) — 349 passed, 1 skipped; ruff + mypy
  clean (80 files); 50 new input tests; fixed the XTEST probe to report the real
  version detail.
- 2026-09-23 — added `tests/integration/test_target_resolver_real_display.py`:
  the live AT-SPI tree driven through the Phase 6 resolver, verifying §43
  ambiguity (on a real duplicated control) and §46 occlusion against real
  desktop geometry — 7 passed, 1 conditional skip; full gate 356 passed,
  2 skipped; ruff + mypy clean (81 files).
