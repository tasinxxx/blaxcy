"""Redaction of secrets before a record reaches any log (specification section 70).

Section 70 requires BLAXCY's logging to carry a **root redaction filter**, and
section 85 makes "secrets never logged" an acceptance criterion. This module is
that filter, and it is deliberately the only thing that decides what counts as a
secret:

* **Registered values.** :func:`register_secret` records a literal secret (the
  Brain API key, a password the Body was asked to type). Every registered value
  is replaced by ``***`` wherever it appears -- in the message, in the arguments,
  or in a structured field, including an exception's formatted traceback.
* **Sensitive field names.** A structured field *named* ``password``, ``token``,
  ``api_key``, ... has its value replaced regardless of content. A name is
  evidence in a way that a value's shape is not.
* **No heuristic value matching, on purpose.** Redacting anything that merely
  "looks like" a key would be a guess, and a guess can be wrong in both
  directions -- it can miss a real secret and it can corrupt ordinary text.
  Section 4 rule 8 is why this module has no pattern list: a caller that knows a
  value is secret registers it.

The filter mutates the ``LogRecord`` in place, so it composes with any formatter,
including the structured JSON formatter in :mod:`core.logging_setup`.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping, Sequence
from typing import Any, Final

#: What replaces a redacted value.
PLACEHOLDER: Final[str] = "***"

#: Structured field names whose value is secret *by name* rather than by content.
SENSITIVE_KEY_NAMES: Final[frozenset[str]] = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "auth",
        "authorization",
        "credential",
        "credentials",
        "gemini_api_key",
        "key",
        "passwd",
        "password",
        "private_key",
        "refresh_token",
        "secret",
        "token",
    }
)

#: The attributes ``logging`` puts on every record itself, never structured data
#: a caller attached. Anything outside this set is treated as caller metadata and
#: is therefore redacted.
RESERVED_RECORD_ATTRS: Final[frozenset[str]] = frozenset(
    logging.LogRecord("", logging.NOTSET, "", 0, "", (), None).__dict__
) | frozenset({"asctime", "message"})

_REGISTERED: set[str] = set()
_REGISTER_LOCK = threading.Lock()


def register_secret(secret: str | None) -> None:
    """Record ``secret`` so every later log record redacts it. Idempotent.

    An empty or ``None`` value is ignored rather than registered: redacting the
    empty string would replace every character boundary in every message, which
    would destroy the log without protecting anything.
    """
    if not secret:
        return
    with _REGISTER_LOCK:
        _REGISTERED.add(secret)


def unregister_secret(secret: str | None) -> None:
    """Forget a previously registered secret (used when a key is cleared)."""
    if not secret:
        return
    with _REGISTER_LOCK:
        _REGISTERED.discard(secret)


def clear_registered_secrets() -> None:
    """Forget every registered secret. Intended for tests and shutdown."""
    with _REGISTER_LOCK:
        _REGISTERED.clear()


def registered_secret_count() -> int:
    """How many distinct secrets are currently registered."""
    with _REGISTER_LOCK:
        return len(_REGISTERED)


def redact_text(text: str) -> str:
    """Replace every registered secret in ``text`` with :data:`PLACEHOLDER`.

    Longer secrets are replaced first so that a secret which contains another
    cannot leave a fragment of the shorter one behind.
    """
    with _REGISTER_LOCK:
        secrets = tuple(sorted(_REGISTERED, key=len, reverse=True))
    for secret in secrets:
        text = text.replace(secret, PLACEHOLDER)
    return text


def redact_value(key: str | None, value: Any) -> Any:
    """Redact one structured field, by name first and then by content.

    A field whose name is sensitive is replaced wholesale; every other value is
    scrubbed for registered secrets and recursed into, so a secret nested inside a
    mapping or a list is still redacted.
    """
    if key is not None and key.lower() in SENSITIVE_KEY_NAMES:
        return PLACEHOLDER
    return _scrub(value)


def _scrub(value: Any) -> Any:
    """Recursively redact registered secrets from an arbitrary log value."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, (bytes, bytearray)):
        # Section 42/70: raw bytes may be protected visual content. Logging them
        # at all would be the defect, so the value is dropped rather than
        # redacted in place.
        return PLACEHOLDER
    if isinstance(value, Mapping):
        return {str(k): redact_value(str(k), v) for k, v in value.items()}
    if isinstance(value, Sequence):
        return [_scrub(item) for item in value]
    return value


class RedactionFilter(logging.Filter):
    """Scrub secrets from a ``LogRecord`` before any formatter renders it.

    Section 70 installs this on the root logger, so it sees every record the
    process emits -- including third-party ones whose message may echo a
    credential back from an SDK error string.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Redact the record in place and always allow it through."""
        record.msg = redact_text(str(record.msg))
        record.args = self._scrub_args(record.args)
        if record.exc_info is not None and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact_text(record.exc_text)
        if record.stack_info:
            record.stack_info = redact_text(record.stack_info)
        for key, value in list(record.__dict__.items()):
            if key in RESERVED_RECORD_ATTRS:
                continue
            record.__dict__[key] = redact_value(key, value)
        return True

    @staticmethod
    def _scrub_args(args: Any) -> Any:
        """Redact a record's ``%``-formatting arguments, preserving their shape."""
        if args is None:
            return args
        if isinstance(args, Mapping):
            return {str(k): redact_value(str(k), v) for k, v in args.items()}
        if isinstance(args, tuple):
            return tuple(_scrub(item) for item in args)
        return _scrub(args)


__all__ = [
    "PLACEHOLDER",
    "RESERVED_RECORD_ATTRS",
    "SENSITIVE_KEY_NAMES",
    "RedactionFilter",
    "clear_registered_secrets",
    "redact_text",
    "redact_value",
    "register_secret",
    "registered_secret_count",
    "unregister_secret",
]
