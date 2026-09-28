"""The GitHub task-envelope format (Phase 2B, requirement 2).

The envelope is the **repository-side document**: the file a trusted producer
commits into the private relay repository, and the file the self-hosted runner
reads. It wraps the Phase 2A task *unchanged* and adds only what a GitHub
relay needs and the local boundary cannot express:

* ``relay_version`` — this envelope contract's own version, pinned and required,
  so a runner built for one contract refuses another loudly.
* ``task_id`` — repeated at the envelope level so a worker can identify, claim
  and deduplicate a task **before** opening the inner task document. Task-id
  equality with the inner task is verified after parsing: an envelope that
  names one task and carries another is a tamper/replay indicator and is
  rejected.
* ``task`` — the exact Phase 2A task document (schema_version 1). It is
  validated with :meth:`bridge.task_protocol.Task.parse`, so **every** guard
  the local boundary enforces — size, unknown fields, credential-shaped keys,
  pre-resolved keys at every depth, coordinate-shaped targets, step limits —
  applies verbatim to a GitHub-submitted task (requirement 7). The relay adds
  no argument vocabulary and accepts no field the bridge would refuse.
* ``expires_at`` — **required** unix seconds after which the envelope is dead.
  A relay task sits in a repository between commit and pickup; without a
  freshness bound, "pick up later" becomes "execute stale plans". The runner
  refuses an expired envelope before any claim-and-execute (requirement 11's
  *expired* case). The default horizon is deliberately short.
* ``submission`` — the producer's ``signature``/``nonce``/``timestamp`` over
  the **inner task document** (the same canonical bytes a local producer
  signs). The runner verifies this MAC with the same shared token before any
  execution, so a relay task meets the identical authentication bar as a local
  one; the envelope body alone is not executable.

The wrapper therefore carries nothing executable: there is no ``command``, no
``script``, no ``args`` field anywhere in the format (requirements 7/13) — a
producer expresses intent as a plan of tool *steps*, and only BLAXCY's own
pipeline decides whether those steps may run.
"""

from __future__ import annotations

import json
import time
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from bridge.auth import canonical_task_document
from bridge.task_protocol import TASK_SCHEMA_VERSION, Task, TaskValidationError

#: The envelope contract this module implements. Independent of the inner task
#: schema version so the two can evolve on their own schedules.
RELAY_ENVELOPE_VERSION: int = 1

#: Maximum envelope size. The inner task is bounded at 64 KiB by the bridge
#: schema; the wrapper adds provenance only, so the envelope bound stays close.
MAX_ENVELOPE_BYTES: int = 68 * 1024

#: How long an unclaimed envelope stays valid, in seconds (one hour). A task
#: committed and never picked up within this window is dead on arrival: the
#: runner refuses it rather than executing a stale plan (requirement 11).
DEFAULT_ENVELOPE_TTL_SECONDS: float = 3600.0

#: Allowed top-level envelope keys. Anything else is rejected.
_ALLOWED_ENVELOPE_KEYS: frozenset[str] = frozenset(
    {"relay_version", "task_id", "task", "expires_at", "submission", "created_at", "producer"}
)

_ALLOWED_SUBMISSION_KEYS: frozenset[str] = frozenset({"signature", "nonce", "timestamp"})


class EnvelopeError(Exception):
    """An envelope the relay refused before any claim or dispatch."""

    def __init__(self, reason: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.details: dict[str, Any] = details or {}


class SubmissionCredentials(BaseModel):
    """The producer's signature over the inner task document."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    signature: str = Field(min_length=16, max_length=128)
    nonce: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    timestamp: float = Field(gt=0)


class TaskEnvelopeDocument(BaseModel):
    """The validated GitHub task envelope (version 1)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    relay_version: int
    task_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    #: The inner task, kept in parsed form. Construct via :meth:`parse`.
    task: Task
    #: Required expiry (unix seconds). There is no default: an envelope without
    #: a stated lifetime is refused, never assumed immortal.
    expires_at: float = Field(gt=0)
    submission: SubmissionCredentials
    created_at: str | None = Field(default=None, max_length=64)
    producer: str | None = Field(default=None, max_length=128)

    @field_validator("relay_version")
    @classmethod
    def _relay_version_must_match(cls, value: int) -> int:
        if value != RELAY_ENVELOPE_VERSION:
            raise ValueError(
                f"unsupported relay_version {value}; this runner speaks {RELAY_ENVELOPE_VERSION}"
            )
        return value

    @model_validator(mode="after")
    def _cross_field_guards(self) -> TaskEnvelopeDocument:
        """The two tamper/replay indicators the wrapper can check by itself."""
        if self.task.task_id != self.task_id:
            raise ValueError(
                f"envelope task_id {self.task_id!r} does not match the inner task's id "
                f"{self.task.task_id!r}"
            )
        return self

    # -- The boundary entry point ---------------------------------------------

    @classmethod
    def parse(cls, payload: Any, *, now: float | None = None) -> TaskEnvelopeDocument:
        """Parse and validate one raw envelope, or raise :class:`EnvelopeError`.

        Order matters and is fail-closed: size and shape first, then the
        wrapper's contract, then the **inner task through the bridge's own
        parser** — so a GitHub task is held to exactly the local standard
        (requirement 7), with the bridge's machine-readable reasons preserved
        in ``details.inner_reason``.
        """
        if isinstance(payload, (str, bytes)):
            size = len(payload)
            if size > MAX_ENVELOPE_BYTES:
                raise EnvelopeError(
                    "ENVELOPE_TOO_LARGE",
                    {"size_bytes": size, "max_bytes": MAX_ENVELOPE_BYTES},
                )
            try:
                payload = json.loads(payload)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise EnvelopeError("ENVELOPE_NOT_JSON", {"error": str(exc)}) from exc
        if not isinstance(payload, dict):
            raise EnvelopeError("ENVELOPE_NOT_AN_OBJECT", {"type": type(payload).__name__})

        unknown = sorted(str(key) for key in payload if str(key) not in _ALLOWED_ENVELOPE_KEYS)
        if unknown:
            raise EnvelopeError("UNKNOWN_ENVELOPE_FIELDS", {"unknown": unknown})

        if payload.get("relay_version") != RELAY_ENVELOPE_VERSION:
            raise EnvelopeError(
                "UNSUPPORTED_RELAY_VERSION",
                {"got": payload.get("relay_version"), "expected": RELAY_ENVELOPE_VERSION},
            )

        inner = payload.get("task")
        if not isinstance(inner, dict):
            raise EnvelopeError("ENVELOPE_TASK_MISSING", {"type": type(inner).__name__})
        if inner.get("schema_version") != TASK_SCHEMA_VERSION:
            raise EnvelopeError(
                "UNSUPPORTED_TASK_SCHEMA",
                {"got": inner.get("schema_version"), "expected": TASK_SCHEMA_VERSION},
            )
        submission = payload.get("submission")
        if not isinstance(submission, dict):
            raise EnvelopeError("SUBMISSION_MISSING", {"type": type(submission).__name__})
        unknown_submission = sorted(
            str(key) for key in submission if str(key) not in _ALLOWED_SUBMISSION_KEYS
        )
        if unknown_submission:
            raise EnvelopeError("UNKNOWN_SUBMISSION_FIELDS", {"unknown": unknown_submission})
        if not isinstance(payload.get("expires_at"), (int, float)) or payload["expires_at"] <= 0:
            raise EnvelopeError("EXPIRY_REQUIRED", {"got": payload.get("expires_at")})

        # -- The inner task goes through the bridge's OWN parser first ----------
        # Pydantic model validation alone would run the Task model's field
        # validators but NOT the raw-payload guards in ``Task.parse`` (the
        # whole-document scan for credential-shaped and pre-resolved keys at
        # every depth). Requirement 7 demands every bridge guard applies
        # verbatim, so the inner document is parsed exactly as the local
        # boundary parses it — and its refusal reasons travel verbatim.
        #
        # The parsed model is NOT re-serialized for signature coverage: a model
        # round-trip re-orders keys and materializes defaults, which would not
        # be the bytes the producer signed. The raw inner document (the exact
        # committed bytes) is what the HMAC covers, and
        # ``inner_task_document`` re-serializes it with the producer's own
        # canonicalization when the runner verifies.
        try:
            inner_task = Task.parse(inner)
        except TaskValidationError as exc:
            raise EnvelopeError(
                "INNER_TASK_REJECTED",
                {"inner_reason": exc.reason, "inner_details": exc.details},
            ) from exc

        envelope_payload = dict(payload)
        envelope_payload["task"] = inner_task  # already validated
        try:
            document = cls.model_validate(envelope_payload)
        except Exception as exc:
            raise EnvelopeError(
                "ENVELOPE_SCHEMA_INVALID", {"errors": _validation_errors(exc)}
            ) from exc
        #: The raw inner document, preserved for signature verification: the
        #: producer's own canonical form (compact separators), byte-identical to
        #: what the HMAC covered.
        object.__setattr__(
            document, "_raw_inner_document", canonical_task_document(inner)
        )
        return document

    # -- Freshness and authentication -----------------------------------------

    def ensure_not_expired(self, *, now: float | None = None) -> None:
        """Refuse an envelope past its stated lifetime (requirement 11)."""
        moment = time.time() if now is None else now
        if moment > self.expires_at:
            raise EnvelopeError(
                "ENVELOPE_EXPIRED",
                {
                    "expires_at": self.expires_at,
                    "now": moment,
                    "expired_by_seconds": round(moment - self.expires_at, 1),
                },
            )

    def inner_task_document(self) -> str:
        """The exact inner-task bytes the producer's HMAC must cover.

        When parsed through :meth:`parse`, this is the raw committed document
        re-serialized with the producer's canonicalization (sorted keys, no
        defaults materialized) — byte-identical to what the producer signed.
        A model round-trip would re-order keys and add defaults, which is not
        what any signature covers.
        """
        raw = cast("str | None", getattr(self, "_raw_inner_document", None))
        if raw is not None:
            return raw
        return canonical_task_document(self.task.model_dump(mode="json"))

    def submission_bytes(self) -> str:
        """The canonical envelope bytes (what gets committed to the repository)."""
        return canonical_task_document(self.model_dump(mode="json", exclude_none=True))


def _validation_errors(exc: Exception) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = getattr(exc, "errors", lambda **_: [])()
    return [
        {"path": ".".join(str(part) for part in error.get("loc", ())), "type": error.get("type")}
        for error in errors
    ]


__all__ = [
    "DEFAULT_ENVELOPE_TTL_SECONDS",
    "MAX_ENVELOPE_BYTES",
    "RELAY_ENVELOPE_VERSION",
    "EnvelopeError",
    "SubmissionCredentials",
    "TaskEnvelopeDocument",
]
