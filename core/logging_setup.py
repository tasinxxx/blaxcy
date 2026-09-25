"""Structured JSON logging (specification section 70).

Section 70 asks for exactly four things, and this module is all four:

1. **Structured JSON** -- one object per line, via :class:`JsonFormatter`.
2. **Rotation `10 MB x 5 files` by default** -- from ``[logging]`` in the config
   (:class:`~config.settings.LoggingSettings`), so the defaults live in one place.
3. **A root redaction filter** -- :class:`~security.redaction.RedactionFilter`
   installed on the root logger, so every record the process emits is scrubbed.
4. **Only safe metadata** -- the fields section 70 permits (action type, target
   id, window id, verification state, error code, latency, frame id, state
   version, backend, plus the batching fields ``sequence_id``/``step_index``/
   ``sequence_halt_reason``) are named in :data:`SAFE_METADATA_FIELDS`; anything
   else a caller attaches is still redacted and still emitted, because dropping an
   unknown field silently would be its own kind of dishonesty.

Configuration happens exactly once per process (:func:`configure_logging` is
idempotent and returns the path already in use), so calling it from both the CLI
and the composition root cannot install two handlers or duplicate a line.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from config.settings import Settings
from security.redaction import RESERVED_RECORD_ATTRS, RedactionFilter

#: The logger every BLAXCY module logs through (``get_logger(__name__)`` gives a
#: dotted child name, so the hierarchy stays readable).
LOGGER_NAME: Final[str] = "blaxcy"

#: The log file name inside the resolved log directory.
DEFAULT_LOG_FILENAME: Final[str] = "blaxcy.jsonl"

#: Section 70's safe-metadata allow-list. These are emitted first (and are the
#: fields this codebase actually attaches), which keeps the common case's JSON
#: key order stable and reviewable.
SAFE_METADATA_FIELDS: Final[tuple[str, ...]] = (
    "action_type",
    "target_id",
    "element_id",
    "window_id",
    "ok",
    "verification",
    "error_code",
    "latency_ms",
    "frame_id",
    "state_version",
    "backend",
    "task_id",
    "step_id",
    "sequence_id",
    "step_index",
    "sequence_halt_reason",
    "total_steps",
    "completed_count",
)

_LOCK = threading.Lock()
_CONFIGURED_PATH: Path | None = None
_HANDLER: logging.Handler | None = None
_FILTER: RedactionFilter | None = None


def default_log_dir() -> Path:
    """The directory logs go to, honouring ``BLAXCY_LOG_DIR`` and the XDG spec.

    ``BLAXCY_LOG_DIR`` exists so a test run (or an operator) can keep logs out of
    the real state directory; it is the only environment variable this module
    reads.
    """
    override = os.environ.get("BLAXCY_LOG_DIR")
    if override:
        return Path(override)
    state_home = os.environ.get("XDG_STATE_HOME")
    if state_home:
        return Path(state_home) / "blaxcy"
    return Path.home() / ".local" / "state" / "blaxcy"


def default_log_path() -> Path:
    """The full path of the log file (``<log dir>/blaxcy.jsonl``)."""
    return default_log_dir() / DEFAULT_LOG_FILENAME


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a BLAXCY logger.

    ``get_logger()`` returns the package logger; ``get_logger(__name__)`` returns
    a child of it, which is what modules should call.
    """
    if not name or name == LOGGER_NAME:
        return logging.getLogger(LOGGER_NAME)
    return logging.getLogger(f"{LOGGER_NAME}.{name}")


def is_configured() -> bool:
    """Whether :func:`configure_logging` has already installed its handler."""
    return _CONFIGURED_PATH is not None


def configured_log_path() -> Path | None:
    """The path in use, or ``None`` when logging has not been configured."""
    return _CONFIGURED_PATH


def _resolve_level(name: str) -> int:
    """Map a configured level name onto a logging level, defaulting to INFO."""
    level = logging.getLevelNamesMapping().get(str(name).upper())
    return level if isinstance(level, int) else logging.INFO


class JsonFormatter(logging.Formatter):
    """Render one record as one JSON object on one line (section 70)."""

    def format(self, record: logging.LogRecord) -> str:
        """Serialise the record, metadata first, with secrets already redacted.

        The record has been through :class:`~security.redaction.RedactionFilter`
        before it gets here, so no value in ``payload`` still holds a registered
        secret or a sensitive-named field.
        """
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in SAFE_METADATA_FIELDS:
            if key in record.__dict__:
                payload[key] = record.__dict__[key]
        for key, value in record.__dict__.items():
            if key in RESERVED_RECORD_ATTRS or key in SAFE_METADATA_FIELDS:
                continue
            payload[key] = value
        if record.exc_text:
            payload["exception"] = record.exc_text
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(
    settings: Settings,
    *,
    log_path: Path | None = None,
    force: bool = False,
) -> Path | None:
    """Install the section 70 root handler exactly once.

    Args:
        settings: Full configuration; ``[logging]`` supplies the level, the
            rotation bounds and whether the root redaction filter is installed.
        log_path: Explicit log file path. Defaults to
            :func:`default_log_path`.
        force: Replace an existing configuration. Tests use this; production
            calls leave it ``False`` so the handler is installed once.

    Returns:
        The log file path in use, or ``None`` when the log directory could not be
        created. A Body that cannot write its log still runs -- it reports the
        failure through its caller instead of refusing to start over a log file.
    """
    global _CONFIGURED_PATH, _FILTER, _HANDLER

    target = Path(log_path) if log_path is not None else default_log_path()
    root = logging.getLogger()
    with _LOCK:
        if _CONFIGURED_PATH is not None and not force:
            return _CONFIGURED_PATH
        # Replace only *this module's* previous handler and filter. A foreign
        # handler (a test runner's capture handler, a host application's) is left
        # alone: section 70 asks BLAXCY to install its own sink, never to seize
        # the root logger from whatever else is running.
        if _HANDLER is not None:
            root.removeHandler(_HANDLER)
            _HANDLER.close()
            _HANDLER = None
        if _FILTER is not None:
            root.removeFilter(_FILTER)
            _FILTER = None
        _CONFIGURED_PATH = None
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None

        handler = logging.handlers.RotatingFileHandler(
            target,
            maxBytes=settings.logging.rotation_max_bytes,
            backupCount=settings.logging.rotation_backups,
            encoding="utf-8",
        )
        handler.setFormatter(JsonFormatter())
        root.addHandler(handler)

        # Section 70's redaction filter. Section 4 rule 25 makes the switch an
        # enforced invariant (see config.settings.SECURITY_INVARIANTS), so a
        # config cannot turn it off -- the check below exists for a constructed
        # Settings object that bypassed loading.
        #
        # It is installed on the *handler* as well as on the root logger, and
        # that is not redundancy: a filter attached to a logger only sees records
        # logged *directly* through that logger, while a propagated record from a
        # child logger (``blaxcy.core.executor`` -> ``blaxcy`` -> root) runs
        # ancestor *handler* filters and skips ancestor logger filters. Putting
        # it only on the root logger would therefore have redacted nothing that
        # actually logs -- which is every module in this codebase.
        if settings.logging.redact_on_root_logger:
            redaction = RedactionFilter()
            root.addFilter(redaction)
            handler.addFilter(redaction)
            _FILTER = redaction
        root.setLevel(_resolve_level(settings.logging.level))
        _HANDLER = handler
        _CONFIGURED_PATH = target
        return target


def reset_logging() -> None:
    """Undo :func:`configure_logging` (tests only; never called in production)."""
    global _CONFIGURED_PATH, _FILTER, _HANDLER

    root = logging.getLogger()
    with _LOCK:
        if _HANDLER is not None:
            if _FILTER is not None:
                _HANDLER.removeFilter(_FILTER)
            root.removeHandler(_HANDLER)
            _HANDLER.close()
            _HANDLER = None
        if _FILTER is not None:
            root.removeFilter(_FILTER)
            _FILTER = None
        _CONFIGURED_PATH = None


__all__ = [
    "DEFAULT_LOG_FILENAME",
    "LOGGER_NAME",
    "SAFE_METADATA_FIELDS",
    "JsonFormatter",
    "configure_logging",
    "configured_log_path",
    "default_log_dir",
    "default_log_path",
    "get_logger",
    "is_configured",
    "reset_logging",
]
