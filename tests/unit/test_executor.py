"""The executor pipeline (specification sections 44, 45, 47, 59, 60).

Two layers are covered: :func:`revalidate_target` on its own (so every section 45
check has a precise test) and the full pipeline over fakes (so the ordering and
the envelope shape are pinned).
"""

from __future__ import annotations

from control.executor import revalidate_target
from control.window_manager import WindowController, WindowInfo, WindowManagerProbe
from schemas.actions import PlannedAction, ToolName
from schemas.elements import ElementQuery, UIElement
from schemas.enums import CoordinateSpace, ErrorCode, PolicyMode, UIRole, VerificationState
from schemas.events import EventType
from schemas.geometry import Point, Rect
from schemas.leases import ElementLease, issue_lease
from tests.harness.phase8 import ExecutorEnv, make_delta, make_element, make_state, make_text_input

_QUERY = ElementQuery(text="Send")

#: A box far from the default one, for fixtures that need two distinct controls.
_OTHER_BOX = Rect(x=600.0, y=300.0, width=120.0, height=32.0, space=CoordinateSpace.DESKTOP)


def _lease_for(element: UIElement, **kwargs: object) -> ElementLease:
    """Issue a lease for ``element`` with the given stamps."""
    defaults: dict[str, object] = {"frame_id": 5, "state_version": 1, "generation": 1}
    defaults.update(kwargs)
    return issue_lease(element, **defaults)  # type: ignore[arg-type]


class FakeWindows(WindowController):
    """A window controller that records requests and can refuse to comply."""

    def __init__(self, active: int | None = 42, *, comply: bool = True) -> None:
        """Create the fake with an initial active window."""
        self.active = active
        self.comply = comply
        self.requested: list[int] = []

    def probe(self) -> WindowManagerProbe:
        return WindowManagerProbe(available=True, backend="fake")

    def active_window(self) -> int | None:
        return self.active

    def list_windows(self) -> tuple[WindowInfo, ...]:
        return ()

    def window_info(self, window_id: int) -> WindowInfo | None:
        return WindowInfo(window_id=window_id)

    def activate(self, window_id: int) -> bool:
        self.requested.append(window_id)
        return True

    def ensure_active(self, window_id: int, *, timeout_ms: int | None = None) -> bool:
        self.requested.append(window_id)
        if not self.comply:
            return False
        self.active = window_id
        return True


# -- revalidate_target: one test per section 45 check -------------------------

def test_revalidation_passes_for_a_fresh_unchanged_target() -> None:
    """The happy path reports exactly which checks ran."""
    element = make_element()
    state = make_state(elements=(element,), monotonic=100.0)
    lease = _lease_for(element)
    result = revalidate_target(lease, state, max_state_age_ms=1500.0, now_monotonic=100.5)
    assert result.ok is True
    assert result.element is not None
    assert result.checks["lease_valid"] is True
    assert result.checks["identity_consistent"] is True


def test_expired_lease_is_stale() -> None:
    """An expired lease is never used for input (section 44)."""
    element = make_element()
    state = make_state(elements=(element,), monotonic=100.0)
    lease = _lease_for(element, ttl_ms=10, now_monotonic=100.0)
    result = revalidate_target(lease, state, max_state_age_ms=1500.0, now_monotonic=101.0)
    assert result.ok is False
    assert result.code is ErrorCode.TARGET_STALE
    assert result.checks["lease_valid"] is False


def test_newer_generation_invalidates_the_lease() -> None:
    """A generation bump invalidates old leases (section 32)."""
    element = make_element()
    state = make_state(elements=(element,), generation=3, monotonic=100.0)
    lease = _lease_for(element, generation=2)
    result = revalidate_target(lease, state, max_state_age_ms=1500.0, now_monotonic=100.1)
    assert result.ok is False
    assert result.checks["generation_current"] is False


def test_stale_state_is_refused() -> None:
    """State older than the configured ceiling is not acted on (sections 45, 76)."""
    element = make_element()
    state = make_state(elements=(element,), monotonic=100.0)
    lease = _lease_for(element)
    result = revalidate_target(lease, state, max_state_age_ms=1500.0, now_monotonic=110.0)
    assert result.ok is False
    assert result.checks["state_fresh"] is False


def test_missing_target_is_stale() -> None:
    """A target that is gone cannot be clicked."""
    element = make_element()
    state = make_state(elements=(), monotonic=100.0)
    result = revalidate_target(_lease_for(element), state, max_state_age_ms=1500.0, now_monotonic=100.1)
    assert result.ok is False
    assert result.checks["target_present"] is False


def test_changed_identity_at_the_same_id_is_stale() -> None:
    """The same element id with a different identity is not the same control."""
    element = make_element()
    impostor = make_element(atspi_path="/p/other")
    state = make_state(elements=(impostor,), monotonic=100.0)
    result = revalidate_target(_lease_for(element), state, max_state_age_ms=1500.0, now_monotonic=100.1)
    assert result.ok is False
    assert result.checks["identity_consistent"] is False


def test_invisible_target_is_stale() -> None:
    """A target that has become invisible is not actionable."""
    element = make_element()
    hidden = make_element(visible=False)
    state = make_state(elements=(hidden,), monotonic=100.0)
    result = revalidate_target(_lease_for(element), state, max_state_age_ms=1500.0, now_monotonic=100.1)
    assert result.ok is False
    assert result.checks["visible"] is False


def test_target_without_geometry_is_stale() -> None:
    """No geometry means nothing to aim at."""
    element = make_element()
    geomless = make_element(bbox=None, center=None)
    state = make_state(elements=(geomless,), monotonic=100.0)
    result = revalidate_target(_lease_for(element), state, max_state_age_ms=1500.0, now_monotonic=100.1)
    assert result.ok is False
    assert result.checks["has_geometry"] is False


def test_offscreen_target_is_reported_offscreen() -> None:
    """A target beyond the desktop is offscreen, not merely stale."""
    element = make_element()
    offscreen = make_element(
        bbox=None, center=Point(x=5000.0, y=5000.0, space=CoordinateSpace.DESKTOP)
    )
    state = make_state(elements=(offscreen,), monotonic=100.0)
    result = revalidate_target(_lease_for(element), state, max_state_age_ms=1500.0, now_monotonic=100.1)
    assert result.ok is False
    assert result.code is ErrorCode.TARGET_OFFSCREEN


def test_occluded_target_is_refused() -> None:
    """Section 46: a covered target is not clickable."""
    element = make_element()
    occluded = make_element(occluded=True)
    state = make_state(elements=(occluded,), monotonic=100.0)
    result = revalidate_target(_lease_for(element), state, max_state_age_ms=1500.0, now_monotonic=100.1)
    assert result.ok is False
    assert result.code is ErrorCode.TARGET_OCCLUDED


def test_wrong_window_is_stale() -> None:
    """A target in a different window is not the one that was leased."""
    element = make_element()
    state = make_state(elements=(element,), monotonic=100.0)
    result = revalidate_target(
        _lease_for(element),
        state,
        max_state_age_ms=1500.0,
        expect_window_id=99,
        now_monotonic=100.1,
    )
    assert result.ok is False
    assert result.checks["window_matches"] is False


def test_small_move_is_measured_but_allowed() -> None:
    """A moved-but-intact target is revalidated with the distance reported."""
    element = make_element()
    moved = make_element(center=Point(x=210.0, y=136.0, space=CoordinateSpace.DESKTOP))
    state = make_state(elements=(moved,), monotonic=100.0)
    result = revalidate_target(
        _lease_for(element),
        state,
        max_state_age_ms=1500.0,
        previous_element=element,
        now_monotonic=100.1,
    )
    assert result.ok is True
    assert result.moved_px is not None
    assert result.moved_px > 0


# -- Pipeline -----------------------------------------------------------------

def _env(mode: PolicyMode = PolicyMode.ASSIST, **kwargs: object) -> ExecutorEnv:
    """An executor environment in ASSIST by default."""
    return ExecutorEnv(mode=mode, **kwargs)  # type: ignore[arg-type]


def test_click_happy_path_is_verified_and_leases_first() -> None:
    """A click with a meaningful change verifies, and a lease was issued."""
    element = make_element()
    state = make_state(frame_id=5, elements=(element,))
    env = _env(state=state)
    after = make_state(frame_id=6, elements=(element,))
    env.perceiver.script(after, make_delta(before=state, after=after))

    envelope = env.executor.execute(PlannedAction(tool=ToolName.CLICK, target=_QUERY))

    assert envelope.ok is True
    assert envelope.verification is VerificationState.VERIFIED
    assert envelope.data["lease"]["element_id"] == element.element_id
    assert env.cache.leases == ()  # the lease was consumed with the observation
    assert env.bus.history_of(EventType.LEASE_ISSUED)
    assert env.backend.payloads("press"), "a click must actually be injected"


def test_click_that_changes_nothing_reports_unverified_failure() -> None:
    """An unprovable outcome is a failure, not a success (sections 60, 85)."""
    element = make_element()
    env = _env(state=make_state(frame_id=5, elements=(element,)))

    envelope = env.executor.execute(PlannedAction(tool=ToolName.CLICK, target=_QUERY))

    assert envelope.ok is False
    assert envelope.error_code is ErrorCode.VERIFICATION_UNVERIFIED
    assert envelope.verification is VerificationState.UNVERIFIED


def test_ambiguous_target_never_clicks() -> None:
    """Two identical visible controls are AMBIGUOUS, never auto-selected."""
    twin_a = make_element("a")
    twin_b = make_element("b")
    env = _env(state=make_state(elements=(twin_a, twin_b)))

    envelope = env.executor.execute(PlannedAction(tool=ToolName.CLICK, target=_QUERY))

    assert envelope.ok is False
    assert envelope.error_code is ErrorCode.TARGET_AMBIGUOUS
    assert env.backend.payloads("press") == []


def test_missing_target_never_clicks() -> None:
    """An absent target is an honest NOT_FOUND with no input injected."""
    env = _env(state=make_state(elements=()))

    envelope = env.executor.execute(PlannedAction(tool=ToolName.CLICK, target=_QUERY))

    assert envelope.ok is False
    assert envelope.error_code is ErrorCode.TARGET_NOT_FOUND
    assert env.backend.events == []


def test_type_text_is_verified_from_the_readable_field() -> None:
    """Typing into a readable field verifies from the field's own content."""
    field = make_text_input()
    state = make_state(frame_id=5, elements=(field,))
    env = _env(state=state)
    after = make_state(frame_id=6, elements=(make_text_input(text="hello"),))
    env.perceiver.script(after, make_delta(before=state, after=after))

    envelope = env.executor.execute(
        PlannedAction(
            tool=ToolName.TYPE_TEXT,
            target=ElementQuery(role_hint=UIRole.TEXT_INPUT),
            params={"text": "hello"},
        )
    )

    assert envelope.ok is True
    assert envelope.verification is VerificationState.VERIFIED
    assert env.backend.payloads("key_press")


def test_drag_needs_a_destination_description() -> None:
    """A drag without a resolved drop target is refused (section 49)."""
    env = _env(state=make_state(elements=(make_element(),)))
    envelope = env.executor.execute(
        PlannedAction(tool=ToolName.DRAG, target=_QUERY), confirmed=True
    )
    assert envelope.ok is False
    assert envelope.error_code is ErrorCode.PERMISSION_DENIED
    assert env.backend.events == []


def test_drag_resolves_its_destination_live() -> None:
    """A drag performs against a freshly resolved destination."""
    source = make_element("src", text="file")
    destination = make_element("dst", text="folder", bbox=_OTHER_BOX, center=_OTHER_BOX.center)
    state = make_state(frame_id=5, elements=(source, destination))
    env = _env(state=state)
    after = make_state(frame_id=6, elements=(source, destination))
    env.perceiver.script(after, make_delta(before=state, after=after))

    envelope = env.executor.execute(
        PlannedAction(
            tool=ToolName.DRAG,
            target=ElementQuery(text="file"),
            params={"destination": {"text": "folder"}},
        ),
        confirmed=True,  # a drag is destructive, so it is confirmed explicitly
    )

    assert envelope.ok is True
    assert [event[0] for event in env.backend.events].count("press") == 1


def test_press_key_injects_a_key_and_verifies_the_consequence() -> None:
    """A key action has no target: it is verified from the observed consequence."""
    state = make_state(frame_id=5, elements=())
    env = _env(state=state)
    after = make_state(frame_id=6, elements=())
    env.perceiver.script(after, make_delta(before=state, after=after))

    envelope = env.executor.execute(PlannedAction(tool=ToolName.PRESS_KEY, params={"key": "a"}))

    assert envelope.ok is True
    assert envelope.verification is VerificationState.VERIFIED
    assert len(env.backend.payloads("key_press")) == 1
    assert "target" not in envelope.data  # there is no element involved


def test_press_key_does_not_fail_for_a_missing_target() -> None:
    """A key action must not be treated as a resolution failure."""
    env = _env(state=make_state(elements=()))
    envelope = env.executor.execute(PlannedAction(tool=ToolName.PRESS_KEY, params={"key": "a"}))
    assert envelope.error_code is not ErrorCode.TARGET_NOT_FOUND


def test_hotkey_injects_the_whole_combo() -> None:
    """A hotkey presses the modifier, the key, then releases both."""
    state = make_state(frame_id=5, elements=())
    env = _env(state=state)
    after = make_state(frame_id=6)
    env.perceiver.script(after, make_delta(before=state, after=after))

    envelope = env.executor.execute(PlannedAction(tool=ToolName.HOTKEY, params={"combo": "ctrl+c"}))

    assert envelope.ok is True
    assert len(env.backend.payloads("key_press")) == 2
    assert env.backend.held_keys == ()


def test_key_action_that_changes_nothing_is_an_unverified_failure() -> None:
    """A key press with no observable consequence is not reported as success."""
    env = _env(state=make_state(elements=()))
    envelope = env.executor.execute(PlannedAction(tool=ToolName.PRESS_KEY, params={"key": "a"}))
    assert envelope.ok is False
    assert envelope.error_code is ErrorCode.VERIFICATION_UNVERIFIED


def test_read_only_tool_never_enters_the_executor() -> None:
    """Read-only work is dispatched on the read path (section 32.1)."""
    env = _env(state=make_state(elements=(make_element(),)))
    envelope = env.executor.execute(PlannedAction(tool=ToolName.FIND_ELEMENT, target=_QUERY))
    assert envelope.ok is False
    assert envelope.error_code is ErrorCode.BACKEND_UNAVAILABLE


def test_activate_element_is_honestly_unavailable() -> None:
    """An unimplemented AT-SPI action is reported unavailable, never faked."""
    env = _env(state=make_state(elements=(make_element(),)))
    envelope = env.executor.execute(PlannedAction(tool=ToolName.ACTIVATE_ELEMENT, target=_QUERY))
    assert envelope.ok is False
    assert envelope.error_code is ErrorCode.BACKEND_UNAVAILABLE
    assert env.backend.events == []


def test_ensure_window_verifies_the_active_window() -> None:
    """Section 47 activation is confirmed by reading the active window back."""
    windows = FakeWindows(active=1)
    env = _env(state=make_state(frame_id=5, active_window_id=1), window_manager=windows)
    env.perceiver.script(make_state(frame_id=6, active_window_id=7))

    envelope = env.executor.execute(
        PlannedAction(tool=ToolName.ENSURE_WINDOW, params={"window_id": 7})
    )

    assert envelope.ok is True
    assert envelope.verification is VerificationState.VERIFIED
    assert windows.requested == [7]


def test_ensure_window_that_does_not_take_effect_is_contradicted() -> None:
    """A request that did not work is a failure, not an assumed success."""
    windows = FakeWindows(active=1, comply=False)
    env = _env(state=make_state(active_window_id=1), window_manager=windows)

    envelope = env.executor.execute(
        PlannedAction(tool=ToolName.ENSURE_WINDOW, params={"window_id": 7})
    )

    assert envelope.ok is False
    assert envelope.error_code is ErrorCode.VERIFICATION_CONTRADICTED


def test_ensure_window_without_a_manager_is_unavailable() -> None:
    """No window manager means an honest backend verdict."""
    env = _env(state=make_state())
    envelope = env.executor.execute(
        PlannedAction(tool=ToolName.ENSURE_WINDOW, params={"window_id": 7})
    )
    assert envelope.ok is False
    assert envelope.error_code is ErrorCode.BACKEND_UNAVAILABLE


def test_validation_rejects_missing_parameters() -> None:
    """The validate stage refuses an incomplete request before perceiving."""
    env = _env(state=make_state())
    envelope = env.executor.execute(PlannedAction(tool=ToolName.TYPE_TEXT, target=_QUERY, params={"text": ""}))
    assert envelope.ok is False
    assert env.backend.events == []


def test_stats_report_actions_and_the_tracker() -> None:
    """The health snapshot exposes the counters and the ownership record."""
    env = _env(state=make_state(elements=(make_element(),)))
    env.executor.execute(PlannedAction(tool=ToolName.CLICK, target=_QUERY))
    stats = env.executor.stats()
    assert stats["actions"] == 1
    assert "tracker" in stats
