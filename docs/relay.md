# The GitHub Relay (Phase 2B)

> A relay layer that lets a trusted external producer place **one** high-level
> BLAXCY task into a private GitHub repository, and a **self-hosted** runner on
> the BLAXCY machine execute it strictly through the Phase 2A LocalBridge.
> GitHub Actions stays orchestration-only; desktop control stays inside BLAXCY.
> No MCP, no ChatGPT/Gemini connection, no public network server.

## Architecture

```
Task producer (trusted, external)
   |  commits one signed envelope to relay/tasks/<task_id>/task.json
   v
Private GitHub repository  (the relay; also the durable record)
   v
GitHub Actions  (orchestration only: checkout + one CLI call)
   v
Self-hosted runner on this Linux PC   [self-hosted, linux]
   v
relay.entry process   -> RelayExecutor -> LocalBridge.submit
   v
ToolDispatcher -> BLAXCY pipeline (policy/resolve/lease/revalidate/execute/verify)
   v
Desktop
   |  result written back, committed by the runner
   v
Private GitHub repository  (relay/results/<task_id>.json)
```

The executor calls `LocalBridge.submit` — the same door, the same HMAC bar, the
same skew window and replay table a local producer faces. It never touches the
dispatcher directly and never injects input itself.

## Repository layout (requirement 3)

```
relay/
  tasks/<task_id>/task.json     the raw envelope (producer, written once)
  tasks/<task_id>/state.json    the lifecycle record (runner; full history)
  tasks/<task_id>/result.json   the TaskEnvelope result (runner, once, atomic)
  claims/<task_id>.claim        exclusive claim marker (O_EXCL = the lock)
  results/<task_id>.json        flattened result copy for producers
```

`<task_id>` is the dedup key everywhere and must match `^[A-Za-z0-9._:-]{1,128}$`.
Claiming uses `O_CREAT|O_EXCL`, so exactly one worker can claim a task; a stale
claim (crashed run) is re-armed only by the explicit `reset_claims` action,
never automatically.

## Task lifecycle (requirement 10)

```
PENDING -> RUNNING -> COMPLETED | HALTED | REJECTED | TIMED_OUT | FAILED
```

| State | Meaning |
|---|---|
| `PENDING` | Committed, never claimed. A re-write of the same document is a producer no-op; any other change is `TASK_CONFLICT`. |
| `RUNNING` | Claimed by exactly one worker. |
| `COMPLETED` | The bridge reported every executed step finished without a halt. |
| `HALTED` | The bridge reported a §66.1 halt (ambiguity, confirmation required, stop, takeover, blocked app, ...). Unreached steps are `NOT_EXECUTED`. |
| `REJECTED` | Refused before any dispatch: malformed, unsafe, expired, unauthenticated, duplicate, or the runner was unavailable. |
| `TIMED_OUT` | The bridge's wall-clock ceiling elapsed; its emergency stop ran (latch reported honestly). |
| `FAILED` | The relay itself failed (Body unreachable, process died). Never a desktop outcome. |

Every transition is validated by `relay.lifecycle` and recorded with a
timestamp, from/to states and a note in `state.json`'s `history`. Terminal
states transition to nothing — a completed task cannot be re-run, a rejected
task cannot be re-validated (fail closed, requirement 11).

## Envelope format (version 1, requirement 2)

`relay/tasks/<task_id>/task.json` — canonical JSON (sorted keys, compact):

| Field | Type | Required | Notes |
|---|---|---|---|
| `relay_version` | int | **yes** | Must equal `1`. |
| `task_id` | str | **yes** | Must equal the inner task's id (mismatch = tamper indicator → reject). |
| `task` | object | **yes** | The Phase 2A task document, validated by `Task.parse` — **every** bridge guard applies verbatim. |
| `expires_at` | float | **yes** | Unix seconds. Expired envelopes are refused before execution (default horizon: 1 h). |
| `submission` | object | **yes** | `{signature, nonce, timestamp}` — the producer's HMAC over the inner task document. |
| `created_at` | str | no | Informational. |
| `producer` | str | no | Informational. |

There is no `command`, `script`, `args`, `halt_on`, coordinates, element ids,
frame ids, state versions, leases or credential-shaped fields anywhere in the
format — the inner task goes through the bridge's own parser, and the wrapper
carries nothing executable (requirement 7/13).

### Authentication across the two boundaries

- **Producer → relay:** the producer's MAC over the *inner task document* is
  verified by the runner using the same shared token and the same MAC
  construction as the bridge (`hmac.compare_digest`, constant time). Freshness
  is judged by `expires_at` — relay-appropriate, since a task commits before
  pickup and the bridge's ±300 s skew window would otherwise make every relay
  task dead on arrival.
- **Runner → bridge:** the runner signs a *fresh* submission of the same inner
  document with a nonce scoped by task id (`relay:<task_id>:<nonce>`) and the
  current timestamp, then calls `LocalBridge.submit`. The bridge's own skew
  window, replay table and validation apply with full force; the bridge has no
  relay-specific logic to bypass.

The relay never re-serializes the inner task through the pydantic model for
signature coverage — a model round-trip re-orders keys and materializes
defaults, which would not be the bytes anyone signed. The raw committed
document is preserved and re-verified byte-identically.

## GitHub Actions flow (requirements 4/5/15)

`relay/workflow/process-task.yml`:

1. `runs-on: [self-hosted, linux]` — **never** hosted runners (they have no
   access to this desktop and must never gain it).
2. `concurrency: blaxcy-relay-desktop` — one relay run at a time; the claim
   files are the second net.
3. Checkout the relay repository and BLAXCY (pinned by `vars.BLAXCY_REF`).
4. `git pull --ff-only` — sync before processing.
5. Run `python -m relay.entry --repo . process` with
   `BLAXCY_BRIDGE_TOKEN` from `secrets.BLAXCY_BRIDGE_TOKEN`.
6. Commit and push `relay/` changes (the results) back to the private
   repository.

The workflow executes **no** repository content of its own: the only program it
runs is the checked-out relay CLI. It installs nothing, upgrades nothing, and
never starts a Body (requirement 13/15).

## Self-hosted runner requirements

- A Linux machine **with this desktop** (the runner must share the Body's
  session; a runner on another machine cannot control this desktop and must
  not be given the token).
- The BLAXCY venv at the repository-relative path the workflow activates
  (`blaxcy/.venv`), containing BLAXCY and its dependencies — including the
  `bridge` and `relay` packages.
- The operator's BLAXCY Body running and available (the relay CLI's
  `inprocess` wiring composes the Body itself; it exits rather than acting if
  the token is missing).
- A runner registered for this repository only, with the default labels
  (`self-hosted`, `linux`), and no `--labels` privilege to run workflows from
  forks (fork pull requests never receive secrets and never reach this job).

## Required GitHub secrets (requirement 8)

| Secret | Purpose |
|---|---|
| `BLAXCY_BRIDGE_TOKEN` | The shared HMAC token (≥ 32 chars) the runner verifies producer signatures with and signs bridge submissions with. Set it in the **private relay repository's** Actions secrets. It is read only into the process environment, never echoed, never written to disk, never committed. |

Optional repository **variables** (not secrets): `BLAXCY_REPO`, `BLAXCY_REF`
(the BLAXCY checkout the workflow pins).

The token is the same one the Body's `LocalBridge` was configured with;
rotating it means updating both the secret and the Body's environment.

## Security boundary (requirement 14)

- **The bridge is not a network service.** Nothing listens on any port. The
  relay is a CLI the self-hosted runner invokes in-process; GitHub's only role
  is storing documents and starting that CLI.
- **One execution path.** Task → RelayExecutor → LocalBridge → ToolDispatcher →
  pipeline. The relay cannot inject input, cannot skip validation, and cannot
  invent a result; every safety property is the core's own, test-pinned by the
  Phase 1/2A suites plus the relay tests.
- **Fail closed everywhere.** Malformed, expired, duplicate, tampered or unsafe
  tasks are refused *before* any dispatch and recorded REJECTED with a
  machine-readable reason; infrastructure errors record FAILED. No path
  executes a task the bridge would have refused locally (requirement 11).
- **No blind retries** (requirement 12). A claim is exclusive; a terminal state
  is final; the executor never re-runs a refused or failed task. Re-delivery of
  the same workflow (or a `repository_dispatch` replay) is harmless: the
  duplicate claim refuses execution and reports the recorded state.
- **Secrets** live in Actions secrets / the Body's environment only
  (requirement 8); the envelope format structurally rejects credential-shaped
  fields, so a producer that leaks a secret into a task gets a rejection, not
  an execution.

## Failure behavior

| Situation | Result |
|---|---|
| Envelope not JSON / too large / unknown fields | `REJECTED`, reason `ENVELOPE_*`, no execution |
| Inner task fails any bridge guard | `REJECTED`, reason `INNER_TASK_REJECTED` with the bridge's verbatim reason |
| Expired (`expires_at` passed) | `REJECTED`, reason `ENVELOPE_EXPIRED` |
| Producer signature invalid | `REJECTED`, reason `SIGNATURE_MISMATCH` |
| Already claimed / executed (`task_id` reuse) | No execution; the recorded state and reason (`DUPLICATE_TASK`) are returned |
| Bridge rejects, halts or times out | The bridge's own status maps 1:1 onto `REJECTED` / `HALTED` / `TIMED_OUT` |
| Body unreachable / relay bug | `FAILED`, reason `RELAY_INFRASTRUCTURE_ERROR` |
| Workflow runs twice | Second run finds every task claimed or terminal; nothing executes twice |

The `process` CLI exits `1` when any task ended `REJECTED`/`FAILED` (so the
workflow run is visibly unhealthy) and `0` for `COMPLETED`/`HALTED`/`TIMED_OUT`
— BLAXCY halting is the safety design working, not a relay failure.

## Test map (requirement 16)

| File | Covers |
|---|---|
| `tests/unit/test_relay_envelope.py` | Envelope validation: version pinning, unknown fields, missing task/submission/expiry, oversize, task-id mismatch, inner-task rejection with bridge reasons (pre-resolved, credential, shell), expiry with an injected clock, signature coverage. |
| `tests/unit/test_relay_store_lifecycle.py` | Lifecycle transitions (legal, illegal, redundant, terminal-final); store claiming (`O_EXCL`, duplicate, rollback, explicit stale-claim reset); history records; atomic write-once results. |
| `tests/integration/test_relay_executor.py` | End-to-end over the real bridge: successful invocation, OBSERVE halt mapping, duplicate/replay (executes once), shared producer nonces (distinct bridge nonces), malformed/unsafe/expired/tampered rejection with zero input events, timeout mapping, infrastructure fail-closed, terminal-state recording. |

## Deliberate non-goals

- **No MCP, no ChatGPT/Gemini connection** — the producer is a future concern;
  today a human or script commits envelopes.
- **No public network surface** — nothing listens; GitHub is the transport.
- **No automatic execution of repository contents** — the workflow runs one
  pinned CLI; everything else in the repository is data, validated before it
  can ever reach the bridge.
