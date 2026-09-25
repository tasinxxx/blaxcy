# BLAXCY — Security

> Required by the specification §26. This states what the implementation
> actually enforces, where, and what it does **not** claim. Every guarantee below
> is backed by code in this repository and by tests; the enforcement point is
> named so it can be checked.

## The threat model in one line

BLAXCY's job is to *not* do something dangerous when asked to by a model. The
model is treated as untrusted input, and the safety boundary is BLAXCY's, not the
model's.

## Enforcement points

| Guarantee | Where it is enforced |
|---|---|
| No physical input without policy approval and a validated target | `control/executor.py` is the only input path; `policy/guards.py`, `policy/modes.py`, `policy/permissions.py` |
| No input without a fresh lease and full revalidation | `control/executor.py` (§44/§45); default lease TTL 800 ms, action state age ≤ 1500 ms |
| OBSERVE mode injects nothing | `policy/modes.py` denies mouse/keyboard/drag/submit/sequence execution |
| Destructive actions need a human | `gui/confirmation.py` (3 s countdown floor, no implicit Enter) + `policy/permissions.py` |
| Terminal submission is a separate decision from typing | `policy/terminal_guard.py`; destructive commands stay protected in **every** mode, including AUTONOMOUS |
| Blocked applications are refused, and halt an in-flight sequence | `policy/guards.py`; `control/sequence_runner.py` |
| Ambiguous targets are never auto-selected | `core/target_resolver.py` absolute ambiguity rule; `best` is `None` rather than a guess |
| Stale targets are never clicked | `core/state_cache.py` + `control/executor.py` revalidation |
| Emergency stop overrides everything and releases held input | `control/emergency_stop.py` (latch → stop → release buttons → release keys → cancel → OBSERVE → event) |
| Human takeover overrides autonomous control | `control/takeover.py`; a sequence halts and is never resumed |
| Bounded recovery, no infinite retry | `control/recovery.py` (2/tool, 6/step, loop guard) |
| Input ownership is tracked and released on exit or crash | `control/action_tracker.py`, `watchdog/watchdog.py` |
| Security invariants cannot be configured off | `config/settings.py` `SECURITY_INVARIANTS`, checked at load |

## Credential and privacy rules

The rules, and the code that implements them:

- **Passwords are never read by perception.** `core/accessibility.py` reports a
  `PASSWORD_INPUT` element with `text=None` and `password=True`; only a
  *navigation* field (address/URL/location bar) has its editable text read.
- **Passwords are never sent to the Brain.** `ai/context_manager.py` excludes
  them from model context.
- **Password fields are never OCR'd and never visually uploaded.**
  `config/settings.py` enforces `privacy.never_ocr_password_fields` and
  `privacy.never_upload_credential_context` as invariants; `core/visual_grounder.py`
  runs its privacy gate *before* an image exists.
- **The clipboard is never used for credential input.** `control/clipboard.py`
  + `policy/guards.py`.
- **Secrets never reach a log.** `security/redaction.py` (the filter and the
  secret registry) is installed by `core/logging_setup.py` on the root logger
  **and on the handler**; a registered secret and any sensitive-named field are
  replaced with `***` before any formatter renders the record. Log redaction is
  an enforced invariant, so it cannot be switched off through configuration.
- **The Brain API key lives only in the OS keyring.** `security/keyring_manager.py`
  is the one module that calls `keyring`; the key is read from a hidden prompt,
  never from a CLI argument, and SDK error strings are passed through the
  redaction helper.

## What is NOT claimed

Stated plainly, because a security document that only lists strengths is
misleading:

- **No sandboxing.** BLAXCY runs with the user's own privileges and can act on
  the user's desktop within the rules above. It is not a containment boundary
  against a hostile local process.
- **Prompt injection is mitigated, not eliminated.** A malicious page or document
  can still attempt to steer the Brain. The mitigations are the confirmation
  gate, the blocked-application list, the terminal guard and the destructive
  action class — not a guarantee about model behaviour.
- **The log has no integrity protection.** It is a plain JSON-lines file on
  disk; an attacker with write access to the log directory can alter it.
- **`--system-deps` reports, it does not install.** No privileged package
  installation has been performed or is attempted unattended.
- **Wayland has no verified input path** on this host; the §30 portal path is
  not implemented. Input is X11/XTEST only.

## Reporting

This is a development-stage system. The `docs/limitations.md` file records the
known functional gaps, including those that touch safety-relevant behaviour, with
the evidence for each.
