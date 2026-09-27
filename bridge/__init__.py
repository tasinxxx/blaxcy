"""The local BLAXCY bridge (Phase 2A).

A thin, local-only boundary that accepts one validated, signed high-level task
and executes it through the existing :class:`~ai.tool_protocol.ToolDispatcher`.
No network listener, no MCP, no new execution path — every safety property is
inherited from the core the bridge composes.

Modules:

* :mod:`bridge.task_protocol` — the strict, versioned ``Task``/``TaskResult``
  schemas and the raw-payload guards (size, unknown keys, credential-shaped
  keys, pre-resolved keys).
* :mod:`bridge.auth` — HMAC-SHA256 request signing for the local
  bridge→runner boundary; token from ``BLAXCY_BRIDGE_TOKEN``, never hard-coded.
* :mod:`bridge.runner` — :class:`LocalBridge`, the Body-side entry point.
"""

from __future__ import annotations

from bridge.auth import BridgeAuth, BridgeAuthenticator, BridgeAuthError, canonical_task_document
from bridge.runner import DEFAULT_TASK_TIMEOUT_SECONDS, BlaxcyBridgeDispatchError, LocalBridge
from bridge.task_protocol import (
    MAX_TASK_PAYLOAD_BYTES,
    MAX_TASK_STEPS,
    TASK_SCHEMA_VERSION,
    Task,
    TaskEnvelope,
    TaskResult,
    TaskStatus,
    TaskTimeoutError,
    TaskValidationError,
)

__all__ = [
    "DEFAULT_TASK_TIMEOUT_SECONDS",
    "MAX_TASK_PAYLOAD_BYTES",
    "MAX_TASK_STEPS",
    "TASK_SCHEMA_VERSION",
    "BlaxcyBridgeDispatchError",
    "BridgeAuth",
    "BridgeAuthError",
    "BridgeAuthenticator",
    "LocalBridge",
    "Task",
    "TaskEnvelope",
    "TaskResult",
    "TaskStatus",
    "TaskTimeoutError",
    "TaskValidationError",
    "canonical_task_document",
]
