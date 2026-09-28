"""The GitHub relay layer (Phase 2B).

The package initializer exposes the relay API without eagerly importing the
self-hosted executor and BLAXCY desktop stack. Lightweight producer jobs can
import relay.envelope/store without GUI, vision, or backend dependencies.
"""

from __future__ import annotations

from typing import Any

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

_ENVELOPE_NAMES = {
    "DEFAULT_ENVELOPE_TTL_SECONDS",
    "RELAY_ENVELOPE_VERSION",
    "EnvelopeError",
    "SubmissionCredentials",
    "TaskEnvelopeDocument",
}
_EXECUTOR_NAMES = {"RelayExecutor"}
_LIFECYCLE_NAMES = {
    "TERMINAL_STATES",
    "LifecycleError",
    "TaskLifecycle",
    "can_transition",
    "require_transition",
}
_STORE_NAMES = {"StoreError", "TaskStore"}


def __getattr__(name: str) -> Any:
    if name in _ENVELOPE_NAMES:
        from relay import envelope
        return getattr(envelope, name)
    if name in _EXECUTOR_NAMES:
        from relay import executor
        return getattr(executor, name)
    if name in _LIFECYCLE_NAMES:
        from relay import lifecycle
        return getattr(lifecycle, name)
    if name in _STORE_NAMES:
        from relay import store
        return getattr(store, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
