"""The relay executor: from a committed task to a recorded result (Phase 2B).

This module is the runner-side glue between the relay repository and the
Phase 2A :class:`~bridge.runner.LocalBridge`. Its discipline is that it adds
**no execution capability**: it can only (a) read an envelope from the store,
(b) validate it with the envelope parser, (c) hand the inner task to the
LocalBridge — the same door a local producer uses — and (d) write back the
result the bridge produced. It cannot inject input, cannot call the dispatcher
directly, and cannot invent a result. When anything goes wrong, the task ends
in a terminal state with an honest record and *no desktop execution*
(requirements 11/12/13).

Authentication across the two boundaries is explicit and different on purpose:

* **Producer → relay.** The producer's ``signature``/``nonce``/``timestamp``
  over the **inner task document** are verified by the runner using the same
  shared token and the same MAC construction as the bridge — but freshness is
  judged by the envelope's ``expires_at`` (relay-appropriate: a task commits
  minutes before pickup, so the bridge's ±300 s skew window does not apply
  here). A bad signature, a reused nonce (the bridge's own replay table is the
  second net), or an expired envelope is refused before any execution.
* **Runner → bridge.** The runner then signs a *fresh* submission of the same
  inner document — nonce scoped by task id, current timestamp — and calls
  :meth:`LocalBridge.submit`, whose skew window, replay table and validation
  all apply with full force. The bridge sees nothing but a normal, correctly
  signed local submission; it has no relay-specific logic to bypass.

The per-task flow::

    claim (PENDING → RUNNING, exclusive, replay-proof)
      → parse envelope (fail closed on malformed/unsafe, requirement 7/11)
      → refuse if expired (requirement 11)
      → verify the producer's MAC over the inner task
      → LocalBridge.submit(...)          ← the ONE execution path (requirement 6)
      → map the bridge status to a terminal lifecycle state (requirement 10)
      → write result.json (once, atomically)

Nothing is ever retried automatically (requirement 12).
"""

from __future__ import annotations

import contextlib
import hmac
import time
from collections.abc import Callable
from typing import Any

from bridge.auth import BridgeAuthenticator
from bridge.runner import LocalBridge
from bridge.task_protocol import TaskEnvelope as BridgeTaskEnvelope
from bridge.task_protocol import TaskStatus
from relay.envelope import EnvelopeError, TaskEnvelopeDocument
from relay.lifecycle import TaskLifecycle
from relay.store import StoreError, TaskStore


class RelayExecutor:
    """Executes relay tasks through the LocalBridge (requirement 6).

    Args:
        store: The repository record store.
        bridge: The Phase 2A bridge. Every execution goes through it; this
            class never touches the dispatcher.
        authenticator: The boundary verifier. Used both to check the producer's
            MAC (via its own MAC construction) and to sign the fresh bridge
            submission. The same ``BLAXCY_BRIDGE_TOKEN`` the bridge uses, so
            only a process holding the token can run the relay.
        clock: Injectable time source.
    """

    def __init__(
        self,
        *,
        store: TaskStore,
        bridge: LocalBridge,
        authenticator: BridgeAuthenticator,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._store = store
        self._bridge = bridge
        self._auth = authenticator
        self._clock = clock if clock is not None else time.time

    # -- The one entry point ------------------------------------------------------

    def process_task(self, task_id: str) -> dict[str, Any]:
        """Claim, execute and record one task. Never raises for a task outcome.

        Every failure mode — duplicate claim, malformed envelope, expired
        envelope, bad signature, bridge rejection, halt, timeout,
        infrastructure error — ends in a terminal lifecycle state with a
        result document naming the reason. An exception escaping this method
        is a relay bug, not a task outcome.
        """
        try:
            self._store.claim(task_id)
        except StoreError as exc:
            # Mostly the duplicate/replay case (requirement 9): the task is
            # already claimed or past PENDING, so nothing is executed and the
            # recorded state stands.
            return self._refused_without_claim(task_id, exc)
        try:
            return self._execute_claimed(task_id)
        except StoreError as exc:
            return self._fail_terminal(
                task_id, TaskLifecycle.REJECTED, "STORE_REFUSED", exc.details
            )
        except Exception as exc:  # infrastructure failure, not a task outcome
            return self._fail_terminal(
                task_id,
                TaskLifecycle.FAILED,
                "RELAY_INFRASTRUCTURE_ERROR",
                {"exception": f"{type(exc).__name__}: {exc}"},
            )

    # -- Claimed execution --------------------------------------------------------

    def _execute_claimed(self, task_id: str) -> dict[str, Any]:
        envelope_path = self._store.task_dir(task_id) / "task.json"
        if not envelope_path.exists():
            return self._fail_terminal(
                task_id, TaskLifecycle.REJECTED, "ENVELOPE_MISSING", {"task_id": task_id}
            )
        raw = envelope_path.read_text(encoding="utf-8")

        # -- Envelope validation (fail closed; the bridge's own parser judges
        #    the inner task, so every local guard applies verbatim) -------------
        try:
            envelope = TaskEnvelopeDocument.parse(raw)
        except EnvelopeError as exc:
            return self._fail_terminal(task_id, TaskLifecycle.REJECTED, exc.reason, exc.details)

        # -- Freshness: an expired envelope is dead, never executed -------------
        try:
            envelope.ensure_not_expired(now=self._clock())
        except EnvelopeError as exc:
            return self._fail_terminal(task_id, TaskLifecycle.REJECTED, exc.reason, exc.details)

        # -- Producer authentication over the inner task -------------------------
        inner_document = envelope.inner_task_document()
        expected = self._auth.sign_request(
            inner_document,
            nonce=envelope.submission.nonce,
            timestamp=envelope.submission.timestamp,
        )
        if not hmac.compare_digest(expected, envelope.submission.signature):
            return self._fail_terminal(
                task_id,
                TaskLifecycle.REJECTED,
                "SIGNATURE_MISMATCH",
                {"covered": "inner task document"},
            )

        # -- Execution through the Phase 2A door (requirement 6) -------------------
        # The runner signs a *fresh* submission of the same inner document: the
        # bridge's skew window, replay table and validation all apply unchanged.
        now = self._clock()
        bridge_nonce = f"relay:{task_id}:{envelope.submission.nonce}"
        bridge_signature = self._auth.sign_request(
            inner_document, nonce=bridge_nonce, timestamp=now
        )
        response = self._bridge.submit(
            inner_document,
            signature=bridge_signature,
            nonce=bridge_nonce,
            timestamp=now,
        )

        # -- Map the bridge status to a terminal lifecycle state (requirement 10) --
        return self._record_outcome(task_id, response)

    # -- Outcome recording ---------------------------------------------------------

    def _record_outcome(self, task_id: str, response: BridgeTaskEnvelope) -> dict[str, Any]:
        status = response.result.status
        lifecycle = {
            TaskStatus.COMPLETED: TaskLifecycle.COMPLETED,
            TaskStatus.HALTED: TaskLifecycle.HALTED,
            TaskStatus.REJECTED: TaskLifecycle.REJECTED,
            TaskStatus.TIMED_OUT: TaskLifecycle.TIMED_OUT,
        }[status]
        self._store.transition(
            task_id,
            lifecycle,
            note="bridge outcome",
            detail={"bridge_status": status.value},
        )
        document = {
            "schema_version": 1,
            "task_id": task_id,
            "lifecycle": lifecycle.value,
            "bridge_status": status.value,
            "result": response.to_payload(),
        }
        self._store.write_result(task_id, document)
        return document

    def _fail_terminal(
        self, task_id: str, lifecycle: TaskLifecycle, reason: str, details: dict[str, Any] | None
    ) -> dict[str, Any]:
        """Record a terminal refusal/failure with its reason (fail closed)."""
        self._store.transition(
            task_id,
            lifecycle,
            note="relay refusal" if lifecycle is TaskLifecycle.REJECTED else "relay failure",
            detail={"reason": reason},
        )
        document = {
            "schema_version": 1,
            "task_id": task_id,
            "lifecycle": lifecycle.value,
            "bridge_status": None,
            "result": None,
            "reason": reason,
            "details": details or {},
        }
        # A result that cannot be written must not hide the refusal: the
        # lifecycle record above is the durable evidence either way.
        with contextlib.suppress(StoreError):
            self._store.write_result(task_id, document)
        return document

    def _refused_without_claim(self, task_id: str, exc: StoreError) -> dict[str, Any]:
        """A task that could not even be claimed (duplicate/replay, requirement 9)."""
        return {
            "schema_version": 1,
            "task_id": task_id,
            "lifecycle": self._store.state_of(task_id).value,
            "bridge_status": None,
            "result": None,
            "reason": exc.reason,
            "details": exc.details,
            "executed": False,
        }


__all__ = ["RelayExecutor"]
