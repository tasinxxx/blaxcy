"""Launcher and control client for the section 74 fixture application.

The harness starts the fixture as a real child process and speaks the
line-delimited JSON control protocol on its stdin/stdout. Readiness is
explicit (the fixture emits a ``ready`` line), so tests never sleep-and-hope.

Default is the Qt ``offscreen`` platform: deterministic, no display server
required, and no window ever appears on the user's desktop. Pass
``platform=None`` to launch on the real display (only valid when one exists).
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Repository root, used as the child's working directory so `main.py`-relative
#: assumptions and AT-SPI/tooling behave the same as a normal run.
PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: Path to the fixture app, launched by path so no install step is needed.
FIXTURE_APP = PROJECT_ROOT / "tests" / "fixtures" / "fixture_app.py"

#: Sentinel pushed onto the reader queue when the child's stdout closes.
_EOF = object()


class FixtureError(RuntimeError):
    """The fixture process failed to start, died, or misbehaved."""


@dataclass(frozen=True)
class FixtureResponse:
    """One parsed response line from the fixture."""

    event: str
    cmd: str | None
    payload: dict[str, Any]

    @property
    def ok(self) -> bool:
        """True when the fixture acknowledged the command."""
        return self.event == "ack"

    @property
    def message(self) -> str:
        """The error message, if this response is an error."""
        value = self.payload.get("message")
        return str(value) if value is not None else ""


class FixtureApp:
    """A running fixture application, driven over its JSON control channel."""

    def __init__(
        self,
        *,
        platform: str | None = "offscreen",
        title: str = "BLAXCY Test Fixture",
        start_timeout: float = 20.0,
        command_timeout: float = 10.0,
    ) -> None:
        self._platform = platform
        self._title = title
        self._start_timeout = start_timeout
        self._command_timeout = command_timeout
        self._process: subprocess.Popen[str] | None = None
        self._lines: queue.Queue[Any] = queue.Queue()
        self._reader: threading.Thread | None = None
        self._stderr_chunks: list[str] = []
        self.ready_payload: dict[str, Any] = {}

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> FixtureApp:
        """Start the child process and block until its ``ready`` line arrives."""
        if self._process is not None:
            raise FixtureError("fixture already started")
        if not FIXTURE_APP.exists():
            raise FixtureError(f"fixture app not found at {FIXTURE_APP}")

        env = dict(os.environ)
        if self._platform is not None:
            env["QT_QPA_PLATFORM"] = self._platform

        self._process = subprocess.Popen(
            [sys.executable, str(FIXTURE_APP), "--title", self._title],
            cwd=str(PROJECT_ROOT),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

        ready = self._next_line(self._start_timeout)
        if ready.get("event") != "ready":
            self.stop()
            raise FixtureError(f"fixture did not report ready; first line was: {ready!r}")
        self.ready_payload = ready
        return self

    def stop(self) -> None:
        """Ask the fixture to quit, then ensure the process is gone."""
        process = self._process
        if process is None:
            return
        try:
            if process.poll() is None and process.stdin is not None:
                process.stdin.write(json.dumps({"cmd": "quit"}) + "\n")
                process.stdin.flush()
                process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            for stream in (process.stdin, process.stdout, process.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except OSError:
                    pass
            self._process = None

    def __enter__(self) -> FixtureApp:
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    # -- reader threads -------------------------------------------------------

    def _read_stdout(self) -> None:
        """Push every stdout line (or the EOF sentinel) onto the queue."""
        process = self._process
        if process is None or process.stdout is None:
            self._lines.put(_EOF)
            return
        for line in process.stdout:
            self._lines.put(line)
        self._lines.put(_EOF)

    def _read_stderr(self) -> None:
        """Keep child stderr for diagnostics without blocking anything."""
        process = self._process
        if process is None or process.stderr is None:
            return
        for line in process.stderr:
            self._stderr_chunks.append(line)
            if len(self._stderr_chunks) > 500:
                del self._stderr_chunks[:250]

    def _next_line(self, timeout: float) -> dict[str, Any]:
        """Return the next JSON object from the child, or raise."""
        try:
            item = self._lines.get(timeout=timeout)
        except queue.Empty as exc:
            raise FixtureError(
                f"timed out after {timeout}s waiting for fixture output; stderr:\n"
                f"{self.stderr_text()}"
            ) from exc
        if item is _EOF:
            raise FixtureError(
                f"fixture process exited unexpectedly; stderr:\n{self.stderr_text()}"
            )
        text = str(item).strip()
        if not text:
            return self._next_line(timeout)
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise FixtureError(f"fixture emitted non-JSON line: {text!r}") from exc
        if not isinstance(parsed, dict):
            raise FixtureError(f"fixture emitted a non-object line: {text!r}")
        return parsed

    def stderr_text(self) -> str:
        """Accumulated child stderr, for error messages."""
        return "".join(self._stderr_chunks)

    # -- protocol -------------------------------------------------------------

    def command(self, cmd: str, **params: Any) -> FixtureResponse:
        """Send one command and return the fixture's response (erroneous or not)."""
        process = self._process
        if process is None or process.stdin is None:
            raise FixtureError("fixture is not running")
        if process.poll() is not None:
            raise FixtureError(f"fixture already exited (code {process.returncode})")
        process.stdin.write(json.dumps({"cmd": cmd, **params}) + "\n")
        process.stdin.flush()
        raw = self._next_line(self._command_timeout)
        payload = {k: v for k, v in raw.items() if k not in ("event", "cmd")}
        return FixtureResponse(
            event=str(raw.get("event", "")),
            cmd=raw.get("cmd") if raw.get("cmd") is None else str(raw["cmd"]),
            payload=payload,
        )

    def command_ok(self, cmd: str, **params: Any) -> FixtureResponse:
        """Like :meth:`command`, but raise when the fixture reports an error."""
        response = self.command(cmd, **params)
        if not response.ok:
            raise FixtureError(f"command {cmd!r} failed: {response.message}")
        return response

    # -- typed conveniences ---------------------------------------------------

    def inventory(self) -> list[dict[str, Any]]:
        """Return the current widget inventory."""
        response = self.command_ok("inventory")
        return list(response.payload["inventory"])

    def stats(self) -> dict[str, Any]:
        """Return the current derived state."""
        response = self.command_ok("stats")
        return dict(response.payload["stats"])

    def set_text(self, target: str, value: str) -> FixtureResponse:
        return self.command_ok("set_text", target=target, value=value)

    def click(self, target: str) -> FixtureResponse:
        return self.command_ok("click", target=target)

    def toggle(self, target: str) -> FixtureResponse:
        return self.command_ok("toggle", target=target)

    def move(self, target: str, dx: int, dy: int) -> FixtureResponse:
        return self.command_ok("move", target=target, dx=dx, dy=dy)

    def destroy(self, target: str) -> FixtureResponse:
        return self.command_ok("destroy", target=target)

    def set_occluder(self, visible: bool) -> FixtureResponse:
        return self.command_ok("set_occluder", visible=visible)

    def set_focus(self, target: str) -> FixtureResponse:
        return self.command_ok("set_focus", target=target)

    def open_dialog(self) -> FixtureResponse:
        return self.command_ok("open_dialog")
