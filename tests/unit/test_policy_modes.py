"""Policy modes and the AUTONOMOUS ceiling (specification section 56).

The behaviour that matters here is that AUTONOMOUS *expires on its own*. A
ceiling that only holds when someone remembers to poll a timer is not a ceiling,
so the mode read itself must latch the fallback to OBSERVE.
"""

from __future__ import annotations

from config.settings import SafetySettings
from core.event_bus import EventBus
from policy.modes import ModeController
from schemas.enums import PolicyMode
from schemas.events import EventType


class _Clock:
    """A controllable monotonic clock."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _controller(
    mode: PolicyMode = PolicyMode.OBSERVE,
    *,
    minutes: int = 30,
    bus: EventBus | None = None,
    clock: _Clock | None = None,
) -> tuple[ModeController, EventBus, _Clock]:
    bus = bus if bus is not None else EventBus()
    clock = clock if clock is not None else _Clock()
    settings = SafetySettings(
        mode=mode, autonomous_max_session_duration_minutes=minutes
    )
    return ModeController(settings, event_bus=bus, clock=clock), bus, clock


def test_default_mode_is_observe() -> None:
    """First launch is OBSERVE (section 56)."""
    controller, _bus, _clock = _controller()
    assert controller.mode is PolicyMode.OBSERVE
    assert controller.is_autonomous is False


def test_set_mode_emits_a_mode_changed_event() -> None:
    """Every real transition is observable on the shared bus."""
    controller, bus, _clock = _controller()
    event = controller.set_mode(PolicyMode.ASSIST, reason="test")
    assert event is not None
    assert controller.mode is PolicyMode.ASSIST
    published = bus.history_of(EventType.MODE_CHANGED)
    assert len(published) == 1
    # Typed events carry their fields as model fields, not in ``payload``.
    assert published[0].model_dump()["current_mode"] == "ASSIST"


def test_setting_the_same_mode_is_a_no_op() -> None:
    """A repeated transition emits nothing and does not touch the deadline."""
    controller, bus, _clock = _controller()
    controller.set_mode(PolicyMode.AUTONOMOUS)
    before = len(bus.history_of(EventType.MODE_CHANGED))
    assert controller.set_mode(PolicyMode.AUTONOMOUS) is None
    assert len(bus.history_of(EventType.MODE_CHANGED)) == before


def test_autonomous_reports_a_remaining_budget() -> None:
    """Entering AUTONOMOUS starts a real, positive budget."""
    controller, _bus, _clock = _controller()
    controller.set_mode(PolicyMode.AUTONOMOUS)
    remaining = controller.remaining_seconds()
    assert remaining is not None
    assert 29 * 60 < remaining <= 30 * 60


def test_autonomous_expires_on_its_own() -> None:
    """Reading the mode after the ceiling passes falls back to OBSERVE."""
    controller, bus, clock = _controller(minutes=1)
    controller.set_mode(PolicyMode.AUTONOMOUS)
    mode_before = controller.mode
    assert mode_before is PolicyMode.AUTONOMOUS

    clock.advance(61.0)

    mode_after = controller.mode
    assert mode_after is PolicyMode.OBSERVE
    assert controller.is_autonomous is False
    assert controller.remaining_seconds() is None
    events = bus.history_of(EventType.MODE_CHANGED)
    last = events[-1].model_dump()
    assert last["current_mode"] == "OBSERVE"
    assert "ceiling" in str(last["reason"])


def test_tick_applies_the_ceiling_explicitly() -> None:
    """``tick`` is the explicit form of the same expiry check."""
    controller, _bus, clock = _controller(minutes=1)
    controller.set_mode(PolicyMode.AUTONOMOUS)
    clock.advance(120.0)
    event = controller.tick()
    assert event is not None
    assert controller.mode is PolicyMode.OBSERVE


def test_force_observe_overrides_every_mode_and_is_idempotent() -> None:
    """Human takeover and emergency stop force OBSERVE (sections 62, 63)."""
    controller, bus, _clock = _controller()
    controller.set_mode(PolicyMode.AUTONOMOUS)
    controller.force_observe(reason="emergency stop")
    assert controller.mode is PolicyMode.OBSERVE

    before = len(bus.history_of(EventType.MODE_CHANGED))
    assert controller.force_observe(reason="again") is None
    assert len(bus.history_of(EventType.MODE_CHANGED)) == before


def test_autonomous_never_survives_a_new_controller() -> None:
    """A restart (a new controller) starts from the configured mode, not AUTONOMOUS."""
    controller, _bus, _clock = _controller()
    controller.set_mode(PolicyMode.AUTONOMOUS)
    restarted, _bus2, _clock2 = _controller(mode=PolicyMode.OBSERVE)
    assert restarted.mode is PolicyMode.OBSERVE


def test_status_reports_the_budget_and_forced_state() -> None:
    """The status snapshot carries what the GUI needs to show."""
    controller, _bus, _clock = _controller()
    controller.set_mode(PolicyMode.AUTONOMOUS)
    status = controller.status()
    assert status.autonomous_active is True
    assert status.autonomous_expires_in_seconds is not None
    controller.force_observe(reason="test")
    forced = controller.status()
    assert forced.forced is True
    assert forced.reason == "test"
