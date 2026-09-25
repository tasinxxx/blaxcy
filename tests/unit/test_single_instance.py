"""Single-instance control (specification sections 72, 85).

Section 85 requires that "second launch is controlled". These tests assert what
that means in practice, and specifically why the mechanism is an ``flock`` rather
than a pid-file heuristic:

* a second acquisition on the same path is refused, and can name the holder;
* the lock is released by *process death*, with no stale-lock case to get wrong
  (tested with a real child process, not a simulated one);
* a lock that cannot even be created fails closed -- refusing to start a second
  Body, never silently running without the guarantee;
* the default location is a per-user runtime path, not a world-writable one.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from core.single_instance import (
    DEFAULT_LOCK_FILENAME,
    InstanceLock,
    LockHolder,
    default_lock_path,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def held(lock: InstanceLock) -> bool:
    """Read ``acquired`` through a call.

    Reading the property directly would let the type checker narrow it to a
    literal and then treat the *next* read as unreachable, which is a property of
    the checker rather than of the lock -- so the assertion is routed through a
    function to keep the test about the lock.
    """
    return lock.acquired


def test_acquiring_and_releasing_round_trips(tmp_path: Path) -> None:
    lock = InstanceLock(tmp_path / "instance.lock")
    assert held(lock) is False
    assert lock.acquire() is True
    assert held(lock) is True
    lock.release()
    assert held(lock) is False
    # Releasing twice is safe: shutdown paths call it more than once.
    lock.release()


def test_acquiring_twice_is_idempotent(tmp_path: Path) -> None:
    lock = InstanceLock(tmp_path / "instance.lock")
    assert lock.acquire() is True
    try:
        assert lock.acquire() is True
    finally:
        lock.release()


def test_a_second_lock_on_the_same_path_is_refused(tmp_path: Path) -> None:
    """The property section 85 asks for: a second launch does not proceed."""
    path = tmp_path / "instance.lock"
    first = InstanceLock(path, pid=4242, command="main.py gui")
    second = InstanceLock(path, pid=4343, command="main.py gui")
    assert first.acquire() is True
    try:
        assert second.acquire() is False
        assert second.acquired is False
        holder = second.holder
        assert holder is not None
        assert holder.pid == 4242, "the refusal should name the live holder"
        assert holder.command == "main.py gui"
    finally:
        first.release()
    # With the holder gone, the lock is available again.
    assert second.acquire() is True
    second.release()


def test_the_refusal_message_is_actionable(tmp_path: Path) -> None:
    path = tmp_path / "instance.lock"
    holder = InstanceLock(path, pid=99, command="main.py run do it")
    blocked = InstanceLock(path)
    assert holder.acquire() is True
    try:
        assert blocked.acquire() is False
        message = blocked.refusal_message()
        assert "already owns this desktop session" in message
        assert "pid 99" in message
        assert "main.py run do it" in message
        assert str(path) in message
        assert "refusing to start a second Body" in message
    finally:
        holder.release()


def test_the_lock_file_records_who_holds_it(tmp_path: Path) -> None:
    path = tmp_path / "instance.lock"
    lock = InstanceLock(path, pid=1234, command="main.py gui")
    assert lock.acquire() is True
    try:
        recorded: Any = json.loads(path.read_text(encoding="utf-8"))
        assert recorded["pid"] == 1234
        assert recorded["command"] == "main.py gui"
        assert isinstance(recorded["started_at"], float)
    finally:
        lock.release()


def test_the_context_manager_releases_on_exit(tmp_path: Path) -> None:
    path = tmp_path / "instance.lock"
    with InstanceLock(path) as lock:
        assert lock.acquired is True
        assert InstanceLock(path).acquire() is False
    assert InstanceLock(path).acquire() is True


def test_a_dead_process_does_not_leave_the_lock_held(tmp_path: Path) -> None:
    """Why this is an ``flock`` and not a pid file: the kernel releases it.

    A child process takes the lock and exits *without* releasing it. If the lock
    survived, a crashed Body would block every future launch until someone found
    the file; the assertion here is that it does not.
    """
    path = tmp_path / "instance.lock"
    script = (
        "import sys; from pathlib import Path;"
        "from core.single_instance import InstanceLock;"
        "lock = InstanceLock(Path(sys.argv[1]), pid=os.getpid());"
        "assert lock.acquire(), 'the child could not take the lock';"
        "print('held')"
    )
    script = "import os;" + script
    environment = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    for _ in range(3):
        completed = subprocess.run(
            [sys.executable, "-c", script, str(path)],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            env=environment,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        assert "held" in completed.stdout
        # The child has exited; the lock must be free without any cleanup step.
        lock = InstanceLock(path)
        assert lock.acquire() is True, "a dead holder blocked the lock"
        lock.release()


def test_an_unusable_lock_path_fails_closed(tmp_path: Path) -> None:
    """A lock we cannot create must refuse the launch, never skip the guarantee."""
    # A *file* where the lock's parent directory needs to be.
    blocker = tmp_path / "blaxcy"
    blocker.write_text("not a directory\n", encoding="utf-8")
    lock = InstanceLock(blocker / "instance.lock")
    assert lock.acquire() is False
    assert lock.acquired is False
    assert lock.last_error is not None
    assert "cannot open the lock file" in lock.refusal_message()


def test_the_default_path_is_per_user_and_under_xdg_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/4242")
    path = default_lock_path()
    assert path == Path("/run/user/4242") / "blaxcy" / DEFAULT_LOCK_FILENAME
    assert path.name == "instance.lock"


def test_the_fallback_path_carries_the_uid(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without XDG_RUNTIME_DIR the temp fallback must still be per-user."""
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    path = default_lock_path()
    if hasattr(os, "getuid"):
        assert path.name == DEFAULT_LOCK_FILENAME
        assert str(os.getuid()) in str(path), "a shared temp path would collide across users"


def test_the_holder_description_handles_a_bare_pid() -> None:
    """A lock file with no recorded command still produces a usable description."""
    assert LockHolder(pid=7).describe() == "pid 7"


def test_an_unreadable_holder_record_is_not_a_crash(tmp_path: Path) -> None:
    """A corrupt lock file must still refuse the launch, just without details."""
    path = tmp_path / "instance.lock"
    holder = InstanceLock(path)
    assert holder.acquire() is True
    try:
        path.write_text("this is not json", encoding="utf-8")
        blocked = InstanceLock(path)
        assert blocked.acquire() is False
        assert blocked.holder is None
        assert "another BLAXCY instance" in blocked.refusal_message()
    finally:
        holder.release()
