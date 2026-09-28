"""The local BLAXCY bridge (Phase 2A).

The package initializer exposes the public bridge API without importing the
desktop execution stack eagerly. Lightweight producers (for example the GitHub
relay signer) can import bridge.auth/task_protocol without pulling in GUI,
vision, NumPy, or backend dependencies. Public names that belong to the full
bridge remain available through lazy attribute loading.
"""

from __future__ import annotations

from typing import Any

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

_AUTH_NAMES = {
    "BridgeAuth",
    "BridgeAuthError",
    "BridgeAuthenticator",
    "canonical_task_document",
}
_RUNNER_NAMES = {
    "DEFAULT_TASK_TIMEOUT_SECONDS",
    "BlaxcyBridgeDispatchError",
    "LocalBridge",
}
_TASK_NAMES = {
    "MAX_TASK_PAYLOAD_BYTES",
    "MAX_TASK_STEPS",
    "TASK_SCHEMA_VERSION",
    "Task",
    "TaskEnvelope",
    "TaskResult",
    "TaskStatus",
    "TaskTimeoutError",
    "TaskValidationError",
}


def __getattr__(name: str) -> Any:
    if name in _AUTH_NAMES:
        from bridge import auth
        return getattr(auth, name)
    if name in _RUNNER_NAMES:
        from bridge import runner
        return getattr(runner, name)
    if name in _TASK_NAMES:
        from bridge import task_protocol
        return getattr(task_protocol, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
