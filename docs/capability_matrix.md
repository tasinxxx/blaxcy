# BLAXCY — Capability Matrix (Phase 15)

> Required by §84 Phase 15 ("full matrix") and §85's release-readiness
> reconciliation. It states, per **session type** and per **backend**, what BLAXCY
> can actually do — and what it cannot — with the evidence for each cell.

## How to read this, and how to re-verify

Two axes:

- **Session type** — what the compositor/display server is: `X11`, `XWayland`,
  `Wayland`, `unknown` (`schemas/enums.py::SessionType`, detected by
  `core/session_detector.py`).
- **Capability → backend** — every capability reports exactly one of
  `AVAILABLE` / `DEGRADED` / `UNAVAILABLE` with a backend, latency, reason and fix
  hint (§28). A backend is never selected because a binary or module merely exists;
  only a passing *functional* probe selects it (§28 rule 12).

The **X11 column below is measured live** on this host and is reproducible:

```bash
python main.py probe          # the capability table
python main.py session        # the detected session facts
```

The XWayland and native-Wayland columns are **not verifiable on this host** (it has
no Wayland compositor). They state the *implementation* status honestly rather than
a measured one; a cell marked "not verified" is exactly that, and no support is
claimed for it (§4 rule 24, §79).

## Session support

| Session | Detected as | Capture | Control (mouse/keyboard) | Claimed? |
|---|---|---|---|---|
| **X11** | `x11` (primary path) | `mss` — **AVAILABLE**, measured live | in-process XTEST — **AVAILABLE**, measured live | yes |
| **XWayland** | `xwayland` (XWayland on a Wayland compositor) | would use the same `mss` path; **not verified here** | would use XTEST against the XWayland server; **not verified here** | **no** — untested, so unclaimed |
| **Wayland (native)** | `wayland` | **not implemented**: requires the XDG ScreenCast portal + PipeWire. The capture probe reports `UNAVAILABLE` with that fix hint on this session type. | **portal backend implemented** (`control/backends/portal.py`): `select_backend()` falls back to a functionally-probed `org.freedesktop.portal.RemoteDesktop`. Keyboard is `AVAILABLE` when the interface is present; the pointer is honestly `DEGRADED` until a ScreenCast stream node is wired (absolute motion). Session establishment needs interactive consent and is **not verified on this host** (see below). | **no** — the portal path is implemented and probe-tested, but its session/injection path is unverified here |
| **unknown** | `unknown` | `DEGRADED` / `UNAVAILABLE` from the probe, with the reason | `UNAVAILABLE` (`UnavailableBackend`) | **no** |

BLAXCY starts on any of these — on a session where no input backend passes, the
composition root composes `UnavailableBackend`, so read-only and `OBSERVE` work is
still available and injection fails as a structured error rather than silently.

## Capability × backend (X11 column measured live, 2026-09-26)

Probe result on this host: **AVAILABLE 9 · DEGRADED 0 · UNAVAILABLE 3**.

| Capability | Implemented backend | X11 (measured) | Latency | Other sessions |
|---|---|---|---|---|
| `capture` | `mss` (`core/frame_engine.py::MssBackend`) | **AVAILABLE** | 34.1 ms | Wayland: not implemented (portal + PipeWire); the probe says `UNAVAILABLE` with that hint |
| `accessibility` | AT-SPI via `gi.repository.Atspi` (`core/accessibility.py`) | **AVAILABLE** | 251.5 ms | same code path on XWayland; **not verified** there |
| `mouse` | in-process XTEST, else RemoteDesktop portal | **AVAILABLE** | — | Wayland: portal backend, **DEGRADED** until a ScreenCast stream node is wired (absolute pointer); probe checks the live interface |
| `keyboard` | in-process XTEST, else RemoteDesktop portal | **AVAILABLE** | — | Wayland: portal backend, **AVAILABLE** when the interface is present (keysym injection needs no stream) |
| `pointer_readback` | `python-xlib` | **AVAILABLE** | 2.3 ms | Wayland: **not implemented** (the portal has no readback) |
| `ocr` | `pytesseract` | **AVAILABLE** | 15.9 ms | session-independent |
| `clipboard` | X11 selection via `python-xlib` (`control/clipboard.py`) | **AVAILABLE** | 4.5 ms | Wayland: **not implemented** (portal clipboard) |
| `window_info` | EWMH via `python-xlib` (`control/window_manager.py`) | **AVAILABLE** | 2.7 ms | Wayland: **not implemented** (no EWMH) |
| `browser_accessibility` | derived from AT-SPI (`core/browser_accessibility.py`) | **UNAVAILABLE** | — | reason: no supported browser is currently exposing an accessibility tree; runtime-probed, nothing relaunched |
| `visual_grounding` | `google-genai` (`core/visual_grounder.py`) | **UNAVAILABLE** | — | reason: no Gemini API key stored; the implementation, gating and privacy rules exist and are tested |
| `sequence_execution` | `control/sequence_runner.py` | **AVAILABLE** | — | session-independent |
| `brain` | Gemini via `google-genai` (`ai/gemini_adapter.py`) | **UNAVAILABLE** | — | reason: no Gemini API key stored |

## Backends named by the specification but **not implemented**

§26/§30 name several backend modules. Only the ones below exist; the rest are
**absent, not stubs** — no dead file claims functionality (§4 rule 10):

| Named backend | Status |
|---|---|
| `control/backends/xtest.py` | **implemented** — the primary X11 input path |
| `control/backends/portal.py` | **implemented** — the XDG RemoteDesktop portal input path (the spec's `portal_remote_desktop` role) |
| `control/backends/xdotool.py` | **not implemented** — `xdotool` is a §30 *fallback*; the in-process XTEST path is the primary and no fallback is silently substituted. A subprocess-per-action path with no owned modifier state would duplicate — and weaken — XTEST, so it is not built while XTEST is available. |
| `control/backends/ydotool.py` | **not implemented** — a §30 *degraded* Wayland fallback needing a privileged `ydotoold`/uinput daemon; the RemoteDesktop portal is the primary Wayland path and is implemented |
| XShm / `ctypes` capture path | **not implemented** — `mss` is the measured primary; an XShm path is only warranted with benchmark evidence that it is needed (§33) |
| XDG ScreenCast portal + PipeWire capture | **implemented, not verified on this host** — `core/portal_capture.py` (`PortalCaptureBackend` + `DbusScreencastTransport` + GStreamer `pipewiresrc` consumer). The probe is functional and non-invasive (it asks the live portal whether ScreenCast is advertised; no session, no consent dialog, no pixels). Unit-tested over fake transports; real capture needs a Wayland compositor and user consent, so it is **unverified here** |
| `security/secrets.py` | not present; the role is `security/keyring_manager.py` (§26 naming note) |
| `core/eye.py` | not present; the role is `core/perception.py` + `core/frame_engine.py` |

## What is unsupported (the explicit list)

- **Native Wayland capture is implemented but not verified here** — the ScreenCast
  + PipeWire path exists (`core/portal_capture.py`) and is unit-tested over fake
  transports, but this host has no Wayland compositor and its X11 portal does not
  advertise `org.freedesktop.portal.ScreenCast`, so a real consented capture has
  never run. No Wayland capture support is claimed (§79).
- **Native Wayland input is implemented but not verified here** — the portal
  backend and its non-invasive functional probe exist and are unit-tested, but this
  host has no Wayland compositor, and its X11 portal does not expose
  `org.freedesktop.portal.RemoteDesktop` at all (measured: the probe reports
  `UNAVAILABLE` with exactly that reason). Session establishment (interactive
  consent) and injection are therefore **unverified** and no Wayland control
  support is claimed (§79). Absolute pointer motion additionally needs a ScreenCast
  stream node, so the pointer is `DEGRADED` even when the interface is present.
- **XWayland** — the X11 code path would likely work, but it is **untested here**
  and therefore **unclaimed**.
- **Browser accessibility** — implemented, but needs a browser to actually expose
  an accessibility tree; `UNAVAILABLE` right now, and honestly reported.
- **Visual grounding and the remote Brain** — implemented and tested, but
  `UNAVAILABLE` until an API key is stored in the OS keyring
  (`python main.py keys set-gemini`).
- **xdotool / ydotool fallbacks** — deliberately not implemented (see the table
  above: they are §30 *fallbacks*/*degraded* paths, weaker than the primary
  XTEST/portal paths). A session where both the XTEST and the portal probe fail
  reports `mouse`/`keyboard` `UNAVAILABLE`, naming the observed `xdotool` binary
  as evidence, rather than pretending a fallback exists.

Nothing above is inferred from an executable on `PATH`; each implemented capability
is selected only after a functional probe, and each unimplemented one is reported
as such.
