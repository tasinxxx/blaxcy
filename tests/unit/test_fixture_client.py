"""The fixture harness's bounded startup retry (specification section 84).

On a loaded desktop the section 74 fixture can exit before or just after it
reports ``ready`` -- it exits on stdin EOF -- and that is a startup *race*, not a
defect in the code under test. The harness now retries a bounded number of times.
The retry logic is exercised here against a fake child process, so no fixture is
launched and no display is needed.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest

from tests.harness import fixture_client
from tests.harness.fixture_client import FixtureApp, FixtureError


class _FakeStream:
    """An iterable, closeable stand-in for a child's stdout or stderr."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines
        self.closed = False

    def __iter__(self) -> Any:
        return iter(self._lines)

    def close(self) -> None:
        self.closed = True


class _FakeStdin:
    """A writeable stand-in for a child's stdin."""

    def __init__(self) -> None:
        self.written: list[str] = []
        self.closed = False

    def write(self, data: str) -> None:
        self.written.append(data)

    def flush(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _FakeProcess:
    """A fake child with scripted output and liveness."""

    def __init__(self, lines: list[dict[str, Any]], *, alive: bool, code: int = 0) -> None:
        self.stdout = _FakeStream([json.dumps(line) + "\n" for line in lines])
        self.stderr = _FakeStream([])
        self.stdin = _FakeStdin()
        self._alive = alive
        self.returncode = None if alive else code

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if self._alive:
            raise subprocess.TimeoutExpired(cmd="fixture", timeout=timeout or 0.0)
        return int(self.returncode or 0)

    def terminate(self) -> None:
        self._alive = False
        self.returncode = -15

    def kill(self) -> None:
        self.terminate()


class _SubprocessShim:
    """A stand-in for the ``subprocess`` module so no real process is spawned."""

    PIPE = subprocess.PIPE
    TimeoutExpired = subprocess.TimeoutExpired

    def __init__(self, popen: Any) -> None:
        self.Popen = popen


def _ready(*, alive: bool) -> _FakeProcess:
    """A child that reports ready, then stays alive (or dies immediately)."""
    return _FakeProcess([{"event": "ready"}], alive=alive)


def _patch(monkeypatch: pytest.MonkeyPatch, processes: list[_FakeProcess]) -> list[list[str]]:
    """Replace the harness's ``Popen`` and return the argv of each launch."""
    calls: list[list[str]] = []

    def fake_popen(argv: list[str], **_kwargs: Any) -> _FakeProcess:
        calls.append(list(argv))
        return processes.pop(0)

    monkeypatch.setattr(fixture_client, "subprocess", _SubprocessShim(fake_popen))
    return calls


def _app(**overrides: Any) -> FixtureApp:
    """A fixture client with fast retries so the tests stay quick."""
    params: dict[str, Any] = {
        "platform": "offscreen",
        "start_attempts": 3,
        "retry_backoff_s": 0.0,
        "ready_grace_s": 0.05,
    }
    params.update(overrides)
    return FixtureApp(**params)


def test_a_healthy_start_needs_no_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ordinary path launches exactly once."""
    calls = _patch(monkeypatch, [_ready(alive=True)])
    app = _app()

    app.start()

    assert len(calls) == 1
    assert app.ready_payload["event"] == "ready"
    app.stop()


def test_a_child_that_dies_right_after_ready_is_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The observed race -- ready then immediate exit -- is retried, not failed."""
    calls = _patch(monkeypatch, [_ready(alive=False), _ready(alive=True)])
    app = _app()

    app.start()

    assert len(calls) == 2
    assert app.ready_payload["event"] == "ready"
    app.stop()


def test_a_child_that_never_reports_ready_is_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unexpected first line is a failed attempt too, not a hard stop."""
    calls = _patch(
        monkeypatch,
        [_FakeProcess([{"event": "unexpected"}], alive=True), _ready(alive=True)],
    )
    app = _app()

    app.start()

    assert len(calls) == 2
    app.stop()


def test_start_raises_only_after_the_bounded_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every attempt is tried, then the failure names how many there were."""
    calls = _patch(monkeypatch, [_ready(alive=False)] * 3)
    app = _app()

    with pytest.raises(FixtureError) as excinfo:
        app.start()

    assert len(calls) == 3
    assert "3 attempts" in str(excinfo.value)


def test_a_single_attempt_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """``start_attempts = 1`` keeps the old single-shot behaviour available."""
    calls = _patch(monkeypatch, [_ready(alive=False)])
    app = _app(start_attempts=1)

    with pytest.raises(FixtureError):
        app.start()

    assert len(calls) == 1
