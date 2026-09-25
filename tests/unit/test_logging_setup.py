"""Structured logging tests (specification sections 70, 85, and 25).

Two properties matter more than the formatting itself: the log is *structured*
(one JSON object per line, carrying section 70's safe metadata) and it is *safe*
(a registered secret and a sensitive-named field never reach the file). The
rotation bounds are asserted against the configuration rather than a literal, so
the defaults stay in one place.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
from pathlib import Path
from typing import Any

import pytest

from config.settings import load_settings
from core.logging_setup import (
    SAFE_METADATA_FIELDS,
    configure_logging,
    configured_log_path,
    default_log_path,
    get_logger,
    is_configured,
    reset_logging,
)
from schemas.errors import BlaxcyError
from security.redaction import RedactionFilter, clear_registered_secrets, register_secret


@pytest.fixture(autouse=True)
def _isolated_logging() -> Any:
    """Never let one test's root handler follow it into the next test."""
    reset_logging()
    clear_registered_secrets()
    yield
    reset_logging()
    clear_registered_secrets()


def _configure(tmp_path: Path) -> Path:
    """Configure logging into a throwaway file and return that path."""
    settings = load_settings(tmp_path / "config.toml")
    log_path = tmp_path / "blaxcy.jsonl"
    assert configure_logging(settings, log_path=log_path, force=True) == log_path
    return log_path


def _lines(path: Path) -> list[dict[str, Any]]:
    """Parse the log file as one JSON object per non-empty line."""
    text = path.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def test_one_json_object_per_record_carries_the_safe_metadata(tmp_path: Path) -> None:
    log_path = _configure(tmp_path)

    get_logger("test").info(
        "tool call",
        extra={
            "action_type": "click",
            "ok": True,
            "verification": "VERIFIED",
            "error_code": None,
            "latency_ms": 12.5,
            "state_version": 7,
            "frame_id": 42,
            "backend": "xtest",
            "sequence_id": "seq-1",
            "step_index": 2,
        },
    )

    (entry,) = _lines(log_path)
    assert entry["level"] == "INFO"
    assert entry["logger"] == "blaxcy.test"
    assert entry["message"] == "tool call"
    assert entry["action_type"] == "click"
    assert entry["verification"] == "VERIFIED"
    assert entry["latency_ms"] == 12.5
    assert entry["state_version"] == 7
    assert entry["frame_id"] == 42
    assert entry["backend"] == "xtest"
    assert entry["sequence_id"] == "seq-1"
    assert entry["step_index"] == 2
    # The record is one line, so a log reader can stream it.
    assert len(log_path.read_text(encoding="utf-8").splitlines()) == 1


def test_every_permitted_metadata_field_is_named_in_the_allow_list() -> None:
    """The allow-list is the contract; these are the fields this Body attaches."""
    for name in ("action_type", "error_code", "verification", "sequence_halt_reason"):
        assert name in SAFE_METADATA_FIELDS


def test_rotation_bounds_come_from_the_configuration(tmp_path: Path) -> None:
    settings = load_settings(tmp_path / "config.toml")

    configure_logging(settings, log_path=tmp_path / "blaxcy.jsonl", force=True)

    sink = [h for h in logging.getLogger().handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
    (handler,) = sink
    assert handler.maxBytes == settings.logging.rotation_max_bytes
    assert handler.backupCount == settings.logging.rotation_backups
    # Section 70's documented defaults.
    assert settings.logging.rotation_max_bytes == 10 * 1024 * 1024
    assert settings.logging.rotation_backups == 5


def test_the_root_redaction_filter_is_installed(tmp_path: Path) -> None:
    _configure(tmp_path)

    assert any(isinstance(f, RedactionFilter) for f in logging.getLogger().filters)


def test_a_registered_secret_never_reaches_the_log_file(tmp_path: Path) -> None:
    """Section 85's "secrets never logged" property, asserted end to end."""
    log_path = _configure(tmp_path)
    register_secret("AIzaSyTOP-SECRET-KEY")

    get_logger("test").info(
        "calling the model with AIzaSyTOP-SECRET-KEY",
        extra={
            "api_key": "AIzaSyTOP-SECRET-KEY",
            "password": "hunter2",
            "details": {"nested": "AIzaSyTOP-SECRET-KEY"},
        },
    )

    text = log_path.read_text(encoding="utf-8")
    assert "TOP-SECRET" not in text, "the registered secret reached the log file"
    assert "hunter2" not in text, "a password-named field reached the log file"
    (entry,) = _lines(log_path)
    assert entry["api_key"] == "***"
    assert entry["password"] == "***"
    assert entry["details"] == {"nested": "***"}


def test_an_unexpected_field_is_still_emitted_and_still_redacted(tmp_path: Path) -> None:
    """Dropping an unknown field silently would hide evidence (section 12.1)."""
    log_path = _configure(tmp_path)
    register_secret("SECRET-VALUE")

    get_logger("test").info("x", extra={"something_new": "SECRET-VALUE"})

    (entry,) = _lines(log_path)
    assert entry["something_new"] == "***"


def test_configuring_twice_installs_one_handler_and_keeps_the_path(tmp_path: Path) -> None:
    settings = load_settings(tmp_path / "config.toml")
    first = configure_logging(settings, log_path=tmp_path / "a.jsonl", force=True)

    second = configure_logging(settings, log_path=tmp_path / "b.jsonl")

    assert first == tmp_path / "a.jsonl"
    assert second == first, "a second call must not re-point the configured handler"
    assert configured_log_path() == first
    assert is_configured() is True
    sinks = [h for h in logging.getLogger().handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
    assert len(sinks) == 1, "a second call must not install a second file sink"


def test_default_log_path_honours_the_environment_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BLAXCY_LOG_DIR", str(tmp_path / "logs"))

    assert default_log_path() == tmp_path / "logs" / "blaxcy.jsonl"


def test_the_exception_text_is_redacted_in_the_structured_output(tmp_path: Path) -> None:
    log_path = _configure(tmp_path)
    register_secret("SECRET-VALUE")

    try:
        raise RuntimeError("the SDK echoed SECRET-VALUE")
    except RuntimeError:
        get_logger("test").exception("request failed")

    text = log_path.read_text(encoding="utf-8")
    assert "SECRET-VALUE" not in text
    (entry,) = _lines(log_path)
    assert "exception" in entry


def test_a_config_cannot_disable_log_redaction(tmp_path: Path) -> None:
    """Section 4 rule 25: a security invariant is not configurable into unsafe."""
    config = tmp_path / "config.toml"
    config.write_text("[logging]\nredact_on_root_logger = false\n", encoding="utf-8")

    with pytest.raises(BlaxcyError) as excinfo:
        load_settings(config)

    assert "security invariant" in excinfo.value.message.lower()
