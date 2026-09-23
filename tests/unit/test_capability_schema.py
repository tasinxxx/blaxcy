"""Capability model and report behaviour (specification section 28)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from schemas.capability import Capability, CapabilityReport
from schemas.enums import CapabilityName, CapabilityStatus


def _cap(
    name: CapabilityName = CapabilityName.CAPTURE,
    status: CapabilityStatus = CapabilityStatus.AVAILABLE,
) -> Capability:
    """Build a capability for tests."""
    return Capability(name=name, status=status)


def test_usable_only_when_available() -> None:
    """Only AVAILABLE capabilities are usable."""
    assert _cap(status=CapabilityStatus.AVAILABLE).usable is True
    assert _cap(status=CapabilityStatus.DEGRADED).usable is False
    assert _cap(status=CapabilityStatus.UNAVAILABLE).usable is False


def test_capability_is_immutable() -> None:
    """Capability verdicts are frozen so status cannot drift silently."""
    assert Capability.model_config.get("frozen") is True


def test_capability_rejects_unknown_fields() -> None:
    """Extra fields are rejected: the model is the contract."""
    with pytest.raises(ValidationError):
        Capability.model_validate(
            {"name": "capture", "status": "AVAILABLE", "bogus": 1}
        )


def test_report_lookup_and_filter() -> None:
    """Reports support name lookup and status filtering."""
    report = CapabilityReport(
        capabilities=(
            _cap(CapabilityName.CAPTURE, CapabilityStatus.AVAILABLE),
            _cap(CapabilityName.OCR, CapabilityStatus.UNAVAILABLE),
        ),
        generated_at=0.0,
    )
    assert report.get(CapabilityName.CAPTURE) is not None
    assert report.get(CapabilityName.BRAIN) is None
    assert len(report.by_status(CapabilityStatus.UNAVAILABLE)) == 1


def test_report_to_dict_is_json_shaped() -> None:
    """The JSON payload carries the evidence fields the spec requires."""
    report = CapabilityReport(
        capabilities=(_cap(),),
        generated_at=123.0,
        session_type="x11",
    )
    payload = report.to_dict()
    assert payload["session_type"] == "x11"
    entry = payload["capabilities"][0]
    assert set(entry) == {"name", "status", "backend", "latency_ms", "details", "reason", "fix_hint"}
