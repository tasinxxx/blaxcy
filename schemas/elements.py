"""UI element model (specification sections 37, 38, 39).

This is the perception vocabulary: what BLAXCY can observe about a UI control,
where that observation came from, how much it should be trusted, and whether it
is safe to act on. Two invariants are enforced here rather than left to callers:

* A password element carries **no** text. Credential content is never read into
  model context, logged, OCR'd or uploaded (sections 42, 55), so a
  ``PASSWORD_INPUT`` role with non-``None`` text is rejected outright.
* Geometry is single-space: an element's bounding box and center must agree on
  their coordinate space, so a FRAME rectangle can never be silently compared
  against a DESKTOP one.
"""

from __future__ import annotations

from typing import Any

import xxhash
from pydantic import BaseModel, ConfigDict, Field, model_validator

from schemas.enums import (
    PASSWORD_ROLES,
    SOURCE_BASE_CONFIDENCE,
    TEXT_ENTRY_ROLES,
    CoordinateSpace,
    PerceptionSource,
    UIRole,
)
from schemas.geometry import Point, Rect


class UIElement(BaseModel):
    """One observed UI control, with provenance and geometry."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    element_id: str = Field(min_length=1)
    role: UIRole
    text: str | None = Field(
        default=None,
        description="Visible text. Must be None for password elements (section 55).",
    )
    accessible_name: str | None = None
    bbox: Rect | None = None
    center: Point | None = None
    coordinate_space: CoordinateSpace | None = Field(
        default=None,
        description="Redundant with bbox/center spaces; validated for agreement.",
    )
    clickable: bool = False
    effective_clickable: bool = Field(
        default=False,
        description="Derived clickability from real UI behaviour (section 38).",
    )
    enabled: bool = True
    focusable: bool = False
    focused: bool = False
    visible: bool = True
    occluded: bool = False
    password: bool = False
    owner_window_id: int | None = None
    #: The title of the window this element belongs to, as the accessibility tree
    #: reports it. One half of the join key between an AT-SPI element and the window
    #: manager's stacking order, which is what makes the section 46 occlusion rule
    #: answerable: only windows *above* the target can hide it (sections 46, 47).
    owner_window_title: str | None = None
    #: The process id of the application this element belongs to, from the
    #: accessibility tree. This is the *exact* half of that join key -- both AT-SPI
    #: and EWMH report the process that owns a window -- so it is preferred over the
    #: title, which is only a heuristic.
    owner_app_pid: int | None = None
    owner_app: str | None = None
    actions: tuple[str, ...] = ()
    monitor_id: int | None = Field(default=None, ge=0)
    frame_id: int | None = Field(default=None, ge=0)
    timestamp: float | None = None
    source: PerceptionSource
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    patch_hash: str | None = None
    atspi_path: str | None = None
    dom_path: str | None = None
    browser_url: str | None = None
    conflict: bool = Field(
        default=False,
        description="True when perception sources disagreed about this element.",
    )

    @model_validator(mode="after")
    def _validate_invariants(self) -> UIElement:
        if self.bbox is not None and self.center is not None and self.bbox.space is not self.center.space:
            raise ValueError("bbox and center must share a coordinate space")
        if self.coordinate_space is not None:
            for geometry in (self.bbox, self.center):
                if geometry is not None and geometry.space is not self.coordinate_space:
                    raise ValueError("coordinate_space must match bbox/center space")
        if self.role in PASSWORD_ROLES and not self.password:
            raise ValueError("a PASSWORD_INPUT element must set password=True")
        if self.password and self.text is not None:
            raise ValueError(
                "password element content must never be stored in 'text' (sections 42, 55)"
            )
        return self

    @property
    def is_password(self) -> bool:
        """True when this element holds credential-like content."""
        return self.password or self.role in PASSWORD_ROLES

    @property
    def is_text_entry(self) -> bool:
        """True when this element accepts typed text (section 51)."""
        return self.role in TEXT_ENTRY_ROLES

    @property
    def is_actionable(self) -> bool:
        """True when the element may be acted on at all (before revalidation).

        This is a *necessary* condition, not a sufficient one: occlusion,
        staleness, focus and policy are still checked per action (sections
        44-46).
        """
        return (
            self.visible
            and self.enabled
            and not self.occluded
            and (self.clickable or self.effective_clickable)
        )

    @property
    def identity(self) -> str:
        """A stable identity fingerprint for lease matching (section 44)."""
        return identity_fingerprint(self)

    def matches_identity(self, other: UIElement) -> bool:
        """Whether ``other`` is the same underlying control as this element."""
        return self.identity == other.identity

    def point_for_input(self) -> Point | None:
        """The coordinate to act on, preferring the center then the bbox center."""
        if self.center is not None:
            return self.center
        if self.bbox is not None:
            return self.bbox.center
        return None


def identity_fingerprint(element: UIElement) -> str:
    """Build a stable identity string for an element.

    Identity paths (AT-SPI / DOM) are preferred because they survive a move or
    a restyle. When neither exists, role + label + owner window is hashed --
    deliberately coarse, because a name-based identity is weaker evidence and
    the revalidation step (section 45) must treat it as such.
    """
    if element.atspi_path:
        return f"atspi:{element.atspi_path}"
    if element.dom_path:
        return f"dom:{element.dom_path}"
    label = element.accessible_name or element.text or ""
    payload = f"{element.role.value}|{label}|{element.owner_window_id}"
    return f"name:{xxhash.xxh64(payload.encode('utf-8')).hexdigest()}"


def perception_confidence(
    source: PerceptionSource,
    *,
    geometry_sanity: float = 1.0,
    freshness: float = 1.0,
    source_consistency: float = 1.0,
) -> float:
    """Compute section 39 confidence from a source base rate times multipliers.

    A resolver-cache hit or a speculative hint is never a confidence source:
    confidence is always derived from the fresh perception result itself, so
    this function only accepts the four real perception sources.
    """
    for name, value in (
        ("geometry_sanity", geometry_sanity),
        ("freshness", freshness),
        ("source_consistency", source_consistency),
    ):
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"{name} must be within [0, 1]")
    base = SOURCE_BASE_CONFIDENCE[source]
    return max(0.0, min(1.0, base * geometry_sanity * freshness * source_consistency))


class ElementQuery(BaseModel):
    """A target *description* supplied by the Brain (never a coordinate/lease)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str | None = None
    role_hint: UIRole | None = None
    context: str | None = Field(
        default=None,
        description="Free-text context (e.g. owning app or dialog) used to disambiguate.",
    )
    window_id: int | None = None
    require_actionable: bool = True

    @model_validator(mode="after")
    def _validate_query(self) -> ElementQuery:
        if not self.text and self.role_hint is None:
            raise ValueError("an element query needs at least one of: text, role_hint")
        return self

    @property
    def normalized_text(self) -> str | None:
        """Case-folded, whitespace-collapsed text for exact/normalized matching."""
        if self.text is None:
            return None
        return " ".join(self.text.split()).casefold()


class ElementCandidate(BaseModel):
    """One scored resolver candidate (section 43)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    element: UIElement
    score: float = Field(ge=0.0, le=1.0)
    breakdown: dict[str, float] = Field(default_factory=dict)

    @property
    def element_id(self) -> str:
        """Convenience accessor for the candidate's element id."""
        return self.element.element_id

    def to_dict(self) -> dict[str, Any]:
        """JSON-shaped candidate for the tool envelope sent to the Brain."""
        return {
            "element_id": self.element.element_id,
            "role": self.element.role.value,
            "text": self.element.text,
            "accessible_name": self.element.accessible_name,
            "score": self.score,
            "breakdown": self.breakdown,
            "source": self.element.source.value,
            "bbox": self.element.bbox.to_dict() if self.element.bbox is not None else None,
        }
