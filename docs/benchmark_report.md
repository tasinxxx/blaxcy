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

Not yet measured (later phases / toward section 76's full list): accessibility
query, target resolution, lease validation, click/keyboard, verification,
end-to-end simple action, and the batching-layer measurements. Those subsystems
do not exist yet, so no number is reported for them.

## Reproducing

```bash
. .venv/bin/activate
python -m pytest tests/integration/test_frame_engine_real_display.py -v   # structural checks
python -m pytest tests/unit/test_ocr.py -v                              # OCR unit + real-backend test
# then re-run the inline capture/change-detection and OCR timing loops used above
```

A reusable benchmark runner arrives in Phase 14 (`bench/`, section 84).
