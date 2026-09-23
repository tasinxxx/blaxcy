# BLAXCY

BLAXCY is a Linux-native AI computer-control **BODY**. It is not a reasoning
model: a connected AI (the **Brain**) decides *what* should happen, and BLAXCY
decides *whether and how* that can happen safely, on a real Linux desktop, with
a real, verified outcome.

```
BRAIN     decides WHAT
BODY      decides HOW SAFELY
EYE       determines WHAT IS ACTUALLY THERE
EXECUTOR  performs the physical action
VERIFIER  determines WHETHER IT ACTUALLY WORKED
RECOVERY  decides WHETHER A SAFE RETRY IS POSSIBLE
```

## Status

The project is in **Phase 0 — repository, harness, and probes**. No physical
input is implemented yet; BLAXCY currently reports what this machine can
actually do, honestly, and refuses to claim anything it has not probed.

See `CONTINUATION_STATE.md` for the current phase, evidence, and next action,
and `knowledge.md` for orientation.

## Requirements

- Linux with Python 3.12+
- A desktop session (X11 primary; Wayland support is degraded until Phase 2/7)
- System accessibility packages for AT-SPI: `python3-gi`, `gir1.2-atspi-2.0`,
  `at-spi2-core`
- Optional runtime tools: `xdotool`, `xclip`, `tesseract-ocr`,
  `tesseract-ocr-eng`, `wmctrl`

## Quickstart

The virtualenv must be able to see the system GI/AT-SPI packages:

```bash
python3 -m venv --system-site-packages .venv
. .venv/bin/activate
pip install -r requirements.txt
```

Then run:

```bash
python main.py probe      # functional capability report (no input injected)
python main.py session    # detected session / environment facts
python main.py config     # resolved configuration and its source path
```

`make check` runs lint, type-check, and tests.

## What BLAXCY will never do

BLAXCY never fakes execution, status, perception, benchmarks, or completion.
Any unsupported capability reports `UNAVAILABLE` or `DEGRADED` with a reason.
No physical input ever occurs without policy approval, target validation, a
valid lease, revalidation, and post-action verification.
