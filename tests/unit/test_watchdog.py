"""Watchdog and crash cleanup (specification sections 64, 78, 85).

The watchdog's value is that it leaves honest evidence, so these tests are about
what the record says after a clean stop versus a crash, and about the watchdog
not becoming one of the problems it monitors (an orphan thread, a false "clean"
claim).
"""

from __future__ import annotations

import json
import signal
import time

import pytest

from watchdog.protocol import (
    HeartbeatRecord,
    decode_record,
    inspect_previous_run,
    read_record,
    write_record,
)
from watchdog.watchdog import CleanupReport, CrashGuard, Watchdog, WatchdogStatus


def _watchdog(tmp_path: object, **kwargs: object) -> Watchdog:
    """A watchdog writing into the test's temporary directory."""
    from pathlib import Path

    base = Path(str(tmp_path))
    return Watchdog(path=base / "watchdog.json", pid=4242, session_id="sess", **kwargs)  # type: ignore[arg-type]


# -- Record round-trip --------------------------------------------------------

def test_a_record_round_trips_through_the_state_file(tmp_path: object) -> None:
    """What is written is what is read back."""
    from pathlib import Path

    path = Path(str(tmp_path)) / "record.json"
    record = HeartbeatRecord(
        pid=11,
        session_id="abc",
        started_at=1.0,
        updated_at=2.0,
        beats=3,
        owned_keys_down=("Control_L",),
        owned_buttons_down=("left",),
    )
    write_record(path, record)
    assert read_record(path) == record


def test_an_unreadable_record_is_reported_not_raised(tmp_path: object) -> None:
    """A corrupt state file is evidence of a crash, not an exception to crash on."""
    from pathlib import Path

    path = Path(str(tmp_path)) / "record.json"
    path.write_text("{ not json", encoding="utf-8")
    assert read_record(path) is None
    report = inspect_previous_run(path)
    assert report.exists is True
    assert report.crashed is True
    assert report.unreadable_reason is not None
    assert "unreadable" in report.summary()


def test_a_malformed_record_is_rejected_by_the_decoder() -> None:
    """The decoder is strict, so a truncated write cannot be half-trusted."""
    with pytest.raises(ValueError):
        decode_record(json.dumps({"pid": "not-a-pid"}))


def test_no_record_means_no_previous_run(tmp_path: object) -> None:
    """A missing state file is a first run, not a crash."""
    from pathlib import Path

    report = inspect_previous_run(Path(str(tmp_path)) / "absent.json")
    assert report.exists is False
    assert report.crashed is False
    assert report.summary() == "no previous watchdog record"


# -- Lifecycle ----------------------------------------------------------------

def test_start_beats_and_stop_marks_a_clean_shutdown(tmp_path: object) -> None:
    """A deliberate stop is recorded as clean."""
    watchdog = _watchdog(tmp_path, interval_seconds=0.01)
    watchdog.start()
    deadline = time.monotonic() + 2.0
    while watchdog.beats < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    watchdog.stop(clean=True)

    record = watchdog.record
    assert record is not None
    assert record.clean_shutdown is True
    assert record.beats >= 1
    assert watchdog.running is False


def test_an_unclean_stop_leaves_a_dirty_record(tmp_path: object) -> None:
    """A crash path that never calls stop leaves evidence behind."""
    watchdog = _watchdog(tmp_path)
    watchdog.start()
    watchdog.stop(clean=False)
    report = watchdog.inspect_previous_run()
    assert report.crashed is True
    assert "did not end cleanly" in report.summary()


def test_the_next_run_can_see_what_the_dead_one_was_holding(tmp_path: object) -> None:
    """Section 64: ownership survives the process, so the next run can report it."""
    watchdog = _watchdog(tmp_path)
    watchdog.start()
    watchdog.set_ownership(keys=("Control_L", "Shift_L"), buttons=("left",))
    watchdog.beat()
    watchdog.stop(clean=False)

    report = watchdog.inspect_previous_run()
    assert report.held_input_at_exit is True
    assert report.owned_keys == ("Control_L", "Shift_L")
    assert report.owned_buttons == ("left",)
    assert "holding keys" in report.summary()


def test_ownership_is_recorded_in_the_heartbeat(tmp_path: object) -> None:
    """The heartbeat itself carries the ownership fields (sections 64, 78)."""
    watchdog = _watchdog(tmp_path)
    watchdog.set_ownership(keys=("Alt_L",), buttons=("right",))
    watchdog.start()
    record = watchdog.record
    assert record is not None
    assert record.owned_keys_down == ("Alt_L",)
    assert record.owned_buttons_down == ("right",)
    watchdog.stop()


def test_start_and_stop_are_idempotent_and_leave_no_orphan_thread(tmp_path: object) -> None:
    """No orphan watchdog: a stopped watchdog has no living thread."""
    import threading

    watchdog = _watchdog(tmp_path, interval_seconds=0.01)
    before = {thread.name for thread in threading.enumerate()}
    watchdog.start()
    watchdog.start()
    started_running = watchdog.running
    watchdog.stop()
    watchdog.stop()
    stopped_running = watchdog.running
    assert started_running is True
    assert stopped_running is False
    after = {thread.name for thread in threading.enumerate()}
    assert not (after - before), "the watchdog thread outlived its run"


def test_staleness_is_reported_from_the_record_age(tmp_path: object) -> None:
    """A record that stopped being refreshed is reported stale, not alive."""
    now = {"value": 1000.0}
    watchdog = _watchdog(
        tmp_path,
        stale_after_seconds=5.0,
        wall_clock=lambda: now["value"],
    )
    watchdog.start()
    assert watchdog.status() is WatchdogStatus.RUNNING
    now["value"] += 60.0
    assert watchdog.status() is WatchdogStatus.STALE
    watchdog.stop()


def test_a_negative_interval_is_refused() -> None:
    """A watchdog that cannot beat is a configuration error, not a silent no-op."""
    with pytest.raises(ValueError):
        Watchdog(interval_seconds=0)


# -- Crash guard --------------------------------------------------------------

def test_cleanup_releases_input_and_marks_the_run_clean(tmp_path: object) -> None:
    """An ordinary exit releases input and records a clean shutdown."""
    watchdog = _watchdog(tmp_path)
    watchdog.start()
    released: list[bool] = []
    guard = CrashGuard(
        release_input=lambda: released.append(True),
        watchdog=watchdog,
        install_atexit=False,
    )
    report = guard.cleanup(signum=None)
    assert released == [True]
    assert report.released is True
    assert report.watchdog_stopped is True
    assert watchdog.clean is True


def test_a_signal_cleans_up_and_then_terminates(tmp_path: object) -> None:
    """Cleanup must not swallow a termination request."""
    released: list[bool] = []
    terminated: list[int] = []
    guard = CrashGuard(
        release_input=lambda: released.append(True),
        terminate=lambda signum: terminated.append(signum),
        install_atexit=False,
    )
    report = guard.handle(signal.SIGTERM)
    assert released == [True]
    assert terminated == [signal.SIGTERM]
    assert report.terminate_called is True


def test_a_failing_release_still_stops_the_watchdog_and_terminates(tmp_path: object) -> None:
    """One failing step must not prevent the others or the termination."""
    watchdog = _watchdog(tmp_path)
    watchdog.start()

    def broken() -> None:
        raise RuntimeError("cannot release")

    terminated: list[int] = []
    guard = CrashGuard(
        release_input=broken,
        watchdog=watchdog,
        terminate=lambda signum: terminated.append(signum),
        install_atexit=False,
    )
    report = guard.handle(signal.SIGTERM)
    assert report.released is False
    assert report.watchdog_stopped is True
    assert report.errors
    assert terminated == [signal.SIGTERM]
    assert watchdog.clean is True


def test_a_nested_cleanup_is_refused() -> None:
    """A cleanup triggered from inside a cleanup must not recurse."""
    nested: list[CleanupReport] = []
    holder: dict[str, CrashGuard] = {}

    def release() -> None:
        nested.append(holder["guard"].cleanup())

    holder["guard"] = CrashGuard(release_input=release, install_atexit=False)
    outer = holder["guard"].cleanup()

    assert outer.released is True
    inner = nested[0]
    assert inner.released is False
    assert "already in progress" in inner.errors[0]


def test_cleanup_can_run_again_after_a_signal_cleanup() -> None:
    """The atexit hook must still work when a signal already cleaned up."""
    calls: list[int] = []
    guard = CrashGuard(
        release_input=lambda: calls.append(1),
        terminate=lambda _signum: None,
        install_atexit=False,
    )
    guard.handle(signal.SIGTERM)
    guard.cleanup()
    assert len(calls) == 2


def test_a_second_signal_during_handling_is_ignored() -> None:
    """A signal arriving during terminate must not restart the cleanup."""
    calls: list[int] = []
    holder: dict[str, CrashGuard] = {}
    inner: list[CleanupReport] = []

    def terminate(_signum: int) -> None:
        inner.append(holder["guard"].handle(signal.SIGTERM))

    holder["guard"] = CrashGuard(
        release_input=lambda: calls.append(1), terminate=terminate, install_atexit=False
    )
    holder["guard"].handle(signal.SIGTERM)

    assert len(calls) == 1
    assert "already running" in inner[0].errors[0]


def test_install_and_uninstall_restore_signal_handlers() -> None:
    """The guard puts the previous handlers back rather than leaving its own."""
    guard = CrashGuard(release_input=lambda: None, install_atexit=False)
    previous = signal.getsignal(signal.SIGTERM)
    try:
        installed = guard.install()
        assert installed
        was_installed = guard.installed
    finally:
        guard.uninstall()
    now_installed = guard.installed
    assert was_installed is True
    assert now_installed is False
    assert signal.getsignal(signal.SIGTERM) is previous
