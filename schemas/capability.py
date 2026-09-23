"""Capability reporting schema (specification section 28).

Every capability reports exactly one of ``AVAILABLE`` / ``DEGRADED`` /
``UNAVAILABLE`` and carries the evidence behind that verdict. A capability is
never selected because a binary exists on ``PATH`` -- only a passing functional
probe may produce ``AVAILABLE``.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from schemas.enums import CapabilityName, CapabilityStatus


class Capability(BaseModel):
    """The honest, evidence-carrying status of one capability."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: CapabilityName
    status: CapabilityStatus
    backend: str | None = Field(
        default=None,
        description="The concrete backend that passed the probe, if any.",
    )
    latency_ms: float | None = Field(
        default=None,
        ge=0.0,
        description="Measured probe latency in milliseconds, if a timed probe ran.",
    )
    details: dict[str, Any] = Field(
        default_factory=dict,
        description="Probe evidence: versions, monitor topology, limits, etc.",
    )
    reason: str | None = Field(
        default=None,
        description="Why the capability is DEGRADED or UNAVAILABLE. None when AVAILABLE.",
    )
    fix_hint: str | None = Field(
        default=None,
        description="A concrete, actionable remediation hint when not AVAILABLE.",
    )

    @property
    def usable(self) -> bool:
        """True when the capability can actually be used right now."""
        return self.status is CapabilityStatus.AVAILABLE


class CapabilityReport(BaseModel):
    """An ordered, queryable collection of capability verdicts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    capabilities: tuple[Capability, ...] = ()
    generated_at: float = Field(
        description="Unix timestamp (seconds) at which the report was produced."
    )
    session_type: str = Field(
        default="unknown",
        description="Detected session flavour at probe time (x11/wayland/xwayland/unknown).",
    )

    def get(self, name: CapabilityName) -> Capability | None:
        """Return the capability by name, or ``None`` if it was not probed."""
        for cap in self.capabilities:
            if cap.name is name:
                return cap
        return None

    def by_status(self, status: CapabilityStatus) -> tuple[Capability, ...]:
        """Return every capability currently reporting ``status``."""
        return tuple(c for c in self.capabilities if c.status is status)

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable form, preserving probe order."""
        return {
            "generated_at": self.generated_at,
            "session_type": self.session_type,
            "capabilities": [
                {
                    "name": c.name.value,
                    "status": c.status.value,
                    "backend": c.backend,
                    "latency_ms": c.latency_ms,
                    "details": c.details,
                    "reason": c.reason,
                    "fix_hint": c.fix_hint,
                }
                for c in self.capabilities
            ],
        }
