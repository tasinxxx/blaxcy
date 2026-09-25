"""Visual grounding -- the final perception fallback (sections 38, 39, 41, 42).

Visual grounding is the last stage of the section 43 cascade: it is asked to
answer only when the deterministic sources (AT-SPI, browser accessibility) and
OCR could not. It is also the **only** perception source that sends screen pixels
to a remote model, so every rule about it is enforced here rather than left to a
caller's judgement:

* **Final fallback only.** A caller reaches this module after a deterministic
  resolution found nothing. Visual grounding never runs first, and a VISUAL
  result can never make a deterministic ambiguity go away -- the section 43
  ambiguity rule is evaluated by the resolver over the elements it is given, and
  two visually-identical candidates are ambiguous there exactly like any others.
* **Section 41 invocation gate.** ``mode >= ASSIST`` is required *in code*. In
  OBSERVE the call is refused with ``VISUAL_FALLBACK_DENIED`` before the backend
  is touched, so a mis-wired caller cannot upload a screenshot by mistake.
* **Section 42/55 privacy gate.** The upload region is checked against every
  supplied credential element and every supplied protected region *before* the
  image is prepared. A region that overlaps one is refused outright; the crop is
  never uploaded, downscaled or encoded.
* **Section 41 geometry and confidence caps.** The image's longest side is
  bounded (default 1024), and every match's confidence is derived through
  :func:`~schemas.elements.perception_confidence` with the ``VISUAL`` base rate
  (0.80) and then clamped to the configured ceiling, which the schema itself
  refuses to raise above 0.80.
* **Revalidation is not optional.** A visual match is a *location*, not an
  authorisation. The elements produced here carry
  ``source=VISUAL`` and a real bounding box, and they still pass through the
  ordinary lease (section 44) and revalidation (section 45) pipeline before any
  input: nothing in this module injects, authorises or verifies anything.

The model transport is a pluggable :class:`VisualGroundingBackend`, exactly like
the OCR backend: the grounder owns the limits, the gates and the geometry, and a
backend only answers *where does this description appear in this image*. Tests
drive a fake backend, so the module's behaviour is verified without a network
call or an API key.
"""

from __future__ import annotations

import importlib
import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol

import cv2
import numpy as np
import numpy.typing as npt
import xxhash

from config.settings import VisualSettings
from core.frame_engine import Frame
from schemas.capability import Capability
from schemas.elements import UIElement, perception_confidence
from schemas.enums import (
    PASSWORD_ROLES,
    CapabilityName,
    CapabilityStatus,
    CoordinateSpace,
    ErrorCode,
    PerceptionSource,
    PolicyMode,
    UIRole,
)
from schemas.errors import BlaxcyError
from schemas.geometry import Rect
from security.keyring_manager import (
    KEYRING_GEMINI_KEY,
    KEYRING_SERVICE,
    ApiKeyManager,
    KeyringError,
    redact_secret,
)

#: Backend identifier reported in capability/benchmark output.
VISUAL_BACKEND_NAME: Final[str] = "google-genai"

#: Section 41 default bound on the uploaded image's longest side.
DEFAULT_MAX_IMAGE_SIDE: Final[int] = 1024

#: Section 41 confidence ceiling. A visual result may be trusted less, never more.
VISUAL_CONFIDENCE_CAP: Final[float] = 0.80

#: Modes in which section 41 permits a visual fallback.
VISUAL_MODES: Final[frozenset[PolicyMode]] = frozenset(
    {PolicyMode.ASSIST, PolicyMode.AUTONOMOUS}
)

_FIX_HINT: Final[str] = (
    "Visual grounding needs the google-genai SDK and a stored Gemini API key "
    "(python main.py keys set-gemini), and it only runs in ASSIST or AUTONOMOUS mode."
)

#: The response shape asked of the model. Coordinates are fractions of the
#: uploaded image so the answer is independent of the image's pixel size -- which
#: is what lets the grounder downscale the image and still map the answer back to
#: DESKTOP space exactly.
_LOCATE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "matches": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "confidence": {"type": "number"},
                    "x": {"type": "number"},
                    "y": {"type": "number"},
                    "width": {"type": "number"},
                    "height": {"type": "number"},
                },
                "required": ["label", "x", "y", "width", "height"],
            },
        }
    },
    "required": ["matches"],
}


@dataclass(frozen=True)
class RawVisualMatch:
    """One located control in the *uploaded image's* normalised coordinates.

    ``x``/``y`` is the box's top-left corner and ``width``/``height`` its size,
    each as a fraction of the image (0.0-1.0), origin top-left. Normalised
    coordinates are what make the answer stable across a downscale.
    """

    label: str
    confidence: float
    x: float
    y: float
    width: float
    height: float

    @property
    def is_degenerate(self) -> bool:
        """True when the box has no usable area, so it is not a real match."""
        return self.width <= 0.0 or self.height <= 0.0


@dataclass(frozen=True)
class VisualMatch:
    """One located control in DESKTOP space, with derived confidence."""

    label: str
    rect: Rect
    confidence: float
    raw_confidence: float


@dataclass(frozen=True)
class VisualGroundingResult:
    """The complete result of one grounding call (section 41)."""

    query: str
    matches: tuple[VisualMatch, ...]
    uploaded_rect: Rect
    image_width: int
    image_height: int
    downscaled: bool
    backend: str
    model: str | None
    elapsed_ms: float

    @property
    def found(self) -> bool:
        """True when the model located at least one control."""
        return bool(self.matches)

    def to_dict(self) -> dict[str, Any]:
        """JSON-shaped evidence for the tool envelope, logs and benchmarks."""
        return {
            "query": self.query,
            "found": self.found,
            "backend": self.backend,
            "model": self.model,
            "uploaded_rect": self.uploaded_rect.to_dict(),
            "image": {"width": self.image_width, "height": self.image_height},
            "downscaled": self.downscaled,
            "elapsed_ms": self.elapsed_ms,
            "matches": [
                {
                    "label": match.label,
                    "rect": match.rect.to_dict(),
                    "confidence": match.confidence,
                    "raw_confidence": match.raw_confidence,
                }
                for match in self.matches
            ],
        }


class VisualGroundingBackend(Protocol):
    """The minimal contract the grounder needs from a visual model.

    A backend answers one question -- where, in this image, does this description
    appear -- and nothing else. It performs no policy check, no downscaling, no
    coordinate mapping and no confidence derivation: those belong to the grounder,
    which is the only place they can be enforced for every backend.
    """

    name: str

    def locate(
        self,
        image: npt.NDArray[np.uint8],
        *,
        query: str,
        max_results: int,
        timeout_ms: int,
    ) -> tuple[RawVisualMatch, ...]:
        """Locate ``query`` in ``image``, returning normalised boxes."""
        ...


class GeminiVisualBackend:
    """The Gemini-backed visual model (the only backend this phase implements).

    The SDK is imported lazily and the key is read through
    :class:`~security.keyring_manager.ApiKeyManager`, so importing this module (and
    therefore BLAXCY) works with no SDK and no credential -- a missing one is the
    honest ``BACKEND_UNAVAILABLE``/``UNAVAILABLE`` rather than an ImportError at
    startup. Every error string is redacted against the API key before it becomes
    a :class:`~schemas.errors.BlaxcyError` (section 70).
    """

    name = VISUAL_BACKEND_NAME

    def __init__(
        self,
        *,
        model: str,
        client: Any = None,
        api_key: str | None = None,
        key_manager: ApiKeyManager | None = None,
        temperature: float | None = None,
    ) -> None:
        """Create the backend. No client is built until the first call."""
        self._model = model
        self._client = client
        self._api_key = api_key
        self._key_manager = key_manager if key_manager is not None else ApiKeyManager()
        self._temperature = temperature

    @property
    def model(self) -> str:
        """The configured model name (safe to log)."""
        return self._model

    def locate(
        self,
        image: npt.NDArray[np.uint8],
        *,
        query: str,
        max_results: int,
        timeout_ms: int,
    ) -> tuple[RawVisualMatch, ...]:
        """Send the image and the description, and parse the model's boxes.

        Raises:
            BlaxcyError: ``BACKEND_UNAVAILABLE`` when the SDK or the key is
                missing, ``RATE_LIMITED``/``MODEL_ERROR`` for a transport or
                response-shape failure. The image is never placed in the error.
        """
        genai, types = _import_sdk()
        client = self._client if self._client is not None else self._build_client(genai)
        encoded = _encode_png(image)
        # One image part plus the instruction, in a single user turn: the model is
        # asked to locate, never to converse.
        contents = [
            types.Content(
                role="user",
                parts=[
                    types.Part.from_bytes(data=encoded, mime_type="image/png"),
                    types.Part(text=_locate_prompt(query, max_results=max_results)),
                ],
            )
        ]
        config = self._build_config(types, timeout_ms=timeout_ms)
        try:
            response = client.models.generate_content(
                model=self._model, contents=contents, config=config
            )
        except BlaxcyError:
            raise
        except Exception as exc:
            raise self._classify(exc) from exc
        return _parse_matches(self._response_text(response))

    def _build_client(self, genai: Any) -> Any:
        """Construct the SDK client from the resolved key."""
        key = self._api_key
        if not key:
            try:
                key = self._key_manager.gemini_key()
            except KeyringError as exc:
                raise BlaxcyError(exc.code, exc.message, details=exc.details) from exc
        if not key:
            raise BlaxcyError(
                ErrorCode.BACKEND_UNAVAILABLE,
                "no Gemini API key is stored, so visual grounding cannot run",
                details={**self._key_manager.status(), "fix_hint": _FIX_HINT},
            )
        self._api_key = key
        try:
            return genai.Client(api_key=key)
        except Exception as exc:
            raise BlaxcyError(
                ErrorCode.MODEL_ERROR,
                "the Gemini client could not be constructed for visual grounding",
                details={"model": self._model, "error": self._redact(str(exc))},
            ) from exc

    def _build_config(self, types: Any, *, timeout_ms: int) -> Any:
        """The request config: strict JSON out, bounded by the caller's timeout."""
        kwargs: dict[str, Any] = {
            "response_mime_type": "application/json",
            "response_json_schema": _LOCATE_SCHEMA,
            "http_options": types.HttpOptions(timeout=timeout_ms),
        }
        if self._temperature is not None:
            kwargs["temperature"] = self._temperature
        return types.GenerateContentConfig(**kwargs)

    def _response_text(self, response: Any) -> str | None:
        """Best-effort response text, or ``None`` when the turn carried none."""
        text = getattr(response, "text", None)
        if text:
            return str(text)
        for candidate in getattr(response, "candidates", None) or ():
            content = getattr(candidate, "content", None)
            for part in getattr(content, "parts", None) or ():
                value = getattr(part, "text", None)
                if value:
                    return str(value)
        return None

    def _classify(self, exc: BaseException) -> BlaxcyError:
        """Map an SDK failure onto the taxonomy, with the key redacted."""
        message = self._redact(str(exc)) or type(exc).__name__
        lowered = message.casefold()
        code = ErrorCode.MODEL_ERROR
        if "429" in message or "resource_exhausted" in lowered or "rate limit" in lowered:
            code = ErrorCode.RATE_LIMITED
        elif "api key" in lowered or "unauthenticated" in lowered or "permission_denied" in lowered:
            code = ErrorCode.BACKEND_UNAVAILABLE
        return BlaxcyError(
            code,
            f"the visual grounding request failed: {message[:300]}",
            details={"model": self._model, "backend": VISUAL_BACKEND_NAME},
        )

    def _redact(self, text: str) -> str:
        """Remove the API key from any string before it leaves this object."""
        key = self._api_key
        if not key:
            try:
                key = self._key_manager.gemini_key()
            except KeyringError:
                key = None
        return redact_secret(text, key)


class VisualGrounder:
    """Gated, bounded visual grounding over an explicit upload region.

    Args:
        settings: The ``[visual]`` configuration (section 41 limits and caps).
        backend: The model transport. Defaults to :class:`GeminiVisualBackend`
            built lazily from the keyring, so constructing the grounder needs no
            SDK and no credential.
        model: Model name for the default backend.
        key_manager: Keyring reader used by the default backend and by
            :meth:`capability`; defaults to :class:`ApiKeyManager`.
        clock: Monotonic clock, injectable for deterministic tests.
    """

    def __init__(
        self,
        settings: VisualSettings,
        *,
        backend: VisualGroundingBackend | None = None,
        model: str | None = None,
        key_manager: ApiKeyManager | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Create a grounder; nothing is called until :meth:`ground` is."""
        self._settings = settings
        self._backend = backend
        self._model = model
        self._key_manager = key_manager if key_manager is not None else ApiKeyManager()
        self._clock = clock
        self._calls = 0
        self._refusals = 0
        self._matches = 0
        self._downscales = 0
        self._last_error: str | None = None

    @property
    def enabled(self) -> bool:
        """Whether the capability is switched on in configuration."""
        return self._settings.enabled

    @property
    def confidence_cap(self) -> float:
        """The effective section 41 ceiling: never above :data:`VISUAL_CONFIDENCE_CAP`."""
        return min(self._settings.max_confidence, VISUAL_CONFIDENCE_CAP)

    # -- The one operation ----------------------------------------------------

    def ground(
        self,
        frame: Frame,
        *,
        query: str,
        mode: PolicyMode,
        region: Rect | None = None,
        password_elements: Sequence[UIElement] = (),
        protected_regions: Sequence[Rect] = (),
        frame_id: int | None = None,
        generation: int | None = None,
        max_results: int | None = None,
    ) -> VisualGroundingResult:
        """Locate ``query`` in ``frame``, if policy and privacy allow the upload.

        Args:
            frame: The captured frame the observation came from. Its
                ``desktop_rect`` maps DESKTOP coordinates onto its pixels.
            query: The target *description* (never a coordinate). An empty
                description is refused: there is nothing to ground.
            mode: The current policy mode. ``OBSERVE`` refuses outright
                (section 41).
            region: The DESKTOP region to upload. ``None`` means the whole
                captured desktop -- which is then checked against credential and
                protected geometry in full.
            password_elements: Credential elements whose bounding boxes must never
                be uploaded (sections 42, 55). Any overlap refuses the call.
            protected_regions: Regions that must never be uploaded (for example a
                protected application's window rectangle).
            frame_id: Frame the call is against; stamped on emitted elements.
            generation: Perception generation; stamped on emitted elements.
            max_results: Per-call override of ``settings.max_results``.

        Returns:
            A :class:`VisualGroundingResult` whose matches carry DESKTOP geometry.

        Raises:
            BlaxcyError: ``VISUAL_FALLBACK_DENIED`` for a disabled, out-of-mode,
                credential or protected-context call; ``TARGET_NOT_FOUND`` for an
                empty description; ``MODEL_ERROR``/``RATE_LIMITED``/
                ``BACKEND_UNAVAILABLE`` for a transport failure.
        """
        started = self._clock()
        upload_rect = region if region is not None else frame.desktop_rect
        try:
            self._check_allowed(
                upload_rect,
                mode=mode,
                query=query,
                password_elements=password_elements,
                protected_regions=protected_regions,
            )
        except BlaxcyError:
            self._refusals += 1
            raise

        limit = self._settings.max_results if max_results is None else max(1, max_results)
        image, uploaded_rect, downscaled = self._prepare_upload(frame, upload_rect)
        backend = self._resolve_backend()
        try:
            raw = backend.locate(
                image,
                query=query,
                max_results=limit,
                timeout_ms=self._settings.model_timeout_ms,
            )
        except BlaxcyError as exc:
            self._fail(exc.message)
            raise
        except Exception as exc:  # pragma: no cover - defensive: a backend bug
            self._fail(str(exc))
            raise BlaxcyError(
                ErrorCode.MODEL_ERROR,
                f"the visual grounding backend failed unexpectedly: {type(exc).__name__}",
                details={"backend": getattr(backend, "name", "unknown")},
            ) from exc

        self._calls += 1
        if downscaled:
            self._downscales += 1
        matches = self._map_matches(raw, uploaded_rect=uploaded_rect, limit=limit)
        self._matches += len(matches)
        return VisualGroundingResult(
            query=query,
            matches=matches,
            uploaded_rect=uploaded_rect,
            image_width=int(image.shape[1]),
            image_height=int(image.shape[0]),
            downscaled=downscaled,
            backend=getattr(backend, "name", VISUAL_BACKEND_NAME),
            model=self._model,
            elapsed_ms=float((self._clock() - started) * 1000.0),
        )

    def as_elements(
        self,
        result: VisualGroundingResult,
        *,
        frame_id: int | None = None,
        generation: int | None = None,
        role: UIRole = UIRole.UNKNOWN,
        owner_app: str | None = None,
        owner_window_id: int | None = None,
    ) -> tuple[UIElement, ...]:
        """Convert located controls into typed elements with honest provenance.

        Every element is ``source=VISUAL`` in DESKTOP space, and its confidence
        is the section 39 ``VISUAL`` base rate (0.80) times the geometry/freshness
        multipliers, clamped to the configured ceiling -- always computable, never
        assumed, and never taken from the model's own self-report.

        ``role`` defaults to ``UNKNOWN`` on purpose: visual grounding proves *where*
        something is, never what it is (section 38 keeps identity and role with
        AT-SPI/DOM). A caller that searched with a role hint may pass it through,
        and the resolver's role weight plus the ordinary revalidation still apply
        before any input.

        Raises:
            ValueError: When asked to emit a credential element. Visual grounding
                is never run over credential context, so producing a
                ``PASSWORD_INPUT`` from it would be a contradiction.
        """
        if role in PASSWORD_ROLES:
            raise ValueError("visual grounding must never produce a credential element")
        elements: list[UIElement] = []
        for match in result.matches:
            elements.append(
                UIElement(
                    element_id=_match_element_id(match),
                    role=role,
                    # The label is what the *caller* asked for, echoed back; it is
                    # not read text, and it is never credential content.
                    text=None,
                    accessible_name=match.label or None,
                    bbox=match.rect,
                    center=match.rect.center,
                    coordinate_space=CoordinateSpace.DESKTOP,
                    # Section 38: visual is the last-resort clickability source.
                    # It asserts a location for a described control -- the lease
                    # and the full section 45 revalidation still precede any input.
                    clickable=True,
                    effective_clickable=False,
                    enabled=True,
                    visible=True,
                    occluded=False,
                    owner_app=owner_app,
                    owner_window_id=owner_window_id,
                    frame_id=frame_id if frame_id is not None else None,
                    timestamp=time.time(),
                    source=PerceptionSource.VISUAL,
                    confidence=match.confidence,
                )
            )
        return tuple(elements)

    # -- Capability / diagnostics --------------------------------------------

    def capability(self) -> Capability:
        """Report visual grounding honestly, with evidence (sections 28, 41, 80)."""
        limits = {
            "max_image_side": self._settings.max_image_side,
            "max_confidence": self.confidence_cap,
            "max_results": self._settings.max_results,
            "model_timeout_ms": self._settings.model_timeout_ms,
            "required_modes": sorted(mode.value for mode in VISUAL_MODES),
            "model": self._model or "default",
            # Presence of the credential, never the credential (section 69).
            **self._key_manager.status(),
        }
        if not self._settings.enabled:
            return Capability(
                name=CapabilityName.VISUAL_GROUNDING,
                status=CapabilityStatus.UNAVAILABLE,
                backend=VISUAL_BACKEND_NAME,
                reason="visual grounding is implemented but disabled by configuration",
                fix_hint="set [visual] enabled = true to use the fallback",
                details=limits,
            )
        if not _sdk_available():
            return Capability(
                name=CapabilityName.VISUAL_GROUNDING,
                status=CapabilityStatus.UNAVAILABLE,
                backend=VISUAL_BACKEND_NAME,
                reason="the google-genai SDK is not importable, so no visual model can be called",
                fix_hint="pip install google-genai",
                details=limits,
            )
        started = self._clock()
        try:
            key = self._key_manager.gemini_key()
        except KeyringError as exc:
            return Capability(
                name=CapabilityName.VISUAL_GROUNDING,
                status=CapabilityStatus.UNAVAILABLE,
                backend=VISUAL_BACKEND_NAME,
                reason=f"keyring backend is unavailable: {exc.message}",
                fix_hint="ensure a working Secret Service/keyring backend is running",
                details=limits,
            )
        if not key:
            return Capability(
                name=CapabilityName.VISUAL_GROUNDING,
                status=CapabilityStatus.UNAVAILABLE,
                backend=VISUAL_BACKEND_NAME,
                reason=(
                    "no Gemini API key stored, so visual grounding cannot call a "
                    f"visual model (keyring service={KEYRING_SERVICE!r}, "
                    f"key={KEYRING_GEMINI_KEY!r})"
                ),
                fix_hint="store the key with 'python main.py keys set-gemini'; never in config or CLI args",
                details=limits,
            )
        latency_ms = (self._clock() - started) * 1000.0
        return Capability(
            name=CapabilityName.VISUAL_GROUNDING,
            status=CapabilityStatus.AVAILABLE,
            backend=VISUAL_BACKEND_NAME,
            latency_ms=round(latency_ms, 3),
            details=limits,
        )

    def diagnostics(self) -> dict[str, Any]:
        """JSON-shaped health/performance evidence for logs and benchmarks."""
        return {
            "backend": getattr(self._backend, "name", VISUAL_BACKEND_NAME),
            "enabled": self._settings.enabled,
            "calls": self._calls,
            "refusals": self._refusals,
            "matches": self._matches,
            "downscales": self._downscales,
            "last_error": self._last_error,
            "confidence_cap": self.confidence_cap,
        }

    # -- Gates (sections 41, 42, 55) -----------------------------------------

    def _check_allowed(
        self,
        upload_rect: Rect,
        *,
        mode: PolicyMode,
        query: str,
        password_elements: Sequence[UIElement],
        protected_regions: Sequence[Rect],
    ) -> None:
        """Refuse before anything is cropped, encoded or sent.

        Order is deliberate and fail-closed: configuration, then mode (section
        41), then the request itself, then privacy (sections 42, 55). Nothing in
        this method touches the network.
        """
        if not self._settings.enabled:
            raise BlaxcyError(
                ErrorCode.VISUAL_FALLBACK_DENIED,
                "visual grounding is disabled by configuration",
                details={"enabled": False},
            )
        if mode not in VISUAL_MODES:
            raise BlaxcyError(
                ErrorCode.VISUAL_FALLBACK_DENIED,
                f"visual grounding requires mode >= ASSIST (section 41), not {mode.value}",
                details={"mode": mode.value, "required_modes": sorted(m.value for m in VISUAL_MODES)},
            )
        if not query.strip():
            raise BlaxcyError(
                ErrorCode.TARGET_NOT_FOUND,
                "visual grounding needs a non-empty target description",
                details={},
            )
        blocked = _overlapping(password_elements, upload_rect)
        if blocked:
            raise BlaxcyError(
                ErrorCode.VISUAL_FALLBACK_DENIED,
                (
                    "the upload region overlaps a credential field, so it must not "
                    "be uploaded to a visual model (sections 42, 55)"
                ),
                details={"credential_elements": list(blocked)},
            )
        protected = _intersecting(protected_regions, upload_rect)
        if protected:
            raise BlaxcyError(
                ErrorCode.VISUAL_FALLBACK_DENIED,
                "the upload region overlaps a protected region, so it must not be uploaded",
                details={"protected_regions": len(protected)},
            )

    # -- Image preparation (section 41) --------------------------------------

    def _prepare_upload(
        self, frame: Frame, upload_rect: Rect
    ) -> tuple[npt.NDArray[np.uint8], Rect, bool]:
        """Crop the upload region and bound its longest side.

        Cropping and downscaling are geometric only: the returned ``Rect`` is the
        DESKTOP-space region the image actually represents, so normalized
        coordinates from the model map back exactly.
        """
        crop = _crop_frame(frame, upload_rect)
        if crop is None:
            raise BlaxcyError(
                ErrorCode.CAPTURE_FAILED,
                "the requested upload region could not be cropped from the frame",
                details={"region": upload_rect.to_dict()},
            )
        height, width = int(crop.shape[0]), int(crop.shape[1])
        longest = max(width, height)
        if longest <= self._settings.max_image_side:
            return crop, upload_rect, False
        scale = self._settings.max_image_side / float(longest)
        new_size = (max(1, round(width * scale)), max(1, round(height * scale)))
        resized = cv2.resize(crop, new_size, interpolation=cv2.INTER_AREA)
        return np.asarray(resized, dtype=np.uint8), upload_rect, True

    # -- Mapping (sections 31, 39, 41) ---------------------------------------

    def _map_matches(
        self,
        raw: Sequence[RawVisualMatch],
        *,
        uploaded_rect: Rect,
        limit: int,
    ) -> tuple[VisualMatch, ...]:
        """Map normalized boxes into DESKTOP geometry with derived confidence.

        Degenerate boxes are dropped (they are not locations), boxes are clipped
        to the uploaded region so a model cannot place a target outside the image
        it was shown, and the result is deterministic: score descending, then
        label, then position -- so the same answer always ranks the same way.

        Matches are *not* deduplicated or merged: if the model reports one
        plausible box twice, two elements exist and the resolver's absolute
        ambiguity rule (section 43) decides, exactly as it would for any other
        source. This module never picks a winner.
        """
        matches: list[VisualMatch] = []
        for item in raw:
            if item.is_degenerate:
                continue
            rect = _normalized_to_desktop(item, uploaded_rect)
            if rect is None:
                continue
            confidence = min(
                self.confidence_cap,
                perception_confidence(
                    PerceptionSource.VISUAL,
                    geometry_sanity=_geometry_sanity(rect, uploaded_rect),
                    freshness=1.0,
                    source_consistency=max(0.0, min(1.0, item.confidence)),
                ),
            )
            matches.append(
                VisualMatch(
                    label=item.label,
                    rect=rect,
                    confidence=confidence,
                    raw_confidence=max(0.0, min(1.0, item.confidence)),
                )
            )
        matches.sort(key=lambda m: (-m.confidence, m.label, m.rect.y, m.rect.x))
        return tuple(matches[:limit])

    # -- Internals ------------------------------------------------------------

    def _resolve_backend(self) -> VisualGroundingBackend:
        """The injected backend, or a lazily built Gemini one."""
        if self._backend is not None:
            return self._backend
        backend = GeminiVisualBackend(
            model=self._model or "gemini-2.5-flash", key_manager=self._key_manager
        )
        self._backend = backend
        return backend

    def _fail(self, message: str) -> None:
        """Record a backend failure without aborting anything else."""
        self._last_error = message


def _import_sdk() -> tuple[Any, Any]:
    """Import ``google.genai`` lazily, or raise an honest unavailability."""
    try:
        genai = importlib.import_module("google.genai")
        types = importlib.import_module("google.genai.types")
    except Exception as exc:
        raise BlaxcyError(
            ErrorCode.BACKEND_UNAVAILABLE,
            f"the google-genai SDK is not importable: {type(exc).__name__}",
            details={"fix_hint": _FIX_HINT},
        ) from exc
    return genai, types


def _sdk_available() -> bool:
    """Whether the SDK can be imported here (no client is constructed)."""
    try:
        importlib.import_module("google.genai")
    except Exception:
        return False
    return True


def _locate_prompt(query: str, *, max_results: int) -> str:
    """The grounding instruction. It asks for locations, never for reasoning."""
    return (
        "You are a visual locator, not an assistant. Find the user-interface "
        "control described below in this screenshot.\n"
        f"Description: {query}\n"
        f"Return at most {max_results} candidate location(s), best first.\n"
        "Answer with JSON only. Coordinates are fractions of the image (0.0-1.0) "
        "with the origin at the top-left: x and y are the box's top-left corner "
        "and width and height its size. If the control is not visible, return an "
        'empty list ({"matches": []}) -- an empty answer is correct and useful. '
        "Never invent a location."
    )


def _encode_png(image: npt.NDArray[np.uint8]) -> bytes:
    """Encode a BGRA/BGR image as PNG bytes for upload."""
    ok, buffer = cv2.imencode(".png", image)
    if not ok:  # pragma: no cover - cv2 only fails on an unusable array
        raise BlaxcyError(
            ErrorCode.MODEL_ERROR,
            "the upload image could not be encoded as PNG",
            details={"shape": list(image.shape)},
        )
    return bytes(buffer.tobytes())


def _parse_matches(text: str | None) -> tuple[RawVisualMatch, ...]:
    """Parse the model's JSON answer into normalized matches.

    A missing or malformed answer is a structured ``MODEL_ERROR`` -- never an
    empty success, which would be indistinguishable from "the control is not
    there" and would quietly hide a broken model.
    """
    if not text or not text.strip():
        raise BlaxcyError(
            ErrorCode.MODEL_ERROR,
            "the visual model returned no text to parse",
            details={"backend": VISUAL_BACKEND_NAME},
        )
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BlaxcyError(
            ErrorCode.MODEL_ERROR,
            "the visual model returned malformed JSON",
            details={"backend": VISUAL_BACKEND_NAME, "error": str(exc)[:200]},
        ) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("matches"), list):
        raise BlaxcyError(
            ErrorCode.MODEL_ERROR,
            "the visual model returned an unexpected response shape",
            details={"backend": VISUAL_BACKEND_NAME, "keys": sorted(payload)[:10] if isinstance(payload, dict) else []},
        )
    matches: list[RawVisualMatch] = []
    for item in payload["matches"]:
        if not isinstance(item, dict):
            continue
        try:
            raw = RawVisualMatch(
                label=str(item.get("label") or "").strip(),
                confidence=float(item.get("confidence", 1.0)),
                x=float(item["x"]),
                y=float(item["y"]),
                width=float(item["width"]),
                height=float(item["height"]),
            )
        except (KeyError, TypeError, ValueError):
            continue
        matches.append(raw)
    return tuple(matches)


def _normalized_to_desktop(match: RawVisualMatch, uploaded_rect: Rect) -> Rect | None:
    """Map one normalized box onto the DESKTOP region the image represented.

    The box is clamped to the unit square first and the result to the uploaded
    region, so a model that answers ``x=1.4`` cannot produce a target outside the
    image it was shown.
    """
    left = _clamp_unit(match.x)
    top = _clamp_unit(match.y)
    right = _clamp_unit(match.x + match.width)
    bottom = _clamp_unit(match.y + match.height)
    if right <= left or bottom <= top:
        return None
    rect = Rect(
        x=uploaded_rect.x + left * uploaded_rect.width,
        y=uploaded_rect.y + top * uploaded_rect.height,
        width=(right - left) * uploaded_rect.width,
        height=(bottom - top) * uploaded_rect.height,
        space=CoordinateSpace.DESKTOP,
    )
    if rect.width <= 0.0 or rect.height <= 0.0:
        return None
    return rect


def _clamp_unit(value: float) -> float:
    """Clamp a fraction into ``[0, 1]``."""
    if value != value:  # NaN: refuse rather than propagate a nonsense coordinate
        return 0.0
    return max(0.0, min(1.0, value))


def _geometry_sanity(rect: Rect, uploaded_rect: Rect) -> float:
    """The section 39 geometry multiplier for a located box.

    A plausible control-sized box scores 1.0; a box that covers essentially the
    whole upload (which is not a location, it is a refusal to choose) or a
    sub-pixel sliver is discounted. This is a trust multiplier, never a gate: a
    discounted box still has to clear the resolver's threshold and revalidation.
    """
    if uploaded_rect.area <= 0.0:
        return 0.0
    ratio = rect.area / uploaded_rect.area
    if ratio > 0.90:
        return 0.3
    if ratio < 0.0005:
        return 0.5
    return 1.0


def _overlapping(elements: Sequence[UIElement], region: Rect) -> tuple[str, ...]:
    """Ids of elements whose bounding box overlaps ``region``.

    A credential element with no geometry cannot be located on screen; the honest
    treatment is to treat a geometry-less credential element as *unknown*
    coverage, but the caller still supplied it, so it is reported and refused too
    -- an upload whose contents cannot be proven clean must not happen.
    """
    hits: list[str] = []
    for element in elements:
        if element.bbox is None:
            hits.append(element.element_id)
            continue
        if element.bbox.space is not region.space:
            # Cross-space geometry is never silently converted (section 31):
            # refuse rather than assume the two rectangles are comparable.
            hits.append(element.element_id)
            continue
        if element.bbox.intersects(region):
            hits.append(element.element_id)
    return tuple(hits)


def _intersecting(regions: Sequence[Rect], region: Rect) -> tuple[Rect, ...]:
    """Regions that overlap ``region``, ignoring cross-space rectangles safely.

    A cross-space protected region is counted as intersecting: refusing an upload
    is always allowed, and converting geometry across spaces is not (section 31).
    """
    hits: list[Rect] = []
    for other in regions:
        if other.space is not region.space or other.intersects(region):
            hits.append(other)
    return tuple(hits)


def _crop_frame(frame: Frame, rect: Rect) -> npt.NDArray[np.uint8] | None:
    """Crop ``frame`` to a DESKTOP-space ``rect``, or ``None`` when empty.

    The frame's image is in FRAME pixel space; its ``desktop_rect`` maps DESKTOP
    coordinates onto those pixels, so the scale is derived, never assumed.
    """
    desktop = frame.desktop_rect
    if desktop.width <= 0 or desktop.height <= 0:
        return None
    scale_x = frame.width / desktop.width
    scale_y = frame.height / desktop.height
    x0 = round((rect.x - desktop.x) * scale_x)
    y0 = round((rect.y - desktop.y) * scale_y)
    x1 = round((rect.x + rect.width - desktop.x) * scale_x)
    y1 = round((rect.y + rect.height - desktop.y) * scale_y)
    x0 = max(0, min(frame.width, x0))
    y0 = max(0, min(frame.height, y0))
    x1 = max(0, min(frame.width, x1))
    y1 = max(0, min(frame.height, y1))
    if x1 <= x0 or y1 <= y0:
        return None
    return np.asarray(frame.image[y0:y1, x0:x1], dtype=np.uint8)


def _match_element_id(match: VisualMatch) -> str:
    """A stable element id for a visual match, derived from geometry + label."""
    payload = (
        f"{match.rect.x:.2f},{match.rect.y:.2f},"
        f"{match.rect.width:.2f},{match.rect.height:.2f}|{match.label}"
    )
    return f"visual:{xxhash.xxh64(payload.encode('utf-8')).hexdigest()}"


__all__ = [
    "DEFAULT_MAX_IMAGE_SIDE",
    "VISUAL_BACKEND_NAME",
    "VISUAL_CONFIDENCE_CAP",
    "VISUAL_MODES",
    "GeminiVisualBackend",
    "RawVisualMatch",
    "VisualGrounder",
    "VisualGroundingBackend",
    "VisualGroundingResult",
    "VisualMatch",
]
