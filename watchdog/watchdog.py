"""Crash-safety watchdog (specification sections 64, 78, 85).

Section 64 asks for a *minimal* watchdog where the platform makes it meaningful,
with two jobs:

1. **Keep a record of a run's life and input ownership.** A background thread
   refreshes a heartbeat file, and the heartbeat carries ``owned_keys_down`` /
   ``owned_buttons_down`` so the next run can report what the dead one was
   holding. It is deliberately a small file on disk rather than a service, a
   socket or a broker -- section 2 rules those out.

2. **Clean up on the way out.** :class:`CrashGuard` installs signal handlers and
   an ``atexit`` hook that release BLAXCY-held input and mark the run as cleanly
   shut down, so an ordinary termination does not look like a crash.

Three properties this module refuses to compromise on:

* **No orphan watchdog.** The heartbeat thread is a daemon and is stopped and
  joined on ``stop``; ``stop`` is idempotent. A watchdog that outlives its run
  would be a second thing holding the record file.
* **No false "clean" claim.** The record is only marked clean when ``stop`` is
  called with ``clean=True``. A crash leaves the record dirty on purpose; the
  next run's :meth:`Watchdog.inspect_previous_run` then reports it honestly.
* **A failed cleanup still cleans up.** Release errors are collected and
  reported, never allowed to prevent the rest of the cleanup or the process from
  terminating.
"""

from __future__ import annotations

import atexit
import os
import signal
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from watchdog.protocol import (
    DEFAULT_STALE_AFTER_SECONDS,
    DEFAULT_WATCHDOG_PATH,
    HeartbeatRecord,
    PreviousRunReport,
    inspect_previous_run,
    read_record,
    write_record,
)


class WatchdogStatus(StrEnum):
    """Whether the watchdog is beating, stopped, or gone quiet."""

    STOPPED = "STOPPED"
    RUNNING = "RUNNING"
    STALE = "STALE"


@runtime_checkable
class _Releaser(Protocol):
    """The part of the input layer the cleanup needs (section 63)."""

    def release_all(self) -> None:
        """Release every held key and button."""


class Watchdog:
    """A minimal heartbeat watchdog with explicit input ownership (section 64).

    Args:
        path: State-file location. ``None`` uses the default runtime path.
        interval_seconds: Heartbeat period.
        stale_after_seconds: How long a record may go unrefreshed before the run
            that wrote it is treated as dead.
        pid: Process id; overridable for tests.
        session_id: Run identifier; overridable for tests.
        wall_clock: Wall clock, injectable for deterministic tests.
    """

    def __init__(
        self,
        *,
        path: Path | None = None,
        interval_seconds: float = 1.0,
        stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS,
        pid: int | None = None,
        session_id: str | None = None,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self._path = path if path is not None else DEFAULT_WATCHDOG_PATH
        self._interval = interval_seconds
        self._stale_after = stale_after_seconds
        self._pid = os.getpid() if pid is None else pid
        self._session_id = session_id if session_id is not None else uuid.uuid4().hex
        self._wall_clock = wall_clock
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._started_at = self._wall_clock()
        self._beats = 0
        self._keys: tuple[str, ...] = ()
        self._buttons: tuple[str, ...] = ()
        self._clean = False
        self._running = False

    # -- Lifecycle ------------------------------------------------------------

    @property
    def path(self) -> Path:
        """The state-file location."""
        return self._path

    @property
    def session_id(self) -> str:
        """This run's identifier."""
        return self._session_id

    @property
    def pid(self) -> int:
        """This run's process id."""
        return self._pid

    @property
    def running(self) -> bool:
        """True while the heartbeat thread is alive."""
        with self._lock:
            return self._running and self._thread is not None and self._thread.is_alive()

    @property
    def beats(self) -> int:
        """How many heartbeats have been written."""
        with self._lock:
            return self._beats

    @property
    def clean(self) -> bool:
        """True once the run has been marked as cleanly shut down."""
        with self._lock:
            return self._clean

    def start(self) -> None:
        """Begin beating. Idempotent."""
        with self._lock:
            if self._running:
                return
            self._started_at = self._wall_clock()
            self._clean = False
            self._stop_event.clear()
            self._write_locked()
            thread = threading.Thread(
                target=self._run, name="blaxcy-watchdog", daemon=True
            )
            self._thread = thread
            self._running = True
        thread.start()

    def stop(self, *, clean: bool = True) -> None:
        """Stop beating and record how the run ended. Idempotent.

        Args:
            clean: Mark the record as a clean shutdown. The default is ``True``
                because a caller who deliberately stops the watchdog is normally
                shutting down; an abnormal path simply never gets here and leaves
                the record dirty for the next run to notice.
        """
        with self._lock:
            thread = self._thread
            self._running = False
            self._clean = clean
            self._stop_event.set()
            self._write_locked()
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)

    def beat(self) -> None:
        """Write one heartbeat immediately."""
        with self._lock:
            self._write_locked()

    def set_ownership(
        self,
        *,
        keys: Iterable[str] = (),
        buttons: Iterable[str] = (),
    ) -> None:
        """Record what input BLAXCY currently holds (sections 64, 78)."""
        with self._lock:
            self._keys = tuple(str(key) for key in keys)
            self._buttons = tuple(str(button) for button in buttons)

    # -- Inspection -----------------------------------------------------------

    @property
    def record(self) -> HeartbeatRecord | None:
        """The record currently on disk, or ``None``."""
        return read_record(self._path)

    def status(self) -> WatchdogStatus:
        """Whether this watchdog is beating, stopped, or has gone quiet."""
        with self._lock:
            running = self._running and self._thread is not None and self._thread.is_alive()
            last = self._beats
        record = self.record
        now = self._wall_clock()
        if record is not None and (now - record.updated_at) > self._stale_after:
            return WatchdogStatus.STALE
        if running and last > 0:
            return WatchdogStatus.RUNNING
        return WatchdogStatus.STOPPED

    def inspect_previous_run(self) -> PreviousRunReport:
        """Report what the previous run left behind (section 64 crash cleanup)."""
        return inspect_previous_run(self._path, now=self._wall_clock())

    def current_record(self) -> HeartbeatRecord:
        """Build the record describing this run right now."""
        with self._lock:
            return HeartbeatRecord(
                pid=self._pid,
                session_id=self._session_id,
                started_at=self._started_at,
                updated_at=self._wall_clock(),
                beats=self._beats,
                owned_keys_down=self._keys,
                owned_buttons_down=self._buttons,
                clean_shutdown=self._clean,
            )

    # -- Internals ------------------------------------------------------------

    def _write_locked(self) -> None:
        """Refresh the record on disk. Caller holds the lock."""
        self._beats += 1
        try:
            write_record(self._path, self.current_record())
        except OSError:
            # Losing the state file must never take the process down; the
            # watchdog is evidence, not a safety gate.
            self._beats -= 1

    def _run(self) -> None:
        """Heartbeat until stopped."""
        while not self._stop_event.wait(self._interval):
            with self._lock:
                if not self._running:
                    return
                self._write_locked()


@dataclass(frozen=True)
class CleanupReport:
    """What a crash guard's cleanup did."""

    signum: int | None
    released: bool
    watchdog_stopped: bool
    terminate_called: bool
    errors: tuple[str, ...] = field(default=())

    def to_dict(self) -> dict[str, Any]:
        """JSON-shaped report."""
        return {
            "signum": self.signum,
            "released": self.released,
            "watchdog_stopped": self.watchdog_stopped,
            "terminate_called": self.terminate_called,
            "errors": list(self.errors),
        }


#: Signals a desktop application should clean up on by default.
DEFAULT_CLEANUP_SIGNALS: tuple[int, ...] = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)


def default_terminate(signum: int) -> None:
    """Re-deliver ``signum`` with its default disposition, ending the process.

    Restoring the default first is what stops the handler from swallowing a
    termination request: cleanup must not turn ``SIGTERM`` into "keep running".
    """
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)


class CrashGuard:
    """Releases held input and marks a clean exit on signal/atexit (section 64).

    Args:
        watchdog: Optional watchdog to stop (cleanly) as part of cleanup.
        release_input: Called to release BLAXCY-held input. Receives nothing and
            is expected to be safe when nothing is held.
        signals: Signals to handle. Defaults to TERM/INT/HUP.
        terminate: Called after cleanup to end the process. Defaults to
            re-delivering the signal with its default disposition; tests inject a
            recorder so no signal is ever really delivered.
        install_atexit: Whether to also register an ``atexit`` hook.
    """

    def __init__(
        self,
        *,
        release_input: Callable[[], None],
        watchdog: Watchdog | None = None,
        signals: Sequence[int] = DEFAULT_CLEANUP_SIGNALS,
        terminate: Callable[[int], None] | None = None,
        install_atexit: bool = True,
    ) -> None:
        self._release = release_input
        self._watchdog = watchdog
        self._signals = tuple(signals)
        self._terminate = terminate if terminate is not None else default_terminate
        self._install_atexit = install_atexit
        self._lock = threading.RLock()
        self._previous: dict[int, Any] = {}
        self._installed = False
        self._cleaning = False
        self._signalling = False
        self._atexit_registered = False
        self._reports: list[CleanupReport] = []

    @property
    def installed(self) -> bool:
        """True while signal handlers are installed."""
        with self._lock:
            return self._installed

    @property
    def reports(self) -> tuple[CleanupReport, ...]:
        """Every cleanup that has run, in order."""
        with self._lock:
            return tuple(self._reports)

    def install(self) -> tuple[int, ...]:
        """Install the handlers, returning the signals actually handled.

        Signal handlers can only be installed from the main thread, so a call
        from another thread installs nothing and says so by returning an empty
        tuple instead of raising: the ``atexit`` hook still covers the ordinary
        exit path.
        """
        installed: list[int] = []
        with self._lock:
            if self._installed:
                return self._signals
            for signum in self._signals:
                try:
                    self._previous[signum] = signal.getsignal(signum)
                    signal.signal(signum, self.handle)
                    installed.append(signum)
                except (ValueError, OSError, RuntimeError):
                    # Not the main thread, or an unusable signal: skip it.
                    self._previous.pop(signum, None)
            self._installed = bool(installed)
            if self._install_atexit and not self._atexit_registered:
                atexit.register(self._on_exit)
                self._atexit_registered = True
        return tuple(installed)

    def uninstall(self) -> None:
        """Restore the previous handlers and drop the ``atexit`` hook."""
        with self._lock:
            for signum, previous in self._previous.items():
                try:
                    signal.signal(signum, previous)
                except (ValueError, OSError, RuntimeError):
                    continue
            self._previous.clear()
            self._installed = False
            if self._atexit_registered:
                atexit.unregister(self._on_exit)
                self._atexit_registered = False

    def cleanup(self, *, signum: int | None = None) -> CleanupReport:
        """Release input and mark the run clean. Safe to call more than once.

        Every step is individually guarded: a failure to release must not stop
        the watchdog from being updated, and neither may stop the process from
        terminating.

        The call is not re-entrant -- a cleanup triggered from inside a cleanup
        is refused rather than run twice -- but the guard is released again when
        it returns, so an ``atexit`` cleanup after a signal-triggered one still
        does its job.
        """
        with self._lock:
            if self._cleaning:
                return CleanupReport(signum, False, False, False, ("cleanup already in progress",))
            self._cleaning = True
        try:
            errors: list[str] = []
            released = False
            try:
                self._release()
                released = True
            except Exception as exc:
                errors.append(f"input release failed: {exc!r}")
            stopped = False
            if self._watchdog is not None:
                try:
                    self._watchdog.stop(clean=True)
                    stopped = True
                except Exception as exc:
                    errors.append(f"watchdog stop failed: {exc!r}")
            report = CleanupReport(signum, released, stopped, False, tuple(errors))
            with self._lock:
                self._reports.append(report)
            return report
        finally:
            with self._lock:
                self._cleaning = False

    # -- Handlers -------------------------------------------------------------

    def handle(self, signum: int, frame: Any = None) -> CleanupReport:
        """Handle a termination signal: clean up, then really terminate.

        Public so a test (or a caller chaining its own signals) can exercise the
        exact path a real signal takes without delivering one.
        """
        del frame
        with self._lock:
            if self._signalling:
                # A second signal arriving during cleanup must not restart it.
                return CleanupReport(
                    signum, False, False, False, ("a signal cleanup is already running",)
                )
            self._signalling = True
        try:
            report = self.cleanup(signum=signum)
            final = CleanupReport(
                report.signum, report.released, report.watchdog_stopped, True, report.errors
            )
            with self._lock:
                if self._reports and self._reports[-1] == report:
                    self._reports[-1] = final
            self._terminate(signum)
            return final
        finally:
            # If the injected terminate did not exit (tests, or a handler that
            # chose not to), allow a later signal to be handled normally again.
            with self._lock:
                self._signalling = False

    def _on_exit(self) -> None:
        """Ordinary process exit: release input and mark the run clean."""
        self.cleanup(signum=None)


__all__ = [
    "DEFAULT_CLEANUP_SIGNALS",
    "CleanupReport",
    "CrashGuard",
    "Watchdog",
    "WatchdogStatus",
    "default_terminate",
]
