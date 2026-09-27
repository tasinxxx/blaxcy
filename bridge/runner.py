"""The local BLAXCY bridge: one validated task, executed through the existing door.

This module is deliberately thin. It composes three existing pieces — the task
schema (:mod:`bridge.task_protocol`), the boundary authentication
(:mod:`bridge.auth`), and BLAXCY's own :class:`~ai.tool_protocol.ToolDispatcher`
— and adds no execution logic of its own:

``verify signature → parse task → dispatch run_sequence → report TaskEnvelope``

Every safety property of the bridge is inherited, not re-implemented:

* **One pipeline.** A task's plan is dispatched through the *same*
  ``run_sequence`` tool a Brain call uses, so every step runs the full
  policy → resolve → lease → revalidate → execute → verify pipeline and the
  sequence runner's halt conditions (§66.1) apply unchanged. There is no second
  path and no bypass (requirements 4/5/14).
* **No new authority.** The bridge accepts exactly one executable payload (the
  plan) and can express nothing else: no raw coordinates, no pre-resolved ids,
  no leases, no shell, no Python, no arbitrary tool arguments. The task schema
  and the sequence schema reject all of these at parse time (requirement 9).
* **Confirmations stay human.** The bridge dispatches with ``confirmed=False``
  always. When a step needs a human, the sequence halts with
  ``CONFIRMATION_REQUIRED`` and the bridge reports ``HALTED`` — the operator's
  confirmation UI, not the producer, decides (§66.1 forbids a pre-authorised
  destructive step, and the runner never forwards confirmations anyway).
* **Stops outrank everything.** An emergency stop or takeover mid-task is
  reported as a halt with its own code. On the bridge's own wall-clock timeout
  — the one case where *the bridge* must take the desktop back — it triggers
  BLAXCY's own emergency stop, and reports honestly whether the latch really
  happened (requirement 13).

Requirement 4 is met literally: :meth:`LocalBridge.submit` calls
``dispatcher.dispatch`` and nothing else.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from typing import Any

from ai.tool_protocol import ToolCall, ToolDispatcher
from bridge.auth import BridgeAuthenticator, BridgeAuthError
from bridge.task_protocol import (
    TASK_SCHEMA_VERSION,
    Task,
    TaskEnvelope,
    TaskResult,
    TaskStatus,
    TaskTimeoutError,
    TaskValidationError,
)
from config.settings import Settings
from core.logging_setup import get_logger
from schemas.actions import ToolEnvelope
from schemas.enums import ErrorCode

_log = get_logger(__name__)

#: The bridge's default wall-clock ceiling per task. The sequence runner has
#: its own §66.1 cap (default 60 s); this is the bridge's outer bound, and it
#: exists for the case the runner's cap cannot cover: a dispatch that never
#: returns at all (a hung backend) must not hang the producer too.
DEFAULT_TASK_TIMEOUT_SECONDS: float = 120.0


class LocalBridge:
    """The Body-side task entry point (Phase 2A).

    Args:
        dispatcher: The existing tool door. Every task executes through it; the
            bridge performs no execution itself.
        authenticator: The boundary verifier. Required; an unconfigured
            authenticator (no token) makes every submission fail closed.
        settings: Full configuration; the ``[sequence]`` limits are consulted
            for status reporting (the runner enforces them; the bridge does not
            duplicate them).
        emergency_stop: Optional stop object (the composition root's
            ``EmergencyStop``). Used **only** on the bridge's wall-clock
            timeout, to take the desktop back from an abandoned dispatch.
        timeout_seconds: The bridge's wall-clock ceiling per task. Must be
            positive.
        clock: Monotonic clock, injectable for deterministic timeout tests.
    """

    def __init__(
        self,
        dispatcher: ToolDispatcher,
        *,
        authenticator: BridgeAuthenticator,
        settings: Settings,
        emergency_stop: Any = None,
        timeout_seconds: float = DEFAULT_TASK_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._dispatcher = dispatcher
        self._auth = authenticator
        self._settings = settings
        self._stop = emergency_stop
        self._timeout_seconds = float(timeout_seconds)
        self._clock = clock
        self._lock = threading.Lock()
        self._tasks_submitted = 0
        self._tasks_completed = 0
        self._tasks_halted = 0
        self._tasks_rejected = 0
        self._tasks_timed_out = 0

    # -- Status ----------------------------------------------------------------

    @property
    def status(self) -> dict[str, Any]:
        """A JSON-shaped health/status snapshot (§70-style honest reporting)."""
        return {
            "schema_version": TASK_SCHEMA_VERSION,
            "authenticated": self._auth.configured,
            "token_source": self._auth.token_source,
            "timeout_seconds": self._timeout_seconds,
            "sequence_limits": {
                "max_sequence_steps": self._settings.sequence.max_sequence_steps,
                "max_sequence_wall_clock_seconds": (
                    self._settings.sequence.max_sequence_wall_clock_seconds
                ),
            },
            "tasks_submitted": self._tasks_submitted,
            "tasks_completed": self._tasks_completed,
            "tasks_halted": self._tasks_halted,
            "tasks_rejected": self._tasks_rejected,
            "tasks_timed_out": self._tasks_timed_out,
        }

    # -- Submission ------------------------------------------------------------

    def submit(
        self,
        task_document: str,
        *,
        signature: str,
        nonce: str,
        timestamp: float,
    ) -> TaskEnvelope:
        """Authenticate, validate and execute one task. Never raises for a task failure.

        Every failure mode — authentication, validation, precondition, timeout —
        becomes a structured :class:`TaskEnvelope`; an exception escaping this
        method is a bridge bug, not a task outcome. This is what makes the
        boundary safe to sit behind a dumb pipe: the caller never has to
        interpret an exception or know BLAXCY's internals.

        Args:
            task_document: The canonical serialized task (see
                :func:`bridge.auth.canonical_task_document`).
            signature: The producer's HMAC over the document (see
                :meth:`BridgeAuthenticator.sign_request`).
            nonce: The submission's unique nonce.
            timestamp: The submission's unix timestamp (seconds).

        Returns:
            A :class:`TaskEnvelope` whose ``result.status`` tells the producer
            what happened: ``COMPLETED``, ``HALTED``, ``REJECTED`` or
            ``TIMED_OUT``.
        """
        started = self._clock()

        # -- 1. Authentication (fail closed; nothing executable parsed before) --
        try:
            self._auth.verify_request(
                task_document, signature=signature, nonce=nonce, timestamp=timestamp
            )
        except BridgeAuthError as exc:
            with self._lock:
                self._tasks_rejected += 1
            return self._rejected(task_document, nonce, exc.reason, exc.details)

        # -- 2. Task validation (strict; rejects before any dispatch) ------------
        try:
            task = Task.parse(task_document)
        except TaskValidationError as exc:
            with self._lock:
                self._tasks_rejected += 1
            return self._rejected(task_document, nonce, exc.reason, exc.details)

        # -- 3. Precondition: the sequence runner must be wired ------------------
        if self._dispatcher.sequence_runner is None:
            with self._lock:
                self._tasks_rejected += 1
            return self._rejected(
                task_document,
                nonce,
                "SEQUENCE_RUNNER_UNAVAILABLE",
                {"reason": "no sequence runner is wired into the dispatcher"},
            )

        # -- 4. Execution through the existing door ------------------------------
        with self._lock:
            self._tasks_submitted += 1
        arguments: dict[str, Any] = {"steps": [step.to_dict() for step in task.plan]}
        try:
            envelope = self._dispatch_with_timeout(
                ToolCall(name="run_sequence", arguments=arguments),
                task_id=task.task_id,
            )
        except TaskTimeoutError as exc:
            with self._lock:
                self._tasks_timed_out += 1
            return self._timed_out(task, exc, started)
        except BlaxcyBridgeDispatchError as exc:
            # A dispatcher-level refusal (step limit, sequence disabled, runner
            # unavailable) or an unexpected dispatcher exception is still an
            # honest structured outcome, never a crash.
            with self._lock:
                self._tasks_rejected += 1
            return self._rejected(task_document, nonce, exc.reason, exc.details)

        return self._completed_or_halted(task, envelope, started)

    # -- Execution ----------------------------------------------------------------

    def _dispatch_with_timeout(self, call: ToolCall, *, task_id: str) -> ToolEnvelope:
        """Dispatch with a wall-clock bound, stopping BLAXCY on expiry.

        The sequence runner enforces its own §66.1 wall-clock limit for normal
        progress; this ceiling covers the pathological case — a dispatch that
        never returns. On expiry the bridge triggers BLAXCY's **own** emergency
        stop (latch, release tracked input, force OBSERVE), because the bridge
        no longer knows what the abandoned dispatch is doing and must take the
        desktop back rather than leave it in control. A stop that is unwired or
        fails is reported honestly in the result; it is never assumed.

        The worker thread is a ``daemon``: on expiry it is abandoned (a hung
        dispatch cannot be safely interrupted mid-injection from outside), but a
        process exit still cannot hang on it.
        """
        finished = threading.Event()
        outcome: dict[str, Any] = {}

        def worker() -> None:
            try:
                outcome["envelope"] = self._dispatcher.dispatch(
                    call, task_id=task_id, confirmed=False
                )
            except BaseException as exc:
                outcome["exception"] = exc
            finally:
                finished.set()

        dispatch_started = self._clock()
        thread = threading.Thread(target=worker, daemon=True, name="bridge-dispatch")
        thread.start()
        if not finished.wait(timeout=self._timeout_seconds):
            # The stop must never be the thing that breaks the timeout path: a
            # stop object that fails (or is absent) is reported as
            # ``timeout_stop_latched=False`` in the result, never swallowed and
            # never assumed to have latched.
            stop_result = None
            if self._stop is not None:
                try:
                    stop_result = self._stop.trigger(reason="bridge task timeout")
                except Exception:
                    stop_result = None
            raise TaskTimeoutError(
                f"task exceeded the bridge timeout of {self._timeout_seconds:g}s",
                task_id=task_id,
                elapsed_ms=(self._clock() - dispatch_started) * 1000.0,
                stop_result=stop_result,
            ) from None
        thread.join(timeout=1.0)

        exception = outcome.get("exception")
        if exception is not None:
            raise BlaxcyBridgeDispatchError(
                "DISPATCH_ERROR",
                {"exception": f"{type(exception).__name__}: {exception}"},
            ) from exception
        envelope: ToolEnvelope = outcome["envelope"]

        # A sequence-level refusal (step limit, sequence disabled, runner
        # missing) arrives as a failed envelope *without* the halt marker; a
        # genuine §66.1 halt carries ``data.halted = True``. Both are honest
        # outcomes; they map to different task statuses.
        if envelope.ok:
            return envelope
        if envelope.data.get("halted") is True:
            return envelope
        raise BlaxcyBridgeDispatchError(
            "DISPATCH_REFUSED",
            {
                "error_code": envelope.error_code.value if envelope.error_code else None,
                "message": envelope.message,
            },
        )

    # -- Result building ------------------------------------------------------------

    def _completed_or_halted(
        self, task: Task, envelope: ToolEnvelope, started: float
    ) -> TaskEnvelope:
        """Map a finished ``run_sequence`` envelope onto ``COMPLETED``/``HALTED``."""
        halted = envelope.data.get("halted") is True
        with self._lock:
            if halted:
                self._tasks_halted += 1
            else:
                self._tasks_completed += 1
        result = TaskResult(
            schema_version=TASK_SCHEMA_VERSION,
            task_id=task.task_id,
            status=TaskStatus.HALTED if halted else TaskStatus.COMPLETED,
            sequence_id=_optional_str(envelope.data.get("sequence_id")),
            halted=halted,
            halt_reason=_optional_str(envelope.data.get("halt_reason")),
            halt_code=_optional_str(envelope.data.get("halt_code")),
            wall_clock_ms=_optional_float(envelope.data.get("wall_clock_ms")),
            completed_count=_optional_int(envelope.data.get("completed_count")),
            steps=tuple(envelope.data.get("steps", ())),
        )
        self._log_task(task.task_id, result)
        return TaskEnvelope(task_id=task.task_id, envelope=envelope.to_dict(), result=result)

    def _rejected(
        self,
        task_document: str,
        nonce: str,
        reason: str,
        details: dict[str, Any] | None,
    ) -> TaskEnvelope:
        """Build a ``REJECTED`` envelope. The task id, if any, labels the refusal only."""
        task_id = _task_id_from_document(task_document) or f"unverified-{nonce[:12]}"
        result = TaskResult(
            schema_version=TASK_SCHEMA_VERSION,
            task_id=task_id,
            status=TaskStatus.REJECTED,
            rejection_reason=reason,
            rejection_details=details or {},
        )
        self._log_task(task_id, result, nonce=nonce)
        return TaskEnvelope(task_id=task_id, envelope=None, result=result)

    def _timed_out(self, task: Task, exc: TaskTimeoutError, started: float) -> TaskEnvelope:
        """Build a ``TIMED_OUT`` envelope, reporting whether the stop really latched."""
        latched = bool(getattr(exc.stop_result, "latched", False))
        result = TaskResult(
            schema_version=TASK_SCHEMA_VERSION,
            task_id=task.task_id,
            status=TaskStatus.TIMED_OUT,
            halted=True,
            halt_reason=f"bridge timeout: {exc}",
            halt_code=ErrorCode.RATE_LIMITED.value,
            wall_clock_ms=(self._clock() - started) * 1000.0,
            completed_count=0,
            steps=(),
            timeout_elapsed_ms=exc.elapsed_ms,
            timeout_stop_latched=latched,
        )
        self._log_task(task.task_id, result)
        return TaskEnvelope(task_id=task.task_id, envelope=None, result=result)

    # -- Internals -----------------------------------------------------------------

    def _log_task(self, task_id: str, result: TaskResult, *, nonce: str | None = None) -> None:
        """One structured log line per finished task (§70: metadata only)."""
        payload: dict[str, Any] = {"action_type": "bridge_task", "task_id": task_id}
        for field in ("status", "sequence_id", "halt_code", "rejection_reason"):
            value = getattr(result, field, None)
            if value is not None:
                payload[field] = getattr(value, "value", value)
        if nonce is not None:
            payload["nonce"] = nonce
        _log.info("bridge task finished", extra=payload)


class BlaxcyBridgeDispatchError(Exception):
    """A dispatcher-level refusal or failure, normalized for the task boundary."""

    def __init__(self, reason: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.details: dict[str, Any] = details or {}


def _task_id_from_document(task_document: str) -> str | None:
    """Best-effort task id from a rejected document, for the response only.

    Parsing here is deliberately defensive: the document already failed
    validation, so it may be anything at all. The id labels the refusal so a
    producer can correlate it with its own submission; it never influences
    execution.
    """
    try:
        payload = json.loads(task_document)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    task_id = payload.get("task_id")
    if isinstance(task_id, str) and 0 < len(task_id) <= 128:
        return task_id
    return None


def _optional_str(value: Any) -> str | None:
    return str(value) if value is not None else None


def _optional_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _optional_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


__all__ = [
    "DEFAULT_TASK_TIMEOUT_SECONDS",
    "BlaxcyBridgeDispatchError",
    "LocalBridge",
]
