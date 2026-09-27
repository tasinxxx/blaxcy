"""Repository-side task/result storage conventions (Phase 2B, requirement 3).

The relay repository is a *directory tree* in this implementation: the same
layout a GitHub repository presents when cloned, so the self-hosted runner and
the tests use identical code paths. Nothing here talks to the GitHub API — the
syncing of these directories is `git`'s job (pull before, commit+push after),
and the workflow in ``.github/workflows/process-task.yml`` orchestrates exactly that.

Layout (documented in ``docs/relay.md``):

```
relay/
  tasks/<task_id>/task.json        the raw envelope (written once by the producer)
  tasks/<task_id>/state.json       the lifecycle record (written by the runner)
  tasks/<task_id>/result.json      the TaskEnvelope result (written once, atomically)
  claims/<task_id>.claim           an exclusive claim marker (O_EXCL = the lock)
  results/<task_id>.json           a flattened copy of the result for producers
```

Two properties make this safe as a deduplication mechanism (requirement 9):

* **Claiming is exclusive by construction.** A claim is
  ``os.open(..., O_CREAT|O_EXCL)`` on a fresh filesystem: exactly one worker
  can create the file, every other worker gets ``FileExistsError`` and must
  treat the task as already taken. On a *dirty* checkout a stale claim can be
  re-armed only through :meth:`TaskStore.reset_claims`, which is an explicit
  operator action, never automatic.
* **Identity is content-bound.** ``task_id`` is the dedup key everywhere, and
  the envelope's task-id/inner-task equality check plus the state machine make
  "the same task committed twice under two ids" harmless: each would be
  claimed and executed on its own merits, but a *re-submission of an executed
  task_id* is refused at the claim (``DUPLICATE``) — the state record proves
  the id was already taken past PENDING.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from relay.lifecycle import (
    PENDING,
    RUNNING,
    TERMINAL_STATES,
    LifecycleError,
    TaskLifecycle,
    require_transition,
)


class StoreError(Exception):
    """A storage-level refusal (duplicate claim, illegal transition, bad record)."""

    def __init__(self, reason: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.details: dict[str, Any] = details or {}


def _valid_task_id(task_id: str) -> bool:
    """The same task-id grammar the bridge and envelope enforce."""
    import re

    return bool(re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", task_id))


class TaskStore:
    """Filesystem-backed relay record store.

    Args:
        root: The repository root directory. Created (with the layout) on
            first use; must exist for a read.
        clock: Injectable time source for recorded timestamps.
    """

    def __init__(self, root: Path, *, clock: Any = None) -> None:
        import time

        self._root = Path(root)
        self._clock = clock if clock is not None else time.time

    # -- Layout ----------------------------------------------------------------

    @property
    def root(self) -> Path:
        return self._root

    @property
    def tasks_dir(self) -> Path:
        return self._root / "relay" / "tasks"

    @property
    def claims_dir(self) -> Path:
        return self._root / "relay" / "claims"

    @property
    def results_dir(self) -> Path:
        return self._root / "relay" / "results"

    def ensure_layout(self) -> None:
        """Create the repository layout if absent (idempotent)."""
        for directory in (self.tasks_dir, self.claims_dir, self.results_dir):
            directory.mkdir(parents=True, exist_ok=True)

    def task_dir(self, task_id: str) -> Path:
        if not _valid_task_id(task_id):
            raise StoreError("INVALID_TASK_ID", {"task_id": task_id})
        return self.tasks_dir / task_id

    # -- Producer side -----------------------------------------------------------

    def write_task(self, task_id: str, envelope_document: str) -> Path:
        """Record a new task envelope (requirement 3).

        Fails closed when the task id already exists **in any non-PENDING
        state**: re-submitting an executed task is the replay this store exists
        to prevent. A ``PENDING`` record may be overwritten only by an
        identical document (a producer retry of its own *unclaimed* write), and
        any other change is a conflict.
        """
        directory = self.task_dir(task_id)
        state_path = directory / "state.json"
        if state_path.exists():
            record = self._read_json(state_path)
            current = TaskLifecycle(str(record.get("state", "")))
            if current is not TaskLifecycle.PENDING:
                raise StoreError(
                    "DUPLICATE_TASK",
                    {"task_id": task_id, "state": current.value},
                )
            existing = (directory / "task.json").read_text(encoding="utf-8")
            if existing != envelope_document:
                raise StoreError(
                    "TASK_CONFLICT",
                    {"task_id": task_id, "reason": "a PENDING task with this id carries a different document"},
                )
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "task.json").write_text(envelope_document, encoding="utf-8")
        if not state_path.exists():
            self._write_json(
                state_path,
                {"task_id": task_id, "state": TaskLifecycle.PENDING.value, "history": []},
            )
        return directory / "task.json"

    # -- Worker side ---------------------------------------------------------------

    def claim(self, task_id: str) -> Path:
        """Claim a task exclusively: PENDING → RUNNING (requirement 9).

        The exclusive ``O_EXCL`` marker is the lock. ``FileExistsError`` means
        another worker holds the claim (or a stale claim was left by a crashed
        run — re-arm it explicitly with :meth:`reset_claims`, never silently).

        Returns:
            The claim file path.

        Raises:
            StoreError: ``DUPLICATE_TASK`` when the claim or a terminal state
                already exists; ``TASK_NOT_PENDING`` when the recorded state is
                not PENDING; ``LifecycleError`` reasons surface as
                ``ILLEGAL_TRANSITION``.
        """
        # Fail closed on an invalid task id *before* any claim marker exists:
        # the claim file name is derived from the id, and a malformed id must
        # surface as ``INVALID_TASK_ID``, never as a stray file or raw OSError.
        self.task_dir(task_id)
        claim = self.claims_dir / f"{task_id}.claim"
        try:
            handle = os.open(str(claim), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise StoreError(
                "DUPLICATE_TASK",
                {"task_id": task_id, "reason": "a claim already exists for this task"},
            ) from exc
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(str(self._clock()))
        try:
            self.transition(task_id, RUNNING, note="claimed")
        except StoreError:
            # Roll the claim back so the task is not left claimed-but-pending.
            claim.unlink(missing_ok=True)
            raise
        return claim

    def transition(
        self,
        task_id: str,
        attempted: TaskLifecycle,
        *,
        note: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Move a task through one validated lifecycle transition.

        The state machine (:mod:`relay.lifecycle`) is consulted *before* the
        record changes; an illegal transition leaves the record untouched and
        raises ``ILLEGAL_TRANSITION``. Every accepted transition is appended to
        the record's history with its timestamp and note (requirement 10).
        """
        directory = self.task_dir(task_id)
        state_path = directory / "state.json"
        if not state_path.exists():
            raise StoreError("TASK_UNKNOWN", {"task_id": task_id})
        record = self._read_json(state_path)
        current = TaskLifecycle(str(record.get("state", "")))
        try:
            require_transition(current, attempted, task_id=task_id)
        except LifecycleError as exc:
            # The store's error surface is uniform for callers (the executor
            # maps every refusal onto a terminal record); the lifecycle
            # evidence travels in the details.
            raise StoreError(
                "ILLEGAL_TRANSITION",
                {
                    "task_id": task_id,
                    "current": current.value,
                    "attempted": attempted.value,
                    "lifecycle_error": str(exc),
                },
            ) from exc
        record["state"] = attempted.value
        history_entry: dict[str, Any] = {
            "from": current.value,
            "to": attempted.value,
            "at": self._clock(),
        }
        if note:
            history_entry["note"] = note
        if detail:
            history_entry["detail"] = detail
        record["history"].append(history_entry)
        self._write_json(state_path, record)
        return record

    def state_of(self, task_id: str) -> TaskLifecycle:
        """The recorded lifecycle state, or ``PENDING`` when never recorded."""
        state_path = self.task_dir(task_id) / "state.json"
        if not state_path.exists():
            return PENDING
        record = self._read_json(state_path)
        return TaskLifecycle(str(record.get("state", PENDING.value)))

    def is_terminal(self, task_id: str) -> bool:
        return self.state_of(task_id) in TERMINAL_STATES

    def write_result(self, task_id: str, result_document: dict[str, Any]) -> tuple[Path, Path]:
        """Persist the TaskEnvelope result (once, atomically, requirement 3/10).

        Writes ``tasks/<id>/result.json`` and the flattened
        ``results/<id>.json``, both via temp-file + rename so a reader never
        observes a partial result. A second write is refused: a result is a
        terminal fact, never an overwriting surface.
        """
        directory = self.task_dir(task_id)
        result_path = directory / "result.json"
        if result_path.exists():
            raise StoreError("RESULT_EXISTS", {"task_id": task_id})
        directory.mkdir(parents=True, exist_ok=True)
        self._atomic_write(result_path, result_document)
        flat_path = self.results_dir / f"{task_id}.json"
        flat_path.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write(flat_path, result_document)
        return result_path, flat_path

    def read_result(self, task_id: str) -> dict[str, Any] | None:
        path = self.task_dir(task_id) / "result.json"
        if not path.exists():
            return None
        return self._read_json(path)

    def reset_claims(self, task_id: str) -> bool:
        """Remove a stale claim. Explicit, operator-driven, never automatic."""
        claim = self.claims_dir / f"{task_id}.claim"
        if claim.exists():
            claim.unlink()
            return True
        return False

    # -- Internals -------------------------------------------------------------

    def _read_json(self, path: Path) -> dict[str, Any]:
        try:
            return json.loads(path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]
        except (OSError, json.JSONDecodeError) as exc:
            raise StoreError("RECORD_UNREADABLE", {"path": str(path), "error": str(exc)}) from exc

    def _write_json(self, path: Path, document: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document, indent=2), encoding="utf-8")

    def _atomic_write(self, path: Path, document: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(document, indent=2), encoding="utf-8")
        os.replace(str(temp), str(path))


__all__ = ["StoreError", "TaskStore"]
