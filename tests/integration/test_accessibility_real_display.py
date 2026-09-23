"""Real-display accessibility integration test (specification section 35).

This is an *integration* test: it starts the real AT-SPI accessibility service
and performs a bounded traversal of the live accessibility tree. It verifies
that:

* initialization against the real bus succeeds (or the test skips honestly),
* the traversal produces :class:`UIElement` observations from AT-SPI,
* geometry, when present, is DESKTOP space and self-consistent,
* no password element carries text (sections 42, 55),
* the capability verdict is one of AVAILABLE/DEGRADED with real details,
* browser context discovery runs against the same service without error.

It asserts structural facts only -- never a specific element or count -- because
the content of a live desktop is not deterministic.
"""

from __future__ import annotations

import os

import pytest

from config.settings import AccessibilitySettings
from core.accessibility import A11Y_BACKEND_NAME, AccessibilityService
from core.browser_accessibility import BrowserAccessibility
from schemas.enums import CapabilityStatus, CoordinateSpace, PerceptionSource
from schemas.errors import BlaxcyError

pytestmark = pytest.mark.skipif(
    not os.environ.get("DISPLAY"),
    reason="real-display accessibility test requires an X display (DISPLAY unset)",
)


def _service() -> AccessibilityService:
    """A started real accessibility service, or a skip when AT-SPI is unavailable."""
    service = AccessibilityService(AccessibilitySettings())
    try:
        service.start()
    except BlaxcyError as exc:
        pytest.skip(f"live AT-SPI accessibility is unavailable: {exc.message}")
    return service


def test_live_traversal_produces_atspi_elements() -> None:
    """Section 35: a real traversal yields UIElement observations from AT-SPI."""
    service = _service()
    try:
        elements = service.refresh()
    finally:
        service.stop()

    assert isinstance(elements, list)
    for element in elements:
        assert element.source is PerceptionSource.ATSPI
        assert 0.0 <= element.confidence <= 1.0
        if element.bbox is not None:
            assert element.bbox.space is CoordinateSpace.DESKTOP
            assert element.bbox.width > 0.0
            assert element.bbox.height > 0.0


def test_live_password_elements_never_carry_text() -> None:
    """Sections 42/55: credential content is never read into an element."""
    service = _service()
    try:
        elements = service.refresh()
    finally:
        service.stop()

    for element in elements:
        if element.password:
            assert element.text is None


def test_live_capability_is_available_or_degraded_with_evidence() -> None:
    """Section 28: the capability verdict carries the backend and real details."""
    service = _service()
    try:
        service.refresh()
        capability = service.capability()
    finally:
        service.stop()

    assert capability.status in (CapabilityStatus.AVAILABLE, CapabilityStatus.DEGRADED)
    assert capability.backend == A11Y_BACKEND_NAME
    assert "refreshes" in capability.details


def test_browser_contexts_run_against_the_live_service() -> None:
    """Section 36: browser discovery consumes the live service without error."""
    service = _service()
    try:
        contexts = BrowserAccessibility(service).contexts(force=True)
    finally:
        service.stop()

    assert isinstance(contexts, list)
    for context in contexts:
        # A discovered context must name a browser and an honest URL source.
        assert context.app_name
        assert context.url_source is not None
