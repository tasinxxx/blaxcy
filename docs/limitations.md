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
| **Realistic §74 workflow happy path** | Open (one of three causes fixed, not re-measured) | `--workload workflow-verifiable` completed **2/20** five-step sequences; halted on `s2:FOCUS_MISMATCH` ×8, `s2:VERIFICATION_CONTRADICTED` ×4, `s4:VERIFICATION_UNVERIFIED` ×6. The `FOCUS_MISMATCH` cause is now **root-caused and fixed**: the §35 element cache did not subscribe to `object:state-changed:focused`, so a click that moved focus was invisible and §51's guard refused to type into a field that really was focused, for up to `cache_ttl_seconds` (2 s) — longer than the §45 action-state-age ceiling. The two remaining causes (the live AT-SPI read of a typed field lagging the injection, and a `LIST_ITEM` selection not repainting `MEANINGFUL`-ly at its own box) are unfixed. **The 2/20 number has not been re-measured** since the fix, so the row stays open. The synthetic `verifiable` workload still meets the §76 target (20/20). See `CONTINUATION_STATE.md` "Known failures". |
| **Wayland** | Not implemented | There is no portal/`libei` input path. `Control` is X11/XTEST only. No Wayland support is claimed (§30, §79). |
| `activate_element` | **Implemented** | `core/accessibility.py` invokes the application's own AT-SPI action; the executor reaches it only after policy → resolve → lease → revalidation, and verifies it like any other `MUTATING` tool. It injects **no** pointer or key input, which is the point: no coordinates and no pointer occlusion. Verified **live** on the §74 fixture (`tests/integration/test_workflow_controls_real_display.py::test_the_live_submit_control_is_really_activated_through_atspi`). A backend that lacks the optional `activate` capability reports a structured `UNAVAILABLE` rather than substituting a click. |
| **Browser accessibility** | UNAVAILABLE | Runtime-probed and reported honestly; no browser was relaunched or configured. |
| **Visual grounding** | UNAVAILABLE | No Gemini API key is stored on this host. The implementation, gating and privacy rules exist and are tested; the capability is reported UNAVAILABLE with the reason. |
| **`--system-deps`** | Reports only | It detects the package manager and states what is wanted; it installs nothing unattended. No system-wide install has been performed (verified installs use throwaway prefixes). |
| **`RecoveryController.begin_task`/`begin_step`** | Uncalled | Per-task/per-step budget scoping has no caller yet; the loop guard is the binding constraint. |
| **Clipboard paste end-to-end** | Untested seam | The controller→paster contract and the real X11 selection transfer are tested; injecting `Ctrl+V` into a real application's own paste handler is not (that would inject into the live desktop, which the suite never does outside the fixture harness). |
| **Flaky test** | Known | `tests/integration/test_clipboard_real_display.py::test_the_previous_content_is_served_during_the_restore_grace` fails roughly 1 run in 5 on this host, because `xfce4-clipman` races the restore grace. The mechanism is asserted deterministically in `tests/unit/test_clipboard.py`. |

## Environment blockers

- **AT-SPI state-change subscriptions.** The element cache invalidates on any
  observed AT-SPI event, so the subscription list decides which changes it can
  see. It now includes the state changes an action gates on (`focused`,
  `enabled`, `sensitive`, `editable`); `checked`/`selected` are still not
  subscribed because no pre-input gate reads them. A focus/enabled change that
  arrives as some other event type would still be missed.
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
| **Phase 14 (§76 benchmarks)** | **PARTIAL.** Every read-only and real-input number is measured and recorded; the realistic workflow happy path is 2/20 (above). |
| **Phase 15 (full matrix / soak / documentation)** | **NOT STARTED** beyond this documentation set. No soak run has been performed. |
| **Batching-layer safety-neutrality proof** | **Met** by `tests/safety/test_sequence_halts.py` plus the resolver-cache and speculative-perceiver suites, which assert that no cached or speculative result reaches input without a fresh live resolution, lease and revalidation. |
| **Git state / secrets in history** | The tree is **uncommitted** on `5e06bc0`; no secret material is present in source, config or tests (the API key lives only in the OS keyring). |

**Conclusion:** BLAXCY is a working, safety-gated Body with every §66 tool
implemented. It is **not complete**: Phase 15 has not been run, one §76 row is
measured but unmet, Wayland has no input path, and the documented `pytest`
command is blocked on a host package.
