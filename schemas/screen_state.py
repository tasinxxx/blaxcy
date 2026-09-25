"""Perception state and change deltas (specification sections 32, 34, 45, 65).

Every perception observation is stamped with ``frame_id``, ``state_version``,
``generation`` and a monotonic clock reading. Those stamps are what make the
staleness rules enforceable:

* ``incoming.frame_id <= current.frame_id``  -> stale, reject
* ``incoming.generation < current.generation`` -> stale, reject
* state older than ``max_action_state_age_ms`` -> re-perceive before acting

A ``ScreenState`` is an immutable snapshot. Nothing mutates one in place; a
new observation produces a new snapshot, so a stale snapshot can never be
mistaken for current reality.
"""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from schemas.elements import UIElement
from schemas.enums import CHANGE_CLASS_SEVERITY, ChangeClass, CoordinateSpace
from schemas.geometry import MonitorLayout, Rect, Size


class WindowStackEntry(BaseModel):
    """One top-level window's identity, in stacking order (bottom to top).

    The section 46 occlusion rule has to know which window an element belongs to
    and where that window sits. Identity is carried as the two things a window can
    be matched on: the **pid** that created it, which both the accessibility tree
    and EWMH report exactly, and its **title**, which is only a heuristic (a browser
    publishes a decorated title to the window manager and a differently decorated
    one to accessibility) and is therefore used as a fallback.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    window_id: int
    title: str | None = None
    pid: int | None = None


class ChangeRegion(BaseModel):
    """One changed region and how structural the change was (section 34)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rect: Rect
    change_class: ChangeClass


class ScreenDelta(BaseModel):
    """The classified difference between two consecutive frames."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    from_frame_id: int = Field(ge=0)
    to_frame_id: int = Field(ge=0)
    from_state_version: int = Field(ge=0)
    to_state_version: int = Field(ge=0)
    generation: int = Field(ge=0)
    regions: tuple[ChangeRegion, ...] = ()
    computed_at: float = Field(description="Unix timestamp (seconds) the delta was computed.")

    @model_validator(mode="after")
    def _validate_ordering(self) -> ScreenDelta:
        if self.to_frame_id < self.from_frame_id:
            raise ValueError("to_frame_id must not precede from_frame_id")
        if self.to_state_version < self.from_state_version:
            raise ValueError("to_state_version must not precede from_state_version")
        return self

    @property
    def change_class(self) -> ChangeClass:
        """The most severe class among the regions (``NONE`` when empty)."""
        if not self.regions:
            return ChangeClass.NONE
        return max((region.change_class for region in self.regions), key=lambda c: CHANGE_CLASS_SEVERITY[c])

    @property
    def is_structural(self) -> bool:
        """True for ``MEANINGFUL``/``MAJOR`` changes, which invalidate leases."""
        return CHANGE_CLASS_SEVERITY[self.change_class] >= CHANGE_CLASS_SEVERITY[ChangeClass.MEANINGFUL]

    def affects(self, rect: Rect) -> bool:
        """True when any changed region overlaps ``rect`` (section 33.2)."""
        return any(region.rect.intersects(rect) for region in self.regions)


class ScreenState(BaseModel):
    """An immutable, versioned observation of the desktop."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    frame_id: int = Field(ge=0)
    state_version: int = Field(ge=0)
    generation: int = Field(ge=0)
    timestamp: float = Field(description="Unix timestamp (seconds) at capture.")
    monotonic: float = Field(
        description="time.monotonic() at capture -- the only clock used for age checks."
    )
    layout: MonitorLayout
    coordinate_space: CoordinateSpace = CoordinateSpace.DESKTOP
    elements: tuple[UIElement, ...] = ()
    active_window_id: int | None = None
    active_app: str | None = None
    patch_hash: str | None = Field(
        default=None,
        description="xxhash of the changed patch this state was derived from, when any.",
    )
    thumbnail_size: Size | None = Field(
        default=None,
        description="Dimensions of the associated thumbnail, if one was produced.",
    )
    capture_backend: str | None = None
    window_stack: tuple[WindowStackEntry, ...] = Field(
        default=(),
        description=(
            "The top-level windows in stacking order, bottom to top, as the window "
            "manager reported them for this observation. Empty means the order is "
            "*unknown*, which the section 46 occlusion rule must treat "
            "conservatively rather than as \"nothing is above anything\"."
        ),
    )

    @model_validator(mode="after")
    def _validate_elements(self) -> ScreenState:
        ids = [element.element_id for element in self.elements]
        if len(ids) != len(set(ids)):
            raise ValueError("element_id values within a ScreenState must be unique")
        return self

    def element_by_id(self, element_id: str) -> UIElement | None:
        """Return the element with ``element_id``, or ``None``."""
        for element in self.elements:
            if element.element_id == element_id:
                return element
        return None

    def age_ms(self, now_monotonic: float | None = None) -> float:
        """Age of this snapshot in milliseconds, using the monotonic clock."""
        now = time.monotonic() if now_monotonic is None else now_monotonic
        return max(0.0, (now - self.monotonic) * 1000.0)

    def is_stale(self, max_age_ms: float, now_monotonic: float | None = None) -> bool:
        """True when this state is too old to act on (section 45/76)."""
        return self.age_ms(now_monotonic) > max_age_ms

    def is_fresh(self, max_age_ms: float, now_monotonic: float | None = None) -> bool:
        """Inverse of :meth:`is_stale`."""
        return not self.is_stale(max_age_ms, now_monotonic)

    def is_newer_than(self, current: ScreenState) -> bool:
        """Whether this state is an acceptable update over ``current``.

        Mirrors the section 32 staleness rules exactly: a frame must be
        strictly newer and must not come from an older generation.
        """
        return self.frame_id > current.frame_id and self.generation >= current.generation

    def to_model_context(self) -> dict[str, Any]:
        """A compact, delta-oriented view for the Brain (section 68.1).

        Deliberately omits full element dumps; callers attach only what the
        Brain needs to decide the next step.
        """
        return {
            "frame_id": self.frame_id,
            "state_version": self.state_version,
            "generation": self.generation,
            "active_window_id": self.active_window_id,
            "active_app": self.active_app,
            "element_count": len(self.elements),
        }


def classify_delta(
    *,
    previous: ScreenState,
    current: ScreenState,
    regions: tuple[ChangeRegion, ...],
    computed_at: float | None = None,
) -> ScreenDelta:
    """Build a :class:`ScreenDelta` from two states and their changed regions."""
    return ScreenDelta(
        from_frame_id=previous.frame_id,
        to_frame_id=current.frame_id,
        from_state_version=previous.state_version,
        to_state_version=current.state_version,
        generation=current.generation,
        regions=regions,
        computed_at=time.time() if computed_at is None else computed_at,
    )
