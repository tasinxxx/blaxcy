"""Element leases (specification section 44).

Every actionable target is bound to a short-lived lease before any input is
injected. A lease is the *only* thing that authorises an input action, and it
is deliberately fragile:

* it expires (default ``800 ms``),
* it is tied to a specific ``frame_id``/``state_version``/``generation``,
* it carries the element's identity fingerprint, so a lease cannot silently
  transfer to a different control that happens to occupy the same pixels.

A lease is never pre-issued for a future action -- including a later step of a
``run_sequence`` (section 66.1). A lease created before its action begins would
already be stale by construction.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from schemas.elements import UIElement, identity_fingerprint
from schemas.enums import PerceptionSource

#: Default lease TTL (specification section 44, configurable via ``[lease]``).
DEFAULT_LEASE_TTL_MS: int = 800


class ElementLease(BaseModel):
    """A short-lived authorisation to act on one specific element."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lease_id: str = Field(min_length=1)
    element_id: str = Field(min_length=1)
    frame_id: int = Field(ge=0)
    state_version: int = Field(ge=0)
    generation: int = Field(ge=0)
    issued_at: float = Field(
        description="time.monotonic() (seconds) at issue; age checks use this clock only."
    )
    ttl_ms: int = Field(gt=0)
    identity: str = Field(min_length=1, description="The element's identity fingerprint.")
    confidence: float = Field(ge=0.0, le=1.0)
    source: PerceptionSource
    monitor_id: int | None = Field(default=None, ge=0)

    @property
    def expires_at(self) -> float:
        """Monotonic time (seconds) at which this lease expires."""
        return self.issued_at + self.ttl_ms / 1000.0

    def age_ms(self, now_monotonic: float | None = None) -> float:
        """Age of the lease in milliseconds."""
        now = time.monotonic() if now_monotonic is None else now_monotonic
        return max(0.0, (now - self.issued_at) * 1000.0)

    def is_expired(self, now_monotonic: float | None = None) -> bool:
        """True when the lease TTL has elapsed."""
        now = time.monotonic() if now_monotonic is None else now_monotonic
        return now >= self.expires_at

    def is_generation_current(self, generation: int) -> bool:
        """True when no newer perception generation has superseded this lease."""
        return self.generation == generation

    def matches_identity(self, identity: str) -> bool:
        """True when ``identity`` is the same fingerprint this lease was issued for."""
        return self.identity == identity

    def matches(self, element: UIElement) -> bool:
        """True when ``element`` is the same control this lease authorises."""
        return self.element_id == element.element_id and self.matches_identity(identity_fingerprint(element))

    def to_dict(self) -> dict[str, Any]:
        """JSON-shaped form for events, logs and the tool envelope.

        Only safe metadata is exposed -- never element content.
        """
        return {
            "lease_id": self.lease_id,
            "element_id": self.element_id,
            "frame_id": self.frame_id,
            "state_version": self.state_version,
            "generation": self.generation,
            "ttl_ms": self.ttl_ms,
            "confidence": self.confidence,
            "source": self.source.value,
            "monitor_id": self.monitor_id,
        }


def issue_lease(
    element: UIElement,
    *,
    frame_id: int,
    state_version: int,
    generation: int,
    ttl_ms: int = DEFAULT_LEASE_TTL_MS,
    now_monotonic: float | None = None,
    lease_id: str | None = None,
) -> ElementLease:
    """Issue a fresh lease for ``element`` against the current observation.

    Args:
        element: The resolved element. Its identity fingerprint is captured.
        frame_id: Frame the element was resolved against.
        state_version: State version the element was resolved against.
        generation: Perception generation at resolution time.
        ttl_ms: Lease lifetime in milliseconds.
        now_monotonic: Override for deterministic tests.
        lease_id: Override for deterministic tests; otherwise a UUID4.
    """
    return ElementLease(
        lease_id=lease_id if lease_id is not None else uuid.uuid4().hex,
        element_id=element.element_id,
        frame_id=frame_id,
        state_version=state_version,
        generation=generation,
        issued_at=time.monotonic() if now_monotonic is None else now_monotonic,
        ttl_ms=ttl_ms,
        identity=identity_fingerprint(element),
        confidence=element.confidence,
        source=element.source,
        monitor_id=element.monitor_id,
    )
