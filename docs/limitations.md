# BLAXCY — Limitations

> Required by the specification §26, and by §85 ("limitations documented"). This
> file states what BLAXCY does **not** do, with the evidence for each claim.
> Nothing here is aspirational: if a row says a thing works, a command in this
> repository demonstrates it; if it says a thing is missing, it is missing.

Environment for every measurement below: Kali GNU/Linux Rolling, kernel
7.1.5+kali-amd64, XFCE on **X11** (`DISPLAY=:0.0`), Python 3.14.6, `.venv`
created with `--system-site-packages`, real XTEST 2.2, live AT-SPI.

## Known functional gaps

| Gap | Status | Evidence / detail |
|---|---|---|
| **Realistic §74 workflow happy path** | **Resolved 2026-09-26 — now met at 20/20** | Pre-fix, `--workload workflow-verifiable` completed **2/20** five-step sequences; halted on `s2:FOCUS_MISMATCH` ×8, `s2:VERIFICATION_CONTRADICTED` ×4, `s4:VERIFICATION_UNVERIFIED` ×6. After the first three fixes it reached **5/20**, the halt set narrowed to `s2:FOCUS_MISMATCH` ×15 (standalone click and keyboard both **30/30 VERIFIED**, up from `5/50` and `1/30`). All four causes are root-caused and fixed: (a) the §35 element cache was never subscribed to `object:state-changed:focused`, so a click that moved focus was invisible and §51's guard refused to type into a field that really was focused; (b) verification judged typed text from a single read taken immediately after injection, which the element cache could serve *before* the action, so a lagging read was reported as a contradiction; (c) a result click's repaint was reclassified `ANIMATION` by the §34 temporal layer when the previous step had changed that region as a side effect, so a real selection was `UNVERIFIED`. The fixes are: subscribe the action-gating state changes (incl. `focused`/`selected`); re-check a negative verdict against fresh, live observations for `verify_settle_ms` before reporting it (§60's `CONTRADICTED` needs positive evidence); verify a click on a selectable control from the control's own `selected` state, positive-only, falling back to the pixel check; and re-check a would-be §51 focus refusal against a fresh live perception before accepting it. **Re-measured 2026-09-26: 20/20 completed, p50 1342.94 ms / p95 1787.70 ms — the ≤ 4000 ms target is met.** No §60 threshold or §51 rule was loosened; a genuinely unfocused target is still refused. The synthetic `verifiable` workload still meets the §76 target (20/20). See `CONTINUATION_STATE.md` "Known failures". |
| **Wayland** | Not implemented | There is no portal/`libei` input path. `Control` is X11/XTEST only. No Wayland support is claimed (§30, §79). |
| `activate_element` | **Implemented** | `core/accessibility.py` invokes the application's own AT-SPI action; the executor reaches it only after policy → resolve → lease → revalidation, and verifies it like any other `MUTATING` tool. It injects **no** pointer or key input, which is the point: no coordinates and no pointer occlusion. Verified **live** on the §74 fixture (`tests/integration/test_workflow_controls_real_display.py::test_the_live_submit_control_is_really_activated_through_atspi`). A backend that lacks the optional `activate` capability reports a structured `UNAVAILABLE` rather than substituting a click. |
| **Browser accessibility** | UNAVAILABLE | Runtime-probed and reported honestly; no browser was relaunched or configured. |
| **Visual grounding** | UNAVAILABLE | No Gemini API key is stored on this host. The implementation, gating and privacy rules exist and are tested; the capability is reported UNAVAILABLE with the reason. |
| **`--system-deps`** | Reports only | It detects the package manager and states what is wanted; it installs nothing unattended. No system-wide install has been performed (verified installs use throwaway prefixes). |
| **`RecoveryController.begin_task`/`begin_step`** | **Wired** | The composition root builds the controller (`core/application.py`) and passes it to both the executor and the `SequenceRunner`, which calls `begin_task`/`begin_step`/`end_step` so each step gets its own budget (§61). Corrected during the 2026-09-26 §85 walk: this row previously said "uncalled", which was stale. |
| **Clipboard paste end-to-end** | Untested seam | The controller→paster contract and the real X11 selection transfer are tested; injecting `Ctrl+V` into a real application's own paste handler is not (that would inject into the live desktop, which the suite never does outside the fixture harness). |
| **Flaky test** | Known | `tests/integration/test_clipboard_real_display.py::test_the_previous_content_is_served_during_the_restore_grace` fails roughly 1 run in 5 on this host, because `xfce4-clipman` races the restore grace. The mechanism is asserted deterministically in `tests/unit/test_clipboard.py`. |
| **Flaky test (fixture startup)** | Known, observed once | Under load right after a long real-input soak, `tests/integration/test_workflow_controls_real_display.py` can fail during fixture startup: the child exits cleanly (`code 0`) before the first command, because the fixture exits on stdin EOF. Observed once on 2026-09-26, green on immediate re-run and in the next full-suite run; the fixture binary itself started and reported its inventory every time. |

## Environment blockers

- **AT-SPI state-change subscriptions.** The element cache invalidates on any
  observed AT-SPI event, so the subscription list decides which changes it can
  see. It now includes the state changes an action or a verdict depends on
  (`focused`, `enabled`, `sensitive`, `editable`, `selected`); `checked` is still
  not subscribed because no gate or postcondition reads it. A change that arrives
  as some *other* event type would still be missed -- the cache is a hint, which is
  why a negative verdict is re-checked against a live read (see `verify_settle_ms`
  below).
- **Negative verdicts are given a settle window.** `[verification] verify_settle_ms`
  (default 1000) re-checks an `UNVERIFIED`/`CONTRADICTED` verdict against fresh,
  live observations before reporting it, because one read taken immediately after
  injection is not positive evidence. The cost is paid only on a would-be failure;
  a `VERIFIED` verdict never waits. Set it to `0` to disable the re-check and get
  the old single-read behaviour.
- **Live elements are often unattributed to a window.** On this host every
  perceived element can carry `owner_window_id = None` (146/146 measured) while the
  state has a real `active_window_id`. The section 43.1 cache therefore keys the
  window component on the observation's active window as a fallback; before
  2026-09-26 it keyed on the element alone and could never hit on a live desktop
  (fixed — see `docs/benchmark_report.md` "Phase 15"). The same fact is why section
  46 occlusion relies on the pid path rather than window titles.
- **The documented bare `pytest` command does not run on this host.** PySide6
  6.10.3 (apt) ships QtCore/QtGui/QtWidgets but not `QtTest`, so the installed
  `pytest-qt` plugin aborts in `pytest_configure` before collecting anything.
  The suite is green with `-p no:pytest-qt`. Fixing it needs one human command:
  `sudo apt-get install -y python3-pyside6.qttest`. No delivered feature depends
  on it — the GUI tests build their own offscreen `QApplication`.

## Repository-naming deviations from §26

§26 sketches a target layout. Where the implementation differs in *name*, the
capability is present under a different module unless marked missing:

- `core/eye.py` — not present; the role is `core/perception.py` +
  `core/frame_engine.py`.
- `security/secrets.py` — not present; secret handling is
  `security/keyring_manager.py`.
- `control/backends/portal_remote_desktop.py` — not present (Wayland, above).
- `bench/` — the spec lists five modules; the implementation has one reusable
  runner, `bench/real_desktop.py`.

## §85 release-readiness reconciliation

Walked against the acceptance criteria, with the honest verdict for each. An
unresolved item below prevents a claim of full completion.

| Criterion | Verdict |
|---|---|
| **Functional** — real capture, perception, resolution, input, verification, recovery, Brain loop, `run_sequence` with genuine per-step re-resolution | **Met.** Real XTEST click 30/30 VERIFIED on the real desktop; `run_sequence` dispatches every step through the same dispatcher. Every §66 tool now has a real implementation, `activate_element` included. |
| **Safety** — OBSERVE blocked, ASSIST confirmation, AUTONOMOUS expiry, stop and takeover (incl. mid-sequence), password/terminal/blocked-app protection, no stale or ambiguous click, identical standalone vs batched | **Met** by `tests/safety/` (77 tests, including the §66.1 halt properties). |
| **Reliability** — no unbounded queue, no stale overwrite, no stuck button/key, no infinite retry, no orphan watchdog, no fake verification, no silently dropped step failure, no cache/speculation reaching input without a live re-check | **Met** by the reliability suites (state cache, action tracker, watchdog, emergency stop, resolver cache, speculative perceiver, sequence halts). |
| **Privacy** — protected content redacted, password fields never uploaded, secrets never logged, no continuous streaming | **Met**, and the logging half is newly implemented: `core/logging_setup.py` + `security/redaction.py`, enforced by tests that assert a registered secret and a password-named field never reach the log file. |
| **Engineering** — test coverage, strict typing, lint passes, no production TODO/FIXME/stubs, benchmarks documented, limitations documented | **Met** for lint/type/tests/benchmarks/limitations. Coverage is meaningful but is *not* reported as a percentage; no coverage gate exists. |
| **Installation** — menu entry, startup, controlled second launch, uninstall, update/rollback | **Met**, verified against throwaway prefixes with a per-file sha256 manifest. |
| **Phase 14 (§76 benchmarks)** | **Met.** Every read-only and real-input number is measured and recorded, and the realistic workflow happy path is 20/20 (above), inside the ≤ 4000 ms target. |
| **Phase 15 (full matrix / soak / documentation)** | **IN PROGRESS.** A soak runner now exists (`bench/soak.py`) and has been run: 30 iterations of the section 74 workflow, **30/30 stable**, flat latency and flat steady-state memory (see `docs/benchmark_report.md` "Phase 15"). Remaining Phase 15 scope: the full environment/capability matrix, and a longer soak than the ~40 iterations run so far. |
| **Batching-layer safety-neutrality proof** | **Met** — walked feature by feature in the section below, with the code path and the test that pins each claim. |
| **Git state / secrets in history** | Recovery checkpoint `372879a` (the three-halt-causes fix, re-measured) and the §51 focus re-check commit that follows it sit on top of `0b86d56`. No secret material is present in source, config or tests (the API key lives only in the OS keyring). |

## §85 batching safety-neutrality proof

Required by §85 ("the batching layer's own safety-neutrality proof"). The claim
under test is that **no batching, caching, speculative or concurrency feature adds
a path by which any action reaches the desktop without Policy → Resolve → Lease →
Revalidate → Execute → Verify**. Each row below names the code that would have to
be wrong for the bypass to exist, and the test that would catch it.

**The container.** `control/sequence_runner.py::_run_step` dispatches through the
**same** `ToolDispatcher` a standalone call uses, which routes to
`Executor.execute` — there is no second pipeline to drift. `step_arguments()`
strips only the runner's own bookkeeping (`step_id`, `tool`); `target`/`role` stay
because they are *descriptions*. No coordinate, `element_id` or lease is ever
carried between steps, and `run()` does `del confirmed`, so a plan can never arrive
pre-authorised for a destructive step.

| Feature | What it could bypass | Why it cannot | Pinned by |
|---|---|---|---|
| **§66.1 `run_sequence`** | reusing a lease or coordinate across steps | every step re-enters the dispatcher → executor, which issues a fresh lease from its own live resolution and runs the full §45 checklist | `tests/safety/test_sequence_halts.py` (11), `tests/unit/test_sequence_runner.py` (21), `tests/unit/test_sequence_dispatch.py` (11) |
| **§43.1 resolver cache** | serving a stale target as authoritative | `TargetResolver._lookup` returns an identity-path hint only; `resolve()` still scores **every** element and applies the same ambiguity rule, `act_threshold` and occlusion. The lookup is attached as metadata. A hint whose path is gone is `forget`-ten. `PASSWORD_INPUT` is never stored (`_NEVER_CACHED_ROLES`) | `test_enabling_the_cache_cannot_change_any_targeting_decision` (status, winner, candidate order and every score, across fresh / disabled / enabled; §4 rule 31), `test_a_disabled_cache_stores_and_returns_nothing`, `test_a_new_generation_invalidates_every_hint` |
| **§33.2 speculative perception** | using a pre-computed element for input | `speculate()` schedules read-side work only; `take()` is consulted only in `_take_speculation`, and its result is (a) an envelope `speculative_hint` *note* and (b) a `prime()` into the §43.1 cache — itself only a hint. Discarded on generation mismatch or a `MEANINGFUL`/`MAJOR` change; credentials are never speculated | `test_a_credential_description_is_never_speculated`, `test_a_structural_change_discards_a_hint_and_a_trivial_one_does_not`, `test_a_stale_generation_discards_the_hint`, `test_the_background_worker_never_blocks_the_caller` |
| **§33.1 capture boost** | skipping a check to go faster | `with self._boost():` wraps the step loop and changes only the capture profile; it reverts on every exit path (halt and exception included) and runs no safety check | `SequenceRunner._boost` / `run` |
| **§32.1 concurrent read-only dispatch** | letting a read substitute for fresh revalidation | `_dispatch_read_only` uses a separate bounded semaphore (a saturated path returns `RATE_LIMITED`, never queues forever) and a read lock; mutating work goes to `Executor.execute` on the single-writer path. Reads are preempted by a latched stop | `test_concurrent_read_only_calls_never_reorder_or_replace_physical_steps` (byte-for-byte the same input sequence as a sequential run), `test_read_only_tools_never_reach_the_executor`, `test_read_only_dispatch_is_bounded_by_configuration`, `test_a_latched_stop_preempts_a_read_only_call` |
| **§68.1 round-trip economy** | fewer round-trips meaning weaker checks | it governs only what the Brain is told and how many calls carry it; it has no desktop-path code. `to_model_context()` suppresses unchanged state and protected-app element content | `ai/context_manager.py`, `ai/prompt_builder.py` |

The 105 tests across `test_resolver_cache`, `test_speculative_perceiver`,
`test_sequence_runner`, `tests/safety/test_sequence_halts`, `test_sequence_dispatch`
and `test_tool_dispatch` pass. The real-desktop run recorded in
`docs/benchmark_report.md` is itself evidence: the workflow completed **20/20**
with the resolver cache, speculation, capture boost and read path all enabled, so
every step still resolved, leased, revalidated and verified live while the
optimizations were active.

**Conclusion:** each feature is a scheduling / traversal-order / hint change. None
removes or reorders a Policy, Resolve, Lease, Revalidate, Execute or Verify step,
and the one place a hint could have become authority — the resolver — is proven
decision-identical with the cache off.

**Conclusion:** BLAXCY is a working, safety-gated Body with every §66 tool
implemented. It is **not complete**: Phase 15 has not been run, Wayland has no
input path, and the documented `pytest` command is blocked on a host package. Every
§76 benchmark row is now measured and met.
