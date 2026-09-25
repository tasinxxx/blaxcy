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
- Store the Brain key (hidden prompt; never an argument): `python main.py keys set-gemini`
- Key status: `python main.py keys`
- Run: `python main.py` (currently defaults to `probe`)
- Assemble the whole Body and report its live state, injecting nothing:
  `python main.py status`
- Open the §71 window over that Body: `python main.py gui` (add `--offscreen` on a
  headless host; the Qt import is local to this command, so every other command
  works without a Qt binding)
- Run one task through the Brain: `python main.py run "<task>"` (needs a stored
  key; there is deliberately no `--yes` flag for confirmations)
- Install as a menu application (§73): `./install.sh` into `~/.local` (no root);
  `./update.sh`, `./uninstall.sh`, or `./install.sh --dry-run` to only report.
  `python -m installer {install,update,uninstall,preflight}` is the same surface.
  Details: `docs/installation.md`
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
  implemented; the resolver cache and speculative perceiver are implemented too
  (`resolver_cache.py` §43.1, `speculative_perceiver.py` §33.2, Phase 10.1), as is
  **visual grounding** (`visual_grounder.py` §41, Phase 11 — the last cascade
  stage, gated to `mode >= ASSIST`, never over credential/protected context), and
  **single-instance control** (`single_instance.py` §72/§85, Phase 13 — an
  `flock` on `$XDG_RUNTIME_DIR/blaxcy/instance.lock`, taken by `main.py`'s `gui`
  and  `run` only; the kernel drops the lock on death, so there is no stale-lock
  case), and **structured logging** (`logging_setup.py` §70 — one JSON object per
  line, rotation from `[logging]`, installed by every `main.py` command and by
  `Application.start`; the root redaction filter lives beside it in
  `security/redaction.py`, and `logging.redact_on_root_logger` is a section 25
  invariant, so a config cannot turn it off)
- `control/` — **input layer (Phase 7)**: `backends/` (`base.py` the narrow
  backend contract, `keys.py` keysym helpers, `xtest.py` the preferred in-process
  XTEST backend, selected only after a functional probe), `mouse.py` (§48 sequence
  + §49 drag) and `keyboard.py` (§50 typing + §51 focus guard + §52 modifier
  hygiene). **Decision/execution layer (Phase 8)**: `executor.py` (the §59 state
  machine, the §44 lease issue, the §45 revalidation checklist and the *only*
  caller of `mouse`/`keyboard`), `verifier.py` (§60 honest postconditions),
  `window_manager.py` (§47 real EWMH activation, verified by reading the active
  window back), `action_tracker.py` (§78 ownership, released on every exit) and
  `clipboard.py` (§50's paste path: `X11ClipboardPaster` owns the X CLIPBOARD
  selection, serves it to the requesting application, and hands it back).
  **Safety gate (Phase 9)**: `emergency_stop.py` (§63 latched stop, released
  input, forced OBSERVE, measured latency), `takeover.py` (§62 pause → release →
  explicit resume → lease invalidation + re-perception), `recovery.py` (§61
  failure classification and bounded budgets), and `sequence_runner.py` (§66.1
  batched multi-step execution — every step still runs the standalone pipeline).
- `watchdog/` — **implemented (Phase 9)**: `protocol.py` (the §64/§78 heartbeat
  record: pid, session, beats, `owned_keys_down`, `owned_buttons_down`,
  clean/dirty shutdown; written atomically outside the repo) and `watchdog.py`
  (a minimal single-thread watchdog plus `CrashGuard`, which releases held input
  and marks the run clean on signal/atexit, then really terminates).
- `policy/` — **implemented (Phase 8)**: `modes.py` (§56 modes + the self-enforcing
  AUTONOMOUS ceiling), `action_classes.py` (§57 classes, the `run_sequence` gate,
  the §32.1 lock classification), `guards.py` (§28/§31/§42/§55/§58 fail-closed
  guards), `terminal_guard.py` (§54 typing vs submission), `permissions.py` (the
  one `PermissionEngine`). Policy returns verdicts and never injects input.
- `ai/` — **implemented (Phase 10)**: `tool_protocol.py` (the §66 tool schemas plus
  strict argument parsing — unknown arguments and pre-resolved
  coordinates/leases are *refused*, never ignored — and `ToolDispatcher`, which
  routes read-only tools onto the bounded §32.1 read path and everything else
  through the executor), `prompt_builder.py` (the system instruction the Brain
  reads), `context_manager.py` (§68/§68.1 budget, the "unchanged state" marker,
  protected-application suppression), `brain_adapter.py` (the abstract adapter
  plus `AgentLoop`, the *manual* tool loop with turn/wall-clock limits, a
  cancellation token and no self-confirmation) and `gemini_adapter.py` (Gemini
  via the keyring key, with `automatic_function_calling` explicitly disabled)
- `gui/` — **implemented (Phase 12)**: `app.py` is the composition entry point
  (builds one `BlaxcyApplication`, hands it the confirmation bridge, and applies
  §71 self-exclusion *in code* so no user config can remove it); `main_window.py`
  is the window; `status_panel.py` carries the §66.1 sequence-progress indicator;
  `capability_panel.py` renders the §28 verdicts; `action_log.py` is a bounded
  view of the one event bus; `confirmation.py` is the §56 dialog plus the thread
  bridge the executor *blocks* on; `emergency_stop_ui.py` is the §63 stop and its
  explicit re-arm. The package `__init__` imports no submodule, so `import gui`
  never requires Qt
- `installer/` — **implemented (Phase 13)**: section 73, in the order it fixes.
  `installer.py` is **standard library only** (it runs before the virtualenv
  exists) and holds the whole implementation: `preflight()`, the venv / system-dep
  / app-file / icon / desktop-entry / validate / cache-refresh steps, the manifest
  with per-file sha256, `rollback()` (every overwrite is backed up first) and
  `uninstall()` (which removes *only* what the manifest lists and restores any
  backup verbatim). `__main__.py` is the CLI (`install`/`update`/`uninstall`/
  `preflight`; exit `0`/`1`/`2`), and `install.sh`/`update.sh`/`uninstall.sh` are
  thin wrappers over it so the documented commands cannot drift from the tested
  ones. `desktop/blaxcy.desktop` is the single source of truth for the entry and
  `desktop/make_icon.py` regenerates the deterministic icon
- `schemas/` — typed geometry, screen state, elements, leases, actions, errors,
  events, sequences (all implemented as of Phase 1). Every coordinate is
  space-tagged (`CoordinateSpace`); `ToolEnvelope` is the one result shape (§66).
- `config/` — TOML settings, schema migration
- `security/` — **implemented (Phase 10)**: `keyring_manager.py` (§69 — the only
  place `keyring` is called; `ApiKeyManager`, an honest `KeyringError` for a
  broken backend, and `redact_secret` for scrubbing a third-party error string
  before it is logged)
- `tests/harness/` — launcher/client for fixture apps
- `tests/fixtures/fixture_app.py` — the §74 fixture application, launched as a real
  child process; drive it with `tests.harness.FixtureApp` (Qt offscreen by default,
  no window appears). It exposes every control §74 requires plus the 5-control
  workflow fixture used for `run_sequence` happy-path and halt-path tests.
- `tests/integration/` — real-display tests that skip honestly without a display:
  calibration, frame engine, accessibility, input/XTest,
  `test_target_resolver_real_display.py` (the **live** AT-SPI tree through the
  Phase 6 resolver, checking §43 ambiguity and §46 occlusion against real desktop
  geometry), and `test_window_manager_real_display.py` (live EWMH read-only: probe,
  active window, client list and window metadata — it never activates a window).
- `tests/e2e/` — the **§75 end-to-end suite** (opt-in, real desktop).
  `test_fixture_confirmation_e2e.py` is the real confirmation dialog gating a real
  destructive action against the §74 fixture: real window, real Body (the production
  composition path), real `XtestBackend` wrapped in a *recording* proxy, and the real
  modal `ConfirmationDialog` answered by pressing its real buttons from inside its own
  event loop. Run it **alone** with `BLAXCY_E2E_REAL_DISPLAY=1 pytest
  tests/e2e/test_fixture_confirmation_e2e.py` — it injects real input, and Qt permits
  one `QApplication` per process while the rest of the GUI suite needs the offscreen
  one. It raises only the §35 accessibility traversal bound, because the fixture
  registers last and the default 250 ms deadline never reaches it.
- `tests/safety/` — the safety-property suites. `test_executor_safety.py`
  (Phase 8) asserts OBSERVE input blocking, destructive confirmation, terminal
  submission, credential privacy, blocked apps, unavailable capabilities,
  stale/ambiguous targets, the abort hook and ownership release, and that every
  refusal injected **nothing** into the desktop. `test_safety_gate.py` (Phase 9)
  is the *wired* gate: stop latched before and during an action, the early
  short-circuit, real input release, stop-before-takeover ordering, a recovery
  retry that genuinely succeeds, and every recovery refusal (destructive,
  already-injected, safety, ambiguous, budget-exhausted, loop-guarded).
- `tests/harness/phase8.py` — builders for the policy/executor tests (`ExecutorEnv`
  wires the executor over `FakeInputBackend` and a scripted `FakePerceiver`, so
  tests can drive a real post-action observation deterministically).
- `tests/harness/phase10.py` — builders for the Brain/tool tests:
  `build_dispatch_env` (a `ToolDispatcher` over the Phase 8 fakes, with a fake
  window controller and capability report), `ScriptedBrain` (an adapter that
  replays fixed model turns, with an `on_call` hook), `build_loop_env` (an
  `AgentLoop` over those two) and `TickClock` (a clock that advances only on
  `sleep`, so wait/timeout logic is tested without waiting).
  Nothing in it injects physical input.

## Conventions

- Python 3.12+, strict typing on `schemas/`, `core/`, `control/`, `policy/`.
- **Logs are structured JSON**, written to `$XDG_STATE_HOME/blaxcy/blaxcy.jsonl`
  (override the directory with `BLAXCY_LOG_DIR`). Never log a secret, a password
  value or protected visual content: register a secret with
  `security.redaction.register_secret` and it is replaced with `***` everywhere,
  including in a formatted traceback (section 70).
- No fake tools, fake status, or fake verification.
- Every capability reports `AVAILABLE` / `DEGRADED` / `UNAVAILABLE`, never assumed.
- Capabilities are established by functional probes, not by the presence of an
  executable on `PATH`.
- AT-SPI has exactly one owner thread (`T-A11Y`); the browser layer consumes the
  accessibility service's observations rather than calling AT-SPI itself, so
  there is never a second accessibility owner.
- Security invariants are enforced in code (`config/settings.py`) and cannot be
  configured into an unsafe state.
- Policy (`policy/*`) is a *value* layer: it reads state and returns a verdict, so
  a policy bug can refuse work but cannot authorise a bypass of the executor's
  lease/revalidation checks. The permission order is fixed and fail-closed:
  blocked application → terminal → credential context → calibration → capability →
  mode/action-class → confirmation. Destructive actions always need an explicit
  human confirmation in every mode (hard-coded, not configurable).
- OBSERVE permits only work that injects no input: READ_ONLY, plus NAVIGATIONAL
  that does not inject (window focus). Scroll is refused because it moves the
  pointer. `run_sequence` is never given a class of its own — the caller must pass
  the gate class (the most restrictive declared step, §57).
- Physical input happens only through `control/executor.py`: fresh lease from the
  resolution observation → full §45 revalidation against live state → abort check
  → inject → observe → verify. Read-only tools are dispatched on the read path and
  never enter the executor (§32.1). The abort hook is consulted **twice**: at the
  top of the pipeline (so a latched stop does no work at all) and immediately
  before injection (so no input escapes it). `EmergencyStop.abort_code` and
  `TakeoverController.abort_code` are combined with `combine_abort_checks`, stop
  first.
- The emergency stop **latches before it cleans up** and takes no lock the running
  action holds, so it cannot queue behind the work it is stopping; every releaser
  is drained even if one raises, and re-arming is explicit (`reset()`). Takeover
  is a latch too: `resume()` clears it, invalidates every lease and re-perceives,
  but never restores the previous mode and never replays an interrupted plan.
- Recovery is bounded by policy, not by hope (§4 rule 21/§21/§61): only TARGET and
  TRANSIENT failures are candidates, and anything that already injected input, any
  destructive action, any safety refusal, any unclassifiable failure, any
  target-identity change and any repeated identical call is refused. Budgets are
  per tool **and** per task step, and every decision is carried in the envelope as
  evidence — an action is never silently retried.
- Verification never flatters a result (§60): positive evidence for `VERIFIED`,
  absent evidence is `UNVERIFIED`, and only positive contradicting evidence is
  `CONTRADICTED`. A credential field is never read back, so typing into one is
  honestly `UNVERIFIED` (`sensitive: true` on the envelope) and the text appears in
  no envelope, event or log.
- **Credential text never takes the clipboard path (§42/§55).** The clipboard is
  readable by every application and a clipboard manager keeps it in history, so
  `KeyboardController.type_text(..., sensitive=True)` ignores the clipboard
  entirely: a password the keyboard mapping cannot produce is refused with
  `UNICODE_UNSUPPORTED` rather than published. The executor passes
  `sensitive=element.is_password`.
- **The clipboard is borrowed, then handed back (§50).** `X11ClipboardPaster`
  captures the previous content and owner *before* taking the selection, keeps
  serving its own text for `paste_grace_ms` (an X selection is read only after the
  paste keystroke, so stopping early pastes nothing), serves the previous content
  for `restore_grace_ms` so a clipboard manager can keep it, and then hands the
  selection back. It never fights for the selection: a `SelectionClear` stops it.
  Re-owning the previous window does *not* make that client serve again, which is
  why the grace window -- not the hand-back -- is what preserves the user's
  content.
- Every performance optimization (batching, caching, speculation, concurrency)
  is a scheduling/hint layer only — it never removes a Policy / Resolve / Lease /
  Revalidate / Execute / Verify step. The batching layer is **enabled by default**
  (`[sequence] enabled`, `[resolver_cache] enabled`, `capture_boost`,
  `speculative_perception`) now that the §66.1/§75 test list is green; each flag
  stays a switch, and turning one off can only remove capability, never a check.
- `ai/tool_protocol.py` is the **only** door between a Brain tool call and BLAXCY.
  Argument keys are accepted only if the tool's own declared schema declares them,
  so a coordinate/`element_id`/lease argument is refused for an action tool while
  `describe_region`'s declared region rectangle is accepted. A read-only tool
  never enters the executor (§32.1), and `run_sequence` delegates to
  `control/sequence_runner.py` when the layer is enabled; it reports an honest
  `BACKEND_UNAVAILABLE` when the layer is switched off or no runner is wired,
  rather than pretending.
- The Brain boundary never self-confirms: `AgentLoop` surfaces
  `CONFIRMATION_REQUIRED` to a human prompt, and without one it halts with
  `CONFIRMATION_DENIED`. `confirmed=True` can never be reached from a tool
  *argument* (an argument named `confirmed` is rejected as unsupported).
- `BLAXCY`'s Gemini credential lives only in the OS keyring
  (`security/keyring_manager.py`); it is never in config, argv, a log, an event or
  a tool envelope, and a third-party error string is passed through
  `redact_secret` before it becomes a `BrainError`.
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
- Visual grounding (`core/visual_grounder.py`) is the **final** cascade stage and
  the only source that uploads pixels: it refuses in OBSERVE *before* it touches a
  backend, checks the upload region against credential/protected geometry before
  any crop or encode, bounds the image's longest side (§41, default 1024), derives
  confidence from the static 0.80 ceiling (a config can only lower it, never raise
  it), and emits elements with `source=VISUAL` that still pass lease → revalidate →
  verify. It never injects, never leases, never verifies, and never overrides §43
  ambiguity. `policy.guards.check_visual_fallback` composes the same verdict.
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
  **§46 real-desktop occlusion was broken and is now FIXED (verified live
  2026-09-25):** every caller passes `occluders=state.elements`, so the desktop
  background and windows *behind* the target were counted and every real target read
  as fully covered (`ratio=1.00`) and therefore non-actionable, meaning no input could
  ever be injected. `occluders_above()` now (a) drops the target's **ancestors** by an
  `atspi_path` *segment*-prefix test, and (b) keeps only candidates whose window is
  **at or above** the target's, attributing a window by **pid** first
  (`owner_app_pid` against the stack's `_NET_WM_PID`) with the title only as a
  tie-break. The stacking order comes from `WindowManager.stacked_windows()`
  (`_NET_CLIENT_LIST_STACKING`, bottom-to-top); an empty result means *unknown* and
  is treated conservatively, never as "nothing is in front". Unknown positions,
  `Rect`-only occluders and duplicated titles are kept, so the check is never weaker
  than before. Live probe (read-only, no input): `'Applications'`, `'Desktop'` and
  `'Terminal'` now resolve `actionable=True ratio=0.0` (were `actionable=False
  ratio=1.0`). Locked by
  `tests/integration/test_target_resolver_real_display.py::test_live_target_is_actionable_through_the_real_occlusion_path`.
  Caveat: at the default 250 ms accessibility deadline the traversal carries pid but
  **no** window titles, so the pid path is what makes this work by default. See
  `CONTINUATION_STATE.md` -> "Known failures".
- Input backends (`control/backends/`) are the *how* only: they perform no
  policy, lease or revalidation check and never decide to act (§13/§30). A
  backend is selected only after a **functional** probe (XTEST version query);
  only XTEST is implemented, and the xdotool path is explicitly not implemented
  (no dead stub). Every held key/button is tracked so `release_all` can undo it
  (§52/§63).
- §43 ambiguity is conservative but no longer over-broad (narrowed 2026-09-25 on
  explicit operator decision): when the query names text and a candidate matches it
  **decisively** (text component > 0.80 — exact / case-insensitive / normalized /
  accessible-name, *not* the weak 0.55 substring or the ≤0.80 fuzzy signal), the
  absolute and gap rules are judged over the text-matching candidates alone. A
  role-only control, or an unrelated duplicate elsewhere on the desktop (e.g. two
  same-named panel buttons), can no longer veto a unique, decisively-named target.
  A text-less query, or one whose only matches are weak, behaves exactly as before,
  and two genuine matches of the requested text are still `AMBIGUOUS`.
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

**On this host `pytest` currently needs one extra flag:** it must be run as
`.venv/bin/pytest -p no:pytest-qt`, because PySide6 6.10.3 (apt) ships no `QtTest`
module and the installed `pytest-qt` plugin aborts *every* pytest run at configure
time, before a single test is collected. Add `-o addopts=""` when you want the
honest final count (the default `-q` suppresses the summary line). Use the plain
command again once QtTest is installed: `sudo apt-get install
python3-pyside6.qttest` (6.10.3-3, the exact version of the installed
`python3-pyside6.qtcore`) fixes it, and `pip install PySide6` into `.venv` is the
no-sudo fallback. See `CONTINUATION_STATE.md` -> "Known failures" for the exact
error. Do not "fix" this by editing test expectations: the suite itself is green
either way; only the plugin's startup is broken.

For anything touching safety code (policy/executor/verifier/recovery/takeover/
emergency_stop/watchdog): also run `pytest tests/safety/` — those suites assert
not only that a refusal is returned but that **nothing was injected**.

For anything touching the GUI (`gui/`), also run
`pytest tests/unit/test_gui_confirmation.py tests/unit/test_gui_panels.py
tests/unit/test_gui_app.py tests/integration/test_gui_real_body.py`. Those build
their own offscreen `QApplication` and never request the `qtbot` fixture, so they
do not depend on `pytest-qt`; the integration one drives the *real* assembled Body
and injects no input. Note the two GUI rules that are easy to break: the panel
rendering must keep showing an unestablished fact as `UNKNOWN` (never `""`),and the self-exclusion token must keep being added by `gui.app`, not by config.


For anything touching the stop, takeover, release paths or the abort hook, also
run `pytest tests/integration/test_emergency_stop_real_display.py` (read-only:
it wires the real XTEST backend as a releaser and injects nothing).

For anything touching window control, also run
`pytest tests/integration/test_window_manager_real_display.py` (read-only against
the live display; it never activates a window).

For anything touching clipboard typing (sections 42/50/55), also run
`pytest tests/integration/test_clipboard_real_display.py`. Unlike the other
real-display suites this one really takes the CLIPBOARD selection and serves it
(that is the feature), so every test hands it back in a `finally`; it injects no
input. Confirm too that a non-ASCII password is still refused
(`pytest tests/safety -k clipboard`).

For anything touching the Brain boundary or tool dispatch (`ai/`, `security/`):
also run `pytest tests/safety/test_tool_dispatch_safety.py` and
`pytest tests/unit/test_tool_protocol.py`. These assert that a coordinate, a
lease, an unknown argument or a smuggled `confirmed` flag is **refused** rather
than partially applied, and that a malformed call never reaches the desktop.

For anything touching the §75 end-to-end path or the real desktop, also run
`BLAXCY_E2E_REAL_DISPLAY=1 pytest tests/e2e/test_fixture_confirmation_e2e.py` on
its own (see above). With the §46 occlusion fix in place this is **`3 passed, 1
skipped`** — the injection paths run. The remaining skip is only when a window is
genuinely in front of the fixture (the honest refusal, not the old defect).

For the **Phase 14 real-desktop benchmarks (§76)**, run
`python -m bench.real_desktop` for the read-only numbers, or
`python -m bench.real_desktop --confirm-real-input` to add the click/keyboard/
sequence ones (it injects real input into the section 74 fixture). Results are
recorded in `docs/benchmark_report.md` -> "Phase 14". Note the honest limit there:
a real-desktop 5-step `run_sequence` *happy path* is not achievable on that
fixture, because §60 verification refuses its sub-`MEANINGFUL` changes, so the
sequence halts at step 1 with `VERIFICATION_UNVERIFIED`.

For anything touching the batching layer (sequence_runner/resolver_cache/
speculative_perceiver): also run `pytest tests/safety/test_sequence_halts.py`
and confirm disabling the feature flag reproduces identical (only slower)
behavior.

For anything touching the installer or the desktop entry, also run
`pytest tests/unit/test_installer.py tests/integration/test_install_scripts.py`.
The integration file runs the *real* scripts into a throwaway prefix and actually
installs, launches, updates, injects a real failure to force a rollback, and
uninstalls, verifying each installed file's checksum. It installs nothing
system-wide and needs no network (it reuses `sys.executable`). Confirm too that
`desktop-file-validate` still accepts the installed entry and that a second
launch is refused (`pytest tests/unit/test_single_instance.py`).

## Source verification

For third-party APIs: inspect the installed version, inspect installed source
when required, consult official documentation, and implement only behavior
actually verified. Do not treat examples from old documentation as guaranteed
current APIs.
