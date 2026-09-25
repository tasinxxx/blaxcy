"""Single-instance control (specification section 72).

Two Bodies on one desktop is a hazard, not an inconvenience. Each would hold its
own record of the keys and buttons *it* owns, each would inject input believing it
had the whole state, and neither could release the other's held input. So a second
launch has to be **refused deliberately** -- and section 85 asks for exactly that
when it requires that a second launch is "controlled".

The mechanism is an advisory ``flock`` on a lock file in the runtime directory, not
a "check the pid file" heuristic, for one specific reason: the kernel drops an
``flock`` when the process dies, so a crashed Body can never leave a lock behind
that blocks the next launch. There is no stale-lock special case to get wrong, and
no timeout after which a supposedly-running instance silently becomes not-running.

What the file *does* carry is the holder's identity (pid, start time, command), so
the refusal can name who holds the lock instead of just failing -- a refusal a
human cannot act on is barely better than a crash.

Scope: the lock guards the *application launch boundary*, which is why
``main.py``'s ``gui`` and ``run`` commands take it and its read-only
``probe``/``session``/``config``/``status`` commands deliberately do not. Those
inject nothing, so running one beside a live Body is safe and useful. It is not
taken by ``BlaxcyApplication`` itself, because constructing a Body (which tests do
constantly, and which the installer's own validation does) is not the same act as
taking over a desktop.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import os
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

#: The lock file's name inside the runtime directory.
DEFAULT_LOCK_FILENAME: Final[str] = "instance.lock"

#: Directory name used for the lock inside ``XDG_RUNTIME_DIR`` / the temp dir.
RUNTIME_DIR_NAME: Final[str] = "blaxcy"


def default_lock_path() -> Path:
    """Where the lock lives when no path is given.

    ``XDG_RUNTIME_DIR`` is the right home: it is per-user, mode 0700, and cleared
    by the system on logout, which is exactly the lifetime a desktop lock wants.
    The temp directory is the documented fallback for a session that does not set
    it, which is why the file name there carries the uid.
    """
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return Path(runtime) / RUNTIME_DIR_NAME / DEFAULT_LOCK_FILENAME
    try:
        uid = os.getuid()
    except AttributeError:  # pragma: no cover - Linux always has getuid
        return Path(tempfile.gettempdir()) / RUNTIME_DIR_NAME / DEFAULT_LOCK_FILENAME
    return Path(tempfile.gettempdir()) / f"{RUNTIME_DIR_NAME}-{uid}" / DEFAULT_LOCK_FILENAME


@dataclass(frozen=True)
class LockHolder:
    """Who currently holds the lock, as far as the file records it."""

    pid: int
    started_at: float | None = None
    command: str | None = None

    def describe(self) -> str:
        """A sentence a human can act on."""
        parts = [f"pid {self.pid}"]
        if self.command:
            parts.append(self.command)
        if self.started_at is not None:
            age = max(0.0, time.time() - self.started_at)
            parts.append(f"started {age:.0f}s ago")
        return ", ".join(parts)


class InstanceLock:
    """An exclusive, self-releasing lock on one BLAXCY desktop session."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        pid: int | None = None,
        command: str | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = Path(path) if path is not None else default_lock_path()
        self._pid = os.getpid() if pid is None else int(pid)
        self._command = command
        self._clock = clock
        self._fd: int | None = None
        self._holder: LockHolder | None = None
        self._last_error: str | None = None

    # -- State -----------------------------------------------------------------

    @property
    def acquired(self) -> bool:
        """True while this object holds the lock."""
        return self._fd is not None

    @property
    def holder(self) -> LockHolder | None:
        """The other instance that refused us, or ``None`` when we hold it."""
        return self._holder

    # -- Acquire / release -----------------------------------------------------

    def acquire(self) -> bool:
        """Try to become the single instance.

        Returns:
            ``True`` when this object now holds the lock, ``False`` when another
            live instance does (in which case :attr:`holder` describes it).
        """
        if self._fd is not None:
            return True
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        except OSError as exc:
            # A lock we cannot even create is treated as "not acquired": failing
            # closed here means refusing to start a second Body, never silently
            # running without the guarantee. The parent directory can be the
            # unopenable part (a file where the directory needs to be, a runtime
            # directory we may not write), so the ``mkdir`` is inside the guard.
            self._holder = None
            self._last_error = f"cannot open the lock file {self.path}: {exc}"
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                self._last_error = f"flock failed on {self.path}: {exc}"
            self._holder = self._read_holder(fd)
            os.close(fd)
            return False
        # We hold it: record who we are so the next refusal can name us.
        os.ftruncate(fd, 0)
        payload = {"pid": self._pid, "started_at": self._clock(), "command": self._command}
        os.write(fd, json.dumps(payload).encode("utf-8"))
        os.fsync(fd)
        self._fd = fd
        self._holder = None
        return True

    def release(self) -> None:
        """Drop the lock. Idempotent, and safe to call from a shutdown path.

        The file itself is deliberately left in place: unlinking a lock file
        another process may already have opened is a classic way to hand the same
        lock to two processes.
        """
        fd, self._fd = self._fd, None
        if fd is None:
            return
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):  # pragma: no cover - closing a valid fd
            os.close(fd)

    def __enter__(self) -> InstanceLock:
        self.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()

    # -- Reporting -------------------------------------------------------------

    @property
    def last_error(self) -> str | None:
        """An I/O reason the lock could not be taken, when that is what happened."""
        return self._last_error

    def refusal_message(self) -> str:
        """The honest, actionable sentence to print when a second launch is refused."""
        if self._last_error is not None:
            return (
                f"refusing to start: {self._last_error} "
                "(a second Body must not run without the single-instance guarantee)"
            )
        holder = self._holder
        who = "another BLAXCY instance" if holder is None else f"another BLAXCY instance ({holder.describe()})"
        return (
            f"{who} already owns this desktop session "
            f"[lock: {self.path}]; refusing to start a second Body. "
            "Exit the running instance first, or pass a different --lock-path."
        )

    # -- Internals -------------------------------------------------------------

    def _read_holder(self, fd: int) -> LockHolder | None:
        """Read the holder record from an open descriptor, never raising."""
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            raw = os.read(fd, 4096)
        except OSError:
            return None
        if not raw:
            return None
        try:
            data: Any = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        pid = data.get("pid")
        if not isinstance(pid, int):
            return None
        started = data.get("started_at")
        command = data.get("command")
        return LockHolder(
            pid=pid,
            started_at=float(started) if isinstance(started, (int, float)) else None,
            command=command if isinstance(command, str) else None,
        )


__all__ = ["DEFAULT_LOCK_FILENAME", "RUNTIME_DIR_NAME", "InstanceLock", "LockHolder", "default_lock_path"]
