"""Watchdog heartbeat protocol (specification sections 64, 78).

The watchdog's whole job is to leave *evidence* behind about a run, because the
failure it exists for is the one where the process is no longer around to
explain itself. This module defines that evidence:

* a heartbeat record, written atomically to a small state file, carrying the
  process id, a session id, the heartbeat count, and -- explicitly, as section
  64 requires -- ``owned_keys_down`` and ``owned_buttons_down``;
* a reader that tells the next run whether the previous run ended *cleanly* or
  died holding input.

Two honest notes about what this can and cannot promise.

* A dead X client's pressed keys and buttons are released by the **X server**
  when its connection is closed, so a crash does not normally leave input stuck.
  BLAXCY does not get to *claim* that as a guarantee it implemented -- the record
  exists so the next run can see what was held and report it truthfully.
* Heartbeats use the wall clock for cross-process comparison (a file written by a
  dead process cannot be compared on a monotonic clock). Wall-clock time can
  jump, so freshness is only ever used for reporting, never for a safety
  decision.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

#: Version of the on-disk record layout. Bump with a migration.
SCHEMA_VERSION: int = 1

#: Default state-file location. Deliberately outside the project directory: the
#: watchdog state is runtime evidence, not repository content (section 22).
DEFAULT_WATCHDOG_PATH: Path = (
    Path.home() / ".local" / "state" / "blaxcy" / "watchdog.json"
)

#: How long a record may go unrefreshed before the run that wrote it is
#: considered dead. Section 64 asks for a *minimal* watchdog; this is the whole
#: of its liveness rule.
DEFAULT_STALE_AFTER_SECONDS: float = 5.0


class HeartbeatRecord(BaseModel):
    """One run's heartbeat and input ownership (sections 64, 78)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = SCHEMA_VERSION
    pid: int = Field(ge=1)
    session_id: str = Field(min_length=1)
    started_at: float = Field(description="Unix timestamp (seconds) the run began.")
    updated_at: float = Field(description="Unix timestamp (seconds) of the last heartbeat.")
    beats: int = Field(default=0, ge=0)
    owned_keys_down: tuple[str, ...] = ()
    owned_buttons_down: tuple[str, ...] = ()
    clean_shutdown: bool = Field(
        default=False,
        description="True only when the run reported a clean shutdown.",
    )

    def to_dict(self) -> dict[str, Any]:
        """JSON-shaped record."""
        return self.model_dump(mode="json")


@dataclass(frozen=True)
class PreviousRunReport:
    """What the previous run left behind (section 64 crash cleanup)."""

    path: Path
    exists: bool
    clean: bool
    age_seconds: float | None
    pid: int | None
    session_id: str | None
    owned_keys: tuple[str, ...]
    owned_buttons: tuple[str, ...]
    unreadable_reason: str | None = None

    @property
    def crashed(self) -> bool:
        """True when a record exists that was not marked clean."""
        return self.exists and not self.clean

    @property
    def held_input_at_exit(self) -> bool:
        """True when the previous run reported holding input when it last beat."""
        return bool(self.owned_keys or self.owned_buttons)

    def summary(self) -> str:
        """A one-line, human-readable description for logs and the report."""
        if not self.exists:
            return "no previous watchdog record"
        if self.unreadable_reason is not None:
            return f"previous watchdog record unreadable: {self.unreadable_reason}"
        if self.clean:
            return "previous run ended cleanly"
        detail = ""
        if self.held_input_at_exit:
            detail = (
                f"; last heartbeat reported holding keys={list(self.owned_keys)} "
                f"buttons={list(self.owned_buttons)}"
            )
        return f"previous run (pid {self.pid}) did not end cleanly{detail}"

    def to_dict(self) -> dict[str, Any]:
        """JSON-shaped report."""
        return {
            "path": str(self.path),
            "exists": self.exists,
            "clean": self.clean,
            "crashed": self.crashed,
            "age_seconds": self.age_seconds,
            "pid": self.pid,
            "session_id": self.session_id,
            "owned_keys": list(self.owned_keys),
            "owned_buttons": list(self.owned_buttons),
            "held_input_at_exit": self.held_input_at_exit,
            "summary": self.summary(),
        }


def write_record(path: Path, record: HeartbeatRecord) -> None:
    """Write ``record`` atomically.

    A half-written record would be read by the next run as a crash we cannot
    explain, so the file is written beside its destination and moved into place.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(record.to_dict(), sort_keys=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(payload, encoding="utf-8")
    os.replace(temporary, path)


def decode_record(text: str) -> HeartbeatRecord:
    """Parse a record, raising ``ValueError`` on anything malformed."""
    try:
        return HeartbeatRecord.model_validate(json.loads(text))
    except (json.JSONDecodeError, ValidationError, TypeError) as exc:
        raise ValueError(f"unreadable watchdog record: {exc}") from exc


def read_record(path: Path | None = None) -> HeartbeatRecord | None:
    """Read the watchdog record, or ``None`` when there is not a valid one."""
    target = path if path is not None else DEFAULT_WATCHDOG_PATH
    try:
        text = target.read_text(encoding="utf-8")
    except (FileNotFoundError, IsADirectoryError, OSError):
        return None
    try:
        return decode_record(text)
    except ValueError:
        return None


def inspect_previous_run(
    path: Path | None = None,
    *,
    now: float | None = None,
) -> PreviousRunReport:
    """Report what the previous run left in the watchdog state file.

    A missing file is not an error and is not a crash: it means either a first
    run or a run that cleaned up after itself by removing its record.
    """
    target = path if path is not None else DEFAULT_WATCHDOG_PATH
    current = time.time() if now is None else now
    if not target.exists():
        return PreviousRunReport(target, False, True, None, None, None, (), ())

    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        return PreviousRunReport(target, True, False, None, None, None, (), (), unreadable_reason=str(exc))

    try:
        record = decode_record(text)
    except ValueError as exc:
        return PreviousRunReport(target, True, False, None, None, None, (), (), unreadable_reason=str(exc))

    return PreviousRunReport(
        path=target,
        exists=True,
        clean=record.clean_shutdown,
        age_seconds=max(0.0, current - record.updated_at),
        pid=record.pid,
        session_id=record.session_id,
        owned_keys=record.owned_keys_down,
        owned_buttons=record.owned_buttons_down,
    )


__all__ = [
    "DEFAULT_STALE_AFTER_SECONDS",
    "DEFAULT_WATCHDOG_PATH",
    "SCHEMA_VERSION",
    "HeartbeatRecord",
    "PreviousRunReport",
    "decode_record",
    "inspect_previous_run",
    "read_record",
    "write_record",
]
