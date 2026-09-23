"""Structured error taxonomy (specification sections 77 and 80).

BLAXCY never surfaces a bare string failure. Every failure carries a structured
:class:`ErrorCode`, a human-readable message, and optional evidence. Platform
limitations produce an honest ``DEGRADED``/``UNAVAILABLE`` state plus one of
these codes rather than a fabricated success.
"""

from __future__ import annotations

from typing import Any

from schemas.enums import ErrorCode


class BlaxcyError(Exception):
    """A structured, taxonomy-bound BLAXCY failure.

    The exception message is safe for logs and for the Brain; credential and
    protected content must never be placed in ``message`` or ``details``.
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details: dict[str, Any] = dict(details) if details else {}
        # Retryability is asserted per failure, never inferred from the code
        # alone: section 21 permits a retry only when the action class and the
        # observed evidence allow it.
        self.retryable = retryable

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable error payload for the tool envelope."""
        return {
            "error_code": self.code.value,
            "message": self.message,
            "details": self.details,
            "retryable": self.retryable,
        }

    def __repr__(self) -> str:  # pragma: no cover - diagnostic only
        return f"BlaxcyError(code={self.code.value!r}, message={self.message!r})"
