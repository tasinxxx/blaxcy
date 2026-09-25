"""Redaction tests (specification sections 70, 85).

Section 85 makes "secrets never logged" an acceptance criterion, so these tests
assert the property directly: after a record passes the filter, the secret is
gone. The filter is also asserted to *always return True*, because a redaction
filter that silently dropped records would hide the very events an investigator
needs.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

from security.redaction import (
    PLACEHOLDER,
    RedactionFilter,
    clear_registered_secrets,
    redact_text,
    redact_value,
    register_secret,
    registered_secret_count,
    unregister_secret,
)


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    """Keep the process-wide secret registry from leaking between tests."""
    clear_registered_secrets()
    yield
    clear_registered_secrets()


def _record(message: str, **extra: object) -> logging.LogRecord:
    """Build a record the way a caller would, with structured fields attached."""
    record = logging.LogRecord(
        name="blaxcy.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_a_registered_secret_is_redacted_everywhere_it_appears() -> None:
    register_secret("AIzaSyTOP-SECRET")

    assert redact_text("before AIzaSyTOP-SECRET after") == f"before {PLACEHOLDER} after"
    assert redact_text("AIzaSyTOP-SECRET twice AIzaSyTOP-SECRET") == f"{PLACEHOLDER} twice {PLACEHOLDER}"
    assert redact_text("no secret here") == "no secret here"


def test_the_longest_registered_secret_is_replaced_first() -> None:
    """A secret containing another must not leave a fragment behind."""
    register_secret("secret")
    register_secret("secret-extension")

    assert redact_text("secret-extension") == PLACEHOLDER


def test_an_empty_or_missing_secret_is_never_registered() -> None:
    """Registering ``""`` would replace every character boundary in every line."""
    register_secret(None)
    register_secret("")

    assert registered_secret_count() == 0
    assert redact_text("nothing is redacted") == "nothing is redacted"


def test_unregister_and_clear_remove_secrets() -> None:
    register_secret("one")
    register_secret("two")
    assert registered_secret_count() == 2

    unregister_secret("one")
    assert registered_secret_count() == 1
    assert redact_text("one and two") == f"one and {PLACEHOLDER}"

    clear_registered_secrets()
    assert registered_secret_count() == 0


def test_a_sensitive_field_name_is_redacted_by_name_not_by_content() -> None:
    """A value that merely looks ordinary is still redacted when its name is not."""
    assert redact_value("password", "hunter2") == PLACEHOLDER
    assert redact_value("api_key", "anything") == PLACEHOLDER
    assert redact_value("Authorization", "Bearer x") == PLACEHOLDER
    assert redact_value("verification", "VERIFIED") == "VERIFIED"


def test_nested_values_are_scrubbed_recursively() -> None:
    register_secret("nested-secret")

    scrubbed = redact_value("details", {"inner": ["nested-secret", 3], "count": 2})

    assert scrubbed == {"inner": [PLACEHOLDER, 3], "count": 2}


def test_raw_bytes_are_dropped_rather_than_logged() -> None:
    """Section 42/70: log bytes may be protected visual content."""
    assert redact_value("frame", b"\x00\x01\x02") == PLACEHOLDER


def test_the_filter_redacts_the_message_and_always_allows_the_record() -> None:
    register_secret("SECRET-VALUE")
    record = _record("call failed with SECRET-VALUE")

    assert RedactionFilter().filter(record) is True
    assert "SECRET-VALUE" not in record.getMessage()
    assert PLACEHOLDER in record.getMessage()


def test_the_filter_redacts_tuple_and_mapping_arguments() -> None:
    register_secret("SECRET-VALUE")
    formatter = logging.Formatter("%(message)s")

    tuple_record = _record("key=%s suffix=%s")
    tuple_record.args = ("SECRET-VALUE", "kept")
    mapping_record = _record("key=%(key)s plain=%(plain)s")
    mapping_record.args = {"key": "SECRET-VALUE", "plain": "kept"}

    RedactionFilter().filter(tuple_record)
    RedactionFilter().filter(mapping_record)

    assert "SECRET-VALUE" not in formatter.format(tuple_record)
    assert "kept" in formatter.format(tuple_record)
    assert "SECRET-VALUE" not in formatter.format(mapping_record)
    assert "kept" in formatter.format(mapping_record)


def test_the_filter_redacts_a_sensitive_structured_field() -> None:
    record = _record("typed into a field", password="hunter2", verification="VERIFIED")

    RedactionFilter().filter(record)

    assert record.__dict__["password"] == PLACEHOLDER
    assert record.__dict__["verification"] == "VERIFIED"


def test_the_filter_redacts_a_formatted_traceback() -> None:
    """The one place a key still leaks is a third-party SDK error string."""
    register_secret("SECRET-VALUE")
    try:
        raise RuntimeError("the SDK echoed SECRET-VALUE")
    except RuntimeError:
        record = logging.LogRecord(
            name="blaxcy.test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="request failed",
            args=(),
            exc_info=__import__("sys").exc_info(),
        )

    RedactionFilter().filter(record)

    assert record.exc_text is not None
    assert "SECRET-VALUE" not in record.exc_text
    assert PLACEHOLDER in record.exc_text
