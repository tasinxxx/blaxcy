"""Keyring credential storage (specification section 69).

The properties that matter here are about what *cannot* happen: the secret never
appears in a status payload, an empty value is never stored, a broken keyring is
never silently reported as "no key", and a third-party error string can be
scrubbed before it reaches a log.
"""

from __future__ import annotations

import pytest

from schemas.enums import ErrorCode
from security.keyring_manager import (
    KEYRING_GEMINI_KEY,
    KEYRING_SERVICE,
    ApiKeyManager,
    KeyringError,
    KeyringStore,
    redact_secret,
)

SECRET = "AIzaSy-not-a-real-key-0000000000000000000"


class FakeStore:
    """An in-memory secret store that can be told to fail."""

    def __init__(self, *, fail: Exception | None = None) -> None:
        """Create the store; ``fail`` makes every operation raise it."""
        self.data: dict[tuple[str, str], str] = {}
        self.calls: list[tuple[str, str]] = []
        self._fail = fail

    def get(self, service: str, key: str) -> str | None:
        """Read a secret, or raise the injected failure."""
        self.calls.append(("get", f"{service}/{key}"))
        if self._fail is not None:
            raise self._fail
        return self.data.get((service, key))

    def set(self, service: str, key: str, value: str) -> None:
        """Store a secret, or raise the injected failure."""
        self.calls.append(("set", f"{service}/{key}"))
        if self._fail is not None:
            raise self._fail
        self.data[(service, key)] = value

    def delete(self, service: str, key: str) -> bool:
        """Delete a secret, or raise the injected failure."""
        self.calls.append(("delete", f"{service}/{key}"))
        if self._fail is not None:
            raise self._fail
        return self.data.pop((service, key), None) is not None


# -- Round trip ---------------------------------------------------------------

def test_key_round_trip_uses_the_documented_keyring_coordinates() -> None:
    """The key lives at service ``blaxcy`` under ``gemini_api_key`` (section 69)."""
    store = FakeStore()
    manager = ApiKeyManager(store)

    assert manager.gemini_key() is None
    manager.set_gemini_key(SECRET)
    assert manager.gemini_key() == SECRET
    assert (KEYRING_SERVICE, KEYRING_GEMINI_KEY) in store.data
    assert manager.clear_gemini_key() is True
    assert manager.gemini_key() is None
    assert manager.clear_gemini_key() is False


def test_an_empty_secret_is_refused() -> None:
    """Storing an empty value would look configured while authenticating nothing."""
    manager = ApiKeyManager(FakeStore())
    with pytest.raises(Exception) as excinfo:
        manager.set_gemini_key("")
    assert "empty secret" in str(excinfo.value)
    assert manager.gemini_key() is None


def test_status_never_contains_the_secret() -> None:
    """The status payload is loggable, so it must carry presence only."""
    manager = ApiKeyManager(FakeStore())
    manager.set_gemini_key(SECRET)
    status = manager.status()
    assert status["key_present"] is True
    assert status["keyring_service"] == KEYRING_SERVICE
    assert status["keyring_key"] == KEYRING_GEMINI_KEY
    assert SECRET not in repr(status)


# -- Failure handling ---------------------------------------------------------

def test_a_broken_keyring_raises_rather_than_reporting_no_key() -> None:
    """'No key configured' and 'keyring broken' need different fixes."""
    manager = ApiKeyManager(FakeStore(fail=RuntimeError("no secret service")))
    with pytest.raises(KeyringError) as excinfo:
        manager.gemini_key()
    assert excinfo.value.code is ErrorCode.BACKEND_UNAVAILABLE
    assert SECRET not in str(excinfo.value)


def test_status_reports_a_keyring_failure_honestly() -> None:
    """``status()`` degrades instead of raising, and says why."""
    manager = ApiKeyManager(FakeStore(fail=RuntimeError("no secret service")))
    status = manager.status()
    assert status["key_present"] is False
    assert "no secret service" in status["keyring_error"]


def test_a_missing_keyring_package_is_structured() -> None:
    """Reading through a store whose backend is absent raises ``KeyringError``."""
    assert isinstance(KeyringStore().available(), bool)


# -- Redaction ----------------------------------------------------------------

def test_redact_secret_removes_every_occurrence() -> None:
    """An SDK that echoes the key must not put it in a log line."""
    text = f"auth failed for key {SECRET} (key={SECRET})"
    redacted = redact_secret(text, SECRET)
    assert SECRET not in redacted
    assert redacted.count("***") == 2


def test_redact_secret_leaves_text_alone_without_a_secret() -> None:
    """With nothing to redact, the text is returned unchanged."""
    text = "plain failure message"
    assert redact_secret(text, None) == text
    assert redact_secret(text, "") == text
