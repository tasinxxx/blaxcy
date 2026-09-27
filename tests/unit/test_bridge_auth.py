"""Unit tests for the bridge boundary authentication (Phase 2A requirement 11)."""

from __future__ import annotations

import hmac
from hashlib import sha256

import pytest

from bridge.auth import (
    MAX_CLOCK_SKEW_SECONDS,
    TOKEN_ENV_VAR,
    BridgeAuthenticator,
    BridgeAuthError,
    canonical_task_document,
)


def _auth(token: str = "a" * 32) -> BridgeAuthenticator:
    return BridgeAuthenticator(token=token)


def _sign(auth: BridgeAuthenticator, document: str, nonce: str = "n1", timestamp: float = 1000.0) -> str:
    return auth.sign_request(document, nonce=nonce, timestamp=timestamp)


# -- Configuration -----------------------------------------------------------------


def test_a_token_shorter_than_the_minimum_is_a_configuration_error() -> None:
    with pytest.raises(BridgeAuthError) as excinfo:
        BridgeAuthenticator(token="short")
    assert excinfo.value.reason == "TOKEN_TOO_SHORT"


def test_no_token_configured_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
    auth = BridgeAuthenticator()
    assert auth.configured is False
    with pytest.raises(BridgeAuthError) as excinfo:
        auth.verify_request("{}", signature="s", nonce="n", timestamp=1000.0)
    assert excinfo.value.reason == "NO_TOKEN_CONFIGURED"
    with pytest.raises(BridgeAuthError):
        auth.sign_request("{}", nonce="n")


def test_the_token_is_loaded_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TOKEN_ENV_VAR, "e" * 32)
    auth = BridgeAuthenticator()
    assert auth.configured is True
    assert auth.token_source == f"env:{TOKEN_ENV_VAR}"


def test_an_explicit_token_beats_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TOKEN_ENV_VAR, "e" * 32)
    auth = BridgeAuthenticator(token="a" * 32)
    assert auth.token_source == "explicit"


# -- Signing and verification --------------------------------------------------------


def test_a_correctly_signed_request_verifies() -> None:
    auth = _auth()
    document = canonical_task_document({"task_id": "t"})
    signature = _sign(auth, document)
    auth.verify_request(document, signature=signature, nonce="n1", timestamp=1000.0, now=1000.0)


def test_a_tampered_document_fails_verification() -> None:
    auth = _auth()
    document = canonical_task_document({"task_id": "t"})
    signature = _sign(auth, document)
    tampered = canonical_task_document({"task_id": "t", "plan": []})
    with pytest.raises(BridgeAuthError) as excinfo:
        auth.verify_request(tampered, signature=signature, nonce="n1", timestamp=1000.0, now=1000.0)
    assert excinfo.value.reason == "SIGNATURE_MISMATCH"


def test_a_wrong_token_cannot_verify() -> None:
    signer = _auth(token="a" * 32)
    verifier = _auth(token="b" * 32)
    document = canonical_task_document({"task_id": "t"})
    signature = _sign(signer, document)
    with pytest.raises(BridgeAuthError) as excinfo:
        verifier.verify_request(document, signature=signature, nonce="n1", timestamp=1000.0, now=1000.0)
    assert excinfo.value.reason == "SIGNATURE_MISMATCH"


def test_a_nonce_cannot_be_replayed_even_with_a_valid_signature() -> None:
    auth = _auth()
    document = canonical_task_document({"task_id": "t"})
    signature = _sign(auth, document)
    auth.verify_request(document, signature=signature, nonce="n1", timestamp=1000.0, now=1000.0)
    with pytest.raises(BridgeAuthError) as excinfo:
        auth.verify_request(document, signature=signature, nonce="n1", timestamp=1000.0, now=1000.0)
    assert excinfo.value.reason == "NONCE_REPLAYED"


def test_a_failed_signature_still_consumes_the_nonce() -> None:
    """The same (nonce, bad signature) pair cannot be probed repeatedly."""
    auth = _auth()
    document = canonical_task_document({"task_id": "t"})
    # The first attempt fails on the MAC...
    with pytest.raises(BridgeAuthError) as initial:
        auth.verify_request(document, signature="bad", nonce="n1", timestamp=1000.0, now=1000.0)
    assert initial.value.reason == "SIGNATURE_MISMATCH"
    # ...but the nonce is consumed: a retry with the same pair is a replay,
    # and so is a retry with a *correct* signature on the same nonce.
    with pytest.raises(BridgeAuthError) as second:
        auth.verify_request(document, signature=_sign(auth, document), nonce="n1", timestamp=1000.0, now=1000.0)
    assert second.value.reason == "NONCE_REPLAYED"


def test_a_timestamp_outside_the_skew_window_is_refused() -> None:
    auth = _auth()
    document = canonical_task_document({"task_id": "t"})
    signature = _sign(auth, document)
    with pytest.raises(BridgeAuthError) as excinfo:
        auth.verify_request(
            document,
            signature=signature,
            nonce="n1",
            timestamp=1000.0,
            now=1000.0 + MAX_CLOCK_SKEW_SECONDS + 1.0,
        )
    assert excinfo.value.reason == "TIMESTAMP_OUT_OF_RANGE"


def test_missing_credentials_are_refused() -> None:
    auth = _auth()
    for kwargs in (
        {"signature": "", "nonce": "n1"},
        {"signature": "s", "nonce": ""},
    ):
        with pytest.raises(BridgeAuthError) as excinfo:
            auth.verify_request("{}", timestamp=1000.0, now=1000.0, **kwargs)
        assert excinfo.value.reason == "MISSING_CREDENTIALS"


def test_the_signature_covers_the_nonce_and_timestamp() -> None:
    """Signing the document alone is not enough — the MAC binds all three."""
    auth = _auth()
    document = canonical_task_document({"task_id": "t"})
    signature = _sign(auth, document, nonce="n1", timestamp=1000.0)
    with pytest.raises(BridgeAuthError):
        auth.verify_request(document, signature=signature, nonce="n2", timestamp=1000.0, now=1000.0)


def test_the_mac_matches_a_reference_hmac_implementation() -> None:
    """The scheme is plain HMAC-SHA256 over nonce|timestamp|document."""
    token = "k" * 32
    auth = BridgeAuthenticator(token=token)
    document = canonical_task_document({"task_id": "t"})
    signature = _sign(auth, document, nonce="n1", timestamp=1000.0)
    expected = hmac.new(
        token.encode(), b"n1|1000.000000|" + document.encode(), sha256
    ).hexdigest()
    assert signature == expected


# -- Canonicalization ----------------------------------------------------------------


def test_canonical_form_is_deterministic_across_key_order() -> None:
    assert canonical_task_document({"a": 1, "b": 2}) == canonical_task_document({"b": 2, "a": 1})


def test_canonical_form_is_compact() -> None:
    assert " " not in canonical_task_document({"a": [1, 2]})
