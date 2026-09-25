"""Human takeover (specification sections 20, 62).

The properties under test are that takeover hands the desktop back completely,
that resuming is only ever explicit, and that nothing about the pre-takeover
world is trusted afterwards.
"""

from __future__ import annotations

from config.settings import SafetySettings
from control.takeover import TakeoverController
from core.event_bus import EventBus
from policy.modes import ModeController
from schemas.enums import ErrorCode, PolicyMode
from schemas.events import EventType


class Releaser:
    """A releaser that records the order of its calls."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def release_buttons(self) -> None:
        self.calls.append("buttons")

    def release_keys(self) -> None:
        self.calls.append("keys")


def _takeover() -> tuple[TakeoverController, EventBus, ModeController]:
    """A takeover controller over a bus and an ASSIST/AUTONOMOUS mode controller."""
    bus = EventBus()
    modes = ModeController(SafetySettings(mode=PolicyMode.ASSIST), event_bus=bus)
    modes.set_mode(PolicyMode.AUTONOMOUS)
    return TakeoverController(event_bus=bus, mode_controller=modes), bus, modes


def test_begin_pauses_releases_and_forces_observe() -> None:
    """Takeover stops automation, releases input and drops to OBSERVE (§62)."""
    takeover, bus, modes = _takeover()
    releaser = Releaser()
    takeover.register_input(releaser)
    cancels: list[str] = []
    takeover.register_cancel(lambda: cancels.append("paused"))

    event = takeover.begin("user grabbed the mouse")

    assert event is not None
    assert cancels == ["paused"]
    assert releaser.calls == ["buttons", "keys"]
    assert modes.mode is PolicyMode.OBSERVE
    assert len(bus.history_of(EventType.HUMAN_TAKEOVER)) == 1
    assert takeover.active is True


def test_abort_code_reports_takeover_while_paused() -> None:
    """The executor's hook refuses to act while a human has control."""
    takeover, _bus, _modes = _takeover()
    assert takeover.abort_code() is None
    takeover.begin()
    assert takeover.abort_code() is ErrorCode.HUMAN_TAKEOVER


def test_begin_twice_still_releases_but_only_emits_once() -> None:
    """A second takeover keeps the desktop handed back without re-announcing it."""
    takeover, bus, _modes = _takeover()
    releaser = Releaser()
    takeover.register_input(releaser)

    assert takeover.begin("first") is not None
    assert takeover.begin("second") is None

    assert releaser.calls.count("buttons") == 2
    assert len(bus.history_of(EventType.HUMAN_TAKEOVER)) == 1


def test_resume_is_explicit_and_invalidates_every_lease() -> None:
    """Resuming clears the latch and throws away the pre-takeover state (§62)."""
    takeover, _bus, modes = _takeover()
    takeover.begin()
    invalidated: list[int] = []
    reperceived: list[bool] = []

    def invalidate() -> int:
        invalidated.append(1)
        return 3

    def reperceive() -> None:
        reperceived.append(True)

    takeover.register_lease_invalidator(invalidate)
    takeover.register_reperceive(reperceive)

    result = takeover.resume()

    assert result.resumed is True
    assert result.leases_invalidated == 3
    assert result.re_perceived is True
    assert takeover.active is False
    assert takeover.abort_code() is None
    # Takeover overrides autonomous control: the mode is not silently restored.
    assert modes.mode is PolicyMode.OBSERVE


def test_resume_without_takeover_does_nothing() -> None:
    """There is nothing to resume when automation was never paused."""
    takeover, _bus, _modes = _takeover()
    result = takeover.resume()
    assert result.resumed is False
    assert result.leases_invalidated == 0
    assert result.re_perceived is False


def test_a_failing_invalidator_or_reperceiver_is_reported_not_swallowed() -> None:
    """Failures during resume are visible rather than silently ignored."""
    takeover, _bus, _modes = _takeover()
    takeover.begin()

    def bad_invalidator() -> int:
        raise RuntimeError("cannot invalidate")

    def bad_reperceiver() -> None:
        raise RuntimeError("cannot see")

    takeover.register_lease_invalidator(bad_invalidator)
    takeover.register_reperceive(bad_reperceiver)

    result = takeover.resume()
    assert result.resumed is True
    assert result.leases_invalidated == 0
    assert result.re_perceived is False
    assert len(result.errors) == 2


def test_status_reports_what_the_gui_needs() -> None:
    """The status snapshot exposes the latch, the reason and the last resume."""
    takeover, _bus, _modes = _takeover()
    assert takeover.status()["active"] is False
    takeover.begin("test reason")
    status = takeover.status()
    assert status["active"] is True
    assert status["reason"] == "test reason"
    assert status["paused_seconds"] is not None
    takeover.resume()
    assert takeover.status()["active"] is False
    assert takeover.last_resume is not None
