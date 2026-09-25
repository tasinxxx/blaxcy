# BLAXCY — Architecture

> Required by the specification §26. This describes the implementation that
> exists in this repository, not an intended design. Where a module is named, it
> exists; the section references are to the master specification.

## What BLAXCY is

A Linux-native **BODY** for computer control. A connected AI (the **Brain**)
decides *what* should happen; BLAXCY decides *whether and how* that can happen
safely, on a real desktop, with a real verified outcome. BLAXCY is not a
reasoning agent and does not contain one.

The permanent invariant (§3/§87), which the whole layout exists to preserve:

```text
BRAIN     decides WHAT
BODY      decides HOW SAFELY
EYE       determines WHAT IS ACTUALLY THERE
EXECUTOR  performs the physical action
VERIFIER  determines WHETHER IT ACTUALLY WORKED
RECOVERY  decides WHETHER A SAFE RETRY IS POSSIBLE
```

## The one pipeline

Every action, standalone or batched, passes through the same ordered stages.
There is no second route to the desktop.

```text
Brain tool call
  -> ai/tool_protocol.py   ToolDispatcher.dispatch  (the only door)
  -> policy/                modes, action classes, guards, terminal guard
  -> core/target_resolver.py  resolve (with resolver_cache as a hint)
  -> schemas/leases.py      fresh ElementLease
  -> control/executor.py    revalidate -> ensure visible -> act
  -> control/backends/      XTEST / xdotool / unavailable
  -> core/perception.py     observe
  -> control/verifier.py    verify
  -> control/recovery.py    bounded recovery, if permitted
  -> schemas/actions.py     ToolEnvelope
```

`control/executor.py` is the **only** module that performs physical input.
`ai/tool_protocol.py` is the **only** door from a Brain call. `run_sequence` is a
container over that same dispatcher (`control/sequence_runner.py`), never a
parallel pipeline, so a sequence step cannot drift from a standalone call.

## Module map

| Area | Modules | Role |
|---|---|---|
| `core/` | `application.py` | **Composition root.** The one place the parts are constructed together. Needs no Qt, which is why `main.py status` and the tests can run the Body headless. |
| | `perception.py` | `PerceptionOrchestrator`: `perceive()` (one ordinary cycle) and `enrich()` (the §43 fallback cascade). |
| | `frame_engine.py`, `change_detector.py` | §33 capture (mss) and §34 change classification. |
| | `accessibility.py` | The **single owner** of AT-SPI, on its own thread; bounded traversal, element cache, role mapping. |
| | `browser_accessibility.py` | Browser/document context, built from the accessibility service's observations. |
| | `ocr.py`, `visual_grounder.py` | §40 OCR (fallback) and §41 visual grounding (last resort, gated and off without a key). |
| | `target_resolver.py`, `resolver_cache.py` | §43 deterministic resolution; §43.1 identity-path cache as a traversal hint. |
| | `speculative_perceiver.py` | §33.2 background prefetch for the next sequence step. |
| | `state_cache.py`, `event_bus.py` | §65 the versioned current-state authority and the one typed event bus. |
| | `capability_probe.py`, `session_detector.py`, `calibration.py`, `single_instance.py` | §28/§29 functional capability probes, §31 calibration, §72 single instance. |
| | `logging_setup.py` | §70 structured JSON logging. |
| `control/` | `executor.py` | §59 state machine; the only physical-input path. |
| | `sequence_runner.py` | §66.1 `run_sequence`; halt conditions and the `NOT_EXECUTED` tail. |
| | `verifier.py`, `recovery.py`, `takeover.py`, `emergency_stop.py`, `action_tracker.py` | §60–§63, §78 verification, bounded recovery, takeover, stop, ownership. |
| | `mouse.py`, `keyboard.py`, `clipboard.py`, `window_manager.py` | §48–§53 input control and §47 window activation. |
| | `backends/` | `xtest.py` (primary), `xdotool.py`, `unavailable.py` (fail-closed), `keys.py`. |
| `policy/` | `modes.py`, `action_classes.py`, `permissions.py`, `guards.py`, `terminal_guard.py` | §56–§58 mode gating, action classes, permission engine, fail-closed guards, §54 terminal submission. |
| `ai/` | `brain_adapter.py`, `gemini_adapter.py`, `tool_protocol.py`, `context_manager.py`, `prompt_builder.py` | §66–§68 the abstract adapter, the Gemini implementation, the single tool dispatcher, and the §68/§68.1 context economy. |
| `schemas/` | `geometry.py`, `screen_state.py`, `elements.py`, `leases.py`, `actions.py`, `errors.py`, `events.py`, `sequences.py`, `enums.py`, `capability.py` | Typed contracts. Coordinates are always space-tagged (§31). |
| `gui/` | `app.py`, `main_window.py`, `status_panel.py`, `capability_panel.py`, `action_log.py`, `confirmation.py`, `emergency_stop_ui.py` | §71 PySide6 view **over** the composition root, never the owner of it. |
| `security/` | `keyring_manager.py`, `redaction.py` | §69 key storage; §70 redaction filter and secret registry. |
| `watchdog/` | `watchdog.py`, `protocol.py` | §64 heartbeat, ownership record, crash cleanup. |
| `bench/` | `real_desktop.py` | §76 reusable real-desktop benchmark runner. |
| `installer/` | `installer.py`, `__main__.py` | §73 standard-library-only installer with manifest and rollback. |

## Concurrency and threads

- **`T-A11Y`** (inside `AccessibilityService`) is the only thread that touches
  GI/GLib/AT-SPI. Every other component — including the speculative perceiver and
  concurrent read-only dispatch — marshals requests to it through one queue.
- **`T-EXEC`** is the single writer to the real input device; only one physical
  action executes at a time.
- The emergency-stop path is deliberately able to interrupt and release held
  input without waiting behind the normal action serialization.
- The §33.2 speculative worker is a background daemon, cancellable and discarded
  on a `MEANINGFUL`/`MAJOR` change; it never issues input and never blocks input.
- Read-only tools (§32.1) run on a bounded separate path so they cannot delay or
  reorder a physical action, and a concurrently-fetched snapshot is never
  substituted for a fresh revalidation.

## State and staleness

`core/state_cache.py` is the sole authority on an accepted state's
`state_version`. Perception builds a `ScreenState`; the cache restamps and
accepts it only if it is not stale (`frame_id`, `generation`, `task_id`). A new
generation invalidates old leases. There is no unbounded frame or perception
queue — the policy is latest-frame.

## Configuration

`config/settings.py` (Pydantic) plus `config/default_settings.toml`, with
`schema_version` and migration (`config/migration.py`). Sections include
`[performance] [capture] [perception] [accessibility] [ocr] [visual] [resolver]
[resolver_cache] [sequence] [input] [clipboard] [lease] [verification] [recovery]
[safety] [terminal] [privacy] [logging] [gemini]`.

Every operational limit is configurable. **Security invariants are not**: they
are listed in `SECURITY_INVARIANTS` and a config that tries to disable one is
refused at load time (§4 rule 25), rather than silently accepted.

## Batching layer

`run_sequence` (§66.1), `resolver_cache` (§43.1), speculative perception
(§33.2), the task-active capture boost (§33.1) and concurrent read-only dispatch
(§32.1) are default-on. Each is a scheduling or traversal-order optimisation: it
removes Brain round-trips and redundant traversal work, and it removes **no**
per-step policy, resolution, lease, revalidation or verification check. Turning
any of them off removes capability, never a check.

## Entry points

```bash
python main.py probe            # functional capability report (default)
python main.py session          # detected session/environment facts
python main.py config           # resolved configuration and its source
python main.py keys             # Brain API key status (keyring only)
python main.py status           # start every component, report the Body's state
python main.py run "TASK"       # one task through the Brain and the real desktop
python main.py gui              # the section 71 window over the same Body
```

`gui` and `run` — the two commands that own the desktop — take the §72
single-instance lock first. The read-only commands deliberately do not.
