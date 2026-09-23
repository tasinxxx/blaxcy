"""Browser accessibility contexts: honest, probed, never fabricated (section 36).

These tests exercise the pure context-discovery logic (URL discovery order and
the "never invent a URL" rule) plus the capability verdicts, using the same
scripted-backend approach as ``test_accessibility.py`` so nothing depends on a
browser actually being installed.
"""

from __future__ import annotations

from typing import Any

from config.settings import AccessibilitySettings
from core.accessibility import AccessibilityService
from core.browser_accessibility import (
    BROWSER_APPLICATIONS,
    BrowserAccessibility,
    UrlSource,
    discover_contexts,
    is_address_bar,
    is_browser_application,
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

_HTTP = "https://example.com/page"
_BBOX = Rect(x=0.0, y=0.0, width=800.0, height=600.0, space=CoordinateSpace.DESKTOP)


def _el(
    role: UIRole,
    *,
    eid: str = "e1",
    app: str | None = None,
    name: str | None = None,
    text: str | None = None,
    url: str | None = None,
    password: bool = False,
) -> UIElement:
    """Build a minimal element for browser-context tests."""
    return UIElement(
        element_id=eid,
        role=role,
        text=text,
        accessible_name=name,
        bbox=_BBOX,
        center=_BBOX.center,
        coordinate_space=CoordinateSpace.DESKTOP,
        password=password,
        owner_app=app,
        source=PerceptionSource.ATSPI,
        confidence=0.9,
        browser_url=url,
    )


class _StaticBackend:
    """A minimal accessibility backend returning a fixed element list."""

    def __init__(self, elements: list[UIElement]) -> None:
        self.elements = list(elements)
        self.initialized = False

    def initialize(self) -> None:
        self.initialized = True

    def shutdown(self) -> None:
        return None

    def enumerate_elements(self) -> list[UIElement]:
        return list(self.elements)

    def query_roles(self, roles: Any) -> list[UIElement]:
        wanted = frozenset(roles)
        return [e for e in self.elements if e.role in wanted]

    def poll_events(self) -> bool:
        return False

    def diagnostics(self) -> dict[str, Any]:
        return {"synthetic": True}


def _browser_service(elements: list[UIElement]) -> AccessibilityService:
    return AccessibilityService(
        AccessibilitySettings(), backend_factory=lambda: _StaticBackend(elements)
    )


# ---------------------------------------------------------------------------
# Recognition.
# ---------------------------------------------------------------------------


def test_is_browser_application_is_case_insensitive_and_none_safe() -> None:
    assert is_browser_application("Firefox") is True
    assert is_browser_application("chromium-browser") is True
    assert is_browser_application("my-editor") is False
    assert is_browser_application(None) is False
    assert "firefox" in BROWSER_APPLICATIONS


def test_is_address_bar_is_narrow() -> None:
    """A page search box is not an address bar just because it is a text input."""
    assert is_address_bar(_el(UIRole.TEXT_INPUT, name="Address and search bar")) is True
    assert is_address_bar(_el(UIRole.COMBO_BOX, name="Location")) is True
    assert is_address_bar(_el(UIRole.TEXT_INPUT, name="Search")) is False
    assert is_address_bar(_el(UIRole.BUTTON, name="address button")) is False


# ---------------------------------------------------------------------------
# Context discovery and the URL-discovery order.
# ---------------------------------------------------------------------------


def test_context_from_document_attributes_is_preferred() -> None:
    """Section 36 stage 1: document attributes win over any address bar."""
    elements = [
        _el(UIRole.DOCUMENT, eid="doc", app="Firefox", name="Example", url=_HTTP),
        _el(
            UIRole.TEXT_INPUT,
            eid="bar",
            app="Firefox",
            name="Address and search bar",
            text="https://example.com/other",
        ),
    ]
    contexts = discover_contexts(elements)
    assert len(contexts) == 1
    context = contexts[0]
    assert context.app_name == "Firefox"
    assert context.page_title == "Example"
    assert context.url == _HTTP
    assert context.url_source is UrlSource.DOCUMENT_ATTRIBUTES
    assert context.document_element_id == "doc"


def test_context_falls_back_to_a_readable_address_bar() -> None:
    """Section 36 stage 2: the address bar is used when readable."""
    elements = [
        _el(UIRole.DOCUMENT, eid="doc", app="chromium", name="Example"),
        _el(
            UIRole.TEXT_INPUT,
            eid="bar",
            app="chromium",
            name="Address and search bar",
            text=_HTTP,
        ),
    ]
    context = discover_contexts(elements)[0]
    assert context.url == _HTTP
    assert context.url_source is UrlSource.ADDRESS_BAR
    assert context.address_bar_element_id == "bar"


def test_no_discoverable_url_is_reported_as_none_never_fabricated() -> None:
    """When neither stage succeeds the URL is None with honest notes."""
    elements = [_el(UIRole.DOCUMENT, eid="doc", app="Firefox", name="Example")]
    context = discover_contexts(elements)[0]
    assert context.url is None
    assert context.url_source is UrlSource.NONE
    assert context.page_title == "Example"
    assert context.notes
    assert any("URL could not be discovered" in note for note in context.notes)


def test_address_bar_present_but_unreadable_is_recorded_in_notes() -> None:
    """A located-but-unreadable address bar is a limitation, not an observation."""
    elements = [
        _el(UIRole.DOCUMENT, eid="doc", app="Firefox", name="Example"),
        _el(UIRole.TEXT_INPUT, eid="bar", app="Firefox", name="Address and search bar"),
    ]
    context = discover_contexts(elements)[0]
    assert context.url is None
    assert any("identified but its text was not readable" in note for note in context.notes)
    assert context.address_bar_element_id == "bar"


def test_non_browser_applications_produce_no_context() -> None:
    elements = [_el(UIRole.DOCUMENT, eid="doc", app="my-editor", name="Untitled")]
    assert discover_contexts(elements) == []


def test_multiple_browsers_are_grouped_separately() -> None:
    elements = [
        _el(UIRole.DOCUMENT, eid="d1", app="Firefox", name="One", url="https://a.test"),
        _el(UIRole.DOCUMENT, eid="d2", app="chromium", name="Two", url="https://b.test"),
    ]
    contexts = discover_contexts(elements)
    assert [c.app_name for c in contexts] == ["Firefox", "chromium"]
    assert [c.url for c in contexts] == ["https://a.test", "https://b.test"]


# ---------------------------------------------------------------------------
# BrowserAccessibility service wrapper and capability verdicts.
# ---------------------------------------------------------------------------


def test_contexts_requires_a_running_accessibility_service() -> None:
    service = _browser_service([])
    try:
        BrowserAccessibility(service).contexts()
    except BlaxcyError as exc:
        assert exc.code is ErrorCode.BACKEND_UNAVAILABLE
        assert exc.details.get("fix_hint")
    else:  # pragma: no cover - defensive
        raise AssertionError("contexts() should require a running service")


def test_capability_is_available_when_a_page_url_was_discovered() -> None:
    service = _browser_service(
        [_el(UIRole.DOCUMENT, eid="doc", app="Firefox", name="Example", url=_HTTP)]
    )
    service.start()
    try:
        cap = BrowserAccessibility(service).capability()
    finally:
        service.stop()
    assert cap.status is CapabilityStatus.AVAILABLE
    assert cap.backend == "atspi"
    assert cap.details["browser_count"] == 1


def test_capability_is_degraded_when_a_tree_exists_but_no_url_is_discoverable() -> None:
    service = _browser_service(
        [_el(UIRole.DOCUMENT, eid="doc", app="Firefox", name="Example")]
    )
    service.start()
    try:
        cap = BrowserAccessibility(service).capability()
    finally:
        service.stop()
    assert cap.status is CapabilityStatus.DEGRADED
    assert cap.reason
    assert cap.fix_hint


def test_capability_is_unavailable_when_no_browser_tree_is_exposed() -> None:
    service = _browser_service([])
    service.start()
    try:
        cap = BrowserAccessibility(service).capability()
    finally:
        service.stop()
    assert cap.status is CapabilityStatus.UNAVAILABLE
    assert cap.reason
