# BLAXCY — Benchmark Report

Every number below is a **measurement**, not a target, and is reported in the
form section 76 requires: `target, actual, machine, desktop/session, backend,
sample count`. Numbers are only valid for the environment recorded with them;
targets remain targets until re-measured in a new environment (section 4 rule 23).

## Environment (all measurements below)

| Field | Value |
|---|---|
| Machine | Kali GNU/Linux Rolling, kernel 7.1.5+kali-amd64 |
| Desktop / session | XFCE, X11 (`DISPLAY=:0.0`), single physical monitor eDP-1 1366x768 |
| Python | 3.14.6 (venv `.venv`, created with `--system-site-packages`) |
| Capture backend | `mss` 10.2.0 (virtual-desktop union, monitor 0) |
| Change detection | `opencv-python-headless` 5.0.0, `numpy` 2.4.6 |
| Date | 2026-09-23 |

## Phase 2 — frame capture and change detection

Command: a one-off inline script using `FrameEngine(CaptureSettings())` and
`ChangeDetector(PerceptionSettings())` against the live desktop, after a warm-up
grab (`n = 200`, change detection `n = 199`).

| Measurement | Target | Actual | Notes |
|---|---|---|---|
| Full-frame capture (`mss`) | none set | p50 **5.73 ms**, p95 **9.80 ms**, min 5.04 ms, max 10.98 ms | 1366x768 BGRA + 480x270 grayscale thumbnail |
| Change detection (`absdiff` → 30x30 tiles → classify) | ≤ 3 ms | p50 **0.050 ms**, p95 **0.176 ms**, min 0.048 ms, max 1.01 ms | 480x270 grayscale |
| Effective capture rate (measured) | 2 FPS idle (target) | **158.4 FPS** observed back-to-back | `measured_fps` is a measurement, never a promise; the idle *profile* simply sleeps to 500 ms between captures |
| Change classes over a mostly-static desktop | n/a | 189 × `NONE`, 10 × `TRIVIAL` | `MEANINGFUL`/`MAJOR` are not expected on a still desktop |
| Capture errors | 0 | **0** | no backend reinitialization was needed during the run |

Change-detection target `≤ 3 ms` is met with roughly two orders of magnitude of
headroom on this machine. Capture is dominated by `mss`/XShm and is the number
that matters for the section 33 adaptive profiles: at ~5.7 ms per grab, the
idle/normal/active targets (2/10/30 FPS) are all comfortably reachable and the
engine is rate-limited by its sleep schedule, not its capture cost.

## Phase 5 — OCR (fallback only)

Command: a one-off inline script that renders the text `Play` into a
full-desktop `1920x1080` frame and calls `OcrEngine.recognize` over a single
`240x80` DESKTOP region. "Cold" constructs a fresh engine (empty cache) per
sample; "warm" repeats against one warmed engine. Backend `pytesseract`
0.3.13 / Tesseract 5.5.0.

| Measurement | Target | Actual | Notes |
|---|---|---|---|
| Cold OCR, one `240x80` region | ≤ 300 ms | p50 **116.63 ms**, p95 **162.30 ms**, min 99.75 ms, max 466.89 ms (n = 200) | full engine path: crop → hash → Tesseract → DESKTOP mapping |
| Warm OCR, same region (cache hit) | — | p50 **0.0333 ms**, p95 **0.0616 ms** (n = 200) | xxhash pixel-keyed cache; 200/200 hits, 0 extra Tesseract calls |
| Recognised text | n/a | `'Play'` | asserted on every cold sample |

The section 76 OCR target `≤ 300 ms` is met at p95 with headroom. The warm
number is the point of the section 40 region-hash cache: unchanged regions skip
Tesseract entirely, which is what keeps OCR from becoming a latency tax when it
is invoked repeatedly during a task.

## Phase 9 — emergency stop latency (section 63)

Command: a one-off inline script wiring a real `XtestBackend` into
`EmergencyStop` (registered as an input releaser, with a real `ModeController`
and `EventBus`), triggering the stop 20 times with a re-arm between samples
(`n = 20`). The measured value is the full section 63 sequence: latch → stop
execution → release buttons → release keys → cancel loop → force OBSERVE → emit.

| Measurement | Target | Actual | Notes |
|---|---|---|---|
| Emergency stop, full sequence, real backend | ≤ 150 ms | p50 **0.014 ms**, p95 **0.027 ms**, min 0.013 ms, max 0.030 ms | `XtestBackend` functional (`xtest_version` 2.2) and registered as a releaser; every sample reported `errors == ()` |

Read this number honestly: the latency is short because **nothing was held** at
the moment of the stop, so the release path on the real backend had no button or
key to lift and no blocking X round-trip to wait on. The measurement therefore
proves the *control path* — latch, sequence, forced OBSERVE, event — is far
cheaper than the 150 ms target, and it does **not** prove the release cost of a
stop taken mid-drag with a modifier held down. That case needs a device it can
hold and release for real, which this benchmark deliberately did not inject into
the live desktop; the target stays a target for it.

## Clipboard-assisted typing (section 50, delivered after Phase 9)

Command: a one-off inline script that drives the **real**
`X11ClipboardPaster` through `KeyboardController` with the Ctrl+V keystroke going
to `FakeInputBackend` (`n = 10` per configuration). Nothing was injected into the
live desktop; everything else -- taking the CLIPBOARD selection, serving the
request, handing it back -- really happened against the running X server.

| Measurement | Target | Actual | Notes |
|---|---|---|---|
| Clipboard probe (`probe_x11_clipboard`, live display) | none set | p50 **4.89 ms**, p95 **8.82 ms**, min 3.30 ms, max 68.91 ms (n = 20) | connects, interns the selection/target atoms, creates the owner window, reads the current owner; takes ownership of nothing |
| Clipboard-assisted typing, default settings | none set | p50 **406.04 ms**, p95 406.88 ms, min 402.44 ms, max 408.55 ms (n = 10) | dominated by `paste_grace_ms = 400`: 2 selection requests were served, text handed back |
| Same path with `paste_grace_ms = 0` and `restore_grace_ms = 0` | none set | p50 **6.13 ms**, p95 6.24 ms, min 2.51 ms, max 6.32 ms (n = 10) | **0 selection requests were served** |

The zero-grace row is the interesting one, and it is why the default is not zero.
An X selection is served lazily: the target application asks the owner for the
data *after* the paste keystroke has been processed. Stopping the serve loop as
soon as the keystroke is flushed leaves nobody to answer, so the paste silently
pastes nothing -- measurably, not theoretically. The default 400 ms buys that
answer, and the whole cost is paid once per long/non-ASCII typing request (a path
that exists only for text the keyboard mapping cannot produce).

Not measured here: the round trip through a *real* application's paste handler
(that would require injecting Ctrl+V into the live desktop, which this
benchmark deliberately does not do), and the target-TO-clipboard handshake time
on other desktops. The window is configurable in `[clipboard]` for exactly that
reason.

## Phase 10 — Brain / tool-protocol overhead (in-process)

Command: a one-off inline script measuring the Phase 10 layer **without a
network model and without a real desktop**: the tool dispatcher runs over the
project's fake input backend and a scripted perception state, and the Gemini
measurement is the request *translation* only (`_to_contents`), not a
`generate_content` call. These numbers therefore describe BLAXCY's own overhead,
which is the part BLAXCY can be held to; the model round trip is the provider's
and is excluded by design.

| Measurement | Target | Actual | Notes |
|---|---|---|---|
| Tool-call parse (`click` description → `PlannedAction`) | none set | p50 **0.0109 ms**, p95 **0.0304 ms**, max 0.0703 (n = 200) | includes strict key/role validation |
| Tool declaration build (19 declarations) | none set | p50 **0.0174 ms**, p95 **0.0183 ms** (n = 200) | rebuilt per model turn on purpose; may be cached later |
| Read-path dispatch `get_screen_state` (2 elements) | none set | p50 **0.0287 ms**, p95 **0.0670 ms** (n = 200) | full read path: bounded slot, abort check, element summaries |
| Read-path dispatch `find_element` | none set | p50 **0.0865 ms**, p95 **0.1679 ms** (n = 200) | includes the section 43 resolver over the candidate set |
| Context state view, unchanged desktop | none set | p50 **0.0014 ms**, p95 **0.0016 ms** (n = 200) | the section 68.1 "unchanged" marker instead of a full re-send |
| Context fit (6-turn conversation) | none set | p50 **0.0316 ms**, p95 **0.0574 ms** (n = 200) | budget accounting and summary generation |
| Gemini request translation (6 turns) | none set | p50 **0.1449 ms**, p95 **0.3450 ms** (n = 200) | neutral turns → SDK contents; **no network call** |

Interpretation, stated narrowly: BLAXCY's share of a Brain round trip is
sub-millisecond per turn on this machine, so the latency a user actually feels
is the model round trip and the physical action, not the protocol layer. No
target is claimed for any of these because section 76 sets none for them; they
are recorded so a later change can be compared against something real.

Not measured here (and not claimed): any end-to-end task latency involving a real
Gemini call (that needs a stored API key and network access, neither of which is
available in this environment). The section 76 batching measurements are recorded
in the Phase 10.1 section below.

Measured **on the real desktop** on 2026-09-25 (section "Phase 14" below):
accessibility query, target resolution, lease validation, end-to-end
click/keyboard, and the real-desktop end-to-end `run_sequence`. The Phase 8
modules (resolution/lease) now have real numbers. The one target that is **not**
met on this host is the real-desktop 5-step `run_sequence` happy path, and the
reason is recorded rather than hidden: section 60 verification (correctly) refuses
the fixture's sub-`MEANINGFUL` changes, so the sequence halts at step 1 with
`VERIFICATION_UNVERIFIED` instead of completing.

## Phase 10.1 — Batching & throughput overhead (in-process)

Command: a one-off inline script over the Phase 10.1 harness
(`tests/harness/phase101.py`): the **section 74 five-control workflow** (search
icon → search field → submit → result list → play) submitted as a single
`run_sequence` through the real `ToolDispatcher`, over the project's fake input
backend and scripted perception. Nothing here injects real input and no model is
called, so these numbers describe BLAXCY's own per-sequence cost (policy gate →
per-step resolve → lease → revalidate → input → verify) rather than Gemini or a
real desktop. Each sample runs a fresh five-step workflow against a freshly
scripted desktop, and every one of the 200 samples completed with all five steps
verified (`failures = 0`). The `≤ 4000 ms` target is section 76's; the in-process
figures clear it by three orders of magnitude precisely because they exclude the
real capture/AT-SPI/input latency the target is really about.

| Measurement | Target | Actual | Notes |
|---|---|---|---|
| 5-step `run_sequence`, cold (no cache, no speculation) | ≤ 4000 ms | p50 **3.238 ms**, p95 **5.066 ms**, min 2.395, max 6.519 (n = 200) | full per-step pipeline in one Brain round trip |
| 5-step `run_sequence`, warm resolver cache (§43.1) | ≤ 4000 ms | p50 **3.283 ms**, p95 **4.864 ms** (n = 200) | hit rate **0.995** (796 hits / 4 misses), **0 invalidations** |
| 5-step `run_sequence`, warm cache + speculation (§33.2) | ≤ 4000 ms | p50 **4.291 ms**, p95 **6.723 ms** (n = 200) | inline worker (see caveat); hit rate **0.9975**, **0 invalidations** |
| Resolver-cache hit rate (warm) | none set | **0.995** (cache alone), **0.9975** (cache + speculation) | 4 targeted steps per pass; 4 misses total across 200 passes |
| Speculative-perception usefulness (§76) | none set | `taken` 400, `useful` 400, `discarded_change` 200 → usefulness rate **1.0 of taken hints** | now measured, not structurally `0.0` |

Interpretation, stated narrowly: the batching layer removes the Brain round trip,
not the per-step work — the runner overhead itself is microseconds, so the wall
clock a user feels is still the physical action and the model turn. The cache and
the speculation are *hint* layers and here they buy little **because the harness
has no real AT-SPI/OCR traversal to skip**; their value is bounded by the cost of
the work they remove, which is exactly why an unproven optimization ships
disabled (section 4 rule 31) until it is measured against something real.

Honest caveats for these numbers:

- In-process with fakes: real capture, AT-SPI traversal, OCR and input injection
  are excluded, as is any Gemini call. No number here is a real-desktop claim.
- The speculation worker runs **inline** in the harness (`background=False`, for
  deterministic tests). Production runs `background=True` (`T-SPECULATE`), so the
  ~1 ms of speculation cost above is an upper bound, not a production figure.
- In this repeated-workflow loop the resolver cache would also hit on repeat, so
  `useful` claims the narrower, defensible thing: the taken hint was **consumed**
  by that step's live resolution (a cache HIT), not that it was the sole reason.
- Defect found *by* running this measurement and fixed in the same session: the
  sequence runner primed the §43.1 cache with `recorded_at = 0.0`, so every
  primed hint was **expired on arrival** — it could never be consumed and instead
  charged a miss plus an invalidation on the next lookup. Measured effect:
  enabling speculation *dropped* the warm hit rate from 0.995 to **0.4975** and
  added **400 invalidations**; after the fix (`ResolverCache.prime` stamps the
  time itself, as `record` already did) the warm hit rate holds at **0.9975** with
  **0 invalidations** and the sequence is ~1.6 ms/run faster. Regression test:
  `tests/unit/test_resolver_cache.py::test_a_primed_hint_is_stamped_fresh_by_the_cache_not_by_its_caller`.

## Phase 14 — real-desktop end-to-end (section 76)

Command (2026-09-25):

```bash
. .venv/bin/activate
python -m bench.real_desktop                                  # read-only
python -m bench.real_desktop --confirm-real-input \
    --samples 200 --input-samples 50 --sequence-samples 20     # injects real input
```

This is the **reusable** Phase 14 runner (`bench/real_desktop.py`, section 26/84).
It drives the production composition (`BlaxcyApplication`) over the live X11
desktop and the section 74 fixture, with the real `XtestBackend`. Read-only
benchmarks inject nothing; the click/keyboard/sequence benchmarks are opt-in and
re-raise the fixture before each *timed* dispatch (the re-raise is outside the
timer, so the measured action is still the action). Sample counts are stated; the
section 76 form is `target, actual, machine, desktop/session, backend, n` —
machine/session/backend are the report's Environment table (`mss` capture, real
XTEST, XFCE X11).

| Measurement | Target | Actual | Notes |
|---|---|---|---|
| Accessibility query (live AT-SPI refresh) | none set | p50 **760.58 ms**, p95 **1080.42 ms**, min 606.57, max 1614.32 (n = 200) | traversal budget raised to 8000 ms to reach the fixture (section 35); 395–621 elements |
| Target resolution (live elements, `occluders=all`) | none set | p50 **2.51 ms**, p95 **4.34 ms** (n = 200) | `'Applications'` over 395 live elements |
| Lease revalidation (section 45 checklist, 11 checks) | ≤ 25 ms | p50 **1.04 ms**, p95 **2.16 ms** (n = 200) | genuinely **met** on a real desktop |
| End-to-end click (resolve→lease→revalidate→click→verify) | none set | p50 **220.20 ms**, p95 **1332.77 ms** (n = 50) | **5/50 VERIFIED**; 45 refused `VERIFICATION_UNVERIFIED` |
| End-to-end keyboard (`type_text` + verify) | none set | p50 **112.52 ms**, p95 **1179.69 ms** (n = 50) | **3/50 VERIFIED**; 47 refused `VERIFICATION_UNVERIFIED` |
| 5-step `run_sequence`, real desktop | ≤ 4000 ms | p50 **99.01 ms**, p95 **133.31 ms**, min 66.76, max 1105.42 (n = 20) | **0/20 completed**: every run halts at step 1 with `VERIFICATION_UNVERIFIED` |
| Section 74 workflow (`Search`→type→`Submit`→`Results`→`Play`), real desktop | n/a | **TARGET_AMBIGUOUS** at w1 (`13.25 ms`) | role-hinted `"Search"` matches several desktop buttons (section 43's rule fails closed) |

Interpretation, stated narrowly and honestly:

- The **lease revalidation target (≤ 25 ms) is met** on the real desktop, and the
  read-only Phase 8 numbers (resolution p50 ≈ 2.5 ms, revalidation p50 ≈ 1.0 ms)
  show the targeting layer is not the cost — capture/AT-SPI is. That is the real
  justification for the section 43.1 cache and the section 33.2 speculation: they
  exist to shorten the *traversals*, which is where the time actually is.
- The end-to-end click/keyboard figures (**p50 ≈ 220 ms / 113 ms**) are the real
  cost of `perceive → resolve → lease → revalidate → inject → observe → verify` on
  this host, and the actions really execute: the fixture's own counters prove the
  input landed. They are reported `VERIFICATION_UNVERIFIED` because the fixture's
  small controls produce only `TRIVIAL`/`ANIMATION` changes, which section 60
  correctly refuses to call a user-visible outcome. **This is the honest result,
  not a benchmark harness failure** — inflating it to `VERIFIED` would be exactly
  the fake success section 4 rule 8 forbids.
- The **real-desktop 5-step `run_sequence` happy path is therefore not achieved on
  this fixture**: with `require_verification_for_mutating = true` (the default), a
  mutating step whose own change is sub-`MEANINGFUL` is `UNVERIFIED`, and section
  66.1 halts the sequence rather than stepping onto an unconfirmed premise. The
  ~99 ms p50 is the honest cost of *reaching* that fateful step-1 decision. A
  happy-path number requires a workload whose controls produce `MEANINGFUL` changes
  at their own boxes; the section 74 fixture (small buttons, small fields) is not
  such a workload, and section 76 says a target stays a target until re-measured.
- The workflow's `TARGET_AMBIGUOUS` is the known **over-broad section 43 ambiguity
  scope** (recorded in `CONTINUATION_STATE.md`): the duplicate `(label, role)` rule
  counts every scored candidate, so a role hint on a desktop with several
  `"Search"`-named buttons is ambiguous even when the text match is decisive. It
  fails closed, so it is safe, but it is why a realistic browser workflow cannot be
  driven on this desktop.
- Prerequisite fixed first (see `CONTINUATION_STATE.md`): section 46 occlusion used
  to refuse **every** real target (`ratio=1.00`), which is why the real-desktop
  numbers above could not be produced earlier. It is now resolved (ancestor
  exclusion + pid-based window attribution); this run is the proof, and the §75
  fixture injection paths moved from **1 passed / 3 skipped** to **3 passed / 1
  skipped** in the same session.

## Reproducing

```bash
. .venv/bin/activate
python -m pytest tests/integration/test_frame_engine_real_display.py -v   # structural checks
python -m pytest tests/unit/test_ocr.py -v                              # OCR unit + real-backend test
python -m pytest tests/integration/test_emergency_stop_real_display.py -v  # section 63, read-only
python -m bench.real_desktop --confirm-real-input                       # Phase 14, real desktop
# then re-run the inline capture/change-detection and OCR timing loops used above
```

`bench/real_desktop.py` is the reusable section 84 Phase 14 runner.
