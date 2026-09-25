"""Secret storage in the OS keyring (specification section 69).

The Brain's API key never lives in a config file, a CLI argument, a log line, a
tool envelope or an event. It lives in the Secret Service keyring and is read on
demand, exactly once per client construction.

Three rules shape this module:

* **The key value never round-trips through a message.** Failures carry the
  keyring service/key *names*, never the secret. A redaction helper is supplied
  because the one place a key can still leak is a traceback or an SDK error
  string, and callers that log such a string must pass it through
  :func:`redact_secret` first.
* **A missing keyring is an honest unavailability, not an exception at import
  time.** ``keyring`` is imported lazily, so BLAXCY still starts (and reports
  ``brain`` as UNAVAILABLE) on a machine with no Secret Service.
* **This is the only place ``keyring`` is called.** The capability probe
  (section 28) consults :class:`ApiKeyManager` rather than reaching for the
  library itself, so there is one code path that knows where the key lives.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from schemas.enums import ErrorCode
from schemas.errors import BlaxcyError

#: Keyring coordinates for the Gemini API key (section 69).
KEYRING_SERVICE: str = "blaxcy"
KEYRING_GEMINI_KEY: str = "gemini_api_key"


class KeyringError(BlaxcyError):
    """The OS keyring could not be reached (section 80: honest degradation)."""

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.BACKEND_UNAVAILABLE, message, details=details)


@runtime_checkable
class SecretStore(Protocol):
    """The narrow secret contract, so tests never need a real keyring."""

    def get(self, service: str, key: str) -> str | None:
        """Return the stored secret, or ``None`` when nothing is stored."""

    def set(self, service: str, key: str, value: str) -> None:
        """Store ``value`` under ``service``/``key``."""

    def delete(self, service: str, key: str) -> bool:
        """Remove the secret; returns whether one was actually removed."""


class KeyringStore:
    """The real Secret Service-backed store (a thin, honest ``keyring`` wrapper).

    ``keyring`` is imported lazily in :meth:`available` and in the three
    operations, because importing it at module load would make every importer of
    this module dependent on a working platform backend even when it never
    touches a secret.
    """

    def available(self) -> bool:
        """True when the ``keyring`` package is importable.

        Note this says nothing about whether a *backend* can actually serve a
        secret; only a real read can establish that, which is why
        :meth:`get` distinguishes "no secret stored" from "backend refused".
        """
        try:
            import keyring  # noqa: F401
        except Exception:
            return False
        return True

    def get(self, service: str, key: str) -> str | None:
        """Read a secret; ``None`` means "nothing stored", not "no keyring"."""
        keyring = self._module()
        try:
            value = keyring.get_password(service, key)
        except Exception as exc:
            raise KeyringError(
                "the OS keyring backend could not be read",
                details={"service": service, "key": key, "error": _safe_error(exc)},
            ) from exc
        return value if value else None

    def set(self, service: str, key: str, value: str) -> None:
        """Store a secret, refusing to persist an empty value."""
        if not value:
            raise BlaxcyError(
                ErrorCode.INTERNAL_ERROR,
                "refusing to store an empty secret",
                details={"service": service, "key": key},
            )
        keyring = self._module()
        try:
            keyring.set_password(service, key, value)
        except Exception as exc:
            raise KeyringError(
                "the OS keyring backend could not be written",
                details={"service": service, "key": key, "error": _safe_error(exc)},
            ) from exc

    def delete(self, service: str, key: str) -> bool:
        """Delete a secret, returning whether one was present."""
        keyring = self._module()
        try:
            existing = keyring.get_password(service, key)
            if not existing:
                return False
            keyring.delete_password(service, key)
        except Exception as exc:
            raise KeyringError(
                "the OS keyring backend could not be modified",
                details={"service": service, "key": key, "error": _safe_error(exc)},
            ) from exc
        return True

    @staticmethod
    def _module() -> Any:
        """Import ``keyring``, raising a structured error when it is missing."""
        try:
            import keyring
        except Exception as exc:  # pragma: no cover - depends on the host
            raise KeyringError(
                "the 'keyring' package is not importable",
                details={"error": _safe_error(exc)},
            ) from exc
        return keyring


class ApiKeyManager:
    """Reads and writes BLAXCY's Brain credentials (specification section 69).

    Args:
        store: The secret store. Defaults to the real keyring.
        service: Keyring service name; ``blaxcy`` unless overridden in tests.
    """

    def __init__(self, store: SecretStore | None = None, *, service: str = KEYRING_SERVICE) -> None:
        self._store = store if store is not None else KeyringStore()
        self._service = service

    @property
    def service(self) -> str:
        """The keyring service name (safe to log; never a secret)."""
        return self._service

    def gemini_key(self) -> str | None:
        """The stored Gemini API key, or ``None`` when none is configured.

        Raises:
            KeyringError: When a keyring backend exists but refused the
                read. A caller must not treat that the same as "no key": one is
                a configuration gap, the other is a broken environment.
        """
        value = self._guard("read", lambda: self._store.get(self._service, KEYRING_GEMINI_KEY))
        return None if value is None else str(value)

    def set_gemini_key(self, key: str) -> None:
        """Store the Gemini API key. The value is never recorded or returned.

        An empty value is refused here rather than only in the concrete store:
        the invariant belongs to the manager, so it holds for any store.
        """
        if not key:
            raise BlaxcyError(
                ErrorCode.INTERNAL_ERROR,
                "refusing to store an empty secret",
                details={"service": self._service, "key": KEYRING_GEMINI_KEY},
            )
        self._guard("write", lambda: self._store.set(self._service, KEYRING_GEMINI_KEY, key))

    def clear_gemini_key(self) -> bool:
        """Remove the stored Gemini API key; returns whether one was removed."""
        removed = self._guard(
            "delete", lambda: self._store.delete(self._service, KEYRING_GEMINI_KEY)
        )
        return bool(removed)

    def _guard(self, operation: str, action: Callable[[], Any]) -> Any:
        """Run a store operation, turning any backend failure into KeyringError.

        The manager is the honest boundary: whatever a store raises, the caller
        gets one structured "the keyring could not be used" error naming the
        operation -- and never a secret in the payload.
        """
        try:
            return action()
        except KeyringError:
            raise
        except Exception as exc:
            raise KeyringError(
                f"the OS keyring could not be used to {operation} the API key: {_safe_error(exc)}",
                details={
                    "service": self._service,
                    "key": KEYRING_GEMINI_KEY,
                    "operation": operation,
                    "error": _safe_error(exc),
                },
            ) from exc

    def status(self) -> dict[str, Any]:
        """A loggable status: presence and coordinates, never the secret."""
        try:
            present = bool(self.gemini_key())
        except KeyringError as exc:
            return {
                "keyring_service": self._service,
                "keyring_key": KEYRING_GEMINI_KEY,
                "key_present": False,
                "keyring_error": exc.message,
            }
        return {
            "keyring_service": self._service,
            "keyring_key": KEYRING_GEMINI_KEY,
            "key_present": present,
        }


def redact_secret(text: str, secret: str | None) -> str:
    """Replace every occurrence of ``secret`` in ``text`` with ``***``.

    Used before a third-party error string reaches a log: an SDK that echoes the
    API key in its exception message (or a URL query string) would otherwise put
    the credential into BLAXCY's logs (section 70).
    """
    if not secret:
        return text
    return text.replace(secret, "***")


def _safe_error(exc: BaseException) -> str:
    """A short, non-secret description of an exception type/message."""
    detail = str(exc).strip()
    if not detail:
        return type(exc).__name__
    return f"{type(exc).__name__}: {detail}"[:200]


__all__ = [
    "KEYRING_GEMINI_KEY",
    "KEYRING_SERVICE",
    "ApiKeyManager",
    "KeyringError",
    "KeyringStore",
    "SecretStore",
    "redact_secret",
]
