"""Element leases (specification section 44)."""

from __future__ import annotations

from schemas.elements import UIElement
from schemas.enums import PerceptionSource, UIRole
from schemas.leases import DEFAULT_LEASE_TTL_MS, ElementLease, issue_lease


def _element(**overrides: object) -> UIElement:
    """Build a valid button element."""
    base: dict[str, object] = {
        "element_id": "e1",
        "role": UIRole.BUTTON,
        "text": "OK",
        "source": PerceptionSource.ATSPI,
        "confidence": 0.9,
        "atspi_path": "/p/ok",
        "monitor_id": 0,
    }
    base.update(overrides)
    return UIElement.model_validate(base)


def test_issue_lease_captures_observation_stamps() -> None:
    """A lease records the frame/version/generation it was issued against."""
    element = _element()
    lease = issue_lease(element, frame_id=10, state_version=4, generation=2, lease_id="L1", now_monotonic=100.0)
    assert isinstance(lease, ElementLease)
    assert lease.lease_id == "L1"
    assert lease.element_id == "e1"
    assert (lease.frame_id, lease.state_version, lease.generation) == (10, 4, 2)
    assert lease.confidence == 0.9
    assert lease.source is PerceptionSource.ATSPI
    assert lease.ttl_ms == DEFAULT_LEASE_TTL_MS


def test_lease_expires_at_ttl() -> None:
    """A lease past its TTL is expired; within it, not."""
    lease = issue_lease(_element(), frame_id=1, state_version=1, generation=0, ttl_ms=800, now_monotonic=100.0)
    assert lease.is_expired(now_monotonic=100.5) is False
    assert lease.is_expired(now_monotonic=100.8) is True
    assert lease.age_ms(now_monotonic=100.25) == 250.0


def test_lease_generation_must_match() -> None:
    """A generation bump invalidates the lease (section 32)."""
    lease = issue_lease(_element(), frame_id=1, state_version=1, generation=5)
    assert lease.is_generation_current(5) is True
    assert lease.is_generation_current(6) is False


def test_lease_matches_only_its_own_element() -> None:
    """A lease does not transfer to a different control on the same pixels."""
    element = _element()
    lease = issue_lease(element, frame_id=1, state_version=1, generation=0)
    assert lease.matches(element) is True
    assert lease.matches(_element(element_id="other", atspi_path="/p/elsewhere")) is False


def test_lease_to_dict_exposes_only_safe_metadata() -> None:
    """Lease serialisation never leaks element content."""
    lease = issue_lease(_element(), frame_id=3, state_version=2, generation=1)
    payload = lease.to_dict()
    assert set(payload) == {
        "lease_id",
        "element_id",
        "frame_id",
        "state_version",
        "generation",
        "ttl_ms",
        "confidence",
        "source",
        "monitor_id",
    }
    assert "text" not in payload
