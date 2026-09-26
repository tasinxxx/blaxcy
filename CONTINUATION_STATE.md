# BLAXCY — Continuation State

> This file is an index, not a source of truth by itself. Before trusting
> anything below, reconcile it against git and the actual repository
> (Part II §8, step 4). If this file and the repository disagree, the
> repository wins and this file gets corrected.

## Project identity
- Name: BLAXCY
- Project root: `/home/tsn/blaxcy` (confirmed via `pwd` this session)
- Repository: **git present**, branch `master`. Five commits now exist, so recovery
  checkpoints reach past the Phase 0/1 baseline (§11):
  - `a56d74a` — "Establish the BLAXCY baseline: Phase 0 scaffold and typed Phase 1
    contracts" (the first commit)
  - `947b001` — "Checkpoint Phases 2-7: perception, state, resolver and input
    layers" (38 files, +10707/-226).
  - `30f0fa3` — "Checkpoint Phases 8-14: safety gate, Brain loop, batching, GUI and
    installer" (131 files, +36183/-263) — the whole Phase 8-14 tree, committed on
    operator instruction.
  - `7950798` — "Narrow the §43 ambiguity rule and measure the §76 verifiable happy
    path".
  - `5e06bc0` — "Report the §76 perception cycle as warm and cold, not as one cached
    number".
  - `686a022` — "Complete §66 tool coverage and add the §70 logging path" — the
    `activate_element` tool and the structured-logging unit, committed on operator
    instruction.
  - `0b86d56` — "Subscribe the AT-SPI state changes that gate input decisions" — the
    `s2:FOCUS_MISMATCH` root-cause fix, committed on operator instruction.
  - `372879a` — "Verify typed text and result selection from live state, and
    re-measure" — 18 files, +754/−74.
  - `181ef84` — "Re-check a would-be section 51 focus refusal against live state" —
    5 files, +280/−72.
  - `ab3d540` — "Add the Phase 15 soak run and fix the section 43.1 cache it
    exposed" — 7 files, +777/−13; new `bench/soak.py`, `tests/unit/test_soak.py`.
  - `8b3307e` — "Retry fixture startup races, and document the capability matrix" —
    6 files, +433/−21; new `docs/capability_matrix.md`,
    `tests/unit/test_fixture_client.py`.
- Last verified commit: `8b3307e` (HEAD). **Index drift corrected 2026-09-26**: this
  section had stopped at `0b86d56` and described the then-uncommitted 18-file unit
  as the working tree; those four commits (`372879a`, `181ef84`, `ab3d540`,
  `8b3307e`) now exist and the tree they described is committed. The repository wins
  (§8 step 4).
- Working tree: **dirty — this session's uncommitted unit**: the stronger §60
  postcondition for clicks (`Postcondition.ELEMENT_STATE`), the XDG RemoteDesktop
  portal input backend, and the soak's per-step latency breakdown (see "Current
  task"). Files: `control/verifier.py`, `control/executor.py`,
  `control/backends/portal.py` (new), `control/backends/__init__.py`,
  `core/capability_probe.py`, `bench/soak.py`,
  `tests/unit/test_{verifier,executor,soak,portal_backend,capability_probe,input_backends}.py`,
  `tests/integration/test_workflow_controls_real_display.py`, `docs/limitations.md`,
  `docs/capability_matrix.md`, `docs/benchmark_report.md`, and this file.
  Re-verified green (**1215 passed, 6 skipped**; ruff + mypy clean, **184 files**)
  but not yet committed (committing needs explicit user confirmation, §11).
- **Index drift corrected 2026-09-25 (this session).** This file was last written by
  the session that became commit `5e06bc0` (the warm/cold perceive-cycle work),
  while a following session built the `workflow-verifiable` workload, the two AT-SPI
  text fixes and the resolver stage split — and then ended without updating this
  index or running the benchmark. The repository had therefore moved past this file
  when this session bootstrapped it. Everything below now records the reconciled
  state; the drift is called out where it matters rather than silently absorbed.
  The earlier drift corrections are kept in the session log (append-only).
- **Index drift corrected again 2026-09-26 (bootstrap session).** This file still
  recorded `5e06bc0` as HEAD and listed only five commits, but two of the units it
  described as uncommitted had in fact been committed as `686a022` and `0b86d56`.
  The repository wins (§8 step 4): the commit list and HEAD above are corrected, and
  the working tree is now accurately described as the 18-file three-halt-causes fix
  unit. The gate was re-run this session, not trusted (**1167 passed, 6 skipped**;
  ruff + mypy clean, 179 files), matching the "Current task" claim exactly.

## Current phase
- Phase: **Phase 15 — Full matrix / soak / documentation** (started 2026-09-26).
  Phases 0-14 are complete (Phase 12-14 are recorded under "Previous phases").
  Phase 15 now has a **soak runner** (`bench/soak.py`; 30-iteration and
  150-iteration runs, the latter **149/150**, with flat latency and flat
  steady-state memory) and the **environment/capability matrix**
  (`docs/capability_matrix.md`). **Still open for Phase 15:** verification on a
  Wayland/XWayland session, which this host cannot provide (no Wayland compositor).
- **The soak surfaced and closed a real resolver-cache defect (2026-09-26).** The
  §43.1 cache could never hit on a live desktop: every perceived element carries
  `owner_window_id = None` (measured 146/146), so hints were keyed under `None`
  while lookups filtered on the real `active_window_id` (`hits 0 / misses 150`).
  It failed safe, but was inert. `TargetResolver._record` now passes the
  observation's active window as the key fallback; re-measured hit rate **0.875**.
- Cross-cutting gaps closed this session: **§70 structured JSON logging now
exists** (`core/logging_setup.py` + `security/redaction.py`, with the root
redaction filter and rotation from `[logging]`), and the **three missing §26 docs
are written** (`docs/architecture.md`, `docs/security.md`, `docs/limitations.md`).
The `[logging]` config section is no longer a dead stub.
- Phase status: **COMPLETE — every §76 row is now measured and met.** The realistic
  §74 workflow (`workflow-verifiable`, search icon → type → submit → result → play)
  was re-measured **2026-09-26**, in two steps: after the three earlier halt causes
  were fixed it reached **5/20** (halt set narrowed to `s2:FOCUS_MISMATCH` ×15); then
  after the §51 focus guard was made to re-check a would-be refusal against a fresh
  live perception, it reached **20/20 completed, p50 1342.94 ms / p95 1787.70 ms
  (≤ 4000 ms target met)**. The standalone click and keyboard rows are **30/30
  VERIFIED** each. The synthetic `verifiable` workload also still meets the target
  (**20/20**, p50 2505.23 ms). Both workloads are recorded in the §76 form in
  `docs/benchmark_report.md`.
- **Closed 2026-09-26: the workflow happy-path gap.** The §51 focus-timing residual
  was the last open §76 item; it was fixed by re-checking the guard against a fresh
  live observation before refusing (see "Current task"). No §60 threshold or §51
  rule changed — a genuinely unfocused target is still refused by test.
- **One environment item is still open, and it is a human action, not code**: this
  host's PySide6 6.10.3 (apt) ships QtCore/QtGui/QtWidgets but **not QtTest**, so
  the installed `pytest-qt` plugin aborts *every* pytest run at configure time. The
  operator chose to install `python3-pyside6.qttest`, but `sudo` requires a password
  this session does not have, so the documented bare `pytest` command is still
  blocked; the suite is green with `-p no:pytest-qt`. **The GUI tests deliberately
  do not depend on `pytest-qt`** (they build their own offscreen `QApplication`),
  and the Phase 13 tests depend on nothing beyond the standard library and the
  running interpreter, so nothing delivered is waiting on it — only the
  *documented* bare `pytest` command is. See "Known failures".

## Previous phases (complete)
- Phase 13 — Installation (§73 + §85's install criteria): the installer
  (`installer/installer.py`, `installer/__main__.py`, `installer/__init__.py`),
  the three entry scripts (`install.sh`, `update.sh`, `uninstall.sh`), the desktop
  assets (`desktop/blaxcy.desktop`, `desktop/blaxcy.png`, `desktop/make_icon.py`),
  and single-instance control (`core/single_instance.py`, §72/§85). `main.py` gained
  the instance lock and a `--lock-path` flag. **COMPLETE and VERIFIED** (1069
  passed, 2 skipped at the time; ruff + mypy clean, 168 files; 39 new tests + a real
  end-to-end install of a throwaway prefix). BLAXCY is a menu application:
  `./install.sh` puts the entry in `~/.local/share/applications`, the launcher in
  `~/.local/bin`, and a per-file sha256 manifest in `~/.local/share/blaxcy/`.
  Documentation: `docs/installation.md`. Two deliberate boundaries: the installer is
  **standard library only** (it runs before the virtualenv it creates exists), and
  its only writes are the file list, the launcher, the icon and the entry — with
  every overwrite backed up so a failed run rolls back and an uninstall can hand the
  user's own files back.
- Phase 12 — GUI (§71) plus the composition root it is a view over: the
  **application composition root** (`core/application.py`), the **perception
  orchestrator** (`core/perception.py`), and the seven `gui/` modules
  (`app.py`, `main_window.py`, `status_panel.py` with the §66.1
  sequence-progress indicator, `capability_panel.py`, `action_log.py`,
  `confirmation.py` §56 dialog + thread bridge, `emergency_stop_ui.py` §63 stop +
  explicit re-arm): **COMPLETE and VERIFIED** (1030 passed, 2 skipped at the time;
  ruff + mypy clean, 160 files; 52 new tests, including an integration test over the
  **real** assembled Body and a live `main.py gui --offscreen` event-loop run). The
  GUI tests build their own offscreen `QApplication` rather than using `pytest-qt`,
  which is why the missing PySide6 `QtTest` module never blocked them.
- Phase 11 — Visual grounding (`core/visual_grounder.py` §38/§39/§41/§42,
  `policy.guards.check_visual_fallback`, the `[visual]` settings section, an honest
  `probe_visual_grounding`): **COMPLETE** (54 tests; 938 passed, 2 skipped at the
  time; ruff + mypy clean, 141 files). Its recorded limitation — a real, tested
  perception source with no caller — is **closed** by this session's perception
  orchestrator and composition root (see the Phase 12 entry below).
- Phase 10.1 — Batching & Throughput Layer (`run_sequence` /
  `control/sequence_runner.py`, resolver cache §43.1, speculative perception
  §33.2, capture boost §33.1, concurrent read-only dispatch §32.1, round-trip
  economy §68.1), built on the completed Phase 10 (`BrainAdapter`, Gemini adapter,
  manual tool loop, exact tool schemas, uniform envelope, `NOT_EXECUTED`
  semantics, context manager): **COMPLETE and enabled by default** (884 passed,
  2 skipped at the time; the §66.1/§75 batching test list is green and §84's
  condition was met). Every flag remains a switch; disabling one removes
  capability, never a check.

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

- **Phase 8 — Policy / executor: COMPLETE**, verified 2026-09-23.
  Evidence: `pytest` -> `541 passed, 2 skipped`; `mypy .` -> `Success: no issues
  found in 101 source files`; `ruff check .` -> `All checks passed!`;
  `pytest tests/safety` -> `24 passed`; the live window-manager integration
  suite -> `4 passed` against the real X11/EWMH session.
  Deliverables:
  - `config/settings.py` + `config/default_settings.toml` (§42/§58) — a new
    `[safety]` surface: `blocked_applications` (the §58 deny list — no input, no
    visual upload, no autonomous override) and `protected_applications` (the §42
    privacy list — never uploaded to a visual model). Both are case-folded
    substring matches over app name / window title / window class.
  - `policy/action_classes.py` (§32.1/§57) — the derived classification: the
    `run_sequence` gate class (most restrictive declared step, §57), whether a
    tool injects physical input, and `requires_exclusive_execution`
    (MUTATING/DESTRUCTIVE, or NAVIGATIONAL that injects) which is exactly the
    §32.1 single-writer-lock rule.
  - `policy/modes.py` (§56) — `ModeController` owning the mode and the
    AUTONOMOUS monotonic deadline. Reading `.mode` *latches* an expired
    AUTONOMOUS session back to OBSERVE and emits `MODE_CHANGED`, so the ceiling
    holds even if nothing polls a timer, and a new controller (a restart) always
    starts from the configured mode rather than AUTONOMOUS.
  - `policy/guards.py` (§28/§31/§42/§55/§58) — fail-closed guards for blocked
    applications, protected applications, credential context (typing permitted
    and marked `sensitive`; *reading* refused), calibration (disarmed →
    `CALIBRATION_FAILED`) and capability (only `AVAILABLE` passes by default).
  - `policy/terminal_guard.py` (§54) — the typing/submission separation.
    `Return`, `KP_Enter`, `ctrl+m`/`ctrl+j` and Return-bearing hotkeys all count
    as submissions; every submission requires confirmation, destructive ones in
    **every** mode including AUTONOMOUS; the destructive classifier is
    deliberately over-inclusive (a false positive costs a confirmation).
  - `policy/permissions.py` (§13/§56/§57/§66.1) — the single `PermissionEngine`
    answering "may this proceed, and does a human confirm first?" in a fixed
    fail-closed order: blocked app → terminal → credential → calibration →
    capability → mode/class → confirmation. `run_sequence` takes an explicit
    gate class; destructive actions always require confirmation, and that is not
    configurable.
  - `control/executor.py` (§44/§45/§47/§59/§60) — the §59 state machine and the
    **only** caller of `control/mouse.py` + `control/keyboard.py`. It issues a
    fresh lease from the exact resolution observation, then runs
    `revalidate_target` (the full §45 checklist: lease live, generation current,
    state fresh, target present, identity consistent, visible/enabled, geometry
    on-screen, not occluded, right window) before injecting, with the abort hook
    checked immediately before injection. Read-only tools are refused here (§32.1
    read path), and `activate_element` returns an honest `BACKEND_UNAVAILABLE`
    because AT-SPI action invocation is not implemented.
  - `control/verifier.py` (§60) — `VERIFIED` only from positive evidence,
    `UNVERIFIED` when evidence is merely absent, `CONTRADICTED` only from
    positive contradicting evidence. A credential field is never read back, so
    typing into one is honestly `UNVERIFIED` (and per §55 halts a sequence).
  - `control/window_manager.py` (§47) — real EWMH `_NET_ACTIVE_WINDOW`
    activation, *verified by reading the active window back* with a bounded
    600 ms wait; it never sends a focus request and assumes success. Verified
    live on this host (`active_window` 23068734; 4 client windows with real
    titles/classes/pids, e.g. `xfce4-panel`).
  - `control/action_tracker.py` (§52/§63/§78) — explicit ownership of the active
    action, lease, task/step/sequence ids, deadline and held keys/buttons,
    cleared on every terminal path (success, failure, exception, cancellation,
    emergency stop) and idempotent for emergency-stop-during-teardown.
  - Tests (185 new): policy modes 9, terminal guard 39, guards 19, permissions 26,
    action tracker 11, verifier 17, executor 30, window manager 6, plus the new
    `tests/safety/test_executor_safety.py` (24) and the live
    `tests/integration/test_window_manager_real_display.py` (4).
  - Defects found and fixed this session: (a) `executor.execute(confirmed=True)`
    was accepted but never forwarded to the permission engine, so a caller's
    confirmation could never satisfy the destructive gate; (b) an early attempt
    to strip `text` from a credential-context action would have prevented the
    password being typed at all — the text is used but never recorded.

- **Phase 9 — Safety gate: COMPLETE**, verified 2026-09-23.
  Evidence: `pytest` -> `631 passed, 2 skipped`; `mypy .` -> `Success: no issues
  found in 112 source files`; `ruff check .` -> `All checks passed!`;
  `pytest tests/safety` -> `39 passed`; the live stop suite -> `4 passed`;
  stop-latency benchmark p50 **0.014 ms** / p95 0.027 ms / max 0.030 ms against
  the §63 target of ≤ 150 ms (n=20, real XTEST 2.2 backend, recorded in
  `docs/benchmark_report.md`).
  Deliverables:
  - `control/emergency_stop.py` (§32/§63) — the latched stop. It latches *first*
    and cleans up after, takes no lock the running action holds (so a stop cannot
    queue behind the action it is stopping), and drains every releaser even when
    one raises: a stuck key/button is the failure mode that matters. The §63
    sequence is explicit and reported step-by-step (latch → stop execution →
    release buttons → release keys → cancel loop → force OBSERVE → emit), with
    the measured latency returned rather than assumed, `abort_code()` as the
    executor hook, `combine_abort_checks` for composition (stop ordered before
    takeover), and an explicit `reset()` because a stop that cleared itself
    would not be a stop.
  - `control/takeover.py` (§20/§62) — human takeover: pause, safely stop the
    current action, release tracked input (the *same* code path as the stop),
    force OBSERVE, then wait for an **explicit** `resume()`. Resume clears the
    latch, invalidates every live lease and forces full re-perception; it does
    not restore the previous mode (takeover outranks autonomous control) and it
    never replays an interrupted plan.
  - `control/recovery.py` (§4 rule 21, §21/§61) — bounded recovery as a policy
    object, not a caller's judgement: a failure is classified first
    (SAFETY / TARGET / TRANSIENT / VERIFICATION / BACKEND / FATAL / UNKNOWN), and
    only TARGET and TRANSIENT are even candidates. Never retried: a safety
    refusal, a **destructive** action (even when the failure preceded any input),
    anything that already injected input, an unclassifiable failure, a call the
    loop guard has seen `identical_call_loop_guard` times, or a materially
    changed target identity. Budgets are consumed per tool *and* per task step
    (so step 4 cannot spend step 1's budget or vice versa), and every decision is
    serialised into the envelope as evidence.
  - `watchdog/protocol.py` (§64/§78) — the heartbeat record (`pid`, `session_id`,
    `beats`, `owned_keys_down`, `owned_buttons_down`, `clean_shutdown`) written
    atomically to a small file **outside** the project directory, plus a reader
    that tells the next run whether the previous one ended cleanly or died
    holding input. Honest about its own limit: a dead X client's input is
    released by the X server, which BLAXCY does not claim as its own mechanism.
  - `watchdog/watchdog.py` (§64) — a minimal watchdog (one daemon heartbeat
    thread, stopped and joined on `stop`; no orphan) and `CrashGuard`, which
    releases held input and marks the run clean on SIGTERM/SIGINT/SIGHUP and on
    `atexit`, then really terminates (the handler restores the default
    disposition rather than swallowing the signal).
  - `control/executor.py` (updated) — `AttemptOutcome` (with `input_performed`),
    the bounded single retry against a freshly perceived state, `_annotate` for
    recovery evidence, and a new **early abort check** at the top of the
    pipeline: a latched stop no longer leaves the body resolving/leasing against
    a desktop the user has already taken back.
  - `control/backends/base.py`, `control/backends/xtest.py`, `control/mouse.py`,
    `control/keyboard.py` (updated) — `held_keys`/`held_buttons` as explicit
    read-only state plus `release_buttons()`/`release_keys()` on the backend and
    the controllers, which is what §63/§64 release. The XTEST backend's release
    is safe to call from the stop thread.
  - `config/settings.py` + `config/default_settings.toml` — the `[recovery]`
    section (`automatic_attempts_per_tool`, `attempts_per_task_step`,
    `identical_call_loop_guard`).
  - Tests (90 new cases): emergency stop 13, takeover 7, recovery 21, watchdog 18,
    `tests/safety/test_safety_gate.py` 15 (the wired gate: stop-before/mid-action/
    short-circuit, controller-wired release, takeover ordering, recovery retry +
    every refusal path + loop-bound), `tests/integration/test_emergency_stop_real_display.py`
    4, and updates to the existing Phase 7/8 test files.
  - Defects found and fixed this session: (a) `CrashGuard.cleanup` never released
    its re-entrancy flag, so the `atexit` cleanup would silently do nothing after
    any earlier cleanup — the classic bug where the safety net has a hole in it;
    (b) the executor only consulted the abort hook immediately before injection,
    so a latched stop still resolved, leased and revalidated first — there is now
    an early check too, and a test asserting no lease is ever issued while
    stopped.

- **§50 clipboard-assisted typing: COMPLETE**, verified 2026-09-23.
  Evidence: `pytest` -> `657 passed, 2 skipped`; `mypy .` -> `Success: no issues
  found in 115 source files`; `ruff check .` -> `All checks passed!`;
  `pytest tests/unit/test_clipboard.py tests/integration/test_clipboard_real_display.py`
  -> `21 passed` against the live X server; `pytest tests/safety` -> `41 passed`;
  the live capability report now reads
  `clipboard AVAILABLE xlib-selection 5.9 ms` (was `xlib`, and previously only
  proved that a connection could be opened).
  Deliverables:
  - `control/clipboard.py` — the real paster section 50 needed.
    `X11ClipboardPaster` takes the CLIPBOARD selection, **serves** it on a daemon
    thread, and hands it back. The serve window is the whole point: an X selection
    is read lazily, by the target application, *after* the paste keystroke, so a
    component that set the selection and returned would paste nothing (measured:
    with `paste_grace_ms = 0`, zero requests are served). It captures the
    previous content and owner before taking the selection, serves the previous
    content for `restore_grace_ms` on the way out (which is what a clipboard
    manager captures), then hands ownership back or releases it; a
    `SelectionClear` stops it rather than fighting for the selection. Targets are
    `UTF8_STRING`/`STRING`/`TEXT`, and `STRING` is refused (protocol-level
    `property = None`) for text Latin-1 cannot represent rather than sending
    mojibake. ``probe_x11_clipboard`` / ``select_clipboard_paster`` are the
    capability and selection entry points; the probe is functional but never takes
    ownership, because testing the clipboard by stealing the user's clipboard is a
    side effect on a shared resource.
  - `config/settings.py` + `config/default_settings.toml` — a `[clipboard]`
    section: `enabled`, `paste_grace_ms`, `restore_grace_ms`, `read_timeout_ms`,
    `max_bytes`. Behavioural knobs, not safety switches: disabling the paster only
    removes a capability (long/non-ASCII text then fails honestly), and a
    disabled paster is never handed to the typing layer at all.
  - `core/capability_probe.py::probe_clipboard` — now exercises the *same* code the
    typing path uses instead of opening a bare X connection, and distinguishes
    UNAVAILABLE (cannot serve the selection / no python-xlib) from DEGRADED
    (mechanism works, disabled by configuration).
  - `control/keyboard.py` — the plug-in interface documents the obligation it now
    has (the text must stay servable until `restore()`), and `type_text` gained
    `sensitive`: **credential content never takes the clipboard path**, because the
    clipboard is readable by every application and a clipboard manager keeps it in
    history. `control/executor.py` passes `sensitive=element.is_password`.
    Without this, wiring a real paster would have turned a non-ASCII password into
    a history entry for every clipboard manager on the session.
  - Tests (26 new): `tests/unit/test_clipboard.py` (10, display-free: honest
    failure on a missing display, the size cap before any X interaction, the
    disabled path, idempotent restore/close, and the real paster plugged into the
    controller), `tests/integration/test_clipboard_real_display.py` (11: the real
    transfer read back by a second client, `TARGETS` truthfulness, the `STRING`
    refusal and its Latin-1 counterpart, previous-content capture + ownership
    hand-back, the restore grace really re-serving the previous content, a reused
    paster serving every round, and `close()` releasing the selection), plus 3
    keyboard tests and 2 executor safety tests for the credential rule.
  - Benchmarks recorded in `docs/benchmark_report.md`: probe p50 4.89 ms (n=20);
    clipboard-assisted typing p50 406.04 ms with the default graces and 6.13 ms
    with zero grace **and zero requests served** — the measurement that justifies
    the non-zero default.

- **Phase 10 — Brain / tools / context: COMPLETE**, verified 2026-09-24.
  Evidence: `pytest` -> `818 passed, 2 skipped`; `mypy .` -> `Success: no issues
  found in 131 source files`; `ruff check .` -> `All checks passed!`;
  `pytest tests/safety` -> `66 passed`; `python main.py probe` -> `AVAILABLE: 8
  DEGRADED: 0 UNAVAILABLE: 4` with `brain UNAVAILABLE` for the honest reason (no
  key stored); `python main.py keys` -> the new §69 status command.
  Deliverables:
  - `ai/tool_protocol.py` (§66) — declaration and dispatch, derived from one
    source of truth so a tool cannot be advertised with one shape and accepted
    with another:
    * `tool_declarations()` emits all 19 §66 tools as strict JSON schemas
      (`additionalProperties: false`), each with real, implementation-true
      guidance text;
    * argument validation **refuses rather than ignores**: a key is allowed only
      if the tool's own declared schema declares it, so a coordinate
      /`element_id`/`lease_id`/`frame_id`/`generation` argument is rejected for
      an action tool (it would be a pre-resolved target that bypasses
      resolution, lease and revalidation) while `describe_region`'s declared
      region rectangle is accepted. A smuggled `confirmed: true` argument is
      rejected too — confirmation is never a tool argument;
    * `build_planned_action` maps every action tool onto a `PlannedAction` with
      the executor's exact parameter names (`text`, `key`+`modifiers`, `combo`,
      `vertical`/`horizontal`, `destination`, `window_id`, `visible_command`);
    * `ToolDispatcher` is the single door: read-only tools go on the bounded
      read path (§32.1 — a `BoundedSemaphore` of
      `max_concurrent_readonly_dispatch`, a timeout that returns a structured
      `RATE_LIMITED` rather than queueing forever, and the same abort hook as the
      executor), everything else goes through `Executor.execute`;
    * `run_sequence` is wired to an optional `SequenceRunner` protocol (so Phase
      10.1 plugs in without touching this module), enforces
      `max_sequence_steps`, and reports `BACKEND_UNAVAILABLE` with a truthful
      reason while no runner exists or the feature is disabled — a plan is never
      pretended to have executed.
  - `ai/prompt_builder.py` (§67) — the system instruction: BLAXCY's role, the
    pipeline the Brain cannot shorten, the refusal codes it should expect, the
    credential/protected-application boundaries, and the §68.1 economy guidance.
    Every claim in it is true of the implementation.
  - `ai/context_manager.py` (§68/§68.1) — the budget and the perception view:
    the last 8 turns stay verbatim and older ones are condensed into a
    **deterministic, factual** summary (no second model call, which would cost
    the tokens it is meant to save and introduce a second narrator); the 1200-token
    budget covers turns + state only, with the system instruction and tool
    declarations excluded as section 68's "current model requirements"; an
    unchanged `state_version` becomes a small marker instead of a re-sent
    screen; and when the active application is on the protected list **no element
    content is attached at all**, not merely its text.
  - `ai/brain_adapter.py` (§67) — the boundary and the **manual** tool loop:
    a neutral `ConversationTurn`/`ModelTurn` vocabulary (so the transport format
    belongs to the adapter), and `AgentLoop`, which walks the calls itself with
    `max_model_turns`, `max_task_wall_clock_seconds`, a cancellation token checked
    before every model turn *and* every tool call, and an abort hook — so a
    latched stop cannot be waiting behind a model round trip. It never confirms
    anything itself: a `CONFIRMATION_REQUIRED` result goes to an injected human
    prompt, and without one the loop halts with `CONFIRMATION_DENIED`.
  - `ai/gemini_adapter.py` (§67/§69) — Gemini via the installed `google-genai`
    2.20.0 API (verified before use: `parameters_json_schema`,
    `response.function_calls`, and function responses as
    `Content(role="user", parts=[Part.from_function_response(...)])`).
    `automatic_function_calling` is explicitly **disabled**, so the SDK never
    executes BLAXCY's tools; the SDK import is lazy (a missing SDK is an honest
    `BACKEND_UNAVAILABLE`, not an ImportError at startup); SDK failures are
    classified (`RATE_LIMITED`/`BACKEND_UNAVAILABLE`/`MODEL_ERROR`); and every
    error string is redacted against the API key before it becomes a
    `BrainError`.
  - `security/keyring_manager.py` (§69) — the one place `keyring` is called:
    `ApiKeyManager` (get/set/clear/status), a structured `KeyringError` for a
    broken backend (distinct from "no key stored"), an empty value refused at the
    manager rather than only in the concrete store, and `redact_secret` for
    sanitising a third-party error before it is logged. `core/capability_probe`
    now reads the key through this manager instead of calling `keyring` itself.
  - `main.py` — a `keys` subcommand (`status` / `set-gemini` / `clear-gemini`).
    The key is read from a **hidden prompt**, never an argument, so it cannot
    reach the shell history or the process table.
  - Tests (160 new): `tests/unit/test_keyring_manager.py` (9),
    `test_tool_protocol.py` (26), `test_tool_dispatch.py` (28),
    `test_sequence_dispatch.py` (11), `test_context_manager.py` (16),
    `test_brain_adapter.py` (19), `test_gemini_adapter.py` (20),
    `test_main_keys.py` (4), plus `tests/safety/test_tool_dispatch_safety.py` (25)
    and the new `tests/harness/phase10.py` builders
    (`build_dispatch_env`/`ScriptedBrain`/`build_loop_env`/`TickClock`).
  - Defects found and fixed while building it: (a) the forbidden-argument rule
    initially rejected `describe_region`'s own legitimate region coordinates —
    the rule is now "allowed only if the tool's declared schema declares it",
    which keeps action tools strict without breaking a read-only region query;
    (b) `ApiKeyManager.set_gemini_key("")` accepted an empty key because the check
    lived only in the concrete store, so a fake/other store could persist one —
    the invariant moved to the manager; (c) `GeminiAdapter._redact` also redacted
    the *service name*, which would have mangled any message containing
    "blaxcy"; it now redacts only the key itself.

- **Phase 10.1 — Batching & Throughput Layer: COMPLETE**, verified 2026-09-24.
  Evidence: `pytest` -> `873 passed, 2 skipped`; `mypy .` -> `Success: no issues
  found in 138 source files`; `ruff check .` -> `All checks passed!`;
  `pytest tests/safety` -> `77 passed`; the §66.1/§75 suite green
  (`test_sequence_runner.py` 21, `test_resolver_cache.py` 22,
  `tests/safety/test_sequence_halts.py` 11, `test_sequence_dispatch.py` 11); the
  §76 batching benchmark recorded in `docs/benchmark_report.md` (cold p50
  3.238 ms, warm-cache p50 3.283 ms, cache hit rate 0.995).
  Deliverables:
  - `control/sequence_runner.py` (§66.1) — `run_sequence` execution as a *container*
    over the one dispatcher: each step is dispatched through the same
    `ToolDispatcher` a standalone call uses, so a step inherits policy (§56/57),
    fresh resolution (§43), a fresh lease (§44), the full revalidation checklist
    (§45), occlusion/visibility (§46-47), physical input (Part VI) and
    verification (§60) by construction — there is no second pipeline to drift.
    No coordinate/`element_id`/lease is ever carried between steps; a plan is
    gated as its most restrictive declared step class; `confirmed` is accepted but
    deliberately **never forwarded** into a step (a destructive step asks the
    human fresh); OBSERVE refuses the plan outright; `max_sequence_steps` and
    `max_sequence_wall_clock_seconds` are enforced; on halt every undelivered step
    returns `NOT_EXECUTED` with a reason; per-step recovery budgets via
    `begin_step`/`end_step`; four lifecycle events + the live `SequenceProgress`
    record (§65/§71).
  - `core/resolver_cache.py` (§43.1) — the bounded TTL LRU of **identity-path**
    hints (never coordinates) plus a pure semantic-score memo; a hit only orders
    the resolver's traversal and is re-queried/re-scored live; invalidated on
    window close, generation change and structural region change; `PASSWORD_INPUT`
    targets are never stored; `rapidfuzz.fuzz.token_set_ratio` behind
    `semantic_match_floor`; disabling it is decision-identical by test.
  - `core/speculative_perceiver.py` (§33.2) — background, latest-wins, single-worker
    prefetch producing hints only (never elements/confidence/input/leases);
    discarded on generation mismatch or a `MEANINGFUL`/`MAJOR` change to their
    region; a credential description is never speculated.
  - `core/frame_engine.py` (§33.1) + the runner's `_boost` context — the `active`
    capture profile is entered for a sequence and reverted on every exit path (a
    scheduling change only).
  - `ai/tool_protocol.py` (Phase 10) — `run_sequence` validation, the step limit,
    the `halt_on`-narrows-only rule and the `SequenceRunner` attach point; §32.1's
    bounded read path (`max_concurrent_readonly_dispatch`, `RATE_LIMITED` on
    saturation) that a sequence never blocks.
  - Config `[sequence]` + `[resolver_cache]` sections; `capability_probe` reports
    the sequence flags; `schemas/events.py` sequence lifecycle events;
    `SequenceResult.halt_code`; `SIGNIFICANT_CHANGE_CLASSES`.
  - Tests (55 new): `test_sequence_runner.py` (21), `test_resolver_cache.py` (22),
    `tests/safety/test_sequence_halts.py` (11), `test_sequence_dispatch.py` (11),
    plus the `tests/harness/phase101.py` builders (`SequenceEnv`, the §74
    five-control workflow as data, `script_workflow_steps`,
    `workflow_plan_with_duplicate_label`).
  - **Defect found and fixed this session** (found by actually running the §76
    benchmark, not by inspection): `control/sequence_runner.py::_as_identity_hint`
    primed the §43.1 cache with `recorded_at = 0.0`, so every primed hint was
    *expired on arrival* — §33.2's speculation could never be consumed (`useful`
    structurally 0) and each hint charged a miss + invalidation. Measured: enabling
    speculation dropped the warm hit rate to 0.4975 with 400 invalidations; after
    the fix (`ResolverCache.prime` stamps the time itself, exactly as `record`
    does) the warm hit rate is 0.9975 with 0 invalidations. Regression test:
    `test_a_primed_hint_is_stamped_fresh_by_the_cache_not_by_its_caller`.
  - Second gap closed: the §76 usefulness rate was structurally `0.0` because
    nothing called `SpeculativePerceiver.mark_useful`. The runner now records a
    taken hint as useful only when that step's own live resolution consumed it (a
    resolver-cache HIT), so the rate is a measurement rather than a constant
    (`test_a_consumed_speculation_is_counted_useful`). Observability only — the
    counter changes no decision, position or safety check.
  - **Enabled by default** on explicit operator instruction once the test list
    went green (§83/§84): `[resolver_cache] enabled`, `[sequence] enabled`,
    `capture_boost` and `speculative_perception` now default **true** in both
    `config/default_settings.toml` and `config/settings.py`. Because that turns the
    `T-SPECULATE` worker into production code, the background path was given the
    coverage §4 rule 31 requires: new `tests/unit/test_speculative_perceiver.py`
    (9 tests) proves the worker resolves and is takeable, never blocks the caller
    (a 250 ms resolver does not delay the scheduling call), leaves no orphan thread
    on `shutdown`, never speculates a credential, discards on
    `MEANINGFUL`/`MAJOR`/generation change but not on `TRIVIAL`, and cannot grow an
    unbounded result map. Two supporting fixes: `wait_idle` now waits for the
    *running* job too (it cleared `_pending` before resolving, so it could return
    before the work finished, and its timeout fallback then claimed success it had
    not earned), and retained hints are capped (`MAX_RETAINED_HINTS = 4`,
    oldest-first) so a caller that never consumes hints cannot leak memory (§32).
  - The switch itself is tested: `test_settings.py` asserts the new defaults **and**
    that a user config can turn each flag back off, while `probe_sequence_execution`
    reports `AVAILABLE`/`sequence_runner` with the flags in its details (and
    `UNAVAILABLE` again when configured off).
- **Phase 11 — Visual grounding: IMPLEMENTED and VERIFIED**, verified 2026-09-24.
  Evidence: `pytest` -> `938 passed, 2 skipped`; `pytest tests/safety` -> `77
  passed`; `mypy .` -> `Success: no issues found in 141 source files`;
  `ruff check .` -> `All checks passed!`; `python main.py probe` ->
  `AVAILABLE: 9 DEGRADED: 0 UNAVAILABLE: 3` with `visual_grounding UNAVAILABLE
  google-genai` for the honest reason (no key stored);
  `python main.py probe --write-report` regenerated `docs/environment_report.md`
  (which had been stale since the Phase 5 era and now matches the tree).
  Deliverables:
  - `core/visual_grounder.py` (§38/§39/§41/§42) — the last stage of the §43
    cascade, built like `core/ocr.py`: the grounder owns the limits, the gates and
    the geometry, and a pluggable `VisualGroundingBackend` only answers *where is
    this description in this image*.
    * **§41 invocation gate enforced in code**: `ground()` refuses with
      `VISUAL_FALLBACK_DENIED` in OBSERVE *before the backend is touched*, and
      again when the capability is disabled by configuration. Since §56 starts
      every configuration in OBSERVE, raising the mode is an explicit operator
      decision before any pixel can leave the machine.
    * **§42/§55 privacy gate**: the upload region is checked against every
      supplied credential element and protected region **before** any crop,
      downscale or encode. A geometry-less credential element is treated as
      *unknown coverage* and refused; cross-space geometry is refused rather than
      silently converted (§31). No image, and no part of one, is ever placed in an
      error or a log.
    * **§41 bounds**: the crop's longest side is bounded (default 1024, via an
      `INTER_AREA` downscale) and every match's confidence is derived through
      `perception_confidence(VISUAL, …)` (base 0.80) and then clamped to the
      configured ceiling — which `VisualSettings(max_confidence=…)` refuses to
      raise above 0.80 (`le=0.80`), so the cap is a bound, not a default. The one
      model call is bounded by `[visual] model_timeout_ms` and passed to the SDK
      as `HttpOptions.timeout`.
    * **Coordinates stay space-tagged** (§31): the model answers in *normalized*
      fractions of the upload, which are clipped to the unit square and mapped
      onto the DESKTOP rect the image actually represented, so a downscale cannot
      shift a target and `x=1.4` cannot land outside what the model was shown.
    * `as_elements()` emits `source=VISUAL`, `coordinate_space=DESKTOP`,
      `clickable=True`/`effective_clickable=False` (visual is the §38 *last-resort*
      clickability source) and `role=UNKNOWN` by default (visual proves *where*,
      never *what*). It **refuses to emit a credential element** at all.
    * **It never picks a winner**: two same-label matches stay two elements, and
      the §43 absolute ambiguity rule then makes them `AMBIGUOUS` with
      `best=None` — asserted end-to-end through the real `TargetResolver`.
    * `GeminiVisualBackend` (§82-verified against installed `google-genai`
      2.20.0: `response_json_schema`, `response_mime_type`, `HttpOptions.timeout`,
      `Part.from_bytes` all confirmed before use): strict JSON out, the SDK
      imported lazily, the key read through `security/keyring_manager.py`
      (the §69 single owner), malformed/empty answers raised as `MODEL_ERROR`
      rather than an implied "not found", and every transport error string
      redacted against the key before it becomes a `BlaxcyError`.
  - `policy/guards.py` — `check_visual_fallback(...)` (§41/§42) composes the one
    place the decision is made: blocked application → protected application →
    disabled capability → mode below ASSIST, in fail-closed order, reusing the
    existing `match_application` (§58/§42 lists) rather than duplicating a
    matcher. `VISUAL_FALLBACK_MODES` is exported from `policy/__init__.py`.
  - `config/settings.py` + `config/default_settings.toml` — the new `[visual]`
    section: `enabled`, `max_image_side = 1024`, `max_confidence = 0.80`,
    `max_results`, `model_timeout_ms`.
  - `core/capability_probe.py::probe_visual_grounding(session, settings)` — now
    delegates to `VisualGrounder.capability()` (one source of truth, as the
    mouse/keyboard probes share the backend probe) instead of reporting "not
    implemented until Phase 11". It makes **no network call and uploads nothing**:
    a capability probe must not spend the user's screen or quota to answer a
    question about itself. UNAVAILABLE with a reason and a fix hint for: disabled
    by configuration, SDK not importable, no key stored, keyring backend broken;
    AVAILABLE with the limits in its details otherwise.
  - Tests (54 new): `tests/unit/test_visual_grounder.py` (46) — the §41 gate (the
    fake backend is provably *not called* on every refusal), the §42/§55 privacy
    gate (inside/outside region, geometry-less, cross-space, protected region),
    normalized→DESKTOP mapping, clipping, degenerate-box rejection, the 1024 px
    downscale (and that normalized mapping survives it), the 0.80 ceiling and the
    refusal to configure it higher, deterministic ordering, `max_results`, empty
    answer as honest not-found, element provenance/ids/role passthrough, the
    credential-element refusal, **two resolver integration checks** (a VISUAL
    element really resolves; two identical ones are `AMBIGUOUS` with `best=None`),
    failure honesty (structured errors propagate, unexpected ones become
    `MODEL_ERROR` without the image, an uncroppable region is `CAPTURE_FAILED`),
    capability honesty in all three states, diagnostics counters, and the Gemini
    request/response translation + key redaction through an injected client (no
    network); `tests/unit/test_policy_guards.py` (+6: the mode gate, disabled,
    protected, blocked-outranks-protected, and a non-matching list changing
    nothing); `tests/unit/test_settings.py` (+3: defaults, the ceiling cannot be
    raised, the switch can be turned off); `tests/unit/test_capability_probe.py`
    (the Phase 11 "not implemented" test replaced by two real availability tests).
  - **Honest limitation (§80), recorded rather than hidden**: the grounder is a
    real, tested perception source that **nothing calls yet**. It is in exactly the
    same position as `core/ocr.py`: the missing application composition root /
    perception orchestrator is what will call it (after a deterministic resolution
    returns `NOT_FOUND`, never before, and never on an `AMBIGUOUS` result). No stub
    claims otherwise, no code path pretends a plan executed, and the capability
    verdict describes availability, not reachability.
  - Defect found and fixed while writing the tests: the missing-credential
    capability path returned `details` without the keyring status, so
    `key_present` — the field that makes the verdict auditable — was absent on
    precisely the branch that needs it most.

- **Phase 12 (first half) — application composition root + perception
  orchestrator: IMPLEMENTED and VERIFIED**, verified 2026-09-24 **this session**.
  The work itself was written by the interrupted session at 19:29–20:30 and left
  both unverified and un-indexed; this session is what actually ran the gate on it
  and reconciled it. Evidence: `pytest -p no:pytest-qt` -> `978 passed, 2 skipped`;
  `pytest tests/safety` -> `77 passed`; `ruff check .` -> `All checks passed!`;
  `mypy .` -> `Success: no issues found in 147 source files`; and a real end-to-end
  startup: `python main.py status` -> `session x11`, `mode OBSERVE`, `input backend
  xtest`, `accessibility True`, `clipboard True`, `window manager True`, `brain
  (not connected)`, `sequence enabled True`, `resolver cache True`, `elements
  observed 80`, `active app Xdg-desktop-portal-gtk`, `AVAILABLE: 9 DEGRADED: 0
  UNAVAILABLE: 3` — one real observation of the live desktop with **no input
  injected**.
  Deliverables:
  - `core/application.py` (§1–§3/§12/§32/§65/§71) — `BlaxcyApplication`, the single
    place the Body is *constructed together*: capture engine, AT-SPI owner, OCR,
    visual grounder, resolver + cache, input controllers + clipboard paster, policy
    gate, executor, sequence runner, tool dispatcher, Brain loop, watchdog and
    crash guard, plus a `StartupReport` that records what actually came up. Its
    three stated properties are load-bearing: **one pipeline** (the executor stays
    the only route to physical input, the dispatcher the only door from a Brain
    call, the sequence runner a container over that same dispatcher, so composition
    adds no second path); **fail-closed composition** (no usable input backend
    yields `UnavailableBackend`, no Brain key yields *no* adapter with the reason
    kept); **honest startup** (nothing is reported ready merely because it was
    constructed). It has **no Qt dependency**, so the tests and `main.py status`
    need no window — the GUI is a view onto this, not the owner of it.
  - `core/perception.py` (§33–§43/§65) — `PerceptionOrchestrator`: one capture
    engine, one accessibility service, the optional OCR engine and the optional
    visual grounder, assembled into a single versioned `ScreenState` in the §38
    source order. `perceive()` is the ordinary cycle (what the executor's
    `perceive` hook and the read-only tools call); `enrich()` is the §43 fallback
    cascade, run **only after a deterministic miss** — never on `AMBIGUOUS` — and
    it returns an *enriched state* so §45 revalidation sees the same observation
    the resolution was made against. OCR contributes evidence, never clickability
    (its output is `TEXT_FRAGMENT`/`clickable=False`, and the resolver only
    considers actionable elements, so an OCR fragment can never *become* the click
    target). Perception stays pull-driven: no background capture thread is added;
    the only long-lived perception thread remains AT-SPI's `T-A11Y`.
  - **The §43 fallback is genuinely wired, and this closes Phase 11's open item.**
    `BlaxcyApplication._resolve_fallback` is passed to *both* the executor
    (`resolve_fallback=`, `control/executor.py`) and the dispatcher
    (`target_fallback=`, `ai/tool_protocol.py`), and each calls it only after a
    deterministic miss, accepts the enriched observation into the cache, and then
    re-resolves through the ordinary resolve -> lease -> revalidate -> execute ->
    verify path. This was checked directly in the source this session rather than
    inferred from the docstrings, because "the grounder has a caller now" is
    exactly the kind of claim §19 says not to make from naming alone.
  - `main.py` — two new subcommands that construct the whole Body: `status`
    (start every component, make one real observation, inject nothing) and
    `run TASK` (one task through the Brain). `run` deliberately has **no**
    `--yes`/`--confirm` flag: answering a confirmation in advance is exactly the
    §54/§71 bypass, so headlessly an action needing one is refused.
  - `tests/harness/perception.py` (builders) plus `tests/unit/test_perception.py`
    (18) and `tests/unit/test_application.py` (21) — 39 test functions / 40 test
    cases, all new.
  - `gui/__init__.py` — the Phase 12 GUI package header only. The modules it names
    (`app`, `main_window`, `status_panel`, `capability_panel`, `action_log`,
    `confirmation`, `emergency_stop_ui`) are **not written yet**; the file states
    their two rules (report what the Body did rather than what was asked, and
    never invent an action) but implements nothing.
  - **Honest limitation (§80), recorded rather than hidden**: these two modules are
    verified by their tests, by `mypy`/`ruff`, and by a real `main.py status` run —
    **not** by a line-by-line audit, and the §43 fallback path is exercised with
    fakes only: it has no live end-to-end test, and visual grounding additionally
    stays UNAVAILABLE on this host (no Gemini key stored). The orchestrator's
    perceive cycle also has no `n>=200` benchmark yet.

- **Phase 12 (§71) — GUI: IMPLEMENTED and VERIFIED**, verified 2026-09-24 **this
  session**. Evidence: `pytest -p no:pytest-qt` -> `1030 passed, 2 skipped`;
  `pytest tests/safety` -> `77 passed`; `ruff check .` -> `All checks passed!`;
  `mypy .` -> `Success: no issues found in 160 source files`;
  `tests/integration/test_gui_real_body.py` -> `2 passed` (the window over the
  *real* assembled Body); and the real entry point ran its event loop:
  `python main.py gui --offscreen` exited `124` under `timeout 8` (i.e. it stayed
  up for the whole window with no traceback; the only output was Qt's normal
  offscreen `propagateSizeHints()` notice). No input was injected anywhere.
  Deliverables:
  - `gui/confirmation.py` (§54/§56/§71) — the seam between the executor thread,
    which *blocks* on a confirmation while holding the single-writer action slot,
    and Qt, which forbids touching a widget off the GUI thread. `ConfirmationDialog`
    implements §71 literally: `MIN_COUNTDOWN_SECONDS = 3.0` is a module constant
    **floor**, not a setting, Confirm starts disabled, neither button is the default
    (so Return cannot confirm), and Escape denies. `ConfirmationBridge.request` is
    the callback handed to `BlaxcyApplication(confirmation=...)`, marshals the
    request through a queued signal, and blocks the caller on a bounded wait.
    **Every failure mode denies**: no `QApplication`, a timeout, a second
    concurrent request, a dialog that raises, a closed bridge — and a latched
    emergency stop or a human takeover (subscribed on the one bus) refuses the
    pending request, because the human has just overridden the question. Credential
    content is never rendered (`sensitive` payloads have their text fields
    suppressed; the dialog shows the *fact* of the action, never the secret).
  - `gui/status_panel.py` (§65/§66.1/§71) — the status read plus the
    **sequence-progress indicator** §71 asks for: current step of total, completed
    count, and the halt reason when a sequence stops. Rendering rules are a pure,
    testable function: an unestablished fact is `UNKNOWN` (never `""`, which reads
    like success) and `False` renders as `no`.
  - `gui/capability_panel.py` (§28/§71) — the probe's verdicts as they were
    measured (status, backend, latency, reason, fix hint), and `not probed` when no
    report exists. It never upgrades a verdict.
  - `gui/action_log.py` (§65/§70/§71) — a **bounded** view of the *one* event bus,
    in arrival order. Entries are marshalled through a queued signal because events
    arrive on arbitrary threads (executor, `T-A11Y`, Brain loop); the widget keeps a
    hard maximum of lines. A separate "GUI log" would have been a second, drifting
    account of what happened.
  - `gui/emergency_stop_ui.py` (§63/§71) — the stop button, an
    application-wide `Ctrl+Shift+Escape`, and a separate explicit re-arm. It calls
    the wired Body's stop and then reports **what the stop actually did** (latency,
    keys/buttons released, and any failed release step, which is the failure that
    hurts). A stop that did not latch says so.
  - `gui/main_window.py` + `gui/app.py` (§56/§62/§63/§71) — one window over the
    assembled Body and the composition entry point. The window is a *view*: its
    controls call the same `set_mode` / `trigger_emergency_stop` /
    `reset_emergency_stop` / `begin_takeover` / `resume_takeover` entry points the
    Brain does, it re-reads `status()` after every request so a refused or forced
    mode is what the operator sees, and it exposes no affordance that could start
    an arbitrary action (asserted by a regression test).
  - **§71 self-exclusion, implemented as a code-level guarantee**: `gui/app.py`
    appends BLAXCY's own window identity (`SELF_EXCLUSION_TOKEN`, carried in both
    the window title and the Qt application name) to
    `settings.safety.blocked_applications` *after* the user's config is loaded, and
    the §58 guard matches it — so BLAXCY cannot act on its own window, and a user
    config cannot remove that. Tested end to end through the real
    `check_blocked_application`, not by asserting the list contents alone.
  - `main.py` — a `gui` subcommand. The Qt import is **local to that command**, so
    `probe`/`session`/`config`/`keys`/`status`/`run` still work on a machine with
    no Qt binding, and a missing PySide6 is an honest `BACKEND_UNAVAILABLE` from
    that one command rather than an ImportError at startup.
  - `tests/conftest.py` + `tests/harness/gui.py` — the Qt fixture. The harness
    builds the offscreen `QApplication` itself rather than depending on
    `pytest-qt`, injects a `FakeClock` so the 3-second countdown is tested without
    a 3-second sleep (the floor stays a real constant), and skips honestly when Qt
    is unavailable.
  - Tests (52 new): `tests/unit/test_gui_confirmation.py` (18),
    `tests/unit/test_gui_panels.py` (18), `tests/unit/test_gui_app.py` (14),
    `tests/integration/test_gui_real_body.py` (2).
- **Honest limitations (§80), recorded rather than hidden**: (a) the GUI's own
  controls are never *clicked* against the live desktop in tests — a click would
  travel the real policy -> resolve -> lease -> revalidate -> execute -> verify path
  on the user's machine, which belongs in the §75 fixture-app end-to-end suite; the
  window's *rendering* and its call wiring are what is asserted. (b) ~~The
  confirmation dialog's real modal path is exercised through a fake dialog and
  through the dialog's own widget tests, not by a human clicking it under a real
  event loop.~~ **Partly closed 2026-09-24**: the real modal path is now exercised
  end to end by `tests/e2e/test_fixture_confirmation_e2e.py`, which shows the real
  dialog under a real event loop and presses its real buttons — verified for the
  deny path. The approve paths are written but skip on this host, blocked by the
  §46 occlusion defect above. (c) No GUI benchmark exists.
- **Phase 13 — Installation and single instance: COMPLETE**, verified 2026-09-24
  (this session). Delivered, with the section 73 step order implemented literally
  (`preflight -> detect package manager -> install system dependencies -> create
  venv -> install python dependencies -> install application files -> install icon
  -> install desktop entry -> create manifest/checksums -> validate runtime ->
  rollback on failure`):
  - `installer/installer.py` — **standard library only**, because the installer
    runs *before* the virtualenv and its dependencies exist, so importing anything
    BLAXCY itself depends on would make it unusable in exactly the situation it
    exists for. It reaches the application by running it as a subprocess instead.
  - `installer/__main__.py` — the command surface (`install` / `update` /
    `uninstall` / `preflight`; `--prefix`, `--source`, `--python`,
    `--reuse-python`, `--system-deps`, `--system`, `--dry-run`, `--json`; exit
    `0`/`1`/`2`). `install.sh`/`update.sh`/`uninstall.sh` are thin wrappers over
    it, so the documented commands cannot drift from the tested ones.
  - `desktop/blaxcy.desktop` — the **single source of truth** for the entry. The
    installer rewrites only what depends on the prefix (`Exec=`, pointing at the
    launcher, and `X-BLAXCY-Version=`) and strips the maintainer comments. The
    installed file is accepted by the desktop's own `desktop-file-validate`.
  - `desktop/make_icon.py` + `desktop/blaxcy.png` — a deterministic 256×256 icon
    generator (`ICON_PIXELS` must match the install path).
  - `core/single_instance.py` — §72/§85's "second launch is controlled". An
    advisory `flock` on `$XDG_RUNTIME_DIR/blaxcy/instance.lock`, **not** a pid-file
    heuristic: the kernel drops an `flock` when the process dies, so there is no
    stale-lock case to get wrong and no timeout after which a running instance
    silently becomes not-running. The file records the holder (pid, start time,
    command) so the refusal names who holds the lock. Taken by `main.py`'s `gui`
    and `run` **only** — the read-only commands deliberately do not take it, and
    `BlaxcyApplication` does not take it either (constructing a Body is not the
    same act as taking over a desktop). A lock that cannot be created at all fails
    **closed**.
  - **Rollback verified against a real failure, not a simulated one**: with a file
    where `share/applications` needs to be a directory, the run reports
    `[FAIL] apply changes -- FileExistsError(17, 'File exists')` and
    `rolled back: every change this run made was undone`, exits `1`, and leaves the
    prefix holding only the user's own file — no `lib/blaxcy`, no `bin/blaxcy`.
  - **Foreign files are handed back**: a pre-existing `blaxcy.desktop` and
    `blaxcy.png` are recorded in the manifest's `backups`, replaced by the install,
    and **restored verbatim** by the uninstall.
  - **Honest limitations (§80), recorded rather than hidden**: (a) `--system-deps`
    is never implied and could not be run on this host (no unattended sudo), so the
    package-manager step is verified as *reporting* (`apt-get` detected, the exact
    command printed) rather than as an install; a non-Debian family is detected and
    *told what capabilities are wanted* rather than fed an invented package list.
    (b) `gtk-update-icon-cache` exits 1 inside a throwaway prefix (no
    `index.theme`); the installer reports that truthfully in the step detail rather
    than claiming the cache was refreshed — the desktop reads `hicolor` directly, so
    nothing is broken. (c) `share/applications/mimeinfo.cache` survives an uninstall
    because it belongs to the desktop, not to BLAXCY. (d) The install is verified
    against throwaway prefixes; no system-wide install was performed.

## Current task
- **2026-09-26 (this session): the `s3` soak halt is addressed, the Wayland portal
  path is implemented, and the soak attributes drift per step.** Three units, all
  uncommitted:
  - **(1) Stronger §60 postcondition for clicks** (`control/verifier.py`,
    `control/executor.py`). The 150-iteration soak's single halt was
    `s3:VERIFICATION_UNVERIFIED` — the `Submit` click. Root cause: a plain push
    button keeps its own state and repaints nothing at its own box, so §60 had only
    the pixel evidence, which can be reclassified `ANIMATION` by §34 or missing a
    delta entirely. The live desktop *does* report a directly observable
    postcondition: a control in the same application becomes enabled (measured on
    the fixture: `Play Button` `enabled=False→True`). New positive-only
    `Postcondition.ELEMENT_STATE` + `Verifier.verify_element_state_change` verifies a
    click from that reported transition — an element present in **both** observations
    (truncation-robust), visible in both, `enabled` false→true, attributable to the
    target's own window/app (the same causality bound the pixel check enforces with
    overlap). It returns `None` (never a verdict) when there is no positive evidence,
    so the click falls through to the ordinary screen-change check; it can never
    produce `CONTRADICTED`. Wired into `Executor._verify`'s click branch after the
    selection check. New tests: 7 in `tests/unit/test_verifier.py`, 2 in
    `tests/unit/test_executor.py`, 1 live in
    `tests/integration/test_workflow_controls_real_display.py`.
  - **(2) XDG RemoteDesktop portal input backend** (`control/backends/portal.py`,
    `control/backends/__init__.py`, `core/capability_probe.py`). A real
    `InputBackend` over `org.freedesktop.portal.RemoteDesktop` (dbus-python, lazy):
    `CreateSession`→`SelectDevices`→`Start` with bounded `Response` waits, then
    `NotifyPointerMotion(Absolute)`/`NotifyPointerButton`/`NotifyPointerAxisDiscrete`/
    `NotifyKeyboardKeysym`. Its **probe is functional and non-invasive** — it
    introspects the live portal for the interface (and version) without creating a
    session or injecting input. `select_backend()` now falls back XTEST → portal.
    Honest limits: no readback, and absolute pointer motion needs a ScreenCast stream
    node, so `mouse` is reported **DEGRADED** on Wayland while `keyboard` is
    `AVAILABLE`. **Measured on this host:** the probe reports `UNAVAILABLE` with the
    exact reason (the X11 gtk portal does not expose the interface); session
    establishment and injection are therefore **unverified** and no Wayland control
    support is claimed. 13 unit tests over a fake transport
    (`tests/unit/test_portal_backend.py`).
  - **(3) Per-step soak timing** (`bench/soak.py`). Each sample now carries the
    executed steps' own `elapsed_ms`; `analyze()` reports a per-step breakdown (plan
    order, `p50`/`p95`/`max`, per-step second-half drift, slowest step by `p50`), and
    the CLI prints a table. A `NOT_EXECUTED` step contributes nothing (no duration,
    not counted as a fast zero). 4 new tests in `tests/unit/test_soak.py` (14 total).
  - **Gate: 1215 passed, 6 skipped**; ruff + mypy clean (**184 files**). Recorded in
    `docs/benchmark_report.md` "Phase 15", `docs/capability_matrix.md`,
    `docs/limitations.md`. **Not yet re-measured with real input** — the workflow/
    soak re-run needs operator approval to inject real input.
- **2026-09-26: Phase 15 continued — harness retry, capability matrix, longer soak.**
  (a) `FixtureApp.start()` now retries a launch up to `start_attempts` (default 3)
  and requires the child to stay alive for `ready_grace_s` after `ready`, so the
  observed startup race is retried instead of failing a test
  (`tests/unit/test_fixture_client.py`, 5 tests). (b) `docs/capability_matrix.md`
  documents capability × session × backend and states everything unsupported (no
  Wayland ScreenCast/RemoteDesktop portal, no `libei`/`ydotool`, no `xdotool`
  fallback, no XShm capture path). (c) A **150-iteration soak**: **149/150 stable**
  (one honest `s3:VERIFICATION_UNVERIFIED`), latency drift −5.15 %, steady-state
  memory 0.002 MB/iteration, resolver-cache hit rate **0.9906**, no stuck input, no
  retained lease, no orphan thread. Recorded in `docs/benchmark_report.md` "Phase
  15". Gate: **1186 passed, 6 skipped**; ruff + mypy clean (182 files).
- **2026-09-26: Phase 15 started — a soak runner is added and run, and it found a real
  defect.** `bench/soak.py` repeats the section 74 workflow end to end on the live
  desktop and reports **stability**, **drift** and **resource invariants** (held
  input, retained leases, orphan threads, memory trend). First run, 30 iterations:
  **30/30 stable, no halts**, latency p50 2207.63 / p95 2405.99 ms with −0.56 % drift,
  RSS warm-up 154.7→248.8 MB but **steady-state 0.004 MB/iteration**, threads
  1→6→1, held keys/buttons 0, leases 0. The soak then surfaced a **real §43.1 cache
  defect**: every live element arrives with `owner_window_id = None` (146/146), so
  hints were recorded under `None` while lookups filtered on the real
  `active_window_id` — `hits 0 / misses 150`, i.e. the cache was inert (fails safe,
  but useless). Fixed in `TargetResolver._record`; re-measured hit rate **0.875**.
  Gate: **1181 passed, 6 skipped**; ruff + mypy clean (181 files). Recorded in
  `docs/benchmark_report.md` "Phase 15". The analysis (`analyze`) is pure and
  unit-tested in `tests/unit/test_soak.py` (10 tests).
- **2026-09-26 (bootstrap session): the §51 focus-timing residual is fixed and the
  workflow now completes 20/20.** The last open §76 item was `s2:FOCUS_MISMATCH` in
  the sequence: `s1` clicked the search icon (whose handler focuses the field) and
  `s2` typed ~120 ms later, but §51's guard judged the type against a §35 cache-served
  observation taken before the focus change, so it wrongly refused. Fix: the executor
  now re-checks a would-be focus refusal against a **fresh live** perception before
  accepting it — mirroring `_confirm_negative`'s live re-check of a would-be failed
  verification — via a new `Executor._focus_checked_state` (bounded by
  `verify_settle_ms`, `verify_settle_ms = 0` disables it, skipped on the
  already-focused happy path). **It can only turn a wrong refusal into a correct
  permission**: a target that is genuinely still unfocused is still refused (locked by
  `test_the_focus_recheck_never_overrides_a_real_focus_refusal`), a latched stop
  short-circuits it, and no §60/§51 rule changed. Measured: the realistic workflow is
  **20/20, p50 1342.94 ms / p95 1787.70 ms (≤ 4000 ms met)**; click and keyboard
  30/30 each. Gate: **1170 passed, 6 skipped** (was 1167); ruff + mypy clean (179
  files). Recorded in `docs/benchmark_report.md` "Phase 14 — the realistic workflow
  after the section 51 focus re-check".
- **2026-09-26 (bootstrap session): the post-fix re-measurement was run, on operator
  approval.** The three halt causes were fixed but unmeasured; the index's next
  concrete action was the opt-in real-input re-run, so it was executed rather than
  asserted. Result (`--workload workflow-verifiable`, n=20 sequences / 30 input
  samples each, real XTEST 2.2): the sequence now completes **5/20** (was 2/20) and
  its halt set is a **single** cause — `s2:FOCUS_MISMATCH` ×15; the standalone click
  and keyboard rows are both **30/30 VERIFIED** (were `5/50` and `1/30`), so the
  typed-field read lag and the result-selection postcondition are demonstrably
  fixed. The residual is **sequence-specific focus timing**: the standalone keyboard
  types into the *same* Qt field and is 30/30 because it focuses the field and
  confirms it through perception before the timed action, whereas the sequence's
  `s1` click focuses the field and `s2` types ~120 ms later. Recorded in
  `docs/benchmark_report.md` "Phase 14 — the same workflow re-measured after the
  three fixes". No code changed, no §60 threshold moved (§4 rule 8).
- **2026-09-25 (bootstrap session, continued): the two remaining workflow halt causes are both root-caused and fixed.** On the operator's instruction ("fix the two remaining workflow halt causes"), each was *measured* read-only before anything was changed, and both diagnoses differed from the index's summary.
  - **`s2:VERIFICATION_CONTRADICTED` — the typed-field read lag.** Measured live, no input injected: with the fixture's field text changed over its own control channel, a plain `perceive()` took **921 ms** to see the new text (the §35 element cache serves its last traversal for the TTL), while a forced live read returned it correctly in 731 ms. `verify_text` therefore judged a *stale* read, and §60 calls a readable-and-wrong read `CONTRADICTED`. Fix: (i) `PerceptionOrchestrator.perceive(force=True)` now really does a live AT-SPI traversal (the parameter existed and was silently ignored — `del force`), (ii) the executor gained a `perceive_fresh` hook, and (iii) `Executor._confirm_negative` re-checks an `UNVERIFIED`/`CONTRADICTED` verdict against fresh live observations for `[verification] verify_settle_ms` (default 1000) before reporting it. A `VERIFIED` verdict never reaches the re-check, so a success is never delayed; the window can only turn a wrong failure into the truth. This is the §60 rule the module docstring already stated — `CONTRADICTED` requires *positive* evidence, and a read that may predate the action is not it.
  - **`s4:VERIFICATION_UNVERIFIED` — the result click.** The index said the selection "does not repaint `MEANINGFUL`-ly at its own box". Measured: **that is not what was wrong.** With both row colours pinned high-contrast the repaint *is* spatially `MEANINGFUL` (a 240x80 row) — but the §34 temporal layer reclassified it `ANIMATION`, because the previous step (Submit) had already changed the same list region as a side effect, and `_apply_animation` treats a region that changed again within `animation_recheck_seconds` (3 s) as moving content. The honest fix is *not* to loosen §34 (that classifier is a verification guard): a click on a selectable control's postcondition is "this control is now selected", which the accessibility tree reports directly and which is stronger evidence than the pixels it repainted. Delivered: `UIElement.selected`, populated from the AT-SPI `SELECTED` state (and `object:state-changed:selected` subscribed), a positive-only `Verifier.verify_selection`, and an executor branch that uses it for clicks on `LIST_ITEM`/`TREE_ITEM`/`TAB`/`MENU_ITEM` and otherwise falls straight through to the ordinary screen-change check — so nothing is weakened. The fixture's workflow layout also pins both row colours, so the repaint is unambiguous at the item's own box rather than theme-dependent.
  - Gate after both fixes: **1167 passed, 6 skipped** (was 1157); ruff + mypy clean (179 files). The live `tests/integration/test_workflow_controls_real_display.py` is **5 passed** and now proves the evidence itself on the real tree: `Result 1` reports `selected=True` and its sibling `Result 2` reports `selected=False`. **The benchmark number is deliberately NOT re-quoted**: all three causes are fixed but `--workload workflow-verifiable` has not been re-run, and a fixed number must be re-measured, never asserted (§76).
- **2026-09-25 (bootstrap session, continued): the `s2:FOCUS_MISMATCH` halts are ROOT-CAUSED and fixed at the source.** On the operator's choice ("investigate FOCUS_MISMATCH first") the recorded blocker was diagnosed read-only rather than accepted or papered over. The chain, all in the tree: `AccessibilityService.elements()` serves the §35 element cache for up to `cache_ttl_seconds` (2 s) unless `force=True`; `PerceptionOrchestrator.perceive()` calls it without `force`; the executor hands that same observation to §51's focus guard (`control/executor.py::_perform`, `state=state`); and `control/keyboard.py::check_focus` reads the `focused` flag off it. The cache invalidates on any *observed* AT-SPI event — but `AtspiBackend._subscribe` registered name/showing/visible/children/defunct and `window:activate/deactivate` **and not `object:state-changed:focused`**. A `setFocus()` on an already-active window emits a focus state-change and nothing else, so a click that moved focus was invisible for the whole TTL — and the TTL (2 s) is longer than the §45 action-state-age ceiling (1500 ms) that is supposed to bound action-relevant state. Result: §51 refused to type into a field that really *was* focused (`FOCUS_MISMATCH`), which is exactly what step `s2` does ~120 ms after step `s1` clicks the search icon. **Fix:** subscribe `object:state-changed:focused`/`:enabled`/`:sensitive`/`:editable` — every state this module reads into a field an action gates on (§51 focus, §45/§51 enabled+editability, §37's Qt `text`+`EDITABLE` refinement). The cache is documented as event-driven ("any observed AT-SPI event conservatively invalidates it"); it simply was not subscribed to the events that matter. Locked by `tests/unit/test_accessibility.py::test_state_events_that_gate_input_are_subscribed` (the pre-existing `test_an_observed_event_invalidates_the_cache` covers the mechanism; nothing covered the registration list). Gate: **1157 passed, 6 skipped**; ruff + mypy clean (179 files). **Not yet re-measured** — the two remaining workflow causes (the live read of a typed field lagging the injection, and a `LIST_ITEM` selection not repainting `MEANINGFUL`-ly) are unfixed, so a real-input re-run is what would show the effect. No safety property was weakened: a live-confirmed focus can only *permit* an action §51's cached check wrongly refused; it never permits one the guard would refuse.
- **2026-09-25 (this session, continued): `activate_element` — the last unimplemented §66 tool — is now implemented and verified live.** §66 lists it as a required tool; the executor deliberately refused it with `BACKEND_UNAVAILABLE` because `core/accessibility.py` only ever *read* `Atspi.Action`, never invoked it. Delivered: `AccessibilityService.activate(path, action=None)` (marshalled to the single `T-A11Y` owner thread, descending the element path **live** rather than reusing a cached node, which would be exactly the stale target §45 exists to reject), the pure helpers behind it (`_descend_path`, `_select_action_index`, `_invoke_action`), `ActivationOutcome` as a shared typed result, the executor branch (an injected `activate=` hook, the same pattern as `perceive`/`resolve_fallback`), and the composition-root wiring. It is `MUTATING`, so it passes policy → resolve → lease → revalidation → verification exactly like a click, and it injects **no** pointer or key input. Because it is XTEST-free, it also works where a synthetic click is unreliable (no coordinates, no pointer occlusion). Activation is an *optional* backend capability: a backend without it reports a structured `UNAVAILABLE` rather than substituting a click.
- **Two live/ordering defects were found by the new tests, not by reasoning.** (1) The first `activate` call raised `unknown accessibility request 'activate'` — `_dispatch` marshals through an explicit kind table and nothing had registered the new kind; the live integration test caught it immediately, which is precisely what a fake-only suite would have missed. (2) An existing safety test asserted that `activate_element` *always* reports `UNAVAILABLE`; once the tool is real that is no longer true (a late full-suite run reports `TARGET_STALE` because the harness's seeded state is past its 1500 ms maximum age), so the test was rewritten to pin the property that actually matters — a structured refusal, never a fabricated success, and nothing injected.
- **2026-09-25 (this session, continued): §70 structured logging was entirely missing and is now implemented.** Reconciling the tree surfaced a real gap that is not a benchmark row: **no `logging` module was imported anywhere in the application**, while `config/settings.py` declared a `[logging]` section (`level`, `rotation_max_bytes`, `rotation_backups`, `redact_on_root_logger`) that no code consumed -- a §4 rule 10 dead stub, a §70 requirement, and the thing §85's "secrets never logged" criterion is about. Delivered: `security/redaction.py` (§26 names this module and it did not exist) with the redaction filter and a secret registry, and `core/logging_setup.py` with the JSON formatter, config-driven rotation and idempotent `configure_logging`. Wired into `main.py` (every command) and `core/application.py` (start/stop), with structured records at the `ToolDispatcher` chokepoint (tool, outcome, error code, verification, state version, frame, latency, task/step/sequence id) and on a sequence halt (`sequence_id`, `step_index`, `sequence_halt_reason`). `logging.redact_on_root_logger` was added to `SECURITY_INVARIANTS`, so a config that disables log redaction is now refused at load (§4 rule 25). **A real bug was found by the new tests and fixed:** the filter was first attached only to the *root logger*, where it never runs for a record propagated from a child logger (`blaxcy.core.executor` -> `blaxcy` -> root) -- so it redacted nothing that actually logs. It is now installed on the handler as well, which is what makes the §85 property true.
- **Also delivered this session: the three §26-mandated docs that did not exist** -- `docs/architecture.md`, `docs/security.md`, `docs/limitations.md` (the last carries the §85 release-readiness reconciliation, which had never been walked). §26 listed six docs; only three existed.
- **2026-09-25 (this bootstrap session): the un-indexed `workflow-verifiable` workload is verified and its §76 number is measured for the first time.** The tree carried a complete but unindexed unit of work from a session that ended before running anything: `core/accessibility.py` (Qt announces an editable text field with the plain AT-SPI role `text`, so it must be refined to `TEXT_INPUT` by the `EDITABLE` state or §51 refuses to type; and `_read_text_content` read through the deprecated `Atspi.Accessible.get_text` shim, which raises on `(start, end)` under the installed PyGObject and silently returned nothing), `core/target_resolver.py` (the weak `substring_text`/`fuzzy_text` signals were promoted to real cascade stages and excluded from `TEXT_MATCH_STAGES`, so a fuzzy `token_set_ratio` hit like `("Search Address Bar", "Search")` can no longer land inside the §43 gap of a decisive match and veto it), the fixture's `--workflow-controls` layout, the third `--workload workflow-verifiable`, and their tests (the full gate moved from the index's **1104 passed** to **1121 passed, 6 skipped**; ruff + mypy clean, 174 files). Re-verified green first, then ran the operator-approved real-input benchmark.
- **Result: the §76 happy path is met by the synthetic workload and NOT yet by the realistic one.** `--workload verifiable` stays **20/20** (p50 2505.23 ms). `--workload workflow-verifiable` completed **2/20**, halting on `s2:FOCUS_MISMATCH` ×8, `s2:VERIFICATION_CONTRADICTED` ×4, `s4:VERIFICATION_UNVERIFIED` ×6; the input really lands (the fixture's own counters: `search_icon: 20`, `submit_button: 76`, `play_button: 2`, and its `search_input` holding every typed sample). Recorded in `docs/benchmark_report.md` "Phase 14 — the section 74 workflow at self-verifying scale".
- (Superseded) **2026-09-25 (previous bootstrap session): the remaining Phase 14 read-only numbers
  are delivered and the half-applied edit is finished.** The tree carried an
  uncommitted, untested edit to `bench/real_desktop.py` adding the orchestrator's
  perceive-cycle and the section 71 GUI-refresh benchmarks. It was completed and
  corrected: the accessibility read is now reported as what it is (a **warm**
  cache-served cycle, with a separate **cold** cycle that forces a live AT-SPI
  traversal inside the timed window — section 35's cache must never be presented
  as the live tree), `bench_accessibility`'s note no longer quotes the *Body state*
  cache instead of the traversal it timed, four tests were added, and the numbers
  are recorded in `docs/benchmark_report.md` "Phase 14". Gate: **1104 passed, 6
  skipped**; ruff + mypy clean (**173 files**).
- **2026-09-25 (bootstrap session) update: the interrupted Phase 14 "verifiable
  workload" work is reconciled and its number is now measured.** The prior session
  had built the §76 verifiable workload (fixture `--bench-controls`,
  `bench/real_desktop.py --workload`) but left it untested, unmeasured, and with a
  botched edit that glued the workload comment onto `SETUP_TIMEOUT_SECONDS = 30.0`.
  This session: fixed that line, made the keyboard benchmark workload-aware (it is
  skipped with a reason on the verifiable layout, which hides the text fields),
  added 11 tests covering the fixture layout and the runner's workload selection,
  re-ran the full gate (**1100 passed, 6 skipped**; ruff + mypy clean, 172 files),
  and — on the operator's go-ahead — ran the real-input verifiable benchmark:
  **20/20 five-step sequences completed**, p50 2505.23 ms. Recorded in
  `docs/benchmark_report.md`.
- Task: **Phase 14 is delivered for every number it was blocked on.** The
  real-desktop end-to-end `run_sequence`, the Phase 8 modules
  (resolution/lease/click), the orchestrator's perceive cycle (warm **and** cold)
  and the section 71 GUI-refresh cost are all measured and recorded in the section
  76 form; the only outstanding number is the workflow-workload keyboard/click
  pair, which has no targets on that layout and is reported `n = 0` with a reason.
- **2026-09-25 update: §46 occlusion is RESOLVED and verified live** (see "Known
  failures"). The index's claim that its steps (a)/(b) were still pending was stale —
  both were already in the tree, confirmed working by a read-only live probe and
  locked by a new live regression test. Step (c) is done too: the §75 injection paths
  now pass on the real desktop (**3 passed / 1 skipped**, was 1/3).
- **2026-09-25 update: Phase 14 real-desktop benchmarks are RUN** (operator chose the
  real-input path). `bench/real_desktop.py` measures the read-only numbers and the
  opt-in real-input ones; results are in `docs/benchmark_report.md` "Phase 14". The
  one honest gap: no real-desktop 5-step `run_sequence` *happy path* is achievable on
  the section 74 fixture, because §60 verification refuses its sub-`MEANINGFUL`
  changes (so the sequence halts at step 1 with `VERIFICATION_UNVERIFIED`). That is a
  verification/workload limit, not a batching defect.
- Subtask: one **human action** is still outstanding and is not code: run the QtTest
  install so the documented bare `pytest` command works again (see "Last known
  blocker").

## Interruption recovery
- Last session state: **clean** — Phase 13 was closed out, not interrupted. The
  tree is green and this index was rewritten in the same session that wrote the
  code, so there is no drift to reconcile (unlike the Phase 12 bootstrap).
- Last operation started and completed: the Phase 13 gate (39 new tests), then a
  real end-to-end install/update/rollback/uninstall against throwaway prefixes, then
  the documentation (`docs/installation.md`, README, this file, `knowledge.md`).
- Half-applied work: **none**. The only environment action left undone is the
  `sudo apt-get install python3-pyside6.qttest` recorded under "Last known blocker",
  which is a human decision and touches no BLAXCY file.
- Recovery action if the next session finds this file stale: the *only* phases not
  covered by a commit are Phases 8-13, all on top of `947b001`; read the
  Completed-milestones entries and then reconcile against `git status`.

## Last known blocker
- **`pytest` cannot run on this host with its documented command** while
  `pytest-qt` is installed, because PySide6 6.10.3 (apt, `--system-site-packages`)
  has no `QtTest` module. Every pytest invocation aborts in `pytest_configure`
  before collecting a single test. This is an *environment* blocker, not a code
  defect: the suite is fully green with `-p no:pytest-qt`. Fixing it properly needs
  a package install (see "Next concrete action") and therefore a human decision.

## Files being actively modified
- (none mid-change; this session's fixes are complete, tested and green)
- Touched this session (Phase 15 continued, 2026-09-26): `tests/harness/fixture_client.py`
  (the bounded startup retry: `_launch`, `_await_ready_grace`, `_join_reader`),
  `tests/unit/test_fixture_client.py` (new, 5 tests), `docs/capability_matrix.md`
  (new), `docs/benchmark_report.md` (the 150-iteration soak table),
  `docs/limitations.md` (Phase 15 row + the 1-in-150 verification note), this file.
- Touched this session (the Phase 15 soak, 2026-09-26): `bench/soak.py` (new),
  `tests/unit/test_soak.py` (new, 10 tests), `core/target_resolver.py`
  (`_record` passes the observation's active window as the cache key fallback),
  `tests/unit/test_resolver_cache.py` (the unattributed-element regression test),
  `docs/benchmark_report.md` (the Phase 15 soak section), `docs/limitations.md`,
  this file.
- Touched this session (the §51 focus-timing fix, 2026-09-26): `control/executor.py`
  (`_focus_checked_state` + the `TYPE_TEXT` branch using it, and the
  `from control.keyboard import ... check_focus` import),
  `tests/unit/test_executor.py` (three focus tests),
  `docs/benchmark_report.md` (the post-fix workflow table),
  `docs/limitations.md`, this file. Committed alongside the checkpoint below.
- Touched this session (the typed-field read lag and the result click):
  `core/perception.py` (the `force` parameter is now real),
  `core/application.py` (`_perceive_fresh` wiring), `control/executor.py`
  (`perceive_fresh` hook, `_confirm_negative`, the selection branch in `_verify`),
  `control/verifier.py` (`Postcondition.SELECTED`, `SELECTABLE_ROLES`,
  `verify_selection`), `schemas/elements.py` (`UIElement.selected`),
  `core/accessibility.py` (populate `selected`, subscribe
  `object:state-changed:selected`), `config/settings.py` +
  `config/default_settings.toml` (`[verification] verify_settle_ms`/`verify_poll_ms`),
  `tests/harness/phase8.py` (`with_perceive_fresh`),
  `tests/harness/fixture_client.py` + `tests/fixtures/fixture_app.py`
  (`set_selection`, and the pinned result-row colours),
  `tests/unit/{test_executor,test_verifier,test_perception}.py`,
  `tests/integration/test_workflow_controls_real_display.py`,
  `docs/limitations.md`, `docs/benchmark_report.md`, this file
- Touched this session (the `s2:FOCUS_MISMATCH` root cause and fix):
  `core/accessibility.py` (`AtspiBackend._subscribe` now registers the
  action-gating state-change events), `tests/unit/test_accessibility.py` (the new
  registration-list regression test), `docs/limitations.md` (the workflow row's
  root cause + the subscription caveat), this file
- Touched by the un-indexed session (complete, verified this session):
  `core/accessibility.py` (the Qt `text`+`EDITABLE` role refinement, the
  `Atspi.Text`-interface read), `core/target_resolver.py` (the
  `substring_text`/`fuzzy_text` stage split and the `TEXT_MATCH_STAGES` narrowing),
  `tests/fixtures/fixture_app.py` (`--workflow-controls` layout),
  `tests/harness/fixture_client.py`, `bench/real_desktop.py`,
  `tests/integration/test_workflow_controls_real_display.py` (new, untracked),
  `tests/integration/test_bench_workload_fixture.py`,
  `tests/unit/test_accessibility.py`, `tests/unit/test_bench_real_desktop.py`,
  `tests/unit/test_target_resolver.py`
- Touched this session (the `activate_element` unit): `core/accessibility.py` (the
  `activate` capability and its helpers), `control/executor.py`
  (`ACTIVATE_ELEMENT` in `EXECUTOR_TOOLS`/`_TARGET_TOOLS`, the `activate=` hook and
  the `_perform` branch), `core/application.py` (`_activate_element` wiring),
  `schemas/actions.py` (`ActivationOutcome`), `tests/harness/phase8.py` (the
  `activate` pass-through), `tests/unit/test_activate_element.py` (new, 13),
  `tests/integration/test_workflow_controls_real_display.py` (the live activation
  test), `tests/safety/test_tool_dispatch_safety.py` (the rewritten property),
  `docs/limitations.md`, this file
- Touched this session: `docs/benchmark_report.md` (the new workload subsection and
  the `Reproducing` command), `docs/architecture.md` (new), `docs/security.md`
  (new), `docs/limitations.md` (new), `security/redaction.py` (new),
  `core/logging_setup.py` (new), `config/settings.py` (the logging invariant),
  `main.py` (logging setup per command), `core/application.py` (start/stop records),
  `ai/tool_protocol.py` (the dispatch wrapper and `_log_dispatch`),
  `control/sequence_runner.py` (the halt record), `tests/conftest.py` (a hermetic
  `BLAXCY_LOG_DIR`), `tests/unit/test_redaction.py` (new),
  `tests/unit/test_logging_setup.py` (new), this file
- Touched this session (complete, verified): `bench/real_desktop.py` (the read-only
  `AppOwner` type, the warm/cold perceive-cycle split, the GUI-refresh benchmark,
  the corrected accessibility note), `tests/unit/test_bench_real_desktop.py` (+1),
  `tests/integration/test_bench_readonly_real_display.py` (new, 3 tests),
  `docs/benchmark_report.md`, this file
- Touched by Phase 13 (complete, verified): `installer/installer.py` (new),
  `installer/__init__.py` (new), `installer/__main__.py` (new), `install.sh` (new,
  executable), `update.sh` (new, executable), `uninstall.sh` (new, executable),
  `desktop/blaxcy.desktop` (new), `desktop/blaxcy.png` (new),
  `desktop/make_icon.py` (new), `core/single_instance.py` (new), `main.py`
  (instance lock + `--lock-path`), `tests/unit/test_single_instance.py` (new),
  `tests/unit/test_installer.py` (new),
  `tests/integration/test_install_scripts.py` (new), `docs/installation.md` (new),
  `pyproject.toml` (`installer` added to `[tool.setuptools] packages`),
  `README.md`, `knowledge.md`, this file
- Touched by the §71 GUI (complete, verified): `gui/app.py` (new),
  `gui/main_window.py` (new), `gui/status_panel.py` (new),
  `gui/capability_panel.py` (new), `gui/action_log.py` (new),
  `gui/confirmation.py` (new), `gui/emergency_stop_ui.py` (new),
  `gui/__init__.py`, `main.py` (the `gui` subcommand),
  `tests/conftest.py` (new), `tests/harness/gui.py` (new),
  `tests/unit/test_gui_confirmation.py` (new), `tests/unit/test_gui_panels.py`
  (new), `tests/unit/test_gui_app.py` (new),
  `tests/integration/test_gui_real_body.py` (new)
- Touched by the interrupted Phase 12 session (now verified): `core/application.py`
  (new), `core/perception.py` (new), `main.py`, `gui/__init__.py`,
  `tests/harness/perception.py` (new), `tests/unit/test_perception.py` (new),
  `tests/unit/test_application.py` (new)
- Touched by Phase 11 (complete, verified): `core/visual_grounder.py` (new),
  `policy/guards.py`, `policy/__init__.py`, `config/settings.py`,
  `config/default_settings.toml`, `core/capability_probe.py`,
  `tests/unit/test_visual_grounder.py` (new), `tests/unit/test_policy_guards.py`,
  `tests/unit/test_settings.py`, `tests/unit/test_capability_probe.py`,
  `docs/environment_report.md` (regenerated)

## Last test results (verbatim, not paraphrased as "passed")
- Command (`2026-09-26`, after Phase 15 continued — harness retry + matrix):
  `. .venv/bin/activate && ruff check . && mypy . && timeout 900 python -m pytest -o addopts="" -p no:pytest-qt -q`
- Result: `1186 passed, 6 skipped, 4 warnings in 36.74s`; `ruff check .` ->
  `All checks passed!`; `mypy .` -> `Success: no issues found in 182 source files`.
- Command (`2026-09-26`, the longer soak, real input, operator-approved):
  `. .venv/bin/activate && timeout 560 python -m bench.soak --confirm-real-input --iterations 150`
- Result: **149/150 completed** (stability 0.9933, `stable: false` — one honest
  `s3:VERIFICATION_UNVERIFIED`); latency p50 2325.18 / p95 2673.68 ms; drift
  −5.15 %; RSS warm-up 154.5→270.4 MB with **steady-state 0.002 MB/iteration**;
  threads 1→6→1; held input 0; leases 0; resolver-cache hit rate **0.9906**
  (741/7); speculation requested/completed 598, taken 1, max_outstanding 3.
- Command (`2026-09-26`, after the Phase 15 soak + the §43.1 cache fix):
  `. .venv/bin/activate && ruff check . && mypy . && timeout 900 python -m pytest -o addopts="" -p no:pytest-qt -q`
- Result: `1181 passed, 6 skipped, 4 warnings in 33.08s`; `ruff check .` ->
  `All checks passed!`; `mypy .` -> `Success: no issues found in 181 source files`.
  (One interim run showed a single failure in
  `tests/integration/test_workflow_controls_real_display.py`, a fixture-startup flake
  under load that was green on immediate re-run — see "Known failures".)
- Command (`2026-09-26`, the Phase 15 soak, real input, operator-approved):
  `. .venv/bin/activate && python -m bench.soak --confirm-real-input --iterations 30`
- Result: **30/30 completed**, stability 1.0, no halts; latency p50 2207.63 ms /
  p95 2405.99 ms; latency drift −0.56 %; RSS warm-up 154.7→248.8 MB with
  **steady-state 0.004 MB/iteration**; threads 1→6→1 (no orphan); max held
  keys/buttons 0; max leases after an iteration 0. Runner: 30 sequences, 150 steps,
  0 halts, 0 refusals. Resolver cache `hits 0 / misses 150` **before** the fix and
  **hit rate 0.875** (35/5 over 8 sequences) after it. Recorded in
  `docs/benchmark_report.md` "Phase 15".
- Command (`2026-09-26`, this bootstrap session — re-verifying the uncommitted
  unit before trusting it): `. .venv/bin/activate && ruff check . && mypy . && timeout 900 python -m pytest -o addopts="" -p no:pytest-qt -q`
- Result: `1167 passed, 6 skipped, 4 warnings in 36.70s`; `ruff check .` ->
  `All checks passed!`; `mypy .` -> `Success: no issues found in 179 source files`.
  This matches the index's claim exactly (the claim was re-run, not trusted).
- Command (`2026-09-26`, opt-in real input, operator-approved):
  `. .venv/bin/activate && timeout 560 python -m bench.real_desktop --confirm-real-input --workload workflow-verifiable --samples 20 --input-samples 30 --sequence-samples 20`
- Result: click **30/30 VERIFIED** (p50 177.12 ms); keyboard **30/30 VERIFIED**
  (p50 679.40 ms); 5-step `run_sequence` **5/20 completed** (p50 184.69 ms, p95
  1457.09 ms), halted `s2:FOCUS_MISMATCH` ×15; revalidation p50 0.42 ms (≤ 25 ms
  met); warm perceive 9.53 ms / cold 579.29 ms (≤ 1500 ms met). Recorded in
  `docs/benchmark_report.md`.
- Command: `. .venv/bin/activate && ruff check . && mypy . && timeout 900 python -m pytest -o addopts="" -p no:pytest-qt -q`
- Result (`2026-09-25`, this session, after implementing `activate_element`):
  `1156 passed, 6 skipped, 4 warnings in 32.86s`; `ruff check .` ->
  `All checks passed!`; `mypy .` -> `Success: no issues found in 179 source files`.
  New: `tests/unit/test_activate_element.py` (13) and the live
  `test_the_live_submit_control_is_really_activated_through_atspi`.
- Command (live, **no pointer or key input injected**, this session):
  `. .venv/bin/activate && python -m pytest -o addopts="" -p no:pytest-qt -q tests/integration/test_workflow_controls_real_display.py`
- Result: `4 passed` — `AccessibilityService.activate` really invoked the Qt fixture's
  Submit action on the live AT-SPI tree, and the fixture's own `submit_button`
  counter proved the application genuinely performed the action (so the effect
  cannot have come from injected input).
- Command: `. .venv/bin/activate && ruff check . && mypy . && timeout 900 python -m pytest -o addopts="" -p no:pytest-qt -q`
- Result (`2026-09-25`, this session, after implementing §70 logging and the three
  missing docs): `1142 passed, 6 skipped, 4 warnings in 32.08s`; `ruff check .` ->
  `All checks passed!`; `mypy .` -> `Success: no issues found in 178 source files`.
  21 new tests: `tests/unit/test_redaction.py` (11) and
  `tests/unit/test_logging_setup.py` (10).
- Command (end-to-end, this session, throwaway log dir, **no input injected**):
  `. .venv/bin/activate && LOGD=$(mktemp -d) && BLAXCY_LOG_DIR="$LOGD" python main.py probe >/dev/null && cat "$LOGD"/blaxcy.jsonl`
- Result: the CLI created `blaxcy.jsonl` and wrote structured JSON
  (`{"ts": "2026-09-25T13:06:09+00:00", "level": "INFO", "logger":
  "numexpr.utils", "message": "NumExpr defaulting to 8 threads."}`) — proving the
  root handler, the formatter and propagation all work, not just the unit paths.
- Command: `. .venv/bin/activate && ruff check . && mypy . && timeout 900 python -m pytest -o addopts="" -p no:pytest-qt -q`
- Result (`2026-09-25`, this session, over the reconciled un-indexed tree):
  `1121 passed, 6 skipped, 5 warnings in 31.10s`; `ruff check .` ->
  `All checks passed!`; `mypy .` -> `Success: no issues found in 174 source files`.
- Command (targeted, this session):
  `. .venv/bin/activate && python -m pytest -o addopts="" -p no:pytest-qt -q tests/integration/test_workflow_controls_real_display.py tests/integration/test_bench_workload_fixture.py tests/unit/test_accessibility.py tests/unit/test_target_resolver.py -v`
- Result: `85 passed` — including the **live** `test_workflow_controls_real_display.py`
  (3): it proves on the real AT-SPI tree that Qt's editable `text` node becomes a
  `TEXT_INPUT` with readable content, that the clickable controls are perceivable
  `BUTTON`s, and that the results are named `LIST_ITEM`s with geometry.
- Command (operator-approved real input, **real mouse/keyboard injected**, this session):
  `. .venv/bin/activate && python -m bench.real_desktop --confirm-real-input --workload workflow-verifiable --samples 20 --input-samples 30 --sequence-samples 20`
- Result (`2026-09-25`, live X11/XTEST, the §74 fixture): accessibility p50
  **633.91 ms** (n=20, 316 elements); perception cycle warm p50 **7.39 ms** / cold p50
  **640.65 ms**; resolution p50 **1.92 ms**; revalidation p50 **0.72 ms** (target
  ≤ 25 ms **met**, checks=11); GUI refresh p50 **0.36 ms**; click p50 **201.84 ms**,
  **30/30 VERIFIED**; keyboard p50 **90.91 ms**, **1/30 VERIFIED** (19
  `VERIFICATION_CONTRADICTED`, 10 `VERIFICATION_UNVERIFIED`); 5-step `run_sequence`
  p50 **345.41 ms** / p95 1635.85 ms, **2/20 completed all 5 steps** (halted:
  `s2:FOCUS_MISMATCH` 8, `s4:VERIFICATION_UNVERIFIED` 6, `s2:VERIFICATION_CONTRADICTED` 4).
  Recorded in `docs/benchmark_report.md` "Phase 14 — the section 74 workflow at
  self-verifying scale". The input genuinely landed — the fixture's own counters after
  the run read `search_icon: 20`, `submit_button: 76`, `play_button: 2` and its
  `search_input` held every typed sample.
- Command: `. .venv/bin/activate && ruff check . && mypy . && timeout 900 python -m pytest -o addopts="" -p no:pytest-qt -q`
- Result (`2026-09-25`, this session, after finishing the half-applied Phase 14
  benchmark edit): `1104 passed, 6 skipped, 4 warnings`; `ruff check .` ->
  `All checks passed!`; `mypy .` -> `Success: no issues found in 173 source files`.
- Command (read-only, **no input injected**, this session): `. .venv/bin/activate && python -m bench.real_desktop --samples 30`
- Result (`2026-09-25`, real X11 desktop, XTEST, n = 30): accessibility p50
  **852.28 ms** (372 elements); perception cycle **warm** p50 **10.61 ms** / p95
  15.22; perception cycle **cold** (forced live AT-SPI traversal) p50 **830.78 ms**
  / p95 922.81; resolution p50 1.64 ms; revalidation p50 **0.90 ms** (target
  <= 25 ms met); GUI refresh p50 **0.39 ms** / p95 1.09. Recorded in
  `docs/benchmark_report.md` "Phase 14".
- Command: `. .venv/bin/activate && ruff check . && mypy . && timeout 600 python -m pytest -o addopts="" -p no:pytest-qt -q`
- Result (`2026-09-25`, this session, after fixing the malformed `SETUP_TIMEOUT_SECONDS`
  line in `bench/real_desktop.py`, making `bench_keyboard` workload-aware, and adding
  11 tests for the §76 verifiable workload): `1100 passed, 6 skipped, 3 warnings`;
  `ruff check .` -> `All checks passed!`; `mypy .` -> `Success: no issues found in
  172 source files`. The new tests are `tests/integration/test_bench_workload_fixture.py`
  (7) and `tests/unit/test_bench_real_desktop.py` (4).
- Command (opt-in real input, this session): `. .venv/bin/activate && python -m bench.real_desktop --confirm-real-input --workload verifiable --samples 20 --input-samples 30 --sequence-samples 20`
- Result (`2026-09-25`, real X11 desktop, XTEST): accessibility p50 **1027.30 ms**
  (n=20, 446 elements); resolution p50 **2.59 ms**; revalidation p50 **1.04 ms**
  (target ≤ 25 ms **met**); click p50 **225.85 ms**, **30/30 VERIFIED**;
  **5-step `run_sequence` p50 2505.23 ms / p95 3671.94 ms, 20/20 completed all 5
  steps** (target ≤ 4000 ms met at p50/p95). Recorded in `docs/benchmark_report.md`.
- Command: `. .venv/bin/activate && timeout 900 python -m pytest -o addopts="" -p no:pytest-qt -q`
- Result (`2026-09-25`, previous session, after adding the §46 live regression test):
  `1084 passed, 6 skipped, 3 warnings in 24.84s`; `ruff check .` ->
  `All checks passed!`; `mypy .` -> `Success: no issues found in 169 source files`.
  The count moved 1069 -> 1076 -> 1083 across the un-indexed §46 work, +1 for the new
  live test. One full-suite run **did** fail first (`1 failed`,
  `test_live_unknown_stacking_stays_conservative`) — that test was **unsound**, not
  the code: ancestor exclusion applies even with an unknown stack, so a heuristic
  that counted ancestors as "covering" was wrong. The redundant test was removed;
  unknown-stack conservatism is already covered by `tests/unit/test_target_resolver.py`.
- Command: `. .venv/bin/activate && python -m pytest -o addopts="" -p no:pytest-qt -q tests/integration/test_target_resolver_real_display.py -v`
- Result: `9 passed, 1 skipped` — including the new
  `test_live_target_is_actionable_through_the_real_occlusion_path`, which drives the
  live AT-SPI tree, the live `WindowManager.stacked_windows()` order and
  `occluders=state.elements` exactly as production does, and asserts a real target is
  `actionable=True`.
- Command (read-only live probe, **no input injected**): perceive the live tree +
  `WindowManager.stacked_windows()`, then resolve `'Applications'` / `'Desktop'` /
  `'Terminal'` with `occluders=state.elements`
- Result: all three now `status=RESOLVED actionable=True ratio=0.0` (were
  `actionable=False ratio=1.0`). 41 elements at the default 250 ms deadline carried
  `owner_app_pid` for 40/41 and `owner_window_title` for **0/41**; at a 2000 ms
  deadline it was 336 elements with 267 titles — so at default settings the **pid**
  path is what makes §46 work. Nothing was clicked or typed.
- Command: `. .venv/bin/activate && BLAXCY_E2E_REAL_DISPLAY=1 timeout 600 python -m pytest -o addopts="" -p no:pytest-qt -q tests/e2e/test_fixture_confirmation_e2e.py -v`
- Result: `3 passed, 1 skipped, 5 warnings in 53.50s` (was `1 passed, 3 skipped`
  before §46 was fixed) — the three real-input injection paths now really inject.
- Command (opt-in real-input benchmark, new this session): `. .venv/bin/activate && python -m bench.real_desktop --confirm-real-input --samples 200 --input-samples 50 --sequence-samples 20`
- Result (real desktop, 2026-09-25): accessibility p50 **760.58 ms** / p95 1080.42
  (n=200); target resolution p50 **2.51 ms** / p95 4.34 (n=200); lease revalidation
  p50 **1.04 ms** / p95 2.16 (n=200, target ≤25 ms **met**); click p50 **220.20 ms** /
  p95 1332.77 (n=50, **5/50 VERIFIED**); keyboard p50 **112.52 ms** / p95 1179.69
  (n=50, **3/50 VERIFIED**); 5-step `run_sequence` p50 **99.01 ms** / p95 133.31
  (n=20, **0/20 completed** — every run halts at step 1 `VERIFICATION_UNVERIFIED`).
  Recorded in `docs/benchmark_report.md` "Phase 14". The input really lands (the
  fixture's own click counters confirm it); section 60 correctly refuses to call the
  fixture's sub-`MEANINGFUL` changes a user-visible outcome. `bench/` is no longer an
  empty package: `bench/real_desktop.py` is the reusable runner.
- Prior run (`2026-09-24`) — Command: `. .venv/bin/activate && timeout 900 python -m pytest -o addopts="" -p no:pytest-qt -q`
- Result: `1069 passed, 2 skipped, 3 warnings in 26.40s` / `27.32s` (2026-09-24,
  this session, after Phase 13: 1069 = 1030 + 39 new cases — 12 single-instance +
  20 installer-helper + 7 installer-script integration. The two skips remain the
  opt-in fixture test and the declared-live-occlusion condition. Run twice, before
  and after the `_prune_created_dirs` fix, both green.)
- Command: `. .venv/bin/activate && python -m pytest -o addopts="" -p no:pytest-qt -q tests/unit/test_single_instance.py tests/unit/test_installer.py tests/integration/test_install_scripts.py -v`
- Result: `32 passed in 0.45s` then `7 passed in 8.07s` (the 39 Phase 13 cases.
  `test_install_scripts.py` runs the **real** scripts into a throwaway prefix and
  actually installs, launches the installed copy, updates, injects a real failure to
  force a rollback, and uninstalls, verifying each installed file's checksum. It
  installs nothing system-wide and needs no network — it reuses `sys.executable`.)
- First run was **not** green, and the failures are worth recording: `3 failed, 29
  passed`.
  - `test_an_unusable_lock_path_fails_closed` — a **real defect in the code**: with
    a *file* where the lock's parent directory needs to be, `acquire()` raised
    `FileExistsError` out of `Path.mkdir` instead of failing closed. The `mkdir` is
    now inside the `try` that already guarded `os.open`, so every unopenable lock
    path (a file in the way, a runtime directory we may not write) returns `False`
    and reports the I/O reason rather than crashing the launch.
  - `test_a_package_with_caches_is_pruned` — the **test** was wrong: it built a
    synthetic `demo/` package, but `iter_app_files` walks the explicit
    `APP_PACKAGES` list and a stray directory is deliberately never installed. The
    test now exercises the cache filter on a package that is actually in the list.
  - `test_preflight_refuses_a_system_prefix_without_the_flag` — the **test** was
    wrong: it asserted that `/usr` preflights with no failing step at all once
    `--system` is passed, but `/usr` is genuinely unwritable for an unprivileged
    user. The assertion is now about the `--system` *gate* (and it asserts that
    writability is probed and honestly fails), plus a writable prefix preflighting
    clean.
- Command: `. .venv/bin/activate && python -m ruff check .`
- Result: `All checks passed!` (7 findings on the first run, all in the new files:
  `I001` ×2, `F401` an unused `argparse` import, `RUF022` `__all__` ordering, and
  `SIM105` ×3 — the `try/except/pass` blocks are now `contextlib.suppress`.)
- Command: `. .venv/bin/activate && python -m mypy .`
- Result: `Success: no issues found in 168 source files` (160 before Phase 13)
- Two mypy findings, both fixed: `read_manifest()` returned `Any` from
  `json.loads` (it now returns `None` for JSON that is not an object — a manifest
  we cannot read is not a manifest, and pretending otherwise would let an update
  trust a file it cannot understand), and `warn_unreachable` flagged a **test** line
  because the checker narrows the `acquired` property to a literal and then treats
  the next read as unreachable (the assertion is routed through a helper function so
  the test stays about the lock).
- **Live end-to-end install, this session, verbatim** (throwaway prefix
  `/tmp/blaxcy-verify`, reusing `$PWD/.venv/bin/python`):
  - `./install.sh --prefix … --dry-run` -> every step `[ok]`/`[skip]`, exit `0`, and
    **the prefix does not exist afterwards** (`ls: cannot access …: No such file or
    directory`) — a dry run really writes nothing.
  - `./install.sh --prefix … --python …` -> exit `0`,
    `install: OK - installed 78 files to …/lib/blaxcy; menu entry
    …/share/applications/blaxcy.desktop`, with `validate -- installed copy ran;
    session report produced; desktop entry valid`.
  - `desktop-file-validate …/blaxcy.desktop` -> exit `0` (accepted by the desktop's
    own validator).
  - `…/bin/blaxcy session` -> exit `0`, printing the real session report
    (`session type x11`, `DISPLAY :0.0`, `desktop XFCE`, `portal available yes`,
    `AT-SPI bus yes`).
  - manifest -> `version 0.1.0`, `78` files each with a 64-char sha256, `ok True`,
    `python` recorded as the reused interpreter, `venv None`, all 16 steps
    `ok`/`skipped`, `backups []`.
  - **Second launch refused**: the lock file held
    `{"pid": 75765, "started_at": …, "command": "…/lib/blaxcy/main.py gui
    --offscreen"}` and the second launch exited `2` with `error: another BLAXCY
    instance (pid 75765, …/main.py gui --offscreen, started 4s ago) already owns
    this desktop session [lock: /run/user/1000/blaxcy/instance.lock]; refusing to
    start a second Body.` After the first process was killed, a fresh launch started
    normally (`exit=124` under `timeout 5`) — **process death releases the lock, no
    stale-lock case**.
  - `./update.sh` -> exit `0`; manifest then records `updated_from 0.1.0` and
    `previous_file_count 78`.
  - `./uninstall.sh` -> exit `0`, `removed 81 files; manifest deleted`; the only
    leftovers were empty `share/` scaffolding and
    `share/applications/mimeinfo.cache` (desktop-managed).
  - foreign-file test -> a pre-existing `blaxcy.desktop` and `blaxcy.png` were
    listed in the manifest's `backups`, replaced by the install, and restored
    **verbatim** (`USER'S OWN DESKTOP ENTRY` / `USER'S OWN ICON`) by the uninstall.
- Superseded (still this session, before Phase 13):
  `. .venv/bin/activate && python -m pytest -o addopts="" -p no:pytest-qt -q`
- Result: `1030 passed, 2 skipped, 3 warnings in 20.38s` (2026-09-24, this session,
  after the §71 GUI: 1030 = 978 + 52 new cases — 18 confirmation + 18 panels +
  14 app + 2 real-Body integration. The two skips remain the opt-in fixture test
  and the declared-live-occlusion condition.)
- Command: `. .venv/bin/activate && python -m pytest -o addopts="" -p no:pytest-qt -q tests/unit/test_gui_confirmation.py tests/unit/test_gui_panels.py tests/unit/test_gui_app.py`
- Result: `50 passed in 2.86s` (the GUI widget/bridge suites: the countdown floor,
  no implicit Enter acceptance, every denial path, preemption by stop/takeover,
  honest panel rendering, the bounded log, and self-exclusion through the real
  guard)
- Command: `. .venv/bin/activate && python -m pytest -o addopts="" -p no:pytest-qt -q tests/integration/test_gui_real_body.py -v`
- Result: `2 passed in 4.29s` (the §71 window built over the **real** assembled
  Body — real capture engine, real AT-SPI owner, real executor — asserting the
  Body's own mode is what the selector shows, that the real probe's capability
  count reaches the panel, and that the window's own title is refused by the
  blocked-application guard. No input injected.)
- Command: `. .venv/bin/activate && timeout 8 python main.py gui --offscreen`
- Result: `exit=124` (the real entry point stayed in its event loop until the
  timeout killed it, i.e. the window ran over the assembled Body with no
  traceback); the only output was Qt's normal offscreen
  `This plugin does not support propagateSizeHints()` notice
- Superseded (bootstrap earlier this session; kept for the record):
  `. .venv/bin/activate && python -m pytest -o addopts="" -p no:pytest-qt -q`
- Result: `978 passed, 2 skipped, 3 warnings in 16.84s` (2026-09-24, this session:
  the reconciled gate over the un-indexed Phase 12 composition-root work. 978 = 938
  (Phase 11) + 40 new cases from `tests/unit/test_perception.py` (18) and
  `tests/unit/test_application.py` (21) = 39 test functions, one of which is
  parametrised. The two skips remain the opt-in fixture test and the
  declared-live-occlusion condition. **`-p no:pytest-qt` is required on this host**
  until the QtTest blocker below is resolved; the plugin registers as `pytest-qt`
  (hyphen), so `-p no:pytestqt` does *not* disable it.)
- Command: `. .venv/bin/activate && python -m pytest -o addopts="" -p no:pytest-qt -q tests/safety`
- Result: `77 passed in 0.57s` (unchanged count: the composition root wires existing
  components together and adds no new safety property of its own — which is the
  point. It is, however, exactly why the gate had to be restored before this work
  could be called complete.)
- Command: `. .venv/bin/activate && python main.py status`
- Result: the assembled Body started for real and reported one real observation:
  `session x11`, `mode OBSERVE`, `started True`, `input backend xtest`,
  `accessibility True`, `clipboard True`, `window manager True`, `brain (not
  connected)`, `sequence enabled True`, `resolver cache True`, `state frame id 0`,
  `state age (ms) 252.13`, `elements observed 80`, `active app
  Xdg-desktop-portal-gtk`, `capabilities AVAILABLE: 9 DEGRADED: 0 UNAVAILABLE: 3`.
  No input was injected.
- Command: `. .venv/bin/activate && python -m ruff check .`
- Result: `All checks passed!`
- Command: `. .venv/bin/activate && python -m mypy .`
- Result: `Success: no issues found in 147 source files` (141 before Phase 12)
- Command: `. .venv/bin/activate && python -m pytest -o addopts="" -q` (the
  documented command, **unpatched**)
- Result: `INTERNALERROR ... AttributeError: module 'PySide6' has no attribute
  'QtTest'` before any test is collected — the blocker recorded above.
- Superseded (Phase 11 session; kept for the record):
  `. .venv/bin/activate && python -m pytest -o addopts="" -q`
- Result: `938 passed, 2 skipped, 4 warnings in 19.59s` (Phase 11; the two skips
  remain the opt-in fixture test and the declared-live-occlusion condition. The
  baseline this session inherited was `884 passed, 2 skipped`; this session added
  54 tests — 46 visual grounder, 6 guard, 3 settings, and the two rewritten
  capability-probe tests replacing the old "not implemented until Phase 11" one).
- Command: `. .venv/bin/activate && python -m pytest tests/safety -o addopts="" -q`
- Result: `77 passed in 1.13s` (unchanged: Phase 11 adds a perception source and a
  policy guard, and no safety property is weakened — the new guard's refusals are
  covered in `tests/unit/test_policy_guards.py` and the grounder's own gates in
  `tests/unit/test_visual_grounder.py`).
- Command: `. .venv/bin/activate && python -m ruff check .`
- Result: `All checks passed!`
- Command: `. .venv/bin/activate && python -m mypy .`
- Result: `Success: no issues found in 141 source files` (139 before Phase 11)
- Command: `. .venv/bin/activate && python main.py probe`
- Result: `AVAILABLE: 9   DEGRADED: 0   UNAVAILABLE: 3`; `visual_grounding
  UNAVAILABLE google-genai`, reason `no Gemini API key stored, so visual
  grounding cannot call a visual model (keyring service='blaxcy',
  key='gemini_api_key')`; the same probe with `[visual] enabled = false` reports
  `UNAVAILABLE | visual grounding is implemented but disabled by configuration`
- Command: `. .venv/bin/activate && python main.py probe --write-report`
- Result: regenerated `docs/environment_report.md`; it had been stale since the
  Phase 5 era (it still claimed browser accessibility and the sequence runner were
  "not implemented until Phase 3/10.1" and that the resolver cache was disabled),
  and now matches the tree: browser accessibility and visual grounding UNAVAILABLE
  with their real reasons, `sequence_execution AVAILABLE sequence_runner`,
  resolver cache + sequence enabled `True`
- Superseded (Phase 10.1 session; kept for the record):
  `884 passed, 2 skipped, 4 warnings in 13.07s`
- Command: `. .venv/bin/activate && python -m pytest -o addopts="" -q`
- Result: `884 passed, 2 skipped, 4 warnings in 13.07s` (the two skips remain the
  opt-in fixture test and the declared-live-occlusion condition; the baseline this
  session inherited from the interrupted Phase 10.1 session was `871 passed, 2
  skipped in 12.62s`. This session added 13 tests: 2 regression tests for the two
  defects, 9 for the now-production background speculation worker
  (`tests/unit/test_speculative_perceiver.py`), and 2 for the enablement/switch
  itself)
- Command: `. .venv/bin/activate && python -m pytest tests/safety -o addopts="" -q`
- Result: `77 passed in 0.69s` (Phase 8 + Phase 9 properties, the
  credential-never-touches-the-clipboard rule, the
  `tests/safety/test_tool_dispatch_safety.py` 25 dispatch-level properties: mode
  gating, no self-confirmation, a smuggled `confirmed` argument, coordinate/lease
  arguments, a destructive step inside a plan, blocked/protected applications,
  credential non-leakage, stop preemption and unknown tools; **plus the §66.1
  sequence-halt properties** in `tests/safety/test_sequence_halts.py`: an
  AMBIGUOUS target mid-sequence halting with a `NOT_EXECUTED` tail, an emergency
  stop and a human takeover mid-sequence, a credential step never batched, a
  blocked application halting the remainder, an application blocked mid-sequence,
  `halt_on` narrowing, an unmapped failure always halting, and concurrent
  read-only calls never reordering or replacing physical steps)
- Command: `. .venv/bin/activate && python -m ruff check .`
- Result: `All checks passed!`
- Command: `. .venv/bin/activate && python -m mypy .`
- Result: `Success: no issues found in 139 source files` (131 before Phase 10.1)
- Command: inline §76 batching benchmark (in-process; §74 five-control workflow via
  `tests/harness/phase101.py`, fake input backend, scripted perception, no model)
- Result: 5-step `run_sequence` cold `p50 3.238 ms / p95 5.066 ms`; warm resolver
  cache `p50 3.283 / p95 4.864` with hit rate `0.995` (796 hits / 4 misses) and
  `0 invalidations`; warm cache + speculation `p50 4.291 / p95 6.723` with hit rate
  `0.9975`, `0 invalidations`, and speculative `taken 400 / useful 400 /
  discarded_change 200` (usefulness rate `1.0` of taken); all three configurations
  `n = 200`, `failures = 0`; recorded in `docs/benchmark_report.md` under
  "Phase 10.1 — Batching & throughput overhead (in-process)" with the caveats
  (fakes, inline speculation worker). Same run *found* the born-expired-hint defect
  above: before the fix, enabling speculation dropped the hit rate to `0.4975`
  with `400` invalidations.
- Command: `. .venv/bin/activate && python main.py probe`
- Result: `AVAILABLE: 9   DEGRADED: 0   UNAVAILABLE: 3`; `sequence_execution
  AVAILABLE sequence_runner` (was UNAVAILABLE while the layer shipped off) and
  `brain UNAVAILABLE google-genai` with the honest reason `no Gemini API key
  stored (keyring service='blaxcy', key='gemini_api_key')` — the probe reads the
  keyring through `security/keyring_manager.py` (the single code path) rather than
  calling `keyring` itself
- Command: `. .venv/bin/activate && python main.py probe --write-report`
- Result: regenerated `docs/environment_report.md` (unchanged capability verdicts)
- Command: `. .venv/bin/activate && python main.py keys`
- Result: `{"keyring_service": "blaxcy", "keyring_key": "gemini_api_key",
  "key_present": false}` (the new §69 management CLI; the key is read from a
  hidden prompt and never from an argument)
- Command: inline Phase 10 overhead benchmark (in-process; fake backend and
  scripted perception, no network Gemini call)
- Result: parse click `p50 0.0109 ms / p95 0.0304 ms`; declarations build
  `p50 0.0174 / p95 0.0183`; read dispatch `get_screen_state` `p50 0.0287 /
  p95 0.0670`; read dispatch `find_element` `p50 0.0865 / p95 0.1679`; unchanged
  state-context marker `p50 0.0014 / p95 0.0016`; context fit (6 turns)
  `p50 0.0316 / p95 0.0574`; Gemini request translation (6 turns, **no network**)
  `p50 0.1449 / p95 0.3450`; all `n = 200`, recorded in
  `docs/benchmark_report.md` under "Phase 10 — Brain / tool-protocol overhead"
- Superseded (previous session, Phase 9 + §50 clipboard; kept for the record):
  `657 passed, 2 skipped, 4 warnings in 14.72s`; `pytest tests/safety` `41 passed`;
  clipboard suites `21 passed`; live stop suite `4 passed`; `mypy` `115 source
  files`
- Command: `. .venv/bin/activate && python -m pytest tests/safety -o addopts="" -q`
- Result: `41 passed in 0.60s` (the Phase 8 + Phase 9 safety properties, plus the
  credential-never-touches-the-clipboard rule)
- Command: `. .venv/bin/activate && python -m pytest tests/unit/test_clipboard.py tests/integration/test_clipboard_real_display.py -o addopts="" -q`
- Result: `21 passed in 3.93s` (the 11 real-display clipboard tests ran, not skipped;
  they take and hand back the CLIPBOARD selection and inject no input)
- Command: `. .venv/bin/activate && python -m pytest tests/integration/test_emergency_stop_real_display.py -o addopts="" -q`
- Result: `4 passed in 0.37s` (live XTEST 2.2 wired as a real input releaser; the
  section 63 sequence ran, nothing was injected, no input was left held)
- Command: inline stop-latency benchmark (20 re-armed triggers, real backend)
- Result: `backend: xtest available: True details: {'xtest_version': '2.2'}`;
  `n=20 min=0.013 p50=0.014 p95=0.027 max=0.030 ms`, target 150.0 ms; every sample
  `latched=True forced_observe=True errors=()`; recorded in
  `docs/benchmark_report.md` with its honest caveat (nothing was held, so the
  release path had no X round-trip to pay for)
- Command: `. .venv/bin/activate && python -m pytest tests/integration/test_window_manager_real_display.py -o addopts="" -v`
- Result: `4 passed in 0.27s` (live EWMH: probe, active window, client list, window metadata; nothing activated)
- Command: `. .venv/bin/activate && python -m pytest tests/integration/test_target_resolver_real_display.py -o addopts="" -q`
- Result: `7 passed, 1 skipped in 2.46s` (ran against the live AT-SPI tree, no input injected)
- Command: inline clipboard-paste benchmark (real paster, Ctrl+V to the fake
  backend, `n = 10` per configuration)
- Result: default graces `p50 406.04 ms` (p95 406.88, min 402.44, max 408.55), with
  `paste_grace_ms = 0` -> `p50 6.13 ms` and **0 selection requests served**; probe
  `p50 4.89 ms` (n = 20). Recorded in `docs/benchmark_report.md`.
- Command: `. .venv/bin/activate && python main.py probe --write-report`
- Result: `clipboard AVAILABLE xlib-selection 5.9 ms` (live functional probe;
  regenerated `docs/environment_report.md`)
- Command: `. .venv/bin/activate && python -m ruff check .`
- Result: `All checks passed!`
- Command: `. .venv/bin/activate && python -m mypy .`
- Result: `Success: no issues found in 115 source files`
- Note: this pytest (9.1.1) prints no final summary line under the project's
  default `addopts = "-q"`; the honest count above is with `-o addopts=""`.
- Date/commit: 2026-09-24 (tree based on `947b001` + uncommitted Phase 8, Phase 9,
  §50 clipboard and Phase 10 work)

## Known failures
*(structure per §12.1)*
- **The §43.1 resolver cache could never hit on a live desktop — RESOLVED 2026-09-26**
  (found by the new Phase 15 soak; fixed the same session)
  - failure: `resolver_cache.hits == 0` while `misses` equalled the lookup count
    (`0 / 150` over 30 sequences), despite 5 entries recorded
  - environment: this host, XFCE X11, live AT-SPI, the section 74 fixture
  - trigger: every perceived element arrives with `owner_window_id = None`
    (measured read-only: **146/146**), while `state.active_window_id` is real
  - observed result: `record()` keyed the hint's window component under `None`;
    `lookup()` filtered on `active_window_id`; no candidate ever matched, so every
    lookup missed with no invalidation counted
  - expected result: a repeated description against an unchanged region is a hit
  - likely cause: `TargetResolver._record` did not pass `active_window_id` to
    `ResolverCache.record`, even though `record` accepts it and falls back to it when
    the element names no window
  - current status: **RESOLVED.** `_record` now passes `state.active_window_id`;
    re-measured hit rate **0.875** (35 hits / 5 misses over 8 sequences). It failed
    **safe** throughout — a miss runs the full cascade — so no safety property was
    ever at stake. Regression test:
    `test_a_hit_is_possible_for_elements_the_traversal_did_not_attribute`.
  - next investigation: none — closed. Worth remembering that a hint must be keyed
    by the same window source a lookup filters on.
- **Fixture-startup flake under load — MITIGATED 2026-09-26 (bounded retry)**
  - failure: `tests/integration/test_workflow_controls_real_display.py` fails during
    fixture startup, the child exiting cleanly (`code 0`) before the first command
  - environment: this host, immediately after a long real-input soak
  - trigger: the fixture exits on stdin EOF; under load the handshake can lose its
    pipe before the first command
  - observed result: `FixtureError: fixture already exited (code 0)`; all 5 tests in
    the file failed in one run, then all 5 passed on immediate re-run and in the next
    full-suite run
  - expected result: the fixture stays up for the test
  - likely cause: real-desktop timing under load, not a code defect (the fixture
    binary started and reported its inventory on every manual launch)
  - current status: **mitigated.** `FixtureApp.start()` retries a launch up to
    `start_attempts` (default 3) and requires the child to remain alive for
    `ready_grace_s` after `ready`; a startup race is now retried rather than
    reported as a test failure. Locked by `tests/unit/test_fixture_client.py`
    (5 tests: healthy no-retry, die-after-ready retried, never-ready retried,
    bounded attempts, single-attempt opt-out). Not reproduced since.
  - next investigation: none — closed unless it recurs at a rate the retry cannot
    absorb; the retry is bounded on purpose so a real defect still surfaces.*
- **The realistic §74 workflow happy path — RESOLVED and met 2026-09-26**
  (`workflow-verifiable` reaches **20/20, p50 1342.94 ms / p95 1787.70 ms**, within
  the ≤ 4000 ms target). Kept for the record: it took four causes, each root-caused
  and fixed rather than tuned away. The first three (the AT-SPI focus-event
  subscription, the typed-field read lag, and the `LIST_ITEM` selection
  postcondition) were fixed on 2026-09-25/26; the last (§51 judging a would-be
  refusal against a cache-served read taken before the click moved focus) was fixed
  2026-09-26 by a live re-check before refusing. **No §60 threshold or §51 rule was
  loosened** — a genuinely unfocused target is still refused, by test.
  - failure: with the fixture's `--workflow-controls` layout and
    `--workload workflow-verifiable`, only 2 of 20 five-step `run_sequence` runs
    completed all five steps; the rest halted at step `s2` (typing) or `s4`
    (result selection)
  - environment: this host, XFCE X11 (`DISPLAY=:0.0`), real XTEST 2.2, live AT-SPI,
    Python 3.14.6, the §74 fixture as target
  - trigger: `require_verification_for_mutating = true` (the default) plus §66.1's
    halt-on-`UNVERIFIED`/`CONTRADICTED`; the step then has to verify against a live
    AT-SPI read of the Qt field/list
  - observed result: `s2:FOCUS_MISMATCH` ×8, `s2:VERIFICATION_CONTRADICTED` ×4,
    `s4:VERIFICATION_UNVERIFIED` ×6; keyboard benchmark `1/30 VERIFIED` with the
    same signature (10 `no readable text`, 19 readable-but-not-containing). The
    typed content really reached the widget — the fixture reports its `search_input`
    holding every sample (`bench0…bench29song name`)
  - expected result: (for a happy path) all five steps verified — the §76 number
  - likely cause: **two distinct limits, neither a §60 defect.** (a) The §51 focus
    guard / §60 text check are timing-sensitive under real input: the fixture's
    search-icon click does focus the field (`_on_search_clicked` calls
    `search_input.setFocus()`), but the bounded AT-SPI traversal does not reliably
    reflect the field's content inside the step's settle window — the read lags the
    injection rather than disagreeing with it. (b) Selecting a `LIST_ITEM` does not
    reliably repaint `MEANINGFUL`-ly at its own box, so `s4` is `UNVERIFIED`.
    `verify_text` uses a *containment* check (not exact match), so accumulated
    field content is not the cause. Section 60 is correctly refusing to call an
    unconfirmed change a success.
  - **root cause of the `s2:FOCUS_MISMATCH` ×8 (found and fixed 2026-09-25, this
    session; see "Current task"): the §35 element cache was never subscribed to
    `object:state-changed:focused`, so a focus move was invisible and §51 refused a
    legitimate type.** Evidence is code-level and complete: `elements()` serves the
    cache for `cache_ttl_seconds` unless `force=True`; `perceive()` never forces;
    the executor passes that observation to `check_focus`; and `_subscribe`'s event
    tuple omitted focus. Adding `focused`/`enabled`/`sensitive`/`editable` makes the
    cache invalidate on the change the guard depends on. That is one of the three
    recorded halt reasons, not all of them: `s2:VERIFICATION_CONTRADICTED` ×4 and
    `s4:VERIFICATION_UNVERIFIED` ×6 are the separate §51/§60 read-lag and
    `LIST_ITEM`-repaint limits and remain open.
  - **root cause of the other two causes (found and fixed 2026-09-25, this session;
    see "Current task" for the measurements):** the typed-field `CONTRADICTED` was
    `verify_text` judging a read the §35 cache could serve from before the action
    (measured: 921 ms for a plain `perceive()` vs 731 ms forced), fixed by making
    `perceive(force=True)` real and re-checking a negative verdict against live
    observations; and the result click's `UNVERIFIED` was **not** a repaint-size
    problem — the repaint is spatially `MEANINGFUL` — but the §34 temporal layer
    reclassifying it `ANIMATION` because the previous step had changed that region.
    Fixed from the control's own `selected` state, positive-only.
  - **RE-MEASURED 2026-09-26** (post-fix, same command): **5/20 completed**
    (was 2/20), and the halt set is now a *single* cause — `s2:FOCUS_MISMATCH` ×15.
    `s2:VERIFICATION_CONTRADICTED` ×4 and `s4:VERIFICATION_UNVERIFIED` ×6 are gone.
    The standalone rows prove the two fixes: click **30/30 VERIFIED** (p50 177.12 ms)
    and keyboard **30/30 VERIFIED** (p50 679.40 ms), where the pre-fix run was
    `5/50` and `1/30`. Recorded in `docs/benchmark_report.md` "Phase 14 — the same
    workflow re-measured after the three fixes".
  - **FINAL RE-MEASUREMENT 2026-09-26 (after the §51 focus re-check): 20/20
    completed all five steps**, p50 1342.94 ms / p95 1787.70 ms (≤ 4000 ms target
    met). The halt set is empty. Recorded in `docs/benchmark_report.md` "Phase 14 —
    the realistic workflow after the section 51 focus re-check".
  - current status: **RESOLVED.** The residual `s2:FOCUS_MISMATCH` was
    sequence-specific: the standalone keyboard benchmark types into the *same* Qt
    field and was already 30/30, because it confirms focus through perception before
    the timed action, whereas the sequence's `s1` click focused the field and `s2`
    typed ~120 ms later while §51 read a §35 cache-served observation taken before
    the change. The fix is a bounded live re-check before a refusal, not a relaxed
    guard: a genuinely unfocused target is still refused
    (`test_the_focus_recheck_never_overrides_a_real_focus_refusal`).
  - next investigation: none — closed. The underlying inconsistency worth remembering
    (§35's 2 s element TTL can exceed §45's 1500 ms action-state-age ceiling) is now
    mitigated on the one path that acted on it, but the general TTL/age relationship
    is still worth keeping in view if a future action gates on a cached read.
- **pytest cannot run at all while `pytest-qt` is installed — PySide6 has no `QtTest`**
  - failure: every `python -m pytest ...` invocation ends in `INTERNALERROR` during
    `pytest_configure`, collecting zero tests; `main.py`, `ruff` and `mypy` are
    unaffected
  - environment: this host, venv `.venv` created with `--system-site-packages`;
    PySide6 6.10.3 from apt (`/usr/lib/python3/dist-packages/PySide6`) with only
    QtCore/QtGui/QtWidgets present; `pytest-qt` 4.5.0 in the venv
  - trigger: installing `pytest-qt` (a declared dev dependency, §27) at 20:27 on
    2026-09-24 by the interrupted Phase 12 session; the pristine plugin auto-detects
    PySide6, which imports successfully, then requires `QtTest`
  - observed result: `pytestqt/plugin.py:241 pytest_configure ->
    qt_api.set_qt_api(config.getini("qt_api")) -> set_qt_api: self.QtTest =
    _import_module("QtTest") -> getattr(PySide6, "QtTest") -> AttributeError:
    module 'PySide6' has no attribute 'QtTest'`
  - expected result: pytest starts and runs the suite
  - likely cause: `pytest-qt`'s auto-detection probes candidates in order and only
    falls back on `ImportError`; `PySide6/__init__.py`'s module `__getattr__` raises
    `AttributeError` instead, so the missing apt component (`python3-pyside6.qttest`)
    turns into a hard abort rather than a fallback to PyQt6 (whose `QtTest` *is*
    installed). Not a BLAXCY code defect: the same suite is `978 passed, 2 skipped`
    with `-p no:pytest-qt`
  - current status: **open, blocking the documented bare `pytest` command**; worked
    around for verification only. The operator chose the apt install, but
    `sudo -n apt-get install -y python3-pyside6.qttest` returned
    `sudo: a password is required`, so it is waiting on the human to run it.
    pyproject sets no `qt_api`, so the plugin's binding choice is automatic
  - **not a blocker for the GUI itself**: the §71 tests build their own offscreen
    `QApplication` in `tests/harness/gui.py` and never request the `qtbot` fixture,
    so the delivered GUI is fully verified without the plugin; only the *documented
    default command* is affected
  - next investigation: delete this entry once the documented command is green
    again, i.e. after `sudo apt-get install python3-pyside6.qttest` (6.10.3-3 — the
    exact version of the installed `python3-pyside6.qtcore`) and a bare `pytest`
    run. If that download is unwanted, the fallback is `pip install PySide6` into
    `.venv` (matches the documented setup and needs no sudo)
- **Intermittent flake — `tests/integration/test_clipboard_real_display.py::test_the_previous_content_is_served_during_the_restore_grace`**
  - environment: this host, X11 with `xfce4-clipman` running, real CLIPBOARD selection
  - trigger: a full-suite run on 2026-09-24 (observed roughly once in five; three
    subsequent full runs and every run of the file alone were green — `12 passed`)
  - observed result: `AssertionError: the previous content was never re-served: [...]`
    — the poll loop never saw the prior text within its 1.0 s window
  - expected result: the restore grace re-serves the previous clipboard content,
    which is what a clipboard manager captures
  - likely cause: a real-selection timing race — the test polls the live selection
    while xfce4-clipman also requests it and while the restore grace is only 1500 ms.
    Unrelated to this session's work: the clipboard code and its settings were
    untouched (only the batching/enablement paths changed)
  - current status: known flaky, not reproduced on demand; the mechanism is asserted
    deterministically by `tests/unit/test_clipboard.py` and by the other 11 tests in
    the same real-display file, which pass
  - next investigation: assert on `last_report` and the paster's own serve counters
    instead of wall-clock polling of a shared resource, so the property is tested
    without depending on the live clipboard manager's timing
- **§46 occlusion blocked *every* target on a real desktop — RESOLVED and verified
  live 2026-09-25** (found 2026-09-24 by the new §75 fixture-app end-to-end test;
  kept for the record because the unit tests could not see it, which is exactly why
  the fix now has a live regression test)
  - failure: on a live desktop, a target that resolves cleanly is still
    `actionable=False`, so no input can ever be injected. `assess_occlusion` skips
    only the target itself and invisible elements, and every caller passes
    `occluders=state.elements` (`control/executor.py` lines 589, 614, 733, 976;
    `core/perception.py` 304, 364, 375, 620; `core/speculative_perceiver.py` 401)
  - observed result (verified this session, read-only, no input injected):
    `'Applications'` (a real xfce4-panel control) -> `resolved=True
    actionable=False ratio=1.0 covering=2`, the two covering elements being
    `desktop[0]/5/0` (`xfdesktop`, 'Desktop', 0,0 1366x768, `visible=True`) and
    `desktop[0]/23/0` (maximized Google Chrome). `'Desktop'` and `'Terminal'`
    report `ratio=1.0` too. 146 elements were perceived
  - expected result: an element that is genuinely visible and unobstructed is
    actionable. The §46 rule exists to avoid clicking something *hidden behind*
    another window; the desktop background is behind everything, and a window that
    is behind the target does not hide it
  - likely cause: the resolver's contract says `occluders` are the "known objects
    **above** the target", but the callers pass the whole element list, so
    containers/ancestors (the target's own window, the desktop root) and windows
    *below* the target are all counted. (An earlier note here claimed
    `_NET_CLIENT_LIST_STACKING` was already used by `control/window_manager.py`;
    that was **wrong** — only `_NET_CLIENT_LIST`, which is in mapping order, not
    stacking order. The stacking read was added in the partial fix below.)
  - why it was invisible until now: the unit tests use synthetic elements with no
    full-screen background element, and `tests/integration/test_target_resolver_real_display.py`
    checks §43 ambiguity and §46 occlusion against geometry it constructs itself
  - **PARTIALLY FIXED 2026-09-24** (operator approved the fix before the Phase 14
    benchmarks). Delivered, all green (1076 passed; ruff + mypy clean, 169 files;
    7 new tests in `tests/unit/test_target_resolver.py`):
    - `WindowManager.title_stack()` reads `_NET_CLIENT_LIST_STACKING` and returns
      window titles **bottom to top**; an empty tuple means the order is *unknown*
      and is never treated as "nothing is above anything".
    - `UIElement.owner_window_title` is populated by the AT-SPI traversal from the
      window node it is under, and `ScreenState.window_stack` carries the order via
      `PerceptionOrchestrator._window_stack()`.
    - `occluders_above(target, candidates, stack)` narrows a caller's candidate list
      to windows **at or above** the target's, and is wired into both
      `TargetResolver.resolve` and `revalidate_target`. Unknown positions and
      `Rect`-only occluders are **kept**, so the check is never weaker than before.
      A window genuinely in front still blocks, and a duplicated window title is
      resolved conservatively (the topmost reading wins, so the occluder is kept).
    - Verified on the live desktop: the stack reads
      `('Desktop', 'QTermWidget', …, 'BLAXCY Test Fixture', 'xfce4-panel')` and the
      background/panel/terminal occluders are now correctly dropped.
  - **What still blocks, measured on the live desktop this session** (`'Movable
    Target'`: `resolved=True actionable=False ratio=1.0`):
    1. **The target's own window frame and the desktop root are counted as occluders
       of the target.** They are *ancestors* in the accessibility tree
       (`desktop[0]`, `desktop[0]/25/0`), which a path-prefix test would exclude
       exactly. `desktop[0]` also has no window title, so no stacking can place it.
    2. **Chrome's AT-SPI window name does not join its `_NET_WM_NAME`** — the browser
       publishes a decorated title to the window manager and a different one to
       accessibility (and the two differed between two runs in this session, because
       the media session prefix changes), so containment matching fails and the
       occluder is conservatively kept.
  - next steps (in order): (a) exclude the target's **ancestors** by `atspi_path`
    prefix — exact, structural, and safe; (b) attribute an element to its window by
    **pid** (`Accessible.get_process_id()` against `_NET_WM_PID`) instead of by
    title, which is exact where a title is a heuristic; (c) re-check the §75
    injection paths and then the Phase 14 benchmarks.
  - **RESOLVED 2026-09-25 (verified live this session, read-only, no input).** Both
    remaining steps were already implemented in the tree — the index was stale by one
    session (see the last "Session log" entries) and wrongly reported them pending:
    (a) the target's **ancestors** are excluded by an `atspi_path` *segment*-prefix
    test (`_is_ancestor_path`, wired into `occluders_above`), and (b) an element is
    attributed to its window by **pid** first (`owner_app_pid` vs the stack entry's
    `_NET_WM_PID`), with the title only as a tie-break. Inventory correction made
    while verifying: the stack reader is `WindowManager.stacked_windows()` — earlier
    entries (and `knowledge.md`) named a `title_stack()` that does not exist.
  - Evidence, the exact call every production caller makes (`occluders=state.elements`
    plus `state.window_stack`), against the live tree this session: `'Applications'`,
    `'Desktop'` and `'Terminal'` all now report
    `status=RESOLVED actionable=True ratio=0.0` (previously `actionable=False
    ratio=1.0`). Locked by the new live test
    `tests/integration/test_target_resolver_real_display.py::test_live_target_is_actionable_through_the_real_occlusion_path`.
  - Honest caveat worth knowing, recorded rather than hidden: at the **default**
    250 ms accessibility deadline the live traversal truncated at 41 elements with
    **zero** `owner_window_title`s (pid was present for 40/41), so at default settings
    the **pid** path is what actually makes §46 work; titles only matter on a deeper
    traversal (336 elements / 267 titles at a 2000 ms deadline). Ancestor exclusion by
    path applies regardless of the stack. No action needed — noted because the docs
    elsewhere implied titles were the mechanism.
  - The §75 injection paths and the real-desktop `run_sequence` benchmark are no
    longer blocked *by this defect*; they now need a real-input run (see "Next
    concrete action").

## Next concrete action
- **Phase 15 (2026-09-26): a soak runner exists and has been run** —
  `python -m bench.soak --confirm-real-input --iterations 30` (30/30 stable, flat
  latency and steady-state memory; see "Current task"). Next: the full
  environment/capability matrix, and a longer soak.
- **DONE 2026-09-26 — the realistic §74 workflow now completes 20/20** (operator
  approved the real-input runs). Intermediate run: 5/20 with the halt set narrowed to
  `s2:FOCUS_MISMATCH` ×15. After the §51 focus re-check: **20/20, p50 1342.94 ms /
  p95 1787.70 ms (≤ 4000 ms met)**; click and keyboard standalone both **30/30
  VERIFIED**. Recorded in `docs/benchmark_report.md`.
- **Phase 14 is COMPLETE.** Every §76 number is measured and in the §76 form
  (`target, actual, machine, desktop/session, backend, sample count`) in
  `docs/benchmark_report.md`, and the one row that was open (the realistic workflow
  happy path) is now met. A target stays a target until re-measured; the numbers here
  were all re-measured, never asserted.
- **Phase 14 is otherwise delivered.** Every other §76 number is measured and in the
  §76 form (`target, actual, machine, desktop/session, backend, sample count`) in
  `docs/benchmark_report.md`, including this session's third-workload run. A target
  stays a target until re-measured.
- **One human command is still outstanding** (not a code task), and it is the only
  thing blocking the *documented* bare `pytest` command:
  ```bash
  sudo apt-get install -y python3-pyside6.qttest
  ```
  then `python -m pytest -o addopts="" -q` should be green with no `-p no:pytest-qt`,
  and the "Known failures" entry above can be deleted.
- **The reconciled tree is still uncommitted.** Re-verified green this session
  (**1167 passed, 6 skipped**; ruff + mypy clean, 179 files). It is the natural next
  recovery checkpoint after `0b86d56`; committing needs explicit user confirmation
  (§11). (This line previously cited the stale 1121/174 figures.)
- (Superseded) **§76 verifiable workload: RUN and recorded (2026-09-25).** With the operator's
  go-ahead:
  ```bash
  . .venv/bin/activate && python -m bench.real_desktop --confirm-real-input --workload verifiable --samples 20 --input-samples 30 --sequence-samples 20
  ```
  Result: **20/20 five-step `run_sequence` runs completed**, p50 **2505.23 ms** /
  p95 **3671.94 ms** (n = 20, inside the `<= 4000 ms` target at p50/p95; one max
  4403.95 ms); click **30/30 VERIFIED**, p50 225.85 ms. The fixture's own counters
  (`bench_target_1: 52`, `bench_target_2..5: 22`) prove the input landed. Recorded in
  `docs/benchmark_report.md` "Phase 14". The earlier 0/20 halt stands as the
  *workflow-controls* result and is now explained rather than open: it was a workload
  limit, not a batching defect. Keyboard is `n = 0` with a reason on this workload
  (the verifiable layout hides the text fields by design).
- **Phase 14's read-only gaps are closed (this session):** the orchestrator's
  perceive cycle is measured warm (p50 10.61 ms) and cold (p50 830.78 ms), and the
  section 71 GUI refresh is measured (p50 0.39 ms) — each with its n and element
  count stated, in `docs/benchmark_report.md`. What remains unmeasured is only the
  workflow layout's keyboard/click pair (no targets on that layout; `n = 0` with a
  reason).
- **§46 and §43 are both resolved** (2026-09-25, see "Known failures"), and Phase 14's
  real-desktop numbers are recorded in `docs/benchmark_report.md`. The §75 injection
  paths pass (3/1). What remains genuinely open is not another benchmark run but a
  **decision**: the real-desktop 5-step `run_sequence` happy path and the §74 workflow
  both still halt at step 1 on the §60 verification limit (the fixture's controls
  change only sub-`MEANINGFUL`-ly at their own boxes) — either build a workload whose
  controls repaint `MEANINGFUL`-ly at their own regions, or accept the honest halt.
- **Phase 14 — benchmarks (§76)** is delivered for every number it was blocked on;
  all of them are recorded in the §76 form
  (`target, actual, machine, desktop/session, backend, sample count`) in
  `docs/benchmark_report.md`, and a target stays a target until re-measured.
- **One human command is still outstanding** (not a code task), and it is the only
  thing blocking the *documented* bare `pytest` command:
  ```bash
  sudo apt-get install -y python3-pyside6.qttest
  ```
  then `python -m pytest -o addopts="" -q` should be green with no `-p no:pytest-qt`,
  and the "Known failures" entry above can be deleted. `sudo -n` cannot do it
  unattended (the session has no password), which is why it is recorded rather than
  done. If the download is unwanted, `pip install PySide6` into `.venv` is the
  no-sudo fallback and matches the documented setup.
- Keep the invariant: no physical input without policy -> resolve -> lease ->
  revalidate -> execute -> verify, for every step, batched or not; the GUI reaches
  the desktop only through the same `dispatch`/`set_mode`/
  `trigger_emergency_stop` entry points the Brain uses; and the installer touches
  only the file list, the launcher, the icon and the entry.
- **Phases 12 and 13 are done; the bullets that follow are superseded forward plans
  kept only so the record of what was expected is not silently rewritten.**
- (Superseded detail from the bootstrap, kept for the record) Evidence gathered so
  the QtTest choice was not a guess:
  - `apt-cache policy python3-pyside6.qttest` -> candidate **6.10.3-3**, i.e. the
    *exact* version of the already-installed `python3-pyside6.qtcore` (6.10.3-3).
    This is the smallest change that keeps one PySide6 source of truth.
  - `pip index versions PySide6` -> 6.10.3 and 6.11.2 available for Python 3.14,
    so a venv-local install also works and needs no sudo, but it would place a
    second Qt next to the apt one.
  - No test currently uses `qtbot`/`qapp` (verified by grep), so the plugin is not
    yet needed by anything — which is what makes deferring it a real option too.
  Options, in the order I would try them: (a) install `python3-pyside6.qttest` via
  apt; (b) `pip install PySide6` into `.venv` (matches the documented setup
  `pip install -r requirements.txt`); (c) leave it and run the gate as
  `-p no:pytest-qt` until the first `qtbot` test exists.
- **Then finish Phase 12's GUI as a view over the Body** (§71): `gui/app.py`
  (composition entry point that builds one `BlaxcyApplication`, hands it a real
  confirmation callback, shows the window, shuts the Body down on exit),
  `gui/main_window.py`, `gui/status_panel.py` (including the §66.1
  sequence-progress indicator), `gui/capability_panel.py`, `gui/action_log.py`
  (the event bus rendered as a bounded log), `gui/confirmation.py` (§56's dialog
  plus the thread bridge that lets a blocked executor thread ask the GUI thread),
  `gui/emergency_stop_ui.py` (§63's stop, its shortcut and the re-arm control).
  Thread-safety rule from §71: only the GUI thread touches Qt widgets.
- Then Phase 13 (installation, §73) and Phase 14 (benchmarks — §76, which still
  owes the real-desktop end-to-end `run_sequence` numbers, the Phase 8 modules
  resolution/lease/click, and now the orchestrator's perceive cycle).
- Keep the invariant: no physical input without policy -> resolve -> lease ->
  revalidate -> execute -> verify, for every step, batched or not, and the GUI
  must reach the desktop only through the same `dispatch`/`set_mode`/
  `trigger_emergency_stop` entry points the Brain uses.

## Architectural decisions made so far
- Python 3.14.6 is installed; spec targets 3.12+, so this is satisfied.
- venv created with `--system-site-packages` so GI/AT-SPI resolve (§27).
- Capability probes perform **no physical input** (§28 rule 12).
- Every capability reports an honest verdict with evidence rather than a guess:
  `sequence_execution` reports its real configured state and flags, and
  `visual_grounding` reports the switch, the SDK and the keyring state through
  `VisualGrounder.capability()` (one source of truth, no network call).
- **AT-SPI has exactly one owner thread** (`T-A11Y`); the browser layer consumes the
  accessibility service's observations rather than calling AT-SPI itself, so there is
  never a second accessibility owner (§35).
- **Credential content is never read by perception**: password elements carry no text,
  and only navigation fields have editable text read (§42/§55).
- Batching layer features (`resolver_cache.enabled`, `sequence.enabled`,
  `capture_boost`, `speculative_perception`) are **enabled by default** now that the
  Phase 9 gate and the §66.1/§75 test list have passed (§83/§84). They remain
  configuration switches; disabling one removes capability, never a check, and the
  `resolver_cache` off-path is asserted decision-identical.
- **A hint prefetcher must not be able to claim it is idle while still running**
  (§33.2/§64): `SpeculativePerceiver.wait_idle` waits for the *running* job as well
  as the queue (`_running`, cleared only when the resolution finishes), because the
  pending slot is cleared before the work starts — the old version could return
  before the job completed, and its timeout fallback then claimed success it had
  not earned. Completed-but-unconsumed hints do not count as busy.
- **Retained hints are bounded** (§32): at most `MAX_RETAINED_HINTS` (4) completed
  hints are kept, oldest-first. The design is one step of lookahead, so real use
  holds one; the cap only stops a caller that never consumes hints from growing an
  unbounded result map.
- **The cache owns the clock its TTL is measured against** (§43.1/§33.2):
  `ResolverCache.prime` stamps `recorded_at` on insert exactly as `record` does, so
  a caller — the §33.2 adapter, which has no clock of its own — cannot prime a hint
  that is born expired. A primed hint is still only a traversal-order hint and is
  re-verified live; the stamp is about being *usable*, not about trust.
- **The §76 speculation-usefulness rate is measured, never assumed**: the runner
  counts a taken hint as useful only when that step's own live resolution consumed
  it (a resolver-cache HIT). `SpeculativePerceiver.mark_useful` touches a counter
  only — it cannot change a decision, a position or a safety check.
- **`run_sequence` is a container, not a second pipeline** (§66.1): a step is
  dispatched through the same `ToolDispatcher` a standalone call uses, so it cannot
  drift from the standalone path, and no coordinate, `element_id` or lease ever
  travels between steps.
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
- **Policy is a value layer, never an input layer** (§13): `policy/*` reads state
  and returns verdicts; it never injects. A policy bug can therefore refuse work
  but can never authorise a bypass of the executor's lease/revalidation checks.
- **The permission order is fixed and fail-closed** (§13/§54-§58): blocked app →
  terminal → credential → calibration → capability → mode/class → confirmation.
  `run_sequence` must be given an explicit gate class (it has none of its own,
  §57). Destructive actions always require confirmation in every mode, and that
  is hard-coded rather than configurable (a switch would be a way to disable it
  later).
- **OBSERVE permits only non-injecting work**: READ_ONLY, plus NAVIGATIONAL that
  does not inject (window focus). Scroll is refused because it moves the pointer
  (§32.1/§56).
- **The AUTONOMOUS ceiling is self-enforcing** (§56): reading the mode latches an
  expired session back to OBSERVE and emits `MODE_CHANGED`; the deadline is on the
  monotonic clock, so a wall-clock step cannot extend autonomy.
- **The executor is the only caller of the input layer** (§13/§44/§45/§59): it
  issues the lease from the resolution observation, runs the full §45 revalidation
  against live state, then checks the abort hook *immediately* before injecting.
  There is no path from a Brain request to `mouse.click` that skips a step.
- **Read-only tools never enter the executor** (§32.1): they are dispatched on the
  read path, so the single-writer serialisation only ever covers work that really
  needs it.
- **Verification never flatters the result** (§60): positive evidence is required
  for `VERIFIED`, absent evidence is `UNVERIFIED`, and only positive *contradicting*
  evidence is `CONTRADICTED`. A required-and-unsuccessful verification is reported
  as a failed envelope.
- **Credential content is used but never recorded** (§42/§55/§70): a password is
  typed (that is the intended interaction) but its text appears in no envelope,
  event, log or report; the envelope carries `sensitive: true` and the outcome is
  honestly `UNVERIFIED` because the field is never read back.
- **Section 47 activation is verified, not assumed**: `ensure_active` reads the
  active window back with a bounded wait and reports `False` when the request did
  not take effect.
- **The emergency stop latches before it cleans up and never queues behind the
  work it is stopping** (§63): the flag is set first, and the release path takes
  no lock the running action holds. Every releaser is drained even if one raises,
  because a held button surviving the stop is the failure that hurts. Re-arming is
  explicit (`reset`); nothing times out into "running again".
- **Takeover is a latch, not a pause button** (§20/§62): resuming is explicit,
  invalidates every lease and re-perceives; it does not restore the previous
  policy mode and never replays an interrupted plan.
- **Recovery is bounded by policy, not by hope** (§4 rule 21/§21/§61): only TARGET
  and TRANSIENT failures are candidates; anything that already injected input, any
  destructive action, any safety refusal, any unclassifiable failure and any
  target-identity change is refused outright; budgets are per tool *and* per task
  step, and the loop guard stops a repeated call. Every decision travels in the
  envelope, so the Brain can see why nothing was retried.
- **The stop is checked twice** (§59/§63): once at the top of the pipeline so a
  latched stop does no work at all, and once immediately before injection so no
  physical input can escape it.
- **The watchdog is evidence, not a safety gate** (§64): losing the heartbeat file
  never takes the process down, a crash deliberately leaves the record dirty, and
  BLAXCY does not claim credit for the X server releasing a dead client's input.
- **The clipboard is borrowed and handed back, never taken** (§50):
  `X11ClipboardPaster` captures the previous content and owner *before* taking the
  selection, keeps serving its own text for `paste_grace_ms` (an X selection is
  read only after the paste keystroke -- measured: zero grace means zero requests
  served, i.e. the paste silently does nothing), serves the previous content for
  `restore_grace_ms` so a clipboard manager keeps it, then hands ownership back.
  Re-owning the previous window does *not* make that client serve again
  (verified), which is why the grace window is what actually preserves the user's
  content. A `SelectionClear` stops the paster; it never re-claims the selection.
- **Credential text never touches the clipboard** (§42/§55): the clipboard is
  readable by every application and clipboard managers persist it, so
  `KeyboardController.type_text(..., sensitive=True)` removes the clipboard path
  entirely and a credential the keyboard cannot produce is refused with
  `UNICODE_UNSUPPORTED`. The executor derives `sensitive` from
  `element.is_password`. This closed a real leak that wiring a paster would
  otherwise have opened.
- **Capability probes do not disturb shared resources**: the clipboard probe
  verifies the connection, atoms and owner window and reports
  `takes_ownership: false`, because testing the clipboard by stealing the user's
  clipboard would be a side effect on something BLAXCY does not own.
- **The tool schema is the contract** (§66): an argument is accepted only if the
  tool's own declared schema declares it. That one rule enforces two different
  requirements without a special case — action tools cannot receive a
  coordinate/lease (`describe_region` legitimately declares `x`/`y`, so it can),
  and no tool can receive an argument BLAXCY does not implement. Nothing is ever
  "partially applied".
- **Confirmation is never a tool argument**: an argument named `confirmed` is
  rejected as unsupported, and `AgentLoop` obtains a real answer from an injected
  human prompt (or halts with `CONFIRMATION_DENIED`). There is no code path where
  the Brain confirms its own destructive action.
- **Read-only dispatch is a separate, bounded path** (§32.1): read-only tools
  never enter the executor, and the read path's concurrency is capped by
  `max_concurrent_readonly_dispatch` with a bounded wait (a saturated path returns
  `RATE_LIMITED`, never an unbounded queue). A read is still preemptable by a
  latched stop, and a snapshot it returns is stamped like any other state — a
  later action revalidates against live state regardless.
- **`run_sequence` is honest before it is available**: the dispatcher parses and
  validates a plan (including "`halt_on` may only narrow within the safety set"),
  enforces the step limit, and then reports `BACKEND_UNAVAILABLE` while no runner
  exists or the feature is disabled. It never reports a plan as executed, and a
  test asserts no input happened on that path.
- **The Brain loop is manual and bounded** (§67): BLAXCY walks the tool calls
  itself (the SDK's `automatic_function_calling` is disabled, so the model can
  never call a BLAXCY function directly), with turn and wall-clock ceilings, a
  cancellation token checked before every model turn and every tool call, and no
  indefinite retry.
- **Context is delta-oriented and privacy-aware** (§68/§68.1): an unchanged
  `state_version` is sent as a marker instead of a repeated screen; older turns
  are condensed by a deterministic summary rather than another model call; and a
  protected active application suppresses **all** attached element content.
- **The credential has exactly one owner** (§69): `security/keyring_manager.py` is
  the only caller of `keyring`, the key is never a config value, an argument, a
  log line, an event or a tool envelope, and third-party error strings are
  redacted against it before becoming a `BrainError`.
- **Visual grounding is the last cascade stage and is gated in code, twice**
  (§41/§42): `VisualGrounder.ground` refuses in OBSERVE *before touching the
  backend*, and `policy.guards.check_visual_fallback` composes the same decision
  (blocked → protected → disabled → mode) for a caller that wants the verdict as
  a value. A guard that could be bypassed by calling the grounder directly would
  not be a guard, so the grounder does not rely on the caller having asked.
- **The pixel-upload boundary is checked before the image exists** (§42/§55): the
  upload region is tested against credential elements and protected regions
  *before* any crop, downscale or encode. A geometry-less credential element is
  treated as unknown coverage and refused (an upload that cannot be proven clean
  must not happen), and cross-space geometry is refused rather than converted
  (§31). No image, and no part of one, appears in an error, a log or a report.
- **A visual result is a location, never an authorisation** (§38/§41): matches
  become elements with `source=VISUAL`, DESKTOP geometry, `clickable=True` (the
  §38 last-resort clickability source) and `role=UNKNOWN` unless a caller passes
  its own role hint. They then travel the ordinary lease → revalidate → verify
  path; the grounder injects nothing, leases nothing and verifies nothing, and
  `as_elements` refuses outright to emit a credential element.
- **Visual grounding never picks a winner** (§43): two same-label matches remain
  two elements, and the resolver's absolute ambiguity rule makes them `AMBIGUOUS`
  with `best=None` — asserted end-to-end through the real resolver, so a visual
  fallback can never out-vote deterministic ambiguity.
- **The §41 confidence ceiling is a bound, not a default**: `VisualSettings`
  declares `max_confidence` with `le=0.80`, so a config can lower the ceiling and
  cannot raise it; the grounder additionally clamps to the module's
  `VISUAL_CONFIDENCE_CAP`, and confidence is always derived from
  `perception_confidence(VISUAL, …)` rather than taken from the model's own
  self-report (the model's number is only the `source_consistency` multiplier).
- **A malformed model answer is a failure, not an empty success** (§80): an
  absent or unparseable response raises `MODEL_ERROR`, because "the model was
  broken" and "the control is not present" must not be the same observation.
- **The one remote model call is bounded and its answer is space-safe**: the
  request carries `response_json_schema` and an `HttpOptions.timeout`, the model
  answers in *normalized* fractions, and those are clipped to the unit square and
  mapped onto the DESKTOP rect the image actually represented — so a downscale
  cannot shift a target and an out-of-range value cannot land outside the image
  the model was shown.

## Migration status
- Config `schema_version = 1`; no migrations registered yet (registry exists in
  `config/migration.py`).

## Open TODOs
- ~~**§46 occlusion needs a decision (found by the new §75 suite)**~~ —
  **RESOLVED 2026-09-25**, verified live: ancestor exclusion by `atspi_path` plus
  pid-based window attribution make real targets actionable again, and it is locked
  by a live integration test. See the "Known failures" entry for the evidence. The
  only remaining §46-adjacent work is step (c) — a **real-input** re-run of the §75
  injection paths — which is an explicit opt-in, not a code change.
- **§75 is now a real suite but only partly runnable here**:
  `tests/e2e/test_fixture_confirmation_e2e.py` verifies the real dialog's deny path
  end to end; its three injection paths need an unoccluded target, which this host
  cannot currently provide. It also needs an opt-in env var and a run of its own
  (one `QApplication` per process).
- ~~§84 Phase 13: installation (§73) and the §85 install criteria~~ — **delivered**
  (the installer, the three scripts, the desktop assets, and single-instance
  control; `docs/installation.md`). What remains on this front is not code: no
  system-wide install was performed (the verified installs use throwaway prefixes),
  and the `--system-deps` package-manager step is verified as *reporting* rather
  than as an install (no unattended sudo on this host).
- ~~§84 Phase 12: the GUI including the §71 sequence-progress indicator~~ —
  **delivered** (all seven `gui/` modules, the sequence-progress indicator, the
  confirmation dialog + bridge, and self-exclusion). Phase 13 (installation) was
  next and is now also delivered.
- §84 Phase 10.1: **delivered and enabled by default** (see the
  Completed-milestones entry). The layer is on because the §66.1/§75 test list is
  green and the operator chose to enable it; each flag remains a switch.
- ~~§84 Phase 11: the grounder has no caller~~ — **closed**. `PerceptionOrchestrator`
  owns a `Frame` source (§41's requirement) and `BlaxcyApplication._resolve_fallback`
  is passed to both the executor and the dispatcher, so OCR and visual grounding are
  reachable in the running program and only on a deterministic miss. What remains is
  a *live* end-to-end test of that fallback path (the tests use fakes) and the §76
  benchmark for the perceive cycle.
- ~~Application composition root still absent~~ — **delivered** (`core/application.py`
  + `core/perception.py`, §8/§12 composition, with `main.py status` and `main.py
  run` as the reachable entry points). The stop's
  `register_input`/`register_cancel`/`register_reperceive` hooks,
  `combine_abort_checks` and `AgentLoop.cancel` are wired there, so a registered
  stop also stops the Brain loop and invalidates leases (takeover re-perceives).
  What is still absent is the **GUI**: `gui/` has no modules yet (Phase 12, second
  half), so the composed Body is reachable from the CLI and tests but not yet from
  a window.
- The clipboard paster is real but **nothing in the application constructs it
  yet** (same missing composition root), so long or non-ASCII typing currently
  reaches `UNICODE_UNSUPPORTED` in the running program even though the mechanism
  works and is tested. The wiring is one call:
  `KeyboardController(backend, input_settings, clipboard=select_clipboard_paster(config.clipboard))`.
- `RecoveryController.begin_task`/`begin_step` are never called by anything yet.
  The Brain dispatcher now exists, but per-task/per-step budget scoping belongs
  with the §66.1 sequence runner (Phase 10.1), where a step is a real unit of
  work; until then the loop guard is the binding constraint, which is why it is
  asserted.
- ~~§66/§67 tool dispatch is not built yet~~ — **delivered**
  (`ai/tool_protocol.py` + `ai/brain_adapter.py`, with 160 new tests).
- ~~`run_sequence` is parsed, validated and dispatched but not executable~~ —
  **delivered**: `control/sequence_runner.py` is the runner the dispatcher walks,
  with the per-step pipeline, the halt conditions and the `NOT_EXECUTED` tail. In
  the running program it is still unreachable because no composition root attaches
  it (see the composition-root item above); the dispatcher keeps reporting an
  honest `BACKEND_UNAVAILABLE` until one does.
- ~~`activate_element` has no implementation anywhere~~ — **DELIVERED 2026-09-25**
  (see "Current task"): `core/accessibility.py` now invokes the application's own
  AT-SPI action, the executor reaches it through the full pipeline, and it is
  verified live on the §74 fixture. Every §66 tool now has a real implementation.
- ~~§50 clipboard-assisted typing needs a real `control/clipboard.py`
  `ClipboardPaster`~~ — **delivered** (`control/clipboard.py`, verified live);
  what remains is only the composition-root wiring noted above.
- §50's paste path has no end-to-end test through a *real* application's paste
  handler: that would mean injecting Ctrl+V into the live desktop, which the test
  suite deliberately never does outside the fixture-app harness. It belongs in the
  §75 fixture-app e2e suite once that harness can take real input. The unit tests
  cover the controller-to-paster contract and the integration tests cover the real
  selection transfer, so the untested seam is exactly the application's own paste.
- ~~§43 ambiguity **scope**~~ — **FIXED 2026-09-25** on explicit operator decision
  (see "Known failures" for the real cause and evidence). The absolute and gap
  rules are now judged over the candidates competitive with a *decisive* text match,
  so role-only ties and unrelated duplicates can no longer veto a unique,
  decisively-named target. Five new tests in `tests/unit/test_target_resolver.py`
  cover the positive, negative, bypass and conservatism-preserved paths.
- Full reproducibility: consider committing the current tree (Phases 2–3) for recovery
  checkpoints (needs user confirmation, §11).

## Verification status snapshot
- Build: N/A (interpreted Python; no build step yet)
- Lint/typecheck: PASS (`ruff check .` clean; `mypy .` clean, **179 files**)
- `activate_element` (§66): PASS — 13 unit tests (`tests/unit/test_activate_element.py`:
  action selection, live path descent, interface-then-fallback invocation, the
  executor's unavailable/absent-target/refused-action paths, the assertion that no
  input is injected, and OBSERVE refusing before invocation) plus the live
  AT-SPI activation test above.
- Logging (§70) and redaction (§85): PASS — 21 new tests in
  `tests/unit/test_redaction.py` and `tests/unit/test_logging_setup.py`, including
  the end-to-end property that a **registered secret and a password-named field
  never appear in the log file**, that the rotation bounds come from `[logging]`,
  and that a config cannot disable log redaction (it is now a §25 invariant).
  Verified live through the CLI (see "Last test results").
- Focus-staleness fix (§35 vs §45/§51): PASS —
  `tests/unit/test_accessibility.py::test_state_events_that_gate_input_are_subscribed`
  asserts the backend registers `object:state-changed:focused`/`:enabled`/
  `:sensitive`/`:editable`, the events the element cache must see for §51's focus
  guard and §45's enabled/editability checks to act on current state (the file now
  collects **26**). It is a *registration-list* lock by necessity: `ScriptedBackend`
  returns a bool from `poll_events`, so it cannot distinguish which event types are
  subscribed — no fake could have caught this, which is why it survived to the live
  workflow benchmark.
- Typed-field read lag and result-click verification (sections 60, 76): PASS —
  `test_perception.py` +2 (`perceive(force=True)` bypasses the element cache, and
  sees a change the cache is still serving), `test_executor.py` +3 (a negative
  verdict is re-checked against a live observation and becomes VERIFIED; a VERIFIED
  verdict is never re-checked; live reads that keep disagreeing are still
  CONTRADICTED, so nothing is softened), `test_verifier.py` +2 (the selection
  postcondition, and that unreported/negative selection is not a verdict either
  way), and the live
  `test_workflow_controls_real_display.py::test_a_result_selection_is_observable_as_positive_evidence`
  (real Qt list: `Result 1` reports `selected=True`, `Result 2` `selected=False`).
- Unit tests: PASS (incl. accessibility 21 + browser accessibility 12; event bus 12 +
  state cache 21; OCR 20 incl. one real-backend functional test; target resolver 26;
  mouse 16 + keyboard 23 + input backends 8; policy modes 9 + terminal guard 39 +
  guards 19 + permissions 26; action tracker 11 + verifier 17 + executor 33 (30 +
  the three §51 focus re-check tests added 2026-09-26) +
  window manager 6; emergency stop 13 + takeover 7 + recovery 21 + watchdog 18;
  clipboard 10; brain adapter 19 + gemini adapter 20 + tool protocol 26 +
  tool dispatch 28 + sequence dispatch 11 + context manager 16 + keyring 9 +
  main keys 4; sequence runner 21 + resolver cache 22 + speculative perceiver
  9; **visual grounder 46**; policy guards 25 (19 + 6 visual-fallback);
  **single instance 12 + installer helpers 20 (new this session)**)
- Integration tests: PASS (real-display calibration 5/5; frame engine 4/4;
  accessibility 4/4; input/XTest 4/4; resolver-over-live-AT-SPI **9/9** with 1 honest
  conditional skip (now including the §46 live actionability regression lock); window manager/EWMH 4/4; emergency stop over the real backend
  4/4; **clipboard over the real selection 11/11**; fixture app via harness;
  1 opt-in skip)
- Safety tests: PASS (`tests/safety/` — **77 tests**; the Phase 8 set covering
  OBSERVE input blocking, destructive confirmation, credential privacy in the
  envelope, terminal submission, blocked applications, unavailable capabilities,
  stale/ambiguous targets and ownership release, plus the Phase 9 wired gate:
  stop before/mid-action/short-circuit, real input release, stop-before-takeover
  ordering, a recovery retry that succeeds, and every recovery refusal path)
- Sequence/batching tests (§66.1, §75): PASS (runtime, not just schema) —
  `test_sequence_runner.py` 21, `test_resolver_cache.py` 22,
  `tests/safety/test_sequence_halts.py` 11, `test_sequence_dispatch.py` 11,
  `test_speculative_perceiver.py` 9; **perception orchestrator 18 + application
  composition root 21 (40 cases, new this session)**; the §74 five-control workflow runs end to
  end with every step verified; the halt / fresh-confirmation / stop / takeover /
  credential / blocked-app paths are each asserted identically for a batched step
  and a standalone one; and the **background** speculation worker (the path the
  default-on flag actually  runs) is covered for non-blocking, discard and no-orphan-thread behaviour
- GUI tests (§71): PASS — 52 cases. `tests/unit/test_gui_confirmation.py` 18
  (countdown floor, no implicit Enter, every denial path, stop/takeover
  preemption, credential content never rendered), `tests/unit/test_gui_panels.py`
  18 (honest `UNKNOWN` rendering, sequence-progress running/halted/complete, the
  bounded bus log and its cross-thread marshalling, the stop's own result
  reporting), `tests/unit/test_gui_app.py` 14 (self-exclusion through the real
  guard, the window showing the Body's mode rather than the request, no second
  dispatch path), and `tests/integration/test_gui_real_body.py` 2 over the real
  assembled Body
- Installer tests (§73, §85): PASS — 39 cases. `tests/unit/test_single_instance.py`
  12 (acquire/release round-trip and idempotence, a second acquisition refused and
  naming the holder, an actionable refusal message, the lock file's contents,
  fail-closed on an unusable path, the XDG default path and its uid fallback, a
  bare-pid description, a corrupt lock file, and **a real child process whose death
  releases the lock**), `tests/unit/test_installer.py` 20 (version read and its
  `unknown` fallback, checksum against `hashlib`, the explicit file list and its
  exclusions, determinism, cache pruning, the launcher forwarding `"$@"`, the
  desktop-entry rewrite rules + comment stripping + the validator's own rules, a
  missing-`Exec` hard error, package-manager detection, system prefixes, the default
  prefix, the four install paths, and every preflight refusal), and
  `tests/integration/test_install_scripts.py` 7 (the scripts are executable, a dry
  run changes nothing, **rollback on an injected real failure**, a full
  install -> launch -> update -> uninstall with per-file checksum verification,
  `update` refusing a never-installed prefix, uninstall on a clean prefix, and a
  foreign desktop entry backed up then handed back). It installs nothing
  system-wide and needs no network.
- End-to-end GUI confirmation (§75, opt-in real desktop): **1 passed, 3 skipped**.
  `tests/e2e/test_fixture_confirmation_e2e.py` launches the real §74 fixture on the
  real display, assembles the real Body through the production composition path with
  the real `XtestBackend` wrapped in a recording proxy, and answers the real
  `ConfirmationDialog` from inside its own event loop. The deny path passed: the
  dialog appeared, its countdown floor was observed closed, Cancel was really
  pressed, the bridge recorded the denial, the envelope was `CONFIRMATION_DENIED`,
  and **nothing was injected**. The three paths that need injection skip, naming the
  §46 occlusion defect above as the reason.
- Menu/desktop verification (live, this session): `desktop-file-validate` accepts
  the installed entry (exit 0); the installed launcher runs the installed copy and
  prints the real session report (exit 0); the manifest records 78 files with
  sha256 each; a second launch is refused with the holder named and exit 2, and a
  launch after the first dies succeeds (no stale lock).

- Workflow workload and the Qt text-field fixes: PASS — the new **live**
  `tests/integration/test_workflow_controls_real_display.py` (3) drives the real
  AT-SPI tree and proves a Qt editable field is a readable `TEXT_INPUT`, the
  clickable controls are perceivable `BUTTON`s, and the results are named
  `LIST_ITEM`s with geometry; the §43 weak-signal split is covered by 3 new
  `tests/unit/test_target_resolver.py` test functions (decisive-vs-fuzzy, fuzzy
  duplicate, fuzzy-only conservatism; the file collects **46**, the index's older
  `26` was stale) and the Qt role/read fixes by 3 new
  `tests/unit/test_accessibility.py` test functions (the file collects **25**, up
  from the `21` recorded); `tests/unit/test_bench_real_desktop.py` collects **9** and
  `tests/integration/test_bench_workload_fixture.py` collects **11**. **The realistic
  5-step workflow now completes 20/20** (p50 1342.94 ms, ≤ 4000 ms met) after the
  §51 focus re-check — see "Current task".
- Benchmark: PASS/COMPLETE (§76 met — Phase 2 capture + change detection, Phase 5
  OCR, the
  Phase 9 stop latency, the Phase 10 tool-protocol overhead and the **Phase 10.1
  batching measurements** are recorded in `docs/benchmark_report.md`; Phase 11's
  grounder is deliberately **unbenchmarked** because every one of its costs is a
  network round trip to a remote model, and this session had no key and made no
  model call — inventing a number would be exactly the fake benchmark §4 rule 8
  forbids; accessibility
  probe latency measured live (251.2 ms first traversal) but not yet an n>=200
  benchmark; the Phase 8 modules — resolution, lease validation, click — are now
  benchmarked on the real desktop; **the real-desktop end-to-end numbers are RUN**
  (2026-09-25): accessibility / resolution / revalidation / click / keyboard, and a
  real-desktop `run_sequence` that honestly halts at step 1 on
  `VERIFICATION_UNVERIFIED` rather than faking a happy path — see
  `docs/benchmark_report.md` "Phase 14". The real-desktop 5-step happy path **is
  now met by both workloads**: the synthetic `verifiable` at **20/20**, p50
  2505.23 ms, and the *realistic* §74 pipeline (`workflow-verifiable`) at **20/20**,
  p50 1342.94 ms / p95 1787.70 ms, after the §51 focus re-check. **This session added the
  orchestrator's perceive cycle (warm p50 7.39 ms / cold p50 640.65 ms) and the
  section 71 GUI refresh (p50 0.36 ms) on the workflow layout**; the one row that
  remains genuinely inapplicable on the *older* `verifiable` layout is its
  keyboard/click pair, reported `n = 0` with a reason, and the `workflow-verifiable`
  layout now supplies the keyboard number instead (`1/30 VERIFIED`, honestly
  reported))

- **Real-desktop `run_sequence` happy path is unachievable on the section 74 fixture —
  §60 verification refuses sub-`MEANINGFUL` changes** (found 2026-09-25 while running
  the Phase 14 real-input benchmarks; **open — needs a decision, not a code fix**)
  - failure: every real-desktop 5-step `run_sequence` sample halts at step 1 with
    `VERIFICATION_UNVERIFIED`; an isolated click/keyboard is `UNVERIFIED` in 45/50 and
    47/50 samples respectively
  - environment: this host, XFCE X11, real XTEST, the section 74 fixture as target
  - trigger: a mutating step (`click`/`type_text`) whose user-visible change is below
    the §34 `MEANINGFUL` threshold, with `require_verification_for_mutating = true`
    (the default) and §66.1's halt-on-`UNVERIFIED`
  - observed result: click p50 220 ms (5/50 VERIFIED); keyboard p50 113 ms (3/50
    VERIFIED); sequence p50 99 ms (0/20 completed, `s1:VERIFICATION_UNVERIFIED`)
  - expected result: (for a *happy path*) all five steps verified — section 76 asks for
    that number
  - likely cause: **not a defect — §60 is behaving correctly.** The fixture's buttons
    are small (a click changes focus/pixels only → `TRIVIAL`/`ANIMATION`), its text
    fields are small and are not read back except navigation fields
    (`verify_text` needs `observed.text`), and `verify_screen_change` needs a
    `MEANINGFUL` change overlapping the target. The input genuinely lands (the
    fixture's own click counters confirm it), so this is a verification/workload
    limit, not a lost action.
  - current status: **open**; numbers and interpretation are in
    `docs/benchmark_report.md` "Phase 14". No safety guarantee is weakened.
  - next: decide whether to (a) accept the honest halt as the §76 result, or
    (b) build a benchmark workload whose controls produce `MEANINGFUL` changes at
    their own boxes (a large control that repaints at its own region), then
    re-measure. `bench/real_desktop.py` already parameterises the plan.
- **§43 ambiguity scope was over-broad — RESOLVED 2026-09-25** (found 2026-09-23;
  re-confirmed by the Phase 14 workflow benchmark and then fixed on explicit
  operator decision)
  - failure: the section 74 workflow (`Search` → type → `Submit` → …) halted at step 1
    with `TARGET_AMBIGUOUS` on `{"target": "Search", "role": "BUTTON"}`
  - observed result before the fix: the fixture's decisive `"Search"` scored **0.990**
    yet the result was `AMBIGUOUS` — the *real cause* (reproduced live with the
    fixture running) was the **absolute duplicate rule firing on unrelated elements
    elsewhere on the desktop**: two same-named `"Button Alpha"` buttons (plus other
    duplicated panel/toolbar controls) share a `(label, role)` key and vetoed the
    winner, even though none of them matched the requested text. It failed closed, so
    it was safe, but wrong.
  - fix: the absolute and gap rules are now judged over the candidates competitive
    with a **decisive** text match (`core/target_resolver.py::_check_ambiguity` +
    `DECISIVE_TEXT_FLOOR = 0.80`). A candidate that matched only the role is a weaker
    cascade stage and cannot veto a real text match; unrelated duplicates elsewhere
    likewise cannot. A role-only match scores at most 0.60 and a decisive text match
    at least 0.94, so the winner is unchanged. A text-less query, or one whose only
    matches are weak substring/fuzzy hits, judges every candidate **exactly as
    before**, and two genuine matches of the requested text remain `AMBIGUOUS`.
  - evidence: live `resolve(text="Search", role_hint=BUTTON)` is now `RESOLVED` at
    0.990 (fixture running); the §74 workflow now gets past `w1` and halts on the §60
    verification limit instead. Five new tests (positive / negative / bypass /
    weak-match / conservatism-preserved); full gate **1089 passed, 6 skipped**.
  - current status: **RESOLVED**; no safety guarantee weakened (fails closed as
    before for every genuinely ambiguous case).

## Environment facts (verified this session)
- Host: Kali GNU/Linux Rolling, kernel 7.1.5+kali-amd64, XFCE, X11 (`DISPLAY=:0.0`)
- Python 3.14.6; venv at `.venv` created with `--system-site-packages`
- XTEST 2.2 confirmed live via python-xlib; AT-SPI bus present and **perception
  functionally verified this session** (live traversal produced AT-SPI elements);
  XDG portal present
- Missing binaries (not required yet): `wmctrl`, `ydotool`. The installer's
  preflight reports `wmctrl` as a DEGRADED (non-blocking) capability; `ydotool` is
  not referenced by the installer at all (the input layer has no ydotool path).
- Installer tooling verified present this session: `desktop-file-validate`
  (`/usr/bin`), `update-desktop-database`, `gtk-update-icon-cache`,
  `xdg-desktop-menu`, `sha256sum`, `apt-get`. **`sudo -n` fails** ("a password is
  required"), so `--system-deps` and a system-wide install could not be run.
- OCR stack verified this session: Tesseract 5.5.0 (`/usr/bin/tesseract`),
  `pytesseract` 0.3.13 (`image_to_data`/`Output.DICT` confirmed), Pillow 12.3.0
- Versions verified this session: `rapidfuzz`, `xxhash`, `numpy`, `mss` 10.2.0,
  `opencv-python-headless` 5.0.0 all import successfully
- `google-genai` 2.20.0 surface verified this session (§82) before writing against
  it: `GenerateContentConfig.response_json_schema`, `.response_mime_type` and
  `.http_options`, `HttpOptions.timeout` (milliseconds), `Part.from_bytes(data=…,
  mime_type=…)`, and `Content(role=…, parts=[…])` all exist and accept the shapes
  the visual grounder sends. No model call was made and no key is stored, so
  visual grounding is honestly `UNAVAILABLE` on this host
- `Atspi.Action.get_action_name` emits a DeprecationWarning on this host (the call
  still works); noted, not yet actioned
- XTEST 2.2 present; the input layer's functional probe reports
  `xtest_version: 2.2` (python-xlib stores the parsed version reply in the private
  `_data` dict — verified this session)
- Clipboard verified live this session: `probe_x11_clipboard()` -> available,
  backend `xlib-selection`, details `{'selection': 'CLIPBOARD', 'targets':
  ['UTF8_STRING', 'STRING', 'TEXT'], 'takes_ownership': False, 'serves_lazily':
  True}`; a second X client really read back non-ASCII text that BLAXCY served.
  The session runs **xfce4-clipman**, which requests the selection as soon as
  ownership changes -- the reason the previous content is re-served during the
  restore grace rather than only re-owned.
- Window control verified live this session: `WindowManager.probe()` ->
  `available=True backend='python-xlib' details={'active_window': 23068734,
  'ewmh': True}`; `list_windows()` -> 4 client windows with usable metadata
  (`xfce4-panel`, pid 2039). No window activation was performed.

## Session log (append-only, informational only — never load-bearing)
- 2026-09-26 — bootstrap session. Root confirmed `/home/tsn/blaxcy`. Read this index,
  then reconciled it against `git log`/`git status`: HEAD is `0b86d56`, not the
  `5e06bc0` this file recorded, and two units it described as uncommitted (`686a022`
  activate_element + §70 logging; `0b86d56` the AT-SPI focus-event fix) were already
  committed. Re-ran the gate before trusting any "done" claim: `ruff check .` clean,
  `mypy .` clean (179 files), `pytest -p no:pytest-qt -q` -> **1167 passed, 6
  skipped**. Then, on operator approval, ran the opt-in real-input re-measurement of
  `--workload workflow-verifiable`: sequence **5/20 completed** (was 2/20), halt set
  now a single cause `s2:FOCUS_MISMATCH` ×15; click **30/30** and keyboard **30/30**
  VERIFIED (were 5/50 and 1/30). Recorded in `docs/benchmark_report.md`. Committed
  the uncommitted three-halt-causes unit as recovery checkpoint `372879a` (18 files,
  operator-approved). Then fixed the remaining sequence-specific focus-timing cause:
  `control/executor.py::_focus_checked_state` re-checks a would-be §51 refusal against
  a fresh live perception before accepting it (+3 tests; gate **1170 passed, 6
  skipped**; ruff + mypy clean, 179 files). Re-ran the workflow benchmark: **20/20
  completed, p50 1342.94 ms / p95 1787.70 ms**, within the ≤ 4000 ms target — the
  realistic §74 happy path is now met and Phase 14 is COMPLETE. Then started
  **Phase 15** with a new soak runner (`bench/soak.py` + `tests/unit/test_soak.py`,
  10 tests): 30 iterations, **30/30 stable**, latency drift −0.56 %, steady-state
  memory 0.004 MB/iteration, no stuck input / retained lease / orphan thread. The
  soak surfaced a **real §43.1 defect** — the cache could never hit on a live
  desktop (`owner_window_id = None` for all 146 elements, so the key's window
  component never matched) — fixed in `TargetResolver._record` and re-measured at
  hit rate **0.875** (and 0.9906 over 150 sequences). Gate **1181 passed, 6
  skipped**; ruff + mypy clean (181 files). Then Phase 15 continued: a bounded
  startup retry in `tests/harness/fixture_client.py` (+5 tests), the capability
  matrix `docs/capability_matrix.md`, and a **150-iteration soak, 149/150 stable**
  with flat latency and steady-state memory. Gate **1186 passed, 6 skipped**;
  ruff + mypy clean (182 files).
- 2026-09-23 — implemented §50's clipboard paste path on request (the item Phase 7
  left open): `control/clipboard.py` `X11ClipboardPaster` (owns the CLIPBOARD
  selection, serves it on its own thread, captures and re-serves the previous
  content, hands ownership back), a `[clipboard]` settings section, a functional
  `probe_clipboard`, and the credential rule that keeps passwords off the
  clipboard (§42/§55) — 657 passed, 2 skipped; ruff + mypy clean (115 files);
  26 new tests (10 unit, 11 real-display integration, 3 keyboard, 2 executor
  safety). Two real defects fixed: a stale `SelectionClear` from BLAXCY's own
  previous restore stopped the *next* transfer (the serve thread died
  immediately), and the previous-content read decoded Latin-1 payloads as UTF-8,
  which would have preserved corrupted text or lost content entirely.
- 2026-09-23 — bootstrap re-run (clean; re-verified Phase 8 at 541 passed, 2
  skipped; ruff + mypy clean, 101 files), then implemented and verified Phase 9
  (emergency stop §63, human takeover §62, bounded recovery §61, watchdog + crash
  cleanup §64, all wired into the executor's abort hook) — 631 passed, 2 skipped;
  ruff + mypy clean (112 files); 90 new tests; stop latency measured at p50
  0.014 ms / max 0.030 ms against the 150 ms target. Fixed two real defects: the
  `CrashGuard` re-entrancy flag that would have silenced the `atexit` cleanup,
  and the missing early abort check that let a latched stop resolve and lease
  before being refused.
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
- 2026-09-23 — committed the accumulated Phase 2-7 work as recovery checkpoint
  `947b001` (38 files, +10707/-226); no code differed from the verified tree.
- 2026-09-23 — bootstrap re-run (clean; re-verified Phase 7 at 356 passed, 2
  skipped; ruff + mypy clean, 81 files), then implemented and verified Phase 8
  (policy modes/classes/guards/permissions/terminal guard + executor, verifier,
  window manager, action tracker) — 541 passed, 2 skipped; ruff + mypy clean
  (101 files); 185 new tests including `tests/safety/test_executor_safety.py`
  (24) and the live EWMH window-manager suite (4). Fixed three real defects found
  by the new tests: the unforwarded `confirmed` flag, the credential-text
  stripping that would have stopped a password being typed, and the key-action
  path (`press_key`/`hotkey` have no target, so the resolve step must be skipped
  for them rather than raising).
- 2026-09-24 — bootstrap re-run on an **interrupted** tree: the repository already
  held the full Phase 10.1 batching layer (mtimes 14:44–14:57, i.e. after this
  file's 13:32 write) from a session that ended before finalizing. Re-verified the
  inherited tree green (871 passed, 2 skipped; ruff + mypy clean, 138 files),
  confirmed no half-applied edit, then ran the §76 batching benchmark — which
  exposed a real defect: `control/sequence_runner.py::_as_identity_hint` primed the
  §43.1 cache with `recorded_at = 0.0`, making every §33.2 hint **born expired**
  (measured: warm hit rate fell 0.995 -> 0.4975 with 400 invalidations). Fixed at
  the cache boundary (`ResolverCache.prime` now stamps the time itself, as
  `record` does) and wired the §76 usefulness metric, which nothing had ever
  incremented (it read a structurally constant `0.0`). Gate after the fixes:
  **873 passed, 2 skipped**; `tests/safety` 77 passed; ruff + mypy clean
  (138 files); benchmark recorded in `docs/benchmark_report.md`. Phase 10.1 marked
  COMPLETE; every batching flag stays default-off pending the operator's decision.
- (Note: the Phase 9, §50 clipboard and Phase 10 sessions recorded their milestones
  above but did not append their own session-log lines; the milestones are the
  load-bearing record, this log is informational only.)
- 2026-09-24 — on explicit operator instruction, enabled the batching layer by
  default (§83/§84 condition met): `[resolver_cache] enabled`, `[sequence] enabled`,
  `capture_boost`, `speculative_perception` → true in `config/default_settings.toml`
  and `config/settings.py`, with the stale "ships disabled" docstrings/comments and
  the three default-off assumptions in the tests updated. Because that makes the
  `T-SPECULATE` background worker production code (and no test had ever used
  `background=True`), added `tests/unit/test_speculative_perceiver.py` (9 tests:
  resolves+takeable, non-blocking against a 250 ms resolver, no orphan thread on
  shutdown, never speculates a credential, discard on MEANINGFUL/MAJOR/generation
  but not TRIVIAL, bounded result map). Two supporting defects found and fixed while
  writing them: `wait_idle` could return while a job was still running (it cleared
  `_pending` before resolving) and its timeout fallback claimed success it had not
  earned, and completed hints had no size bound. Gate: **884 passed, 2 skipped**;
  `tests/safety` 77 passed; ruff + mypy clean (139 files); `python main.py probe` →
  `AVAILABLE: 9 DEGRADED: 0 UNAVAILABLE: 3` with `sequence_execution AVAILABLE
  sequence_runner`; `docs/environment_report.md` regenerated. `run_sequence` is now
  enabled but is still unreachable in the running program until the Phase 12
  composition root wires the runner.
- 2026-09-24 — bootstrap re-run (clean; the inherited tree reproduced this index
  exactly at `884 passed, 2 skipped`, ruff + mypy clean, 139 files), which located
  the next open phase as **Phase 11 — visual grounding** (§41; `core/visual_grounder.py`
  did not exist and the capability still reported "not implemented until Phase 11").
  Implemented and verified it: a gated, bounded, injectable visual-grounding
  perception source, `policy.guards.check_visual_fallback`, the `[visual]` config
  section, and an honest `probe_visual_grounding`. Two invariants were fixed as
  bounds at the schema level (the §41 confidence ceiling cannot be configured
  above 0.80) and the whole privacy gate runs *before* an image exists. Verified the
  SDK surface actually used against the installed `google-genai` 2.20.0 before
  writing against it (`response_json_schema`, `response_mime_type`,
  `HttpOptions.timeout`, `Part.from_bytes`). Gate: **938 passed, 2 skipped** (54 new
  tests); `tests/safety` 77 passed; ruff + mypy clean (141 files); `python main.py
  probe` → `AVAILABLE: 9 DEGRADED: 0 UNAVAILABLE: 3` with `visual_grounding
  UNAVAILABLE google-genai` for the honest reason (no key stored) and the disabled
  path asserting `disabled by configuration`. Regenerating `docs/environment_report.md`
  also corrected a report that had been stale since Phase 5. One defect found and
  fixed while writing tests: the missing-credential capability branch omitted the
  keyring status from its `details`, so `key_present` was absent on exactly the
  branch that needs it. Honest limitation recorded rather than hidden: the grounder,
  like `core/ocr.py`, is a real tested source that nothing calls until the Phase 12
  composition root/perception orchestrator exists — it is deliberately not wired
  into the executor's resolve step, because §41 takes a `Frame` the same
  orchestrator owns, and faking that path would be a bypass, not an optimization.
  **[Superseded 2026-09-24: that last sentence was wrong about the repository.
  `BlaxcyApplication._resolve_fallback` is passed to both the executor and the
  dispatcher, so the grounder and OCR *are* wired into the resolve path via
  `PerceptionOrchestrator.enrich()`, and only on a deterministic miss. See the
  Phase 12 milestone entry.]**
- 2026-09-24 — bootstrap re-run on a **drifted** tree (this session). The index was
  24 minutes stale and the repository had moved well past it: the interrupted
  session built the Phase 12 composition root (`core/application.py`), the
  perception orchestrator (`core/perception.py`), `main.py status`/`run`,
  `tests/harness/perception.py`, 40 tests, and a `gui/__init__.py` header — then
  installed the `pytest-qt` dev dependency at 20:27, which made **every** pytest run
  abort at configure time (PySide6 6.10.3 from apt has no `QtTest`; pytest-qt
  auto-selects PySide6 and does not fall back on `AttributeError`). Restored the
  gate with `-p no:pytest-qt` and verified the un-indexed work end to end: **978
  passed, 2 skipped**; `tests/safety` 77 passed; ruff clean; mypy clean (147 files);
  `python main.py status` started the real assembled Body and made one real
  observation (xtest backend, AT-SPI, clipboard, EWMH, brain unconnected, 80
  elements, `AVAILABLE: 9 DEGRADED: 0 UNAVAILABLE: 3`) with no input injected.
  Checked the §43 fallback wiring in the source rather than trusting the docstrings,
  which closed Phase 11's "no caller" item. **No code was changed this session** —
  it was verification, reconciliation, and the corrected blocker record.
- 2026-09-24 — same session, continued: implemented and verified **Phase 12's §71
  GUI** on operator instruction (`core.application` + `core.perception` had been
  built but unverified by the interrupted session; the GUI itself did not exist).
  Seven modules: `gui/app.py` (composition entry point, self-exclusion applied in
  code after the user's config loads), `gui/main_window.py`, `gui/status_panel.py`
  (§71 sequence-progress indicator), `gui/capability_panel.py`, `gui/action_log.py`
  (a bounded view of the one bus), `gui/confirmation.py` (§56 dialog + the thread
  bridge the executor blocks on; every failure path denies, the 3-second countdown
  is a constant floor and there is no implicit Enter acceptance),
  `gui/emergency_stop_ui.py` (§63 stop + explicit re-arm, reporting what the stop
  actually did). `main.py` gained a `gui` subcommand whose Qt import is local, so
  every other command still works without a Qt binding. 52 new tests, including an
  integration test over the **real** assembled Body and a live `main.py gui
  --offscreen` event-loop run (`exit=124` under `timeout`). Gate: **1030 passed, 2
  skipped**; `tests/safety` 77 passed; ruff clean; mypy clean (160 files). The
  GUI tests deliberately build their own offscreen `QApplication` rather than using
  `pytest-qt`, so the missing PySide6 `QtTest` module does not block any of this —
  it only leaves the documented bare `pytest` command waiting on one `sudo apt-get
  install python3-pyside6.qttest`, which the session could not run unattended.
- 2026-09-24 — same session, continued: implemented and verified **Phase 13 —
  installation (§73) and the §85 install criteria**, on operator instruction, so
  that BLAXCY becomes a menu application with a desktop entry, a manifest and
  rollback. `installer/installer.py` (standard library only — it runs before the
  virtualenv it creates exists, so it reaches the application by running it as a
  subprocess rather than importing it), `installer/__main__.py` (the CLI;
  `install`/`update`/`uninstall`/`preflight`, exit `0`/`1`/`2`),
  `install.sh`/`update.sh`/`uninstall.sh` as thin wrappers so the documented
  commands cannot drift from the tested ones, `desktop/blaxcy.desktop` as the
  single source of truth for the menu entry (only `Exec=` and
  `X-BLAXCY-Version=` are rewritten, and the maintainer comments are stripped),
  `desktop/make_icon.py` + `desktop/blaxcy.png` for a deterministic icon, and
  `core/single_instance.py` for §72/§85's "second launch is controlled" — an
  `flock` on `$XDG_RUNTIME_DIR/blaxcy/instance.lock` rather than a pid-file
  heuristic, precisely because the kernel drops the lock on process death and there
  is then no stale-lock case to get wrong. `main.py` takes that lock for `gui` and
  `run` only (the read-only commands deliberately stay lock-free) and grew a
  `--lock-path` flag. 39 new tests. The first gate run was **not** green — `3
  failed, 29 passed` — and both kinds of problem are worth recording: one **real
  defect** (with a file where the lock's parent directory needed to be, `acquire()`
  raised `FileExistsError` out of `Path.mkdir` instead of failing *closed*; the
  `mkdir` is now inside the guard that already covered `os.open`), and two tests
  that had over-specified reality (one built a package `iter_app_files`
  deliberately never installs, the other asserted that `/usr` preflights with no
  failing step at all, when `/usr` is genuinely unwritable without root — the
  assertion is now about the `--system` *gate*). Then the real end-to-end
  verification, which is the part that matters: a dry run that leaves no prefix at
  all; an install that puts 78 files, a launcher, an icon and an entry in place and
  whose installed entry is accepted by `desktop-file-validate`; the installed
  launcher printing the real session report; a manifest of per-file sha256; a
  **second launch refused** with the holder named (pid, command, age) and exit 2,
  and a fresh launch succeeding after the first process dies; `update` recording
  `updated_from`; a **rollback against a real injected failure** that leaves the
  prefix holding only the user's own file; an uninstall that removes exactly what
  the manifest lists; and a pre-existing `blaxcy.desktop`/`blaxcy.png` backed up,
  replaced, and restored **verbatim**. Also fixed while verifying: an empty `lib/`
  survived a rolled-back install (the prune list covered the icon, entry, launcher
  and manifest parents but not the app tree's own parent). Gate: **1069 passed, 2
  skipped**; ruff clean; mypy clean (**168 files**); 7 lint findings and 2 type
  findings on the new code, all fixed. Documentation added:
  `docs/installation.md`, plus the README's status/install sections and
  `knowledge.md`. Honest limitations recorded rather than hidden: `--system-deps`
  is verified as *reporting* (no unattended sudo, so nothing was installed), a
  non-Debian family is detected and told what capabilities are wanted rather than
  fed an invented package list, `gtk-update-icon-cache` exits 1 inside a throwaway
  prefix and the installer says so instead of claiming a refresh, and no
  system-wide install was performed. The one human action still outstanding is
  unchanged and unrelated: `sudo apt-get install python3-pyside6.qttest`, which
  only affects the documented bare `pytest` command.
- 2026-09-24 — same session, continued: added the **§75 fixture-app end-to-end test
  for the GUI confirmation dialog on a real destructive action**
  (`tests/e2e/test_fixture_confirmation_e2e.py`, the `tests/e2e/` package's first
  real content). It is opt-in (`BLAXCY_E2E_REAL_DISPLAY=1`) and runs on its own,
  because it injects real input and Qt permits one `QApplication` per process while
  the rest of the GUI suite needs the offscreen one. What is real: the §74 fixture as
  a child process with a real window on the real display, the Body assembled through
  the production composition path (`gui.app.build_application`) with the real
  `XtestBackend`, the real modal `ConfirmationDialog` answered by pressing its real
  buttons from inside its own nested event loop, and real pointer/key injection. The
  one instrument is a recording proxy around the real backend, so a *refusal* can be
  shown to have injected nothing — the property this suite asserts everywhere else.
  Four cases: drag denied / drag approved, and destructive terminal submission denied
  / approved (the section 54 gate is on the **submission**; typing a destructive line
  is allowed and flagged, which was confirmed by probing before writing the test).
  Verbatim result: **1 passed, 3 skipped** — the deny path really works, and the
  three injection paths skip with an actionable reason. Three things were found while
  making it work, all recorded: (1) **`tests/e2e/` was an empty promise** — the
  package existed with a docstring and nothing in it, which is exactly the gap the
  Phase 12 "honest limitations" note had admitted; (2) the accessibility traversal's
  **default 250 ms deadline is too short to reach the fixture**, which registers last
  in the desktop's application list — the suite raises only that performance bound
  (`deadline_ms`, `max_nodes`) and this is documented in the file; (3) the big one —
  **§46 occlusion makes every real target non-actionable**, because the callers pass
  `occluders=state.elements` and the desktop background plus any maximized window
  therefore cover everything (`'Applications'`: `resolved=True actionable=False
  ratio=1.0`). That is filed as a Known failure with its evidence and three candidate
  fixes; it is a safety rule, so it is *not* fixed unilaterally, and the test skips
  rather than injecting at a target the Body has refused. Gate: **1069 passed, 6
  skipped** (4 new opt-in skips); ruff clean; mypy clean (**169 files**).
- 2026-09-24 — same session, continued: began **Phase 14 (§76 benchmarks)** and hit
  its prerequisite head-on. The requested numbers are the real-desktop
  `run_sequence`, resolution/lease/click, and the perception cycle. Asking for the
  real-desktop one first surfaced why it cannot be measured yet: **§46 occlusion
  makes every real target non-actionable**, because all nine call sites pass
  `occluders=state.elements` and the desktop background and every window behind the
  target therefore count as occluders of it. With the operator's approval I started
  the fix and delivered a **partial** one, verified green: `WindowManager.title_stack()`
  reads EWMH `_NET_CLIENT_LIST_STACKING` bottom-to-top; the AT-SPI traversal stamps
  `owner_window_title` on each element and `PerceptionOrchestrator` carries the order
  in the new `ScreenState.window_stack`; and `occluders_above()` narrows a caller's
  candidate list to windows **at or above** the target's, wired into both
  `TargetResolver.resolve` and `revalidate_target`. Unknown positions, `Rect`-only
  occluders and duplicated window titles are all kept, so the check is never weaker
  than before, and a window genuinely in front still blocks. Verified on the live
  desktop: the stack reads `('Desktop', 'QTermWidget', …, 'BLAXCY Test Fixture',
  'xfce4-panel')` and the background/panel/terminal occluders are now correctly
  dropped. **It is not fixed**: `'Movable Target'` is still
  `resolved=True actionable=False ratio=1.0` because (1) the target's own window
  frame and the desktop root are counted as occluders of the target — they are
  *ancestors* in the accessibility tree, which a path-prefix test would exclude
  exactly — and (2) Chrome's AT-SPI window name does not join its `_NET_WM_NAME`
  (the browser decorates the title differently for accessibility than for the window
  manager, and it changed between two runs here), so it is conservatively kept. The
  two candidate next steps are recorded in the Known-failures entry: exclude
  ancestors by `atspi_path` prefix, and attribute elements to windows by **pid**
  (`Accessible.get_process_id()` against `_NET_WM_PID`) instead of by title.
  Gate: **1076 passed, 6 skipped** (7 new resolver tests); ruff clean; mypy clean
  (169 files). **The Phase 14 benchmark numbers are NOT delivered** — they remain
  blocked on the rest of this fix, and `bench/` is still an empty package.
- 2026-09-25 — bootstrap re-run on a **drifted** tree again (this session). The
  repository had moved past this index: the previous entry recorded the §46 fix as
  *partial* at `1076 passed` with steps (a) ancestor-exclusion and (b) pid-attribution
  still "next", but both were already implemented in the tree (mypy 169 files,
  `1083 passed` before this session's edit). Re-verified green first (1083 passed, 6
  skipped; ruff + mypy clean), then established the key fact **read-only, with no
  input injected**: the exact production call (`occluders=state.elements` +
  `state.window_stack`) now returns `status=RESOLVED actionable=True ratio=0.0` for
  `'Applications'`, `'Desktop'` and `'Terminal'`, where the index recorded
  `actionable=False ratio=1.0`. So **§46 is functionally FIXED** for the live path.
  Two findings recorded while verifying: (1) the stack reader is
  `WindowManager.stacked_windows()`, not the `title_stack()` the index and
  `knowledge.md` named; (2) at the *default* 250 ms accessibility deadline the
  traversal truncated at 41 elements with **zero** `owner_window_title`s (pid present
  for 40/41), so at default settings the **pid** path is what makes the rule work —
  the title path only matters on a deeper traversal (336 elements / 267 titles at
  2000 ms). The defect had been invisible to the unit tests because they build
  synthetic elements, so a **live regression test** was added
  (`test_live_target_is_actionable_through_the_real_occlusion_path`), which drives the
  live tree, the live stacking order and `occluders=state.elements` together. A
  second live test I first wrote was **unsound** and removed (ancestor exclusion
  applies even with an unknown stack, so counting ancestors as "covering" was wrong);
  the full-suite run caught it (`1 failed`) even though it passed in isolation — a
  reminder that a test that passes alone is not verified. Final gate this session:
  **`1084 passed, 6 skipped`**; ruff clean; mypy clean (169 files). **No code
  production fix was needed this session** — the work was verification, the missing
  regression lock, and correcting a materially stale index that claimed a fixed
  defect was still open. Phase 14's benchmark numbers remain NOT delivered and are
  now  gated only on a deliberate real-input run (see "Next concrete action").
- 2026-09-25 — same session, continued: on the operator's explicit choice ("run the
  real-input benchmarks now") I closed step (c) and ran **Phase 14**. First the §75
  real-input suite, now unblocked by the §46 fix: `BLAXCY_E2E_REAL_DISPLAY=1 pytest
  tests/e2e/test_fixture_confirmation_e2e.py` → **`3 passed, 1 skipped`** (was `1
  passed, 3 skipped`), i.e. the real dialog really gates real injection on a real
  desktop. Then `bench/` was no longer empty: `bench/real_desktop.py` is a reusable
  runner that drives the production `BlaxcyApplication` over the live X11 desktop and
  the section 74 fixture with the real `XtestBackend`. Read-only numbers (n=200):
  accessibility p50 760.58 ms / p95 1080.42; target resolution p50 2.51 ms / p95 4.34;
  lease revalidation p50 **1.04 ms** / p95 2.16 — the **≤ 25 ms target is met** on a
  real desktop. Real-input numbers: click p50 **220.20 ms** / p95 1332.77 (n=50,
  **5/50 VERIFIED**), keyboard p50 **112.52 ms** / p95 1179.69 (n=50, **3/50
  VERIFIED**), 5-step `run_sequence` p50 **99.01 ms** / p95 133.31 (n=20, **0/20
  completed** — every run halts at step 1 `VERIFICATION_UNVERIFIED`). The actions
  really execute (the fixture's own counters prove the clicks landed; the input path
  is real). The honest conclusion, recorded rather than papered over: on this fixture
  §60 verification correctly refuses sub-`MEANINGFUL` changes, so there is **no
  real-desktop 5-step happy path to report** — the §76 target stays a target, and
  §4 rule 8 forbids inventing success. Two findings filed with structure: the
  verification/workload limit above, and the §43 over-broad ambiguity that stops the
  realistic browser workflow at `"Search"`. One harness lesson worth keeping: the
  first long real-input run saw every target as `TARGET_OCCLUDED` because the fixture
  window had been lowered over the run; the fix was to re-raise the fixture *before*
  each timed dispatch (outside the timer), which is correct because a real desktop is
  not a stable fixture. Gate after all changes: **`1084 passed, 6 skipped`**; ruff
  clean; mypy clean (**170** source files, including the new `bench/real_desktop.py`). No
  production code changed this session — the work was verification, the missing
  regression lock, the Phase 14 runner, and correcting a materially stale index.
- 2026-09-25 — same session, continued: committed the accumulated Phases 8-14 tree as
  recovery checkpoint **`30f0fa3`** (131 files, +36183/-263) on the operator's
  instruction, *then* made code changes on top. On explicit operator decision,
  **narrowed the §43 ambiguity rule** (`core/target_resolver.py`). Reproduced the real
  cause live with the fixture running: the fixture's decisive `"Search"` scored 0.990
  yet was vetoed as `AMBIGUOUS` because the **absolute duplicate rule fired on two
  unrelated `"Button Alpha"` buttons** elsewhere in the accessibility tree. The rule
  now judges ambiguity over the candidates competitive with a **decisive** text match
  (`DECISIVE_TEXT_FLOOR = 0.80`): a role-only match or an unrelated duplicate can no
  longer veto a uniquely-named target, a role-only match scoring at most 0.60 against
  at least 0.94 for a decisive text match. A text-less query, or one whose only matches
  are weak substring/fuzzy hits, judges every candidate **exactly as before**, and two
  genuine matches of the requested text remain `AMBIGUOUS`. Five new tests cover every
  §19.2 path (positive, negative, bypass, weak-match, conservatism-preserved). One
  full-suite failure improved the design: the ungated narrowing also resolved a *weak
  substring* match in `test_a_primed_hint_is_verified_before_it_is_used`, so the gate
  was tightened to a decisive match — a test failing for a real design reason, kept as
  evidence. Verified live: `resolve(text="Search", role_hint=BUTTON)` is now `RESOLVED`
  at 0.990, and the §74 workflow now clears `w1` (it halts instead on the §60
  verification limit — the separate open finding). Gate: **1089 passed, 6 skipped**;
  ruff clean; mypy clean (170 files). **These two changes are on top of `30f0fa3` and
  are not yet committed.**
- 2026-09-25 — next session, bootstrap: reconciled the interrupted Phase 14
  "verifiable workload" work. Found and fixed a real defect from a half-applied edit:
  `bench/real_desktop.py` had `SETUP_TIMEOUT_SECONDS = 30.0#: The section 76 ...`, a
  workload comment glued onto the assignment (valid Python, but wrong). Added the
  coverage the new feature was missing — `tests/integration/test_bench_workload_fixture.py`
  (7: the bench layout is present only when requested, is sized `240x80` for the §34
  thresholds, is distinctly named, each click really toggles its own control, and the
  ordinary widgets are hidden but still state-readable) and
  `tests/unit/test_bench_real_desktop.py` (3: `--workload` selects the right layout
  and plan, the verifiable plan is five distinct description-only clicks, the two
  workloads share no controls) — and updated `docs/benchmark_report.md` to state
  honestly that the harness is built and tested but the real-desktop number is not yet
  measured. Gate: **1099 passed, 6 skipped**; ruff clean; mypy clean (172 files). The
  only remaining work was the opt-in real-input run — which the operator then
  approved and it ran green: **20/20 five-step sequences completed**, p50 2505.23 ms,
  recorded in `docs/benchmark_report.md`; keyboard is `n = 0` with a reason on this
  workload.
- 2026-09-25 — same session: made the harness honest for the verifiable workload
  before running it (`bench_keyboard` no longer reports a number for a target the
  layout hides; `diagnose_fixture` diagnoses the active workload's controls), added
  the matching test, re-ran the gate (**1100 passed, 6 skipped**; ruff + mypy clean,
  172 files), then executed the approved real-input run above.
- 2026-09-25 — next session, bootstrap: the index matched the tree except for one
  **dirty file**: `bench/real_desktop.py` carried an uncommitted, untested edit
  adding the orchestrator's perceive-cycle and the section 71 GUI-refresh
  benchmarks — exactly the "still unmeasured" Phase 14 polish the index listed, from
  a session that ended before finishing it. Re-verified the inherited tree first
  (**1100 passed, 6 skipped**; ruff + mypy clean, 172 files), then finished the
  work rather than trusting it: a read-only `AppOwner` type so a Body can be
  benchmarked without an input backend; the perceive cycle split into **warm**
  (cache-served, section 35) and **cold** (forces a live traversal) so the number
  cannot present a cache as the live tree; `bench_accessibility`'s note corrected to
  report the traversal it timed instead of the *Body state* cache (it printed "0
  elements" while its own traversal returned hundreds); 4 new tests
  (`tests/integration/test_bench_readonly_real_display.py` 3, incl. one that proves
  the cold cycle really pays more than the warm one, plus 1 unit); and the real
  read-only run recorded in `docs/benchmark_report.md`. Measured (n = 30): warm
  cycle p50 **10.61 ms**, cold cycle p50 **830.78 ms**, GUI refresh p50 **0.39 ms**,
  revalidation p50 0.90 ms (target <= 25 ms met). Gate: **1104 passed, 6 skipped**;
  ruff + mypy clean (**173 files**). Still uncommitted on top of `30f0fa3`.
- 2026-09-25 — next session, bootstrap: the index matched the tree except for a
  complete-but-unindexed unit of work (the repository had moved two commits past
  this file, `7950798` and `5e06bc0`). Reconciled and verified it rather than
  trusting it: `core/accessibility.py` now refines a Qt editable `text` node to
  `TEXT_INPUT` via the `EDITABLE` state (§37) — without it §51's focus guard refused
  to type into any Qt field — and `_read_text_content` reads through the
  `Atspi.Text` interface instead of the deprecated `Atspi.Accessible.get_text` shim,
  which raises on `(start, end)` under the installed PyGObject and so silently
  returned nothing for a navigation field; `core/target_resolver.py` promotes the
  weak `substring_text`/`fuzzy_text` signals to their own cascade stages and removes
  them from `TEXT_MATCH_STAGES`, so a fuzzy `token_set_ratio` hit
  (`("Search Address Bar", "Search")` scores 100) can no longer sit inside the §43
  0.08 gap of a decisive match and veto a uniquely-named target; and the fixture
  gained a `--workflow-controls` layout plus a third `--workload workflow-verifiable`
  whose point is to make the *realistic* §74 pipeline (search icon → type → submit →
  result → play) complete and verify. Verified green first (**1121 passed, 6
  skipped**; ruff + mypy clean, 174 files; the new live
  `tests/integration/test_workflow_controls_real_display.py` **3 passed** against the
  real AT-SPI tree), then, on the operator's explicit approval, ran the real-input
  benchmark. **The honest result: the synthetic `verifiable` workload stays 20/20
  (the §76 5-step happy path is therefore met), while the realistic
  `workflow-verifiable` pipeline completed only 2/20**, halting on
  `s2:FOCUS_MISMATCH` ×8, `s2:VERIFICATION_CONTRADICTED` ×4 and
  `s4:VERIFICATION_UNVERIFIED` ×6 — the live AT-SPI read of the typed field lagging
  the injection, and a `LIST_ITEM` selection that does not repaint `MEANINGFUL`-ly at
  its own box. The input genuinely landed (the fixture's own counters:
  `search_icon: 20`, `submit_button: 76`, `play_button: 2`, and its `search_input`
  holding every sample). Recorded in `docs/benchmark_report.md`; the gap is filed as
  a new "Known failures" entry with both candidate next paths rather than papered
  over or made to pass by loosening §60 (§4 rule 8). No production code was changed
  this session — it was verification, the one measurement, and correcting a
  materially stale index that predated two commits and an entire unindexed workload.
- 2026-09-25 — same session, continued: on the operator's "continue" instruction,
  resolved where the project actually stands against §26/§85 and implemented the
  highest-value real gap rather than documenting around it. **§70 structured
  logging did not exist** — no `logging` import anywhere in the application, and a
  `[logging]` config section nothing consumed (§4 rule 10, §70, §85). Implemented
  `security/redaction.py` (a §26-named module that was absent: the redaction filter
  plus a secret registry) and `core/logging_setup.py` (JSON formatter, rotation from
  `[logging]`, idempotent `configure_logging`, `BLAXCY_LOG_DIR` override), wired it
  into every CLI command and into `Application.start`/`shutdown`, added structured
  records at the `ToolDispatcher` chokepoint and on a sequence halt, and made
  `logging.redact_on_root_logger` a §25 security invariant so a config cannot turn
  log redaction off. **The new tests found a real defect in the first attempt:** a
  filter attached to the *root logger* never runs for a record propagated from a
  child logger, so it redacted nothing the codebase actually logs; the filter now
  also sits on the handler. Gate: **1142 passed, 6 skipped** (was 1121); ruff + mypy
  clean (**178 files**); verified end-to-end through the CLI with a throwaway
  `BLAXCY_LOG_DIR`. Also wrote the three §26-mandated docs that did not exist
  (`docs/architecture.md`, `docs/security.md`, `docs/limitations.md`, the last
  carrying the §85 release-readiness reconciliation, which had never been walked)
  and corrected `docs/limitations.md`'s honest verdict: the project is **not
  complete** — Phase 15 is unstarted, one §76 row is measured but unmet, Wayland and
  `activate_element` are unimplemented, and the documented bare `pytest` is still
  blocked on `python3-pyside6.qttest`. Nothing committed; the tree is green and
  uncommitted on top of `5e06bc0`.
- 2026-09-25 — same session, continued: closed the last unimplemented §66 tool.
  **`activate_element`** was the one required tool with no implementation anywhere
  (the executor refused it with `BACKEND_UNAVAILABLE` because the accessibility
  layer only *read* `Atspi.Action`). Added the invocation at the accessibility
  layer (on the single `T-A11Y` owner thread, descending the element path live),
  a shared `ActivationOutcome`, an injected `activate=` hook on the executor, and
  the composition-root wiring; it is `MUTATING`, so it travels the full policy →
  resolve → lease → revalidate → verify pipeline and injects no pointer or key
  input — which is what makes it usable where a synthetic click is unreliable.
  Activation is an optional backend capability, so a backend without it reports a
  structured `UNAVAILABLE` instead of substituting a click. **Two defects were
  found by the tests rather than by reasoning, which is the point of adding them:**
  the first live `activate` call raised `unknown accessibility request 'activate'`
  because `_dispatch`'s kind table had never been taught the new request (a fake-only
  suite would have missed it), and a pre-existing safety test that asserted
  "activation always reports UNAVAILABLE" stopped being true once the tool worked,
  so it was rewritten to assert the property that still matters — a structured
  refusal, never a fabricated success, nothing injected. Gate: **1156 passed, 6
  skipped**; ruff + mypy clean (**179 files**); `docs/limitations.md` updated
  (  `activate_element` is no longer listed as a gap). Every §66 tool now has a real
  implementation. Nothing committed.
- 2026-09-25 — next session, bootstrap: the tree reproduced this index exactly
  (**1156 passed, 6 skipped**; ruff + mypy clean, 179 files), so this was a clean
  resumption, not a drifted one. First action, on the operator's explicit choice:
  **committed the verified tree as recovery checkpoint `686a022`** (31 files,
  +2977/-97) — the §70 logging path, `security/redaction.py`, the three §26 docs,
  `activate_element`, the workflow workload and its tests, all of which had been
  green but unindexed since `5e06bc0`. Then, on the operator's next choice
  ("investigate FOCUS_MISMATCH first"), diagnosed the realistic §74 workflow's
  halts read-only and **found a real production defect**: the §35 element cache was
  never subscribed to `object:state-changed:focused` (nor `enabled`/`sensitive`/
  `editable`), so a focus change caused by a click was invisible for the whole
  `cache_ttl_seconds` (2 s) — longer than §45's 1500 ms action-state-age ceiling —
  and §51's focus guard refused to type into a field that really was focused. That
  is the `s2:FOCUS_MISMATCH` ×8 in the recorded benchmark, and it survived every
  unit test because `ScriptedBackend` reports events as a bool and cannot model
  *which* events are registered; only the live workflow run could surface it. Fixed
  by subscribing the action-gating state events, locked with a registration-list
  test, and documented. Gate after the fix: **1157 passed, 6 skipped**; ruff + mypy
  clean (179 files). Deliberately **not** re-measured: the effect needs an opt-in
  real-input run, and the other two halt causes (`s2:VERIFICATION_CONTRADICTED`,
  `s4:VERIFICATION_UNVERIFIED`) are independent and remain open.
- 2026-09-25 — same session, continued: on the operator's instruction, fixed the
  two remaining `workflow-verifiable` halt causes. Both were **measured read-only
  before anything changed**, and both diagnoses turned out to be different from the
  index's summary. (1) The typed-field `s2:VERIFICATION_CONTRADICTED` is the §35
  element cache: a plain `perceive()` took **921 ms** to see a field-text change,
  while a forced live read returned it in **731 ms**, so `verify_text` was judging a
  pre-action read and §60 was calling it a contradiction. Fixed by making
  `PerceptionOrchestrator.perceive(force=True)` real (the parameter existed and was
  ignored — `del force`), adding a `perceive_fresh` executor hook, and having the
  executor re-check a negative verdict against fresh live observations for
  `[verification] verify_settle_ms` (default 1000) before reporting it; a VERIFIED
  verdict never reaches that window. (2) The result-click `s4:VERIFICATION_UNVERIFIED`
  is **not** a repaint-size problem as the index said: with the row colours pinned
  high-contrast the repaint is spatially `MEANINGFUL` (240x80) but the §34 temporal
  layer reclassifies it `ANIMATION` because the previous step had changed the same
  region. Rather than loosen a verification classifier, a click on a selectable
  control is now verified from the control's own `selected` state (positive-only,
  falling back to the pixel check). **A first version of the new live test asserted
  the delta class and failed on `ANIMATION` — that failure is what produced the real
  diagnosis**, and the test was rewritten to assert the semantic evidence, which is
  both stronger and not dependent on the rest of the desktop. New: `UIElement.selected`,
  `[verification] verify_settle_ms`/`verify_poll_ms`, `set_selection` on the fixture
  harness, and 10 new tests. Gate: **1167 passed, 6 skipped** (was 1157); ruff + mypy
  clean (179 files); the live workflow suite is **5 passed**. Nothing was loosened to
  obtain a number: §60's thresholds are untouched and the benchmark has deliberately
  **not** been re-quoted.
- 2026-09-26 — a later session, three queued units. (1) Investigated the 150-iteration
  soak's single halt (`s3:VERIFICATION_UNVERIFIED`, the `Submit` click) and decided
  its postcondition **can** be verified from a stronger signal: measured read-only on
  the fixture, the click enable-transitions `Play Button` (`enabled=False→True`), a
  directly reported accessibility state. Added a positive-only
  `Postcondition.ELEMENT_STATE`/`Verifier.verify_element_state_change` (element present
  in both observations, visible in both, enabled false→true, same window/app as the
  target) wired into the click branch with fall-through to the pixel check; it never
  yields `CONTRADICTED`. (2) Implemented the XDG RemoteDesktop portal backend
  (`control/backends/portal.py`) with a functional, non-invasive probe and wired it as
  the XTEST fallback; **measured on this host the probe reports UNAVAILABLE** because
  the X11 gtk portal does not expose the interface, so session/injection are unverified
  and no Wayland control support is claimed (absolute pointer also needs a ScreenCast
  stream node → `mouse` DEGRADED, `keyboard` AVAILABLE on Wayland). (3) Gave the soak a
  per-step latency breakdown so drift names its step. Gate: **1215 passed, 6 skipped**;
  ruff + mypy clean (184 files). The real-input re-measurement is pending operator
  approval.
