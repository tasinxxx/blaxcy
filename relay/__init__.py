"""The GitHub relay layer (Phase 2B).

Connects a private GitHub repository to the Phase 2A :class:`~bridge.runner.LocalBridge`
without adding any execution path of its own:

* :mod:`relay.lifecycle` — the strict task lifecycle state machine
  (``PENDING → RUNNING → {COMPLETED | HALTED | REJECTED | TIMED_OUT | FAILED}``),
  fail-closed on every illegal transition.
* :mod:`relay.envelope` — the versioned GitHub task-envelope format wrapping the
  Phase 2A task unchanged, with required expiry and producer submission
  credentials.
* :mod:`relay.store` — the repository-side task/result storage conventions with
  exclusive claiming (the replay/duplicate guard, requirement 9).
* :mod:`relay.executor` — the runner-side executor: claim → validate → verify →
  ``LocalBridge.submit`` → record. The bridge stays the ONE execution path.
* :mod:`relay.entry` — the self-hosted runner CLI (``process``/``status``).

GitHub Actions remains orchestration only (requirement 15): it syncs the
repository and invokes the CLI; desktop control stays inside BLAXCY.
"""

from __future__ import annotations

from relay.envelope import (
    DEFAULT_ENVELOPE_TTL_SECONDS,
    RELAY_ENVELOPE_VERSION,
    EnvelopeError,
    SubmissionCredentials,
    TaskEnvelopeDocument,
)
from relay.executor import RelayExecutor
from relay.lifecycle import (
    TERMINAL_STATES,
    LifecycleError,
    TaskLifecycle,
    can_transition,
    require_transition,
)
from relay.store import StoreError, TaskStore

__all__ = [
    "DEFAULT_ENVELOPE_TTL_SECONDS",
    "RELAY_ENVELOPE_VERSION",
    "TERMINAL_STATES",
    "EnvelopeError",
    "LifecycleError",
    "RelayExecutor",
    "StoreError",
    "SubmissionCredentials",
    "TaskEnvelopeDocument",
    "TaskLifecycle",
    "TaskStore",
    "can_transition",
    "require_transition",
]
