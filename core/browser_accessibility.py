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
fallback. All four stages are wired: the OCR fallback (stage 4) reads *only* the
identified address bar's own region -- never an unbounded scan -- and is skipped
with an honest note when no OCR engine or no frame is available, rather than
being reported as unavailable or silently dropped.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from core.accessibility import NAVIGATION_FIELD_HINTS, AccessibilityService
from schemas.capability import Capability
from schemas.elements import UIElement
from schemas.enums import CapabilityName, CapabilityStatus, ErrorCode, UIRole
from schemas.errors import BlaxcyError
from schemas.geometry import Rect


class OcrRegionReader(Protocol):
    """The one OCR method the section 36 address-bar fallback needs.

    ``core.ocr.OcrEngine`` satisfies this structurally; it is declared here so the
    browser layer does not have to import the OCR module (and its numpy/cv2
    weight) just to talk about the one method it uses.
    """

    def read_text(
        self,
        frame: Any,
        *,
        rect: Rect,
        frame_id: int | None = None,
        generation: int | None = None,
        excluded: Sequence[Rect] = (),
    ) -> str | None:
        """OCR exactly one region, returning its text or ``None``."""


#: A callable that returns the current frame for the OCR stage, or ``None`` when
#: no fresh pixels are available (in which case the OCR stage is skipped honestly).
FrameProvider = Callable[[], Any | None]


def _looks_like_url(text: str) -> bool:
    """A lenient sanity check before treating OCR text as an address-bar URL.

    An address bar also shows a placeholder ("Search or enter address") or, after
    a bad OCR read, arbitrary glyphs. Requiring a non-empty, whitespace-free
    string that contains a dot keeps a placeholder from being reported as a URL
    while still accepting ordinary ``host/path`` forms. It can reject an unusual
    URL, and when it does the URL is reported as undiscovered with a note -- an
    honest absence, never a fabricated one.
    """
    stripped = text.strip()
    if not stripped or any(character.isspace() for character in stripped):
        return False
    return "." in stripped

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


def discover_contexts(
    elements: Sequence[UIElement],
    *,
    ocr: OcrRegionReader | None = None,
    frame: Any | None = None,
) -> list[BrowserContext]:
    """Derive one :class:`BrowserContext` per observed browser application.

    The URL is discovered in the section 36 order. When only a weaker stage
    succeeds, the context records which stage it was and what it could not do,
    rather than presenting a guess as an observation.

    Args:
        elements: The elements the accessibility service already observed.
        ocr: The section 40 OCR engine, used only for the last-resort
            address-bar read (stage 4). ``None`` disables that stage (the
            default, so the stage was previously "not wired" and now is).
        frame: The current frame the OCR stage would read from. When either
            ``ocr`` or ``frame`` is ``None`` the stage is skipped and the
            context says so, rather than reporting a URL it did not read.
    """
    grouped: dict[str, list[UIElement]] = {}
    for element in elements:
        if is_browser_application(element.owner_app):
            grouped.setdefault(element.owner_app or "", []).append(element)

    contexts: list[BrowserContext] = []
    for app_name in sorted(grouped):
        contexts.append(_context_for(app_name, grouped[app_name], ocr=ocr, frame=frame))
    return contexts


def _context_for(
    app_name: str,
    elements: list[UIElement],
    *,
    ocr: OcrRegionReader | None = None,
    frame: Any | None = None,
) -> BrowserContext:
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

    # Stage 4: the OCR address-bar fallback (section 36). It reads *only* the
    # identified address bar's own region, so it cannot OCR an unbounded area; and
    # it never invents a URL -- a placeholder, an unreadable box or an unlikely
    # read leaves the URL undiscovered with a note.
    notes = ["the page URL could not be discovered from document attributes or the address bar"]
    if address_bar is not None:
        notes.append("an address bar was identified but its text was not readable")
        if ocr is not None and frame is not None and address_bar.bbox is not None:
            try:
                read = ocr.read_text(
                    frame,
                    rect=address_bar.bbox,
                    frame_id=getattr(frame, "frame_id", None),
                    generation=getattr(frame, "generation", None),
                )
            except Exception:  # pragma: no cover - defensive: OCR is best-effort
                read = None
            if read and _looks_like_url(read):
                return BrowserContext(
                    app_name=app_name,
                    page_title=page_title,
                    url=read.strip(),
                    url_source=UrlSource.OCR,
                    document_element_id=document.element_id if document is not None else None,
                    address_bar_element_id=address_bar.element_id,
                    notes=(
                        "the URL was read by OCR from the address bar; an OCR read can "
                        "mis-correct a character, so treat it as evidence, not proof",
                    ),
                )
            notes.append(
                "the OCR address-bar fallback was tried but produced no plausible URL"
            )
        elif ocr is None or frame is None:
            notes.append(
                "the OCR address-bar fallback is available but was not wired for this "
                "observation (no OCR engine or no frame)"
            )
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

    def __init__(
        self,
        service: AccessibilityService,
        *,
        ocr: OcrRegionReader | None = None,
        frame_provider: FrameProvider | None = None,
    ) -> None:
        """Wire the browser layer.

        Args:
            service: The AT-SPI owner (the single accessibility thread).
            ocr: The section 40 OCR engine for the address-bar fallback. ``None``
                (the default) leaves the OCR stage off.
            frame_provider: Supplies the current frame for the OCR stage. ``None``
                also leaves the stage off; a provider that returns ``None``
                (no fresh pixels) skips it for that observation.
        """
        self._service = service
        self._ocr = ocr
        self._frame_provider = frame_provider

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
        frame = self._frame_provider() if self._frame_provider is not None else None
        return discover_contexts(elements, ocr=self._ocr, frame=frame)

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
                "discovered (no document URL attribute and no readable address bar; the "
                "section 40 OCR address-bar fallback was unavailable or produced no "
                "plausible URL)"
            ),
            fix_hint=(
                "enable the browser's own accessibility support so the document exposes a "
                "URL, or supply the OCR engine and a frame so the address-bar fallback can run"
            ),
        )
