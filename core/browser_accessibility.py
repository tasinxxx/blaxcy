"""Browser accessibility perception (specification section 36).

Browsers are the one application class where a *document* has its own meaning --
a URL, a page title, a DOM -- on top of the ordinary widget tree. This module
turns the accessibility elements BLAXCY already observes into browser contexts,
honestly reporting how much of the page it could actually see.

Two hard rules come straight from the specification:

* **Never assume DOM access.** Whether a browser exposes a usable accessibility
  tree (and a document URL) is *probed at runtime* from the live tree; it is not
  inferred from the browser's name. A browser that exposes nothing yields no
  context, not an invented one.
* **Never mutate the browser.** This module never relaunches a browser, never
  writes browser configuration, and never touches profile data. If the tree is
  not there, the answer is ``UNAVAILABLE``/``DEGRADED`` with a reason.

URL discovery follows the section 36 order: browser accessibility/document
attributes first, then the address bar, then visible browser UI, then the OCR
fallback. The OCR engine now exists (Phase 5, `core/ocr.py`) but this browser
layer is not yet wired to it, so that stage is reported as
unavailable rather than silently skipped.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from core.accessibility import NAVIGATION_FIELD_HINTS, AccessibilityService
from schemas.capability import Capability
from schemas.elements import UIElement
from schemas.enums import CapabilityName, CapabilityStatus, ErrorCode, UIRole
from schemas.errors import BlaxcyError

#: Browser application names (lower-cased, as AT-SPI reports them) whose trees
#: this module understands. Recognition is by name *and* by an actually observed
#: tree; a name match alone proves nothing.
BROWSER_APPLICATIONS: frozenset[str] = frozenset(
    {
        "firefox",
        "firefox-esr",
        "firefox developeredition",
        "chromium",
        "chromium-browser",
        "google-chrome",
        "google-chrome-stable",
        "brave-browser",
        "brave",
        "opera",
        "vivaldi",
        "microsoft-edge",
        "epiphany",
        "gnome-web",
        "midori",
    }
)

#: Roles a browser document can carry.
_DOCUMENT_ROLES: frozenset[UIRole] = frozenset({UIRole.DOCUMENT})

#: Roles that can host an address/location bar.
_ADDRESS_BAR_ROLES: frozenset[UIRole] = frozenset({UIRole.TEXT_INPUT, UIRole.COMBO_BOX})


class UrlSource(StrEnum):
    """Where a browser context's URL actually came from (section 36 order)."""

    DOCUMENT_ATTRIBUTES = "document_attributes"
    ADDRESS_BAR = "address_bar"
    WINDOW_TITLE = "window_title"
    OCR = "ocr"
    NONE = "none"


class BrowserContext(BaseModel):
    """What BLAXCY could observe about one browser application's current page."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    app_name: str = Field(min_length=1)
    page_title: str | None = Field(
        default=None, description="The document's accessible name, when it exposes one."
    )
    url: str | None = Field(
        default=None, description="The discovered page URL, or None when not discoverable."
    )
    url_source: UrlSource = UrlSource.NONE
    document_element_id: str | None = None
    address_bar_element_id: str | None = None
    notes: tuple[str, ...] = Field(
        default=(), description="Honest limitations encountered while observing this browser."
    )


def is_browser_application(name: str | None) -> bool:
    """True when ``name`` is a known browser application name."""
    if not name:
        return False
    return name.strip().casefold() in BROWSER_APPLICATIONS


def is_address_bar(element: UIElement) -> bool:
    """True when an element looks like a browser address/location bar.

    The check is deliberately narrow: the role must be able to host a location
    bar *and* the accessible name must carry a navigation hint. A page's own
    search box is not an address bar, so it is not treated as one.
    """
    if element.role not in _ADDRESS_BAR_ROLES:
        return False
    name = (element.accessible_name or element.text or "").casefold()
    return any(hint in name for hint in NAVIGATION_FIELD_HINTS)


def discover_contexts(elements: Sequence[UIElement]) -> list[BrowserContext]:
    """Derive one :class:`BrowserContext` per observed browser application.

    The URL is discovered in the section 36 order. When only a weaker stage
    succeeds, the context records which stage it was and what it could not do,
    rather than presenting a guess as an observation.
    """
    grouped: dict[str, list[UIElement]] = {}
    for element in elements:
        if is_browser_application(element.owner_app):
            grouped.setdefault(element.owner_app or "", []).append(element)

    contexts: list[BrowserContext] = []
    for app_name in sorted(grouped):
        contexts.append(_context_for(app_name, grouped[app_name]))
    return contexts


def _context_for(app_name: str, elements: list[UIElement]) -> BrowserContext:
    """Build the context for one browser from its observed elements."""
    document = next((e for e in elements if e.role in _DOCUMENT_ROLES), None)
    page_title = document.accessible_name if document is not None else None

    # Stage 1: document accessibility attributes (the strongest evidence).
    if document is not None and document.browser_url:
        return BrowserContext(
            app_name=app_name,
            page_title=page_title,
            url=document.browser_url,
            url_source=UrlSource.DOCUMENT_ATTRIBUTES,
            document_element_id=document.element_id,
        )

    # Stage 2: the address bar, when its text could actually be read.
    address_bar = next((e for e in elements if is_address_bar(e)), None)
    if address_bar is not None and address_bar.text:
        return BrowserContext(
            app_name=app_name,
            page_title=page_title,
            url=address_bar.text,
            url_source=UrlSource.ADDRESS_BAR,
            document_element_id=document.element_id if document is not None else None,
            address_bar_element_id=address_bar.element_id,
        )

    # Stage 3 (visible browser UI) yields a title, not a URL; stage 4 (OCR) is
    # not yet wired into this layer (the OCR engine itself exists as of Phase 5).
    # Report what is true instead of inventing a URL from a title.
    notes = ["the page URL could not be discovered from document attributes or the address bar"]
    if address_bar is not None:
        notes.append("an address bar was identified but its text was not readable")
    notes.append("the OCR address-bar fallback engine exists (Phase 5) but is not yet wired here")
    return BrowserContext(
        app_name=app_name,
        page_title=page_title,
        url=None,
        url_source=UrlSource.NONE,
        document_element_id=document.element_id if document is not None else None,
        address_bar_element_id=address_bar.element_id if address_bar is not None else None,
        notes=tuple(notes),
    )


class BrowserAccessibility:
    """Browser-context perception layered on the AT-SPI accessibility service.

    It performs no AT-SPI calls of its own: it reads the elements the
    accessibility service already observed, so the dedicated-thread rule
    (section 35) is never violated and there is exactly one accessibility owner.
    """

    def __init__(self, service: AccessibilityService) -> None:
        self._service = service

    def contexts(self, *, force: bool = False) -> list[BrowserContext]:
        """Observe the current browser contexts.

        Raises:
            BlaxcyError: ``A11Y_TIMEOUT``/``BACKEND_UNAVAILABLE`` when the
                accessibility service cannot produce an observation.
        """
        if not self._service.is_running:
            raise BlaxcyError(
                ErrorCode.BACKEND_UNAVAILABLE,
                "browser accessibility requires the AT-SPI accessibility service, which is not running",
                details={"fix_hint": "start AccessibilityService and ensure at-spi2-core is running"},
            )
        elements = self._service.elements(force=force)
        return discover_contexts(elements)

    def capability(self) -> Capability:
        """Report browser-accessibility availability with its evidence."""
        base_details: dict[str, object] = {"supported_browsers": sorted(BROWSER_APPLICATIONS)}
        try:
            contexts = self.contexts()
        except BlaxcyError as exc:
            return Capability(
                name=CapabilityName.BROWSER_ACCESSIBILITY,
                status=CapabilityStatus.UNAVAILABLE,
                backend="atspi",
                details={**base_details, "error": exc.code.value},
                reason=exc.message,
                fix_hint=exc.details.get("fix_hint") if exc.details else None,
            )

        details: dict[str, object] = {
            **base_details,
            "browser_count": len(contexts),
            "url_sources": sorted({c.url_source.value for c in contexts}),
        }
        if not contexts:
            return Capability(
                name=CapabilityName.BROWSER_ACCESSIBILITY,
                status=CapabilityStatus.UNAVAILABLE,
                backend="atspi",
                details=details,
                reason="no supported browser is currently exposing an accessibility tree",
                fix_hint="open a supported browser (Firefox/Chromium) with accessibility enabled",
            )
        if all(context.url for context in contexts):
            return Capability(
                name=CapabilityName.BROWSER_ACCESSIBILITY,
                status=CapabilityStatus.AVAILABLE,
                backend="atspi",
                details=details,
            )
        return Capability(
            name=CapabilityName.BROWSER_ACCESSIBILITY,
            status=CapabilityStatus.DEGRADED,
            backend="atspi",
            details=details,
            reason=(
                "a browser accessibility tree is present but the page URL could not be "
                "discovered (the OCR address-bar fallback engine exists as of Phase 5 "
                "but is not yet wired into this layer)"
            ),
            fix_hint="enable a browser accessibility integration that exposes document attributes",
        )
