"""Accessibility perception: threading, caching, bounding, honesty (section 35).

The dedicated AT-SPI thread and the element cache are exercised through an
injected :class:`AccessibilityBackend`, so the real service logic -- cache TTL,
request marshalling, timeout accounting, capability verdicts -- is tested without
depending on a live accessibility bus. The AT-SPI *mapping* logic is exercised
against a scripted node so the password-content invariant and the navigation-field
read are verified at the accessibility layer, not just in the schema.

A separate integration test runs the real traversal against the live bus.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

from config.settings import AccessibilitySettings
from core.accessibility import (
    A11Y_BACKEND_NAME,
    AccessibilityService,
    AtspiBackend,
    NodeBlacklist,
    map_role_name,
)
from schemas.elements import UIElement
from schemas.enums import (
    CapabilityStatus,
    CoordinateSpace,
    ErrorCode,
    PerceptionSource,
    UIRole,
)
from schemas.errors import BlaxcyError
from schemas.geometry import Rect

_BBOX = Rect(x=10.0, y=20.0, width=80.0, height=24.0, space=CoordinateSpace.DESKTOP)


def _element(
    element_id: str = "e1",
    role: UIRole = UIRole.BUTTON,
    *,
    text: str | None = None,
    name: str | None = None,
    app: str | None = None,
    password: bool = False,
    bbox: Rect | None = _BBOX,
) -> UIElement:
    """Build a minimal AT-SPI element for service tests."""
    return UIElement(
        element_id=element_id,
        role=role,
        text=text,
        accessible_name=name,
        bbox=bbox,
        center=bbox.center if bbox is not None else None,
        coordinate_space=CoordinateSpace.DESKTOP if bbox is not None else None,
        password=password,
        owner_app=app,
        source=PerceptionSource.ATSPI,
        confidence=0.9,
    )


class ScriptedBackend:
    """A scripted :class:`AccessibilityBackend` for deterministic service tests."""

    def __init__(
        self,
        elements: list[UIElement] | None = None,
        *,
        fail_init: bool = False,
        init_delay: float = 0.0,
        enumerate_delay: float = 0.0,
        fail_enumerate: bool = False,
        events: int = 0,
    ) -> None:
        self.elements = list(elements or [])
        self.fail_init = fail_init
        self.init_delay = init_delay
        self.enumerate_delay = enumerate_delay
        self.fail_enumerate = fail_enumerate
        self.enumerate_calls = 0
        self.query_calls = 0
        self.queried_roles: tuple[UIRole, ...] = ()
        self.initialized = False
        self.shutdown_called = False
        self._events_remaining = events
        self.events_consumed = 0

    def initialize(self) -> None:
        if self.init_delay:
            time.sleep(self.init_delay)
        if self.fail_init:
            raise RuntimeError("synthetic AT-SPI init failure")
        self.initialized = True

    def shutdown(self) -> None:
        self.shutdown_called = True

    def enumerate_elements(self) -> list[UIElement]:
        self.enumerate_calls += 1
        if self.fail_enumerate:
            raise RuntimeError("synthetic enumerate failure")
        if self.enumerate_delay:
            time.sleep(self.enumerate_delay)
        return list(self.elements)

    def query_roles(self, roles: Any) -> list[UIElement]:
        self.query_calls += 1
        self.queried_roles = tuple(roles)
        wanted = frozenset(roles)
        return [e for e in self.elements if e.role in wanted]

    def poll_events(self) -> bool:
        if self._events_remaining > 0:
            self._events_remaining -= 1
            self.events_consumed += 1
            return True
        return False

    def diagnostics(self) -> dict[str, Any]:
        return {"synthetic": True, "enumerate_calls": self.enumerate_calls}


def _service(backend: ScriptedBackend, **overrides: Any) -> AccessibilityService:
    """Build a service bound to one scripted backend instance."""
    settings = AccessibilitySettings(**overrides)
    return AccessibilityService(settings, backend_factory=lambda: backend)


def _wait_until(predicate: Any, *, timeout: float = 1.0) -> bool:
    """Poll ``predicate`` until it is true or the timeout elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def _is_running(service: AccessibilityService) -> bool:
    """Read the running flag through a function boundary.

    This keeps mypy from carrying an earlier in-block ``assert ... is True``
    narrowing of the property past a later method call, which would make the
    subsequent ``is False`` assertion look unreachable.
    """
    return service.is_running


# ---------------------------------------------------------------------------
# Role mapping.
# ---------------------------------------------------------------------------


def test_map_role_name_maps_known_names_and_unknown_to_unknown() -> None:
    """Section 37: AT-SPI role names map to the BLAXCY vocabulary, never guessed."""
    assert map_role_name("push button") is UIRole.BUTTON
    assert map_role_name("  Entry ") is UIRole.TEXT_INPUT
    assert map_role_name("password text") is UIRole.PASSWORD_INPUT
    assert map_role_name("page tab") is UIRole.TAB
    assert map_role_name("not a real role") is UIRole.UNKNOWN


# ---------------------------------------------------------------------------
# Blacklist.
# ---------------------------------------------------------------------------


def test_blacklist_blocks_after_threshold_then_expires() -> None:
    """Section 35: a repeatedly-hanging path is quarantined, then retried."""
    blacklist = NodeBlacklist(threshold=3, cooldown_seconds=30.0)
    for _ in range(3):
        blacklist.record_failure("root/1", now=0.0)
    assert blacklist.is_blocked("root/1", now=0.0) is True
    assert blacklist.is_blocked("root/1", now=29.9) is True
    # After the cooldown it is attempted again.
    assert blacklist.is_blocked("root/1", now=31.0) is False


def test_blocked_paths_lists_currently_blocked_paths() -> None:
    """The diagnostics property reports the paths currently inside their cooldown."""
    blacklist = NodeBlacklist(threshold=1, cooldown_seconds=30.0)
    blacklist.record_failure("root/1")
    blacklist.record_failure("root/2")
    assert set(blacklist.blocked_paths) == {"root/1", "root/2"}


def test_blocked_paths_does_not_raise_when_entries_expire_mid_iteration() -> None:
    """Regression: entries expiring during iteration must not mutate the mapping.

    ``is_blocked`` removes elapsed entries, so ``blocked_paths`` has to iterate a
    snapshot; a zero cooldown makes every entry expire on the read.
    """
    blacklist = NodeBlacklist(threshold=1, cooldown_seconds=0.0)
    blacklist.record_failure("root/1")
    blacklist.record_failure("root/2")
    assert blacklist.blocked_paths == ()


def test_blacklist_success_clears_failure_history() -> None:
    """A path that answers is no longer a blacklist candidate."""
    blacklist = NodeBlacklist(threshold=2, cooldown_seconds=30.0)
    blacklist.record_failure("root/2", now=0.0)
    blacklist.record_success("root/2")
    blacklist.record_failure("root/2", now=0.0)
    # Only one failure after the reset, so it is still below the threshold.
    assert blacklist.is_blocked("root/2", now=0.0) is False


# ---------------------------------------------------------------------------
# AT-SPI node mapping (scripted node).
# ---------------------------------------------------------------------------


class _FakeStateType:
    SHOWING = "SHOWING"
    VISIBLE = "VISIBLE"
    ENABLED = "ENABLED"
    SENSITIVE = "SENSITIVE"
    FOCUSABLE = "FOCUSABLE"
    FOCUSED = "FOCUSED"
    EDITABLE = "EDITABLE"
    DEFUNCT = "DEFUNCT"


class _FakeAtspi:
    StateType = _FakeStateType
    CoordType = SimpleNamespace(SCREEN="SCREEN")


class _FakeStateSet:
    def __init__(self, names: tuple[str, ...]) -> None:
        self._names = set(names)

    def contains(self, member: Any) -> bool:
        return member in self._names


class _Extents:
    def __init__(self, x: int, y: int, width: int, height: int) -> None:
        self.x = x
        self.y = y
        self.width = width
        self.height = height


class FakeNode:
    """A minimal AT-SPI accessible exposing only what ``_observe`` reads."""

    def __init__(
        self,
        role_name: str,
        name: str = "",
        *,
        states: tuple[str, ...] = ("SHOWING", "ENABLED"),
        actions: tuple[str, ...] = (),
        extents: tuple[int, int, int, int] | None = (10, 20, 80, 24),
        is_text: bool = False,
        text: str = "",
    ) -> None:
        self.role_name = role_name
        self.name = name
        self._states = states
        self._actions = actions
        self._extents = extents
        self._is_text = is_text
        self._text = text

    def get_role_name(self) -> str:
        return self.role_name

    def get_name(self) -> str:
        return self.name

    def get_state_set(self) -> _FakeStateSet:
        return _FakeStateSet(self._states)

    def get_n_actions(self) -> int:
        return len(self._actions)

    def get_action_name(self, index: int) -> str:
        return self._actions[index]

    def get_extents(self, coord: Any) -> _Extents | None:
        if self._extents is None:
            return None
        return _Extents(*self._extents)

    def is_text(self) -> bool:
        return self._is_text

    def get_text(self, start: int, end: int) -> str:
        return self._text


def _backend() -> AtspiBackend:
    """An AtspiBackend whose AT-SPI module is a scripted fake."""
    backend = AtspiBackend(AccessibilitySettings())
    backend._atspi = _FakeAtspi()
    return backend


def test_password_node_content_is_never_read() -> None:
    """Sections 42/55: a password field is reported with text=None and password=True."""
    node = FakeNode("password text", "Password", is_text=True, text="hunter2")
    element, _window, _app = _backend()._observe(node, "root/0", None, None)
    assert element is not None
    assert element.role is UIRole.PASSWORD_INPUT
    assert element.password is True
    assert element.text is None


def test_navigation_field_url_is_read_but_a_search_box_is_not() -> None:
    """Section 36: only an address/location bar has its editable text read."""
    nav = FakeNode(
        "entry", "Address and search bar", is_text=True, text="https://example.com"
    )
    search = FakeNode("entry", "Search", is_text=True, text="secret query")

    nav_element, *_ = _backend()._observe(nav, "root/0", None, None)
    search_element, *_ = _backend()._observe(search, "root/1", None, None)

    assert nav_element is not None
    assert nav_element.text == "https://example.com"
    assert search_element is not None
    assert search_element.text is None


def test_defunct_and_unreportable_nodes_are_dropped() -> None:
    """A DEFUNCT node and an anonymous unknown-role node are not emitted."""
    defunct = FakeNode("push button", "Go", states=("DEFUNCT",))
    anonymous = FakeNode("panel", "", states=("SHOWING",))

    assert _backend()._observe(defunct, "root/0", None, None)[0] is None
    assert _backend()._observe(anonymous, "root/1", None, None)[0] is None


def test_zero_sized_extents_produce_no_geometry() -> None:
    """A not-laid-out component has no usable geometry, not a bogus rectangle."""
    node = FakeNode("push button", "Go", extents=(0, 0, 0, 0))
    element, *_ = _backend()._observe(node, "root/0", None, None)
    assert element is not None
    assert element.bbox is None
    assert element.visible is False


# ---------------------------------------------------------------------------
# Service lifecycle and caching.
# ---------------------------------------------------------------------------


def test_start_initializes_the_backend_and_stop_is_idempotent() -> None:
    backend = ScriptedBackend([_element()])
    service = _service(backend)
    service.start()
    assert _is_running(service) is True
    assert backend.initialized is True
    service.stop()
    service.stop()
    assert _is_running(service) is False
    assert backend.shutdown_called is True


def test_start_failure_is_reported_as_backend_unavailable() -> None:
    backend = ScriptedBackend(fail_init=True)
    service = _service(backend)
    try:
        service.start()
    except BlaxcyError as exc:
        assert exc.code is ErrorCode.BACKEND_UNAVAILABLE
    else:  # pragma: no cover - defensive
        raise AssertionError("start() should have raised on init failure")
    assert service.capability().status is CapabilityStatus.UNAVAILABLE
    assert service.capability().fix_hint


def test_start_times_out_when_initialization_is_slow() -> None:
    backend = ScriptedBackend(init_delay=0.4)
    service = _service(backend, startup_timeout_ms=1)
    try:
        service.start()
    except BlaxcyError as exc:
        assert exc.code is ErrorCode.A11Y_TIMEOUT
    else:  # pragma: no cover - defensive
        raise AssertionError("start() should have raised on startup timeout")
    service.stop()


def test_elements_reuses_the_cache_within_its_ttl() -> None:
    """Section 35: the cache exists to avoid redundant traversals within its TTL."""
    backend = ScriptedBackend([_element()])
    service = _service(backend)
    service.start()
    try:
        first = service.elements()
        second = service.elements()
        assert backend.enumerate_calls == 1
        assert [e.element_id for e in first] == [e.element_id for e in second]
    finally:
        service.stop()


def test_refresh_forces_a_live_traversal() -> None:
    backend = ScriptedBackend([_element()])
    service = _service(backend)
    service.start()
    try:
        service.elements()
        service.refresh()
        assert backend.enumerate_calls == 2
    finally:
        service.stop()


def test_invalidate_drops_the_cached_elements() -> None:
    backend = ScriptedBackend([_element()])
    service = _service(backend)
    service.start()
    try:
        service.elements()
        service.invalidate()
        service.elements()
        assert backend.enumerate_calls == 2
    finally:
        service.stop()


def test_query_roles_routes_to_the_backend() -> None:
    backend = ScriptedBackend([_element(role=UIRole.BUTTON), _element(role=UIRole.LINK)])
    service = _service(backend)
    service.start()
    try:
        found = service.query([UIRole.LINK])
        assert backend.query_calls == 1
        assert backend.queried_roles == (UIRole.LINK,)
        assert [e.role for e in found] == [UIRole.LINK]
    finally:
        service.stop()


def test_snapshot_never_populated_reports_infinite_age() -> None:
    """A never-observed cache cannot be mistaken for a fresh one."""
    service = _service(ScriptedBackend())
    elements, age = service.snapshot()
    assert elements == []
    assert age == float("inf")


def test_a_request_that_exceeds_its_timeout_raises_a11y_timeout() -> None:
    """Section 35: a hung backend yields A11Y_TIMEOUT, never a silent hang."""
    backend = ScriptedBackend([_element()], enumerate_delay=0.5)
    service = _service(backend, deadline_ms=1, method_timeout_ms=1)
    service.start()
    try:
        try:
            service.refresh()
        except BlaxcyError as exc:
            assert exc.code is ErrorCode.A11Y_TIMEOUT
        else:  # pragma: no cover - defensive
            raise AssertionError("refresh() should have timed out")
        assert service.capability().status is CapabilityStatus.DEGRADED
    finally:
        service.stop()


def test_an_observed_event_invalidates_the_cache() -> None:
    """Any AT-SPI event is evidence the tree moved, so the cache is dropped."""
    backend = ScriptedBackend([_element()], events=1)
    service = _service(backend)
    service.start()
    try:
        service.elements()
        assert backend.enumerate_calls == 1
        assert _wait_until(lambda: backend.events_consumed > 0)
        # The non-forced query now re-observes because the cache was invalidated.
        service.elements()
        assert backend.enumerate_calls == 2
        assert service.stats()["event_invalidations"] >= 1
    finally:
        service.stop()


def test_capability_is_unavailable_before_start_and_available_after() -> None:
    backend = ScriptedBackend([_element()])
    service = _service(backend)
    before = service.capability()
    assert before.status is CapabilityStatus.UNAVAILABLE
    assert before.backend == A11Y_BACKEND_NAME

    service.start()
    try:
        after = service.capability()
        assert after.status is CapabilityStatus.AVAILABLE
        assert after.backend == A11Y_BACKEND_NAME
        assert after.details["running"] is True
    finally:
        service.stop()


def test_service_context_manager_starts_and_stops() -> None:
    backend = ScriptedBackend([_element()])
    with _service(backend) as service:
        assert _is_running(service) is True
    assert _is_running(service) is False
