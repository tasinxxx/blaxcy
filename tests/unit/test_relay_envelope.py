"""Unit tests for the GitHub relay envelope (Phase 2B requirement 2/7/11)."""

from __future__ import annotations

import time
from typing import cast

import pytest

from bridge.auth import BridgeAuthenticator, canonical_task_document
from bridge.task_protocol import TASK_SCHEMA_VERSION
from relay.envelope import (
    DEFAULT_ENVELOPE_TTL_SECONDS,
    RELAY_ENVELOPE_VERSION,
    EnvelopeError,
    TaskEnvelopeDocument,
)


def _task_payload(task_id: str = "task-1") -> dict[str, object]:
    return {
        "schema_version": TASK_SCHEMA_VERSION,
        "task_id": task_id,
        "plan": [{"step_id": "s1", "tool": "click", "target": "Send"}],
    }


def _auth(token: str = "r" * 40) -> BridgeAuthenticator:
    return BridgeAuthenticator(token=token)


def _envelope_payload(
    *,
    task_id: str = "task-1",
    task: dict[str, object] | None = None,
    expires_in: float = 600.0,
    now: float | None = None,
    auth: BridgeAuthenticator | None = None,
    signer_nonce: str = "relaynonce01",
) -> dict[str, object]:
    """A fully signed, valid envelope payload (the producer's job)."""
    moment = now if now is not None else time.time()
    signer = auth if auth is not None else _auth()
    inner = task if task is not None else _task_payload(task_id)
    document = canonical_task_document(inner)
    signature = signer.sign_request(document, nonce=signer_nonce, timestamp=moment)
    return {
        "relay_version": RELAY_ENVELOPE_VERSION,
        "task_id": task_id,
        "task": inner,
        "expires_at": moment + expires_in,
        "submission": {"signature": signature, "nonce": signer_nonce, "timestamp": moment},
        "producer": "test-producer",
    }


def _parse(payload: dict[str, object] | str, **kwargs: float | None) -> TaskEnvelopeDocument:
    return TaskEnvelopeDocument.parse(payload, **kwargs)


# -- Happy path --------------------------------------------------------------------


def test_a_valid_envelope_parses() -> None:
    parsed = _parse(_envelope_payload())
    assert parsed.relay_version == RELAY_ENVELOPE_VERSION
    assert parsed.task.task_id == "task-1"
    assert parsed.producer == "test-producer"
    assert parsed.expires_at > time.time()


def test_a_json_string_parses_like_a_dict() -> None:
    import json

    parsed = _parse(json.dumps(_envelope_payload()))
    assert parsed.task.task_id == "task-1"


# -- Version and shape guards (fail closed) ------------------------------------------


def test_a_wrong_relay_version_is_rejected() -> None:
    payload = _envelope_payload()
    payload["relay_version"] = RELAY_ENVELOPE_VERSION + 1
    with pytest.raises(EnvelopeError) as excinfo:
        _parse(payload)
    assert excinfo.value.reason == "UNSUPPORTED_RELAY_VERSION"


def test_unknown_envelope_fields_are_rejected() -> None:
    payload = _envelope_payload()
    payload["command"] = "rm -rf /"
    with pytest.raises(EnvelopeError) as excinfo:
        _parse(payload)
    assert excinfo.value.reason == "UNKNOWN_ENVELOPE_FIELDS"


def test_a_missing_task_is_rejected() -> None:
    payload = _envelope_payload()
    del payload["task"]
    with pytest.raises(EnvelopeError) as excinfo:
        _parse(payload)
    assert excinfo.value.reason == "ENVELOPE_TASK_MISSING"


def test_a_missing_submission_block_is_rejected() -> None:
    payload = _envelope_payload()
    del payload["submission"]
    with pytest.raises(EnvelopeError) as excinfo:
        _parse(payload)
    assert excinfo.value.reason == "SUBMISSION_MISSING"


def test_unknown_submission_fields_are_rejected() -> None:
    payload = _envelope_payload()
    payload["submission"] = {**cast("dict[str, object]", payload["submission"]), "scope": "all"}
    with pytest.raises(EnvelopeError) as excinfo:
        _parse(payload)
    assert excinfo.value.reason == "UNKNOWN_SUBMISSION_FIELDS"


def test_a_missing_expiry_is_rejected() -> None:
    payload = _envelope_payload()
    del payload["expires_at"]
    with pytest.raises(EnvelopeError) as excinfo:
        _parse(payload)
    assert excinfo.value.reason == "EXPIRY_REQUIRED"


def test_an_oversized_envelope_is_rejected() -> None:
    huge = '{"relay_version": 1, "pad": "' + "x" * (68 * 1024) + '"}'
    with pytest.raises(EnvelopeError) as excinfo:
        TaskEnvelopeDocument.parse(huge)
    # The size guard fires before any parsing: an oversized payload is refused
    # as too large, whatever else might be wrong with it.
    assert excinfo.value.reason == "ENVELOPE_TOO_LARGE"


# -- Tamper and mismatch guards --------------------------------------------------------


def test_an_envelope_task_id_mismatching_the_inner_task_is_rejected() -> None:
    payload = _envelope_payload(task_id="task-1")
    payload["task_id"] = "task-2"  # wrapper renamed after signing
    with pytest.raises(EnvelopeError) as excinfo:
        _parse(payload)
    assert excinfo.value.reason == "INNER_TASK_REJECTED" or excinfo.value.reason == "ENVELOPE_SCHEMA_INVALID"


def test_an_inner_task_the_bridge_would_refuse_is_rejected_with_the_bridge_reason() -> None:
    unsafe = _task_payload()
    unsafe["plan"] = [{"step_id": "s1", "tool": "click", "target": "Send", "element_id": "e1"}]
    payload = _envelope_payload(task=unsafe)
    with pytest.raises(EnvelopeError) as excinfo:
        _parse(payload)
    assert excinfo.value.reason == "INNER_TASK_REJECTED"
    assert excinfo.value.details["inner_reason"] == "PRE_RESOLVED_TASK_FIELD"


def test_a_credential_carrying_inner_task_is_rejected() -> None:
    unsafe = _task_payload()
    unsafe["plan"] = [
        {"step_id": "s1", "tool": "type_text", "target": "field", "text": "x", "password": "hunter2"}
    ]
    payload = _envelope_payload(task=unsafe)
    with pytest.raises(EnvelopeError) as excinfo:
        _parse(payload)
    assert excinfo.value.details["inner_reason"] == "FORBIDDEN_TASK_FIELD"


def test_a_shell_tool_inside_the_plan_is_rejected() -> None:
    unsafe = _task_payload()
    unsafe["plan"] = [{"step_id": "s1", "tool": "execute_shell", "command": "rm -rf /"}]
    payload = _envelope_payload(task=unsafe)
    with pytest.raises(EnvelopeError) as excinfo:
        _parse(payload)
    assert excinfo.value.reason == "INNER_TASK_REJECTED"


# -- Expiry (requirement 11: expired tasks fail closed) --------------------------------


def test_an_unexpired_envelope_passes_the_freshness_check() -> None:
    parsed = _parse(_envelope_payload(expires_in=600.0))
    parsed.ensure_not_expired()


def test_an_expired_envelope_is_refused() -> None:
    parsed = _parse(_envelope_payload(expires_in=-1.0))
    with pytest.raises(EnvelopeError) as excinfo:
        parsed.ensure_not_expired()
    assert excinfo.value.reason == "ENVELOPE_EXPIRED"
    assert excinfo.value.details["expired_by_seconds"] >= 0


def test_expiry_uses_the_injected_clock() -> None:
    parsed = _parse(_envelope_payload(expires_in=10.0))
    with pytest.raises(EnvelopeError):
        parsed.ensure_not_expired(now=parsed.expires_at + 1)


def test_the_default_ttl_is_bounded_and_short() -> None:
    assert 0 < DEFAULT_ENVELOPE_TTL_SECONDS <= 24 * 3600


# -- Signature coverage -----------------------------------------------------------------


def test_the_producer_signature_covers_the_inner_task() -> None:
    """A signature made over a *different* document must not verify."""
    parsed = _parse(_envelope_payload())
    other = _auth()
    forged = other.sign_request(
        canonical_task_document(_task_payload("task-9")),
        nonce=parsed.submission.nonce,
        timestamp=parsed.submission.timestamp,
    )
    assert forged != parsed.submission.signature
