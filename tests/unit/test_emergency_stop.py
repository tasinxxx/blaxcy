"""Emergency stop (specification section 63).

The properties under test are the ones that make a stop a stop: it latches
before it does anything else, it releases buttons before keys, it completes the
release even when part of it fails, and it cannot be un-latched by anything
except an explicit re-arm.
"""

from __future__ import annotations

from config.settings import SafetySettings
from control.emergency_stop import (
    EmergencyStop,
    combine_abort_checks,
    release_tracked_input,
)
from core.event_bus import EventBus
from policy.modes import ModeController
from schemas.enums import ErrorCode, PolicyMode
from schemas.events import EventType


class FakeReleaser:
    """An input releaser that records call order and can fail on demand."""

    def __init__(
        self,
        *,
        buttons: tuple[str, ...] = (),
        keys: tuple[str, ...] = (),
        raise_buttons: bool = False,
        raise_keys: bool = False,
    ) -> None:
        self._buttons = list(buttons)
        self._keys = list(keys)
        self.raise_buttons = raise_buttons
        self.raise_keys = raise_keys
        self.calls: list[str] = []

    @property
    def held_buttons(self) -> tuple[str, ...]:
        return tuple(self._buttons)

    @property
    def held_keys(self) -> tuple[str, ...]:
        return tuple(self._keys)

    def release_buttons(self) -> None:
        self.calls.append("buttons")
        if self.raise_buttons:
            raise RuntimeError("button release failed")
        self._buttons.clear()

    def release_keys(self) -> None:
        self.calls.append("keys")
        if self.raise_keys:
            raise RuntimeError("key release failed")
        self._keys.clear()


def _stop(**kwargs: object) -> tuple[EmergencyStop, EventBus, ModeController]:
    """An emergency stop wired to a bus and a mode controller in ASSIST."""
    bus = EventBus()
    modes = ModeController(SafetySettings(mode=PolicyMode.ASSIST), event_bus=bus)
    modes.set_mode(PolicyMode.AUTONOMOUS)
    estop = EmergencyStop(event_bus=bus, mode_controller=modes, **kwargs)  # type: ignore[arg-type]
    return estop, bus, modes


# -- Latching and the step sequence -------------------------------------------

def test_trigger_latches_and_runs_every_section_63_step() -> None:
    """The full documented sequence runs, in order."""
    releaser = FakeReleaser(buttons=("left",), keys=("Control_L",))
    estop, bus, modes = _stop()
    estop.register_input(releaser)
    estop.register_cancel(lambda: None)

    result = estop.trigger("unit test")

    assert result.latched is True
    assert [name for name, _ in result.steps] == [
        "latch",
        "stop_execution",
        "release_buttons",
        "release_keys",
        "cancel_loop",
        "force_observe",
        "emit",
    ]
    assert all(ok for _, ok in result.steps)
    assert result.released_buttons == ("left",)
    assert result.released_keys == ("Control_L",)
    assert modes.mode is PolicyMode.OBSERVE
    assert len(bus.history_of(EventType.EMERGENCY_STOP)) == 1
    assert estop.latched is True


def test_abort_code_is_available_immediately_after_the_latch() -> None:
    """The executor's hook sees the stop without waiting on the release."""
    estop, _bus, _modes = _stop()
    assert estop.abort_code() is None
    estop.trigger()
    assert estop.abort_code() is ErrorCode.EMERGENCY_STOP_ACTIVE


def test_buttons_are_released_before_keys() -> None:
    """A drag ends by letting go of the button, not by dropping a modifier."""
    releaser = FakeReleaser(buttons=("left",), keys=("Shift_L",))
    estop, _bus, _modes = _stop()
    estop.register_input(releaser)
    estop.trigger()
    assert releaser.calls == ["buttons", "keys"]


def test_second_trigger_still_releases_newly_held_input() -> None:
    """Anything grabbed after the first stop must also come off."""
    releaser = FakeReleaser(buttons=("left",))
    estop, _bus, _modes = _stop()
    estop.register_input(releaser)
    estop.trigger()
    estop.trigger("second press")

    result = estop.last_result
    assert result is not None
    assert result.already_latched is True
    assert estop.trigger_count == 2
    assert releaser.calls.count("buttons") == 2


# -- Robustness --------------------------------------------------------------

def test_a_failing_releaser_does_not_stop_the_rest_of_the_release() -> None:
    """The failure mode that matters is a button left down, so cleanup finishes."""
    broken = FakeReleaser(buttons=("left",), raise_buttons=True)
    healthy = FakeReleaser(buttons=("right",), keys=("Alt_L",))
    estop, _bus, _modes = _stop()
    estop.register_input(broken)
    estop.register_input(healthy)

    result = estop.trigger()

    assert healthy.calls == ["buttons", "keys"]
    assert result.released_buttons == ("right",)
    assert result.released_keys == ("Alt_L",)
    assert result.errors
    assert result.step_ok("release_buttons") is False


def test_a_failing_key_release_does_not_stop_the_button_release() -> None:
    """Each release step is guarded independently."""
    releaser = FakeReleaser(buttons=("left",), keys=("Super_L",), raise_keys=True)
    estop, _bus, _modes = _stop()
    estop.register_input(releaser)
    result = estop.trigger()
    assert result.step_ok("release_buttons") is True
    assert result.step_ok("release_keys") is False


def test_a_failing_cancel_callback_is_recorded_not_fatal() -> None:
    """A cancel that raises is reported; the stop still completes."""
    def bad() -> None:
        raise RuntimeError("cancel failed")

    estop, bus, modes = _stop()
    estop.register_cancel(bad)
    result = estop.trigger()
    assert result.cancelled == 0
    assert result.errors
    assert modes.mode is PolicyMode.OBSERVE
    assert len(bus.history_of(EventType.EMERGENCY_STOP)) == 1


def test_registrations_are_idempotent_by_identity() -> None:
    """Wiring the same controller twice must not double the release."""
    releaser = FakeReleaser(buttons=("left",))
    estop, _bus, _modes = _stop()
    estop.register_input(releaser)
    estop.register_input(releaser)
    estop.trigger()
    assert releaser.calls == ["buttons", "keys"]


def test_release_tracked_input_never_invents_a_held_name() -> None:
    """A releaser that cannot report what it held yields no names, not fake ones."""

    class Opaque:
        def release_buttons(self) -> None:
            pass

        def release_keys(self) -> None:
            pass

    buttons, keys, errors = release_tracked_input([Opaque()])
    assert buttons == ()
    assert keys == ()
    assert errors == []


def test_latency_is_measured_and_reported() -> None:
    """The stop reports its own latency instead of assuming the target."""
    estop, _bus, _modes = _stop()
    estop.register_input(FakeReleaser(keys=("Control_L",)))
    result = estop.trigger()
    assert result.latency_ms >= 0.0
    assert estop.last_latency_ms == result.latency_ms


# -- Reset and composition ----------------------------------------------------

def test_reset_is_the_only_way_to_clear_the_latch() -> None:
    """A stop that cleared itself would not be a stop."""
    estop, _bus, _modes = _stop()
    estop.trigger()
    assert estop.latched is True
    first_reset = estop.reset()
    assert first_reset is True
    assert estop.abort_code() is None
    # A second re-arm has nothing left to clear, and must say so.
    second_reset = estop.reset()
    assert second_reset is False


def test_a_stop_without_a_bus_or_mode_controller_still_releases() -> None:
    """The mechanism works even when nothing is listening."""
    releaser = FakeReleaser(buttons=("left",), keys=("a",))
    estop = EmergencyStop()
    estop.register_input(releaser)
    result = estop.trigger()
    assert result.released_buttons == ("left",)
    assert result.forced_observe is True


def test_combine_abort_checks_returns_the_first_latched_code() -> None:
    """Emergency stop is reported ahead of takeover when both are latched."""
    estop, _bus, _modes = _stop()
    takeover_code: dict[str, ErrorCode | None] = {"value": None}

    def takeover() -> ErrorCode | None:
        return takeover_code["value"]

    combined = combine_abort_checks(estop.abort_code, takeover)
    assert combined() is None

    takeover_code["value"] = ErrorCode.HUMAN_TAKEOVER
    assert combined() is ErrorCode.HUMAN_TAKEOVER

    estop.trigger()
    assert combined is not None
    assert combined() is ErrorCode.EMERGENCY_STOP_ACTIVE
