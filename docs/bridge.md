# The Local BLAXCY Bridge (Phase 2A)

> A thin, **local-only** boundary that accepts one validated, signed high-level
> task and executes it through BLAXCY's existing tool door. This is *not* MCP,
> *not* a network server, and *not* a second execution path: every safety
> property is inherited from the core the bridge composes.

## What it is

```
Task producer (CI runner / automation script, same machine)
   |  canonical task document + HMAC signature + nonce + timestamp
   v
bridge.auth.BridgeAuthenticator.verify_request()      <- boundary authentication
   v
bridge.task_protocol.Task.parse()                     <- strict validation
   v
bridge.runner.LocalBridge.submit()
   v
ai.tool_protocol.ToolDispatcher.dispatch(run_sequence) <- the ONE tool door
   v
BLAXCY pipeline: policy -> resolve -> lease -> revalidate -> execute -> verify
   v
bridge.task_protocol.TaskEnvelope (JSON result)
```

No socket is opened. No port is listened on. The bridge is an **in-process API**
that a same-machine producer calls through a Python entry point; the boundary
between the two processes is the signed task document itself. All desktop
interaction stays inside BLAXCY (requirement 14).

## The task contract (version 1)

A task is exactly one ordered plan executed as a `run_sequence`. Top-level
fields — all others are rejected:

| Field | Type | Required | Notes |
|---|---|---|---|
| `schema_version` | int | **yes** | Must equal `1`. Missing (not just wrong) is rejected: a versioned contract only works if the producer states its version. |
| `task_id` | str | **yes** | 1-128 chars, `[A-Za-z0-9._:-]` only. |
| `plan` | list | **yes** | 1-12 steps. Each step is the exact standalone-tool argument shape plus `step_id`; targets are descriptions, never coordinates. |
| `created_at_ms` | int | no | Producer timestamp (informational). |

There is **no** `halt_on` field: a task cannot narrow the §66.1 safety halt
set, ever. There is no free-form tool dispatch: `run_sequence` is the only
executable payload a task can carry, and sequences cannot nest.

### What a task can never carry (all rejected at parse, before any dispatch)

- Raw coordinates or coordinate-like targets (`"100, 200"`, `"x=10"`).
- Pre-resolved identity: `element_id`, `lease_id`, `frame_id`,
  `state_version`, `generation` — scanned at **every** depth of the document,
  not just the top level.
- Credential-shaped fields: any key containing `password`, `passwd`,
  `credential`, `secret`, `token`, `api_key`, `apikey`, `authorization`.
- Unknown top-level fields (strict contract; no silent ignoring).
- Documents larger than 64 KiB, non-objects, non-JSON.
- Duplicate `step_id`s.

Rejection reasons (machine-readable, in `rejection_reason`):
`TASK_TOO_LARGE`, `TASK_NOT_JSON`, `TASK_NOT_AN_OBJECT`, `UNKNOWN_TASK_FIELDS`,
`FORBIDDEN_TASK_FIELD`, `PRE_RESOLVED_TASK_FIELD`, `TASK_SCHEMA_INVALID`,
`SEQUENCE_RUNNER_UNAVAILABLE`, `SIGNATURE_MISMATCH`, `NONCE_REPLAYED`,
`TIMESTAMP_OUT_OF_RANGE`, `NO_TOKEN_CONFIGURED`, `MISSING_CREDENTIALS`,
`DISPATCH_REFUSED`, `DISPATCH_ERROR`.

## The result contract

`TaskEnvelope.to_payload()` returns the JSON document:

```json
{
  "task_id": "task-1",
  "envelope": { "ok": true, "error_code": null, "...": "the run_sequence tool envelope (§66)" },
  "result": {
    "schema_version": 1,
    "task_id": "task-1",
    "status": "COMPLETED | HALTED | REJECTED | TIMED_OUT",
    "sequence_id": "seq-1",
    "halted": false,
    "halt_reason": null,
    "halt_code": null,
    "wall_clock_ms": 1342.9,
    "completed_count": 5,
    "steps": [ { "...": "one uniform §66 envelope per step, in plan order" } ],
    "rejection_reason": null,
    "rejection_details": null,
    "timeout_elapsed_ms": null,
    "timeout_stop_latched": null
  }
}
```

Status semantics:

- **COMPLETED** — every executed step finished without a §66.1 halt (steps can
  still individually carry verification states; `halted` is the flag).
- **HALTED** — the sequence runner halted (ambiguity, contradiction,
  confirmation required, blocked app, stop, takeover, ...). Unreached steps
  carry `error_code: "NOT_EXECUTED"`. The bridge never guesses forward.
- **REJECTED** — refused before any dispatch; `envelope` is `null` because no
  tool ran, and `rejection_reason` names the cause.
- **TIMED_OUT** — the bridge's wall-clock ceiling elapsed. `timeout_stop_latched`
  reports **honestly** whether the emergency stop it then triggers actually
  latched (`null` is never reported as `true`).

Steps are the core's own `SequenceStepResult` shapes — byte-compatible with the
`run_sequence` tool's results, no translation layer.

## Authentication (requirement 11)

The bridge→runner boundary uses **HMAC-SHA256 request signing**, not a network
login:

- The shared token comes from the `BLAXCY_BRIDGE_TOKEN` environment variable
  (minimum 32 chars). It is never hard-coded, never written to config, never
  logged, never echoed. An unconfigured bridge **fails closed**: every
  submission is rejected with `NO_TOKEN_CONFIGURED`.
- The producer signs `nonce || timestamp || task_document` with
  `BridgeAuthenticator.sign_request`; the Body verifies with
  `verify_request` before parsing anything executable.
- **Replay protection:** the bridge tracks nonces (bounded at 10 000, FIFO
  eviction) and refuses a repeated nonce even with a valid signature; a failed
  signature also consumes the nonce, so a (nonce, signature) pair cannot be
  probed repeatedly. Timestamps outside ±300 s of the bridge's clock are
  refused (`TIMESTAMP_OUT_OF_RANGE`).
- **Tamper evidence:** the MAC covers the exact document bytes, so a modified
  step or argument fails verification even though the token never crossed the
  boundary.
- Comparison is constant-time (`hmac.compare_digest`).

A producer signs and sends the canonical form:

```python
from bridge.auth import BridgeAuthenticator, canonical_task_document

auth = BridgeAuthenticator()               # token from BLAXCY_BRIDGE_TOKEN
document = canonical_task_document(task_dict)
nonce = secrets.token_hex(16)
now = time.time()
signature = auth.sign_request(document, nonce=nonce, timestamp=now)
response = bridge.submit(document, signature=signature, nonce=nonce, timestamp=now)
```

## Safety boundary (requirements 5/13)

What the bridge **cannot** do, by construction:

- Bypass policy, verification, confirmation, or any §66.1 halt — a task is a
  `run_sequence`, dispatched through the same door with `confirmed=False`
  always; the confirmation callback is the operator's, never the producer's.
- Set `halt_on` (the field does not exist in the task schema).
- Retry anything. There is no retry logic in the bridge at all: a halt, a
  timeout, or a contradiction ends the task and is reported.
- Inject input after a stop: a latched emergency stop (or takeover) halts the
  task with its own code before any step runs.
- Keep acting after its own timeout: on expiry the bridge triggers BLAXCY's
  **own** `EmergencyStop` (latch → release input → force OBSERVE) and reports
  whether the latch really happened.

The bridge adds **zero** execution logic. Its `submit()` calls
`ToolDispatcher.dispatch` and nothing else; every safety property below that
call is the core's own, test-pinned by the Phase 1/9/10.1 suites.

## Configuration

| Setting | Default | Meaning |
|---|---|---|
| `BLAXCY_BRIDGE_TOKEN` env | *(unset → all submissions rejected)* | The shared HMAC token (≥ 32 chars). |
| `LocalBridge(timeout_seconds=...)` | `120.0` | Wall-clock ceiling per task; on expiry the emergency stop is triggered. |
| `[sequence] max_sequence_steps` | `12` | Enforced by the core runner (bridge reports, never duplicates). |
| `[sequence] max_sequence_wall_clock_seconds` | `60` | Enforced by the core runner. |

## Status and observability

`LocalBridge.status` reports (JSON): schema version, whether the authenticator
is configured and the token's *source* (never its value), timeout, the
sequence limits, and per-outcome task counters (`submitted` / `completed` /
`halted` / `rejected` / `timed_out`). The task document itself is never logged
(§70: arguments are exactly where a secret would be).

## Test map

| File | Covers |
|---|---|
| `tests/unit/test_bridge_task_protocol.py` | Parsing, version pinning, size/field guards, unsafe-input rejection (coordinates, ids, secrets, nesting), result serialization. |
| `tests/unit/test_bridge_auth.py` | Token handling, signing, tamper evidence, replay, skew, constant-time reference MAC. |
| `tests/integration/test_bridge_runner.py` | End-to-end dispatch over the real dispatcher + sequence runner with fakes: success, rejections, OBSERVE refusal, ambiguity halt, mid-plan halt with `NOT_EXECUTED` tail, latched-stop halt, timeout (incl. failing/unwired stop reported honestly). |

## Deliberate non-goals

- **No MCP** — a later, separate phase (the schemas are shaped so the mapping
  is mechanical; see `docs/mcp_readiness` in the Phase 1 tests).
- **No HTTP/network listener** of any kind.
- **No free-form tool dispatch** from a task; no single-action bypass of the
  sequence container; no `halt_on` narrowing; no shell/Python/filesystem
  capability.
