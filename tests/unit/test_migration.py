"""Config schema migration behaviour (specification section 72)."""

from __future__ import annotations

import pytest

from config.migration import CURRENT_SCHEMA_VERSION, MigrationError, migrate


def test_missing_version_is_recorded_as_first_schema() -> None:
    """A missing schema_version is recorded explicitly, not left absent."""
    out = migrate({})
    assert out["schema_version"] == 1


def test_current_version_is_a_noop() -> None:
    """A current-version config passes through without mutation of the input."""
    raw = {"schema_version": CURRENT_SCHEMA_VERSION, "safety": {"mode": "OBSERVE"}}
    out = migrate(raw)
    assert out == raw
    assert raw == {"schema_version": CURRENT_SCHEMA_VERSION, "safety": {"mode": "OBSERVE"}}


def test_newer_version_is_refused() -> None:
    """A config newer than this build must never be silently coerced."""
    with pytest.raises(MigrationError):
        migrate({"schema_version": CURRENT_SCHEMA_VERSION + 1})


def test_non_integer_version_is_refused() -> None:
    """schema_version must be an integer."""
    with pytest.raises(MigrationError):
        migrate({"schema_version": "one"})
