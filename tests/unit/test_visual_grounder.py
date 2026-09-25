"""Visual grounding: the gated final perception fallback (spec sections 38, 39, 41, 42).

The model transport is a fake backend, so these tests verify the grounder's
*own* behaviour -- the invocation gate, the privacy gate, the image bound and the
confidence ceiling, the coordinate mapping, and the provenance of the elements it
produces -- with no network call and no API key. A separate group exercises the
Gemini backend's request/response translation through an injected SDK client
(also no network), and one cross-check drives the real ``TargetResolver`` over the
elements a grounding call produces, because "a VISUAL element is usable but never
out-votes ambiguity" is a claim about the resolver, not about this module.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import numpy as np
import numpy.typing as npt
import pytest
from pydantic import ValidationError

from config.settings import ResolverSettings, VisualSettings
from core.frame_engine import Frame
from core.target_resolver import ResolutionStatus, TargetResolver
from core.visual_grounder import (
    VISUAL_CONFIDENCE_CAP,
    GeminiVisualBackend,
    RawVisualMatch,
    VisualGrounder,
    VisualGroundingBackend,
)
from schemas.elements import UIElement
from schemas.enums import (
    CapabilityStatus,
    CoordinateSpace,
    ErrorCode,
    PerceptionSource,
    PolicyMode,
    UIRole,
)
from schemas.errors import BlaxcyError
from schemas.geometry import Rect

_DESKTOP_WIDTH = 1920
_DESKTOP_HEIGHT = 1080
_DESKTOP = Rect(
    x=0.0,
    y=0.0,
    width=float(_DESKTOP_WIDTH),
    height=float(_DESKTOP_HEIGHT),
    space=CoordinateSpace.DESKTOP,
)

API_KEY = "AIzaSy-not-a-real-key-0000000000000000000"


# -- Doubles ------------------------------------------------------------------


class FakeClock:
    """A monotonic clock that advances a fixed step on every read."""

    def __init__(self, step: float = 0.0) -> None:
        """Create the clock with the per-read advance."""
        self._now = 0.0
        self._step = step

    def __call__(self) -> float:
        """Return the current reading and advance."""
        self._now += self._step
        return self._now


class FakeBackend:
    """A visual backend returning scripted matches, with call accounting."""

    name = "fake-visual"

    def __init__(
        self,
        matches: tuple[RawVisualMatch, ...] = (),
        *,
        error: Exception | None = None,
    ) -> None:
        """Create the backend with a scripted answer or a scripted failure."""
        self._matches = matches
        self._error = error
        self.calls: list[dict[str, Any]] = []

    def locate(
        self,
        image: npt.NDArray[np.uint8],
        *,
        query: str,
        max_results: int,
        timeout_ms: int,
    ) -> tuple[RawVisualMatch, ...]:
        """Record the request and return (or raise) the scripted result."""
        self.calls.append(
            {
                "shape": tuple(int(v) for v in image.shape),
                "query": query,
                "max_results": max_results,
                "timeout_ms": timeout_ms,
            }
        )
        if self._error is not None:
            raise self._error
        return self._matches

    @property
    def called(self) -> bool:
        """Whether the backend was invoked at all."""
        return bool(self.calls)


class FakeKeyManager:
    """A keyring manager double."""

    def __init__(self, key: str | None = API_KEY) -> None:
        """Create the manager with (or without) a stored key."""
        self._key = key

    def gemini_key(self) -> str | None:
        """Return the scripted key."""
        return self._key

    def status(self) -> dict[str, Any]:
        """A loggable status without the secret."""
        return {"keyring_service": "blaxcy", "keyring_key": "gemini_api_key", "key_present": bool(self._key)}


# -- Builders -----------------------------------------------------------------


def _frame(width: int = _DESKTOP_WIDTH, height: int = _DESKTOP_HEIGHT) -> Frame:
    """A frame whose DESKTOP space matches its pixels 1:1."""
    image = np.full((height, width, 4), 200, dtype=np.uint8)
    return Frame(
        frame_id=7,
        generation=3,
        captured_at_monotonic=0.0,
        captured_at=0.0,
        desktop_rect=Rect(
            x=0.0, y=0.0, width=float(width), height=float(height), space=CoordinateSpace.DESKTOP
        ),
        image=image,
        thumbnail=np.zeros((270, 480), dtype=np.uint8),
        latency_ms=0.0,
        backend="fake",
    )


def _settings(**overrides: Any) -> VisualSettings:
    """VisualSettings with section 41 defaults, overridable per test."""
    return VisualSettings(**overrides)


def _match(
    label: str = "Play",
    *,
    confidence: float = 1.0,
    x: float = 0.1,
    y: float = 0.1,
    width: float = 0.1,
    height: float = 0.1,
) -> RawVisualMatch:
    """A normalized backend match."""
    return RawVisualMatch(label=label, confidence=confidence, x=x, y=y, width=width, height=height)


def _rect(x: float, y: float, width: float, height: float, *, space: CoordinateSpace = CoordinateSpace.DESKTOP) -> Rect:
    """A rectangle in the given space."""
    return Rect(x=x, y=y, width=width, height=height, space=space)


#: A placeholder credential box; tests override it (or pass ``None``) as needed.
_PASSWORD_BOX = Rect(x=800.0, y=400.0, width=200.0, height=40.0, space=CoordinateSpace.DESKTOP)


def _password_element(bbox: Rect | None = _PASSWORD_BOX) -> UIElement:
    """A credential input element (its text is always None, section 55)."""
    return UIElement(
        element_id="pw-1",
        role=UIRole.PASSWORD_INPUT,
        password=True,
        bbox=bbox,
        center=bbox.center if bbox is not None else None,
        coordinate_space=bbox.space if bbox is not None else None,
        source=PerceptionSource.ATSPI,
        confidence=0.95,
    )


def _grounder(backend: VisualGroundingBackend, **overrides: Any) -> VisualGrounder:
    """A grounder wired to a fake backend and a fake keyring."""
    return VisualGrounder(
        _settings(**overrides),
        backend=backend,
        model="gemini-2.5-flash",
        key_manager=FakeKeyManager(),  # type: ignore[arg-type]
        clock=FakeClock(step=1.0),
    )


# -- Section 41 invocation gate ----------------------------------------------


def test_observe_mode_refuses_before_the_backend_is_touched() -> None:
    """Section 41: OBSERVE may not upload anything, so the model is never called."""
    backend = FakeBackend((_match(),))
    with pytest.raises(BlaxcyError) as excinfo:
        _grounder(backend).ground(_frame(), query="Play", mode=PolicyMode.OBSERVE)
    assert excinfo.value.code is ErrorCode.VISUAL_FALLBACK_DENIED
    assert excinfo.value.details["mode"] == PolicyMode.OBSERVE.value
    assert backend.called is False


@pytest.mark.parametrize("mode", [PolicyMode.ASSIST, PolicyMode.AUTONOMOUS])
def test_assist_and_autonomous_may_ground(mode: PolicyMode) -> None:
    """Section 41 permits the fallback from ASSIST upward."""
    backend = FakeBackend((_match(),))
    result = _grounder(backend).ground(_frame(), query="Play", mode=mode)
    assert result.found is True
    assert backend.called is True


def test_disabled_configuration_refuses_and_never_calls_out() -> None:
    """The switch removes the capability; it cannot remove a check."""
    backend = FakeBackend((_match(),))
    with pytest.raises(BlaxcyError) as excinfo:
        _grounder(backend, enabled=False).ground(_frame(), query="Play", mode=PolicyMode.ASSIST)
    assert excinfo.value.code is ErrorCode.VISUAL_FALLBACK_DENIED
    assert backend.called is False


def test_an_empty_description_is_refused() -> None:
    """There is nothing to ground, and an empty upload is not a valid request."""
    backend = FakeBackend((_match(),))
    with pytest.raises(BlaxcyError) as excinfo:
        _grounder(backend).ground(_frame(), query="   ", mode=PolicyMode.ASSIST)
    assert excinfo.value.code is ErrorCode.TARGET_NOT_FOUND
    assert backend.called is False


# -- Section 42/55 privacy gate ----------------------------------------------


def test_a_credential_field_inside_the_upload_region_refuses() -> None:
    """A whole-desktop upload containing a password box is refused outright."""
    backend = FakeBackend((_match(),))
    with pytest.raises(BlaxcyError) as excinfo:
        _grounder(backend).ground(
            _frame(),
            query="Play",
            mode=PolicyMode.ASSIST,
            password_elements=(_password_element(),),
        )
    assert excinfo.value.code is ErrorCode.VISUAL_FALLBACK_DENIED
    assert excinfo.value.details["credential_elements"] == ["pw-1"]
    assert backend.called is False


def test_a_credential_field_outside_an_explicit_region_does_not_refuse() -> None:
    """Region-scoped uploads stay usable: only the region is checked, not the screen."""
    backend = FakeBackend((_match(x=0.0, y=0.0),))
    result = _grounder(backend).ground(
        _frame(),
        query="Play",
        mode=PolicyMode.ASSIST,
        region=_rect(0, 0, 400, 300),
        password_elements=(_password_element(),),
    )
    assert result.uploaded_rect.width == 400.0
    assert backend.called is True


def test_a_credential_element_without_geometry_refuses() -> None:
    """Unknown coverage is not proven-clean coverage: the upload is refused."""
    backend = FakeBackend((_match(),))
    with pytest.raises(BlaxcyError) as excinfo:
        _grounder(backend).ground(
            _frame(),
            query="Play",
            mode=PolicyMode.ASSIST,
            region=_rect(0, 0, 400, 300),
            password_elements=(_password_element(bbox=None),),
        )
    assert excinfo.value.code is ErrorCode.VISUAL_FALLBACK_DENIED
    assert backend.called is False


def test_cross_space_credential_geometry_refuses_rather_than_assuming() -> None:
    """Section 31: geometry is never silently converted across coordinate spaces."""
    backend = FakeBackend((_match(),))
    monitor_space = _password_element(_rect(10, 10, 50, 20, space=CoordinateSpace.MONITOR))
    with pytest.raises(BlaxcyError) as excinfo:
        _grounder(backend).ground(
            _frame(),
            query="Play",
            mode=PolicyMode.ASSIST,
            region=_rect(0, 0, 400, 300),
            password_elements=(monitor_space,),
        )
    assert excinfo.value.code is ErrorCode.VISUAL_FALLBACK_DENIED


def test_a_protected_region_refuses() -> None:
    """A protected application's window rectangle must never be uploaded."""
    backend = FakeBackend((_match(),))
    with pytest.raises(BlaxcyError) as excinfo:
        _grounder(backend).ground(
            _frame(),
            query="Play",
            mode=PolicyMode.ASSIST,
            region=_rect(0, 0, 800, 600),
            protected_regions=(_rect(100, 100, 200, 200),),
        )
    assert excinfo.value.code is ErrorCode.VISUAL_FALLBACK_DENIED
    assert backend.called is False


# -- Geometry and the image bound (sections 31, 41) --------------------------


def test_normalized_boxes_map_into_desktop_geometry() -> None:
    """A fraction of the uploaded image is a fraction of the DESKTOP rect."""
    backend = FakeBackend((_match(x=0.5, y=0.5, width=0.1, height=0.1),))
    result = _grounder(backend).ground(
        _frame(),
        query="Play",
        mode=PolicyMode.ASSIST,
        region=_rect(100, 50, 200, 100),
    )
    rect = result.matches[0].rect
    assert (rect.x, rect.y) == (200.0, 100.0)
    assert rect.width == pytest.approx(20.0)
    assert rect.height == pytest.approx(10.0)
    assert rect.space is CoordinateSpace.DESKTOP


def test_matches_are_clipped_to_the_uploaded_region() -> None:
    """A model cannot place a target outside the image it was shown."""
    backend = FakeBackend((_match(x=0.9, y=0.9, width=0.5, height=0.5),))
    result = _grounder(backend).ground(_frame(), query="Play", mode=PolicyMode.ASSIST)
    rect = result.matches[0].rect
    assert rect.x + rect.width <= _DESKTOP_WIDTH
    assert rect.y + rect.height <= _DESKTOP_HEIGHT


def test_degenerate_boxes_are_not_locations() -> None:
    """A zero-area box is dropped rather than treated as a clickable point."""
    backend = FakeBackend((_match(width=0.0), _match(label="Real", x=0.2, y=0.2)))
    result = _grounder(backend).ground(_frame(), query="Play", mode=PolicyMode.ASSIST)
    assert [m.label for m in result.matches] == ["Real"]


def test_the_image_is_downscaled_to_the_max_side_and_mapping_still_holds() -> None:
    """Section 41's bound is enforced on the upload, not merely documented."""
    backend = FakeBackend((_match(x=0.5, y=0.5, width=0.1, height=0.1),))
    grounder = _grounder(backend, max_image_side=1024)
    result = grounder.ground(_frame(), query="Play", mode=PolicyMode.ASSIST)
    assert result.downscaled is True
    assert max(result.image_width, result.image_height) == 1024
    assert (result.image_width, result.image_height) == (1024, 576)
    assert backend.calls[0]["shape"][:2] == (576, 1024)
    # Normalized coordinates survive the downscale exactly.
    assert result.matches[0].rect.x == pytest.approx(_DESKTOP_WIDTH * 0.5)


def test_a_small_region_is_not_resampled() -> None:
    """No resize when the crop is already inside the bound."""
    backend = FakeBackend((_match(),))
    result = _grounder(backend).ground(
        _frame(),
        query="Play",
        mode=PolicyMode.ASSIST,
        region=_rect(10, 10, 300, 200),
    )
    assert result.downscaled is False
    assert (result.image_width, result.image_height) == (300, 200)


def test_the_model_timeout_is_passed_through() -> None:
    """The one model call is bounded by configuration."""
    backend = FakeBackend((_match(),))
    _grounder(backend, model_timeout_ms=4000).ground(_frame(), query="Play", mode=PolicyMode.ASSIST)
    assert backend.calls[0]["timeout_ms"] == 4000


# -- Confidence (sections 39, 41) --------------------------------------------


def test_confidence_never_exceeds_the_section_41_ceiling() -> None:
    """A perfectly confident model still cannot exceed the VISUAL base rate."""
    backend = FakeBackend((_match(confidence=1.0),))
    result = _grounder(backend).ground(_frame(), query="Play", mode=PolicyMode.ASSIST)
    match = result.matches[0]
    assert match.raw_confidence == 1.0
    assert match.confidence <= VISUAL_CONFIDENCE_CAP
    assert match.confidence == pytest.approx(VISUAL_CONFIDENCE_CAP)


def test_a_lower_configured_ceiling_is_honoured() -> None:
    """The ceiling is tunable downward, and the configured value wins."""
    backend = FakeBackend((_match(confidence=1.0),))
    result = _grounder(backend, max_confidence=0.5).ground(_frame(), query="Play", mode=PolicyMode.ASSIST)
    assert result.matches[0].confidence == pytest.approx(0.5)


def test_the_ceiling_cannot_be_configured_above_the_spec_bound() -> None:
    """Section 41's 0.80 is a bound, not a default."""

    with pytest.raises(ValidationError):
        VisualSettings(max_confidence=0.95)


def test_a_low_model_confidence_lowers_the_result() -> None:
    """Confidence is derived, never assumed: the model's own doubt is applied."""
    backend = FakeBackend((_match(confidence=0.4),))
    result = _grounder(backend).ground(_frame(), query="Play", mode=PolicyMode.ASSIST)
    assert result.matches[0].confidence == pytest.approx(VISUAL_CONFIDENCE_CAP * 0.4)


# -- Ordering and limits ------------------------------------------------------


def test_matches_are_ordered_deterministically_and_limited() -> None:
    """Higher confidence first, then label/position, capped by max_results."""
    backend = FakeBackend(
        (
            _match("Beta", confidence=1.0),
            _match("Alpha", confidence=1.0),
            _match("Gamma", confidence=0.2),
        )
    )
    result = _grounder(backend, max_results=2).ground(_frame(), query="Play", mode=PolicyMode.ASSIST)
    assert [m.label for m in result.matches] == ["Alpha", "Beta"]
    assert backend.calls[0]["max_results"] == 2


def test_a_per_call_limit_overrides_the_configured_one() -> None:
    """A caller may ask for fewer results than the configuration allows."""
    backend = FakeBackend((_match("Alpha"), _match("Beta", x=0.5)))
    result = _grounder(backend, max_results=3).ground(
        _frame(), query="Play", mode=PolicyMode.ASSIST, max_results=1
    )
    assert len(result.matches) == 1
    assert backend.calls[0]["max_results"] == 1


def test_an_empty_answer_is_an_honest_not_found() -> None:
    """No location found is a normal result, not an error."""
    result = _grounder(FakeBackend(())).ground(_frame(), query="Play", mode=PolicyMode.ASSIST)
    assert result.found is False
    assert result.matches == ()
    assert result.to_dict()["found"] is False


# -- Elements (sections 37, 38, 44) ------------------------------------------


def _elements(grounder: VisualGrounder, backend: FakeBackend, **kwargs: Any) -> tuple[UIElement, ...]:
    """Ground one match and convert it to elements."""
    result = grounder.ground(_frame(), query="Play", mode=PolicyMode.ASSIST)
    return grounder.as_elements(result, frame_id=7, generation=3, **kwargs)


def test_elements_carry_visual_provenance_and_a_real_box() -> None:
    """A grounded element says exactly where it came from and how sure it is."""
    backend = FakeBackend((_match("Play"),))
    grounder = _grounder(backend)
    (element,) = _elements(grounder, backend)

    assert element.source is PerceptionSource.VISUAL
    assert element.coordinate_space is CoordinateSpace.DESKTOP
    assert element.bbox is not None and element.bbox.area > 0.0
    assert element.center == element.bbox.center
    assert element.clickable is True
    assert element.effective_clickable is False
    assert element.role is UIRole.UNKNOWN
    assert element.frame_id == 7
    assert element.confidence <= VISUAL_CONFIDENCE_CAP
    assert element.element_id.startswith("visual:")


def test_element_ids_are_stable_and_unique() -> None:
    """The same answer produces the same ids; different answers do not collide."""
    backend = FakeBackend((_match("Play"), _match("Play", x=0.6)))
    grounder = _grounder(backend)
    first = _elements(grounder, backend)
    second = _elements(grounder, backend)
    ids = [element.element_id for element in first]
    assert len(set(ids)) == 2
    assert [element.element_id for element in second] == ids


def test_a_caller_supplied_role_is_reported_without_being_invented() -> None:
    """Section 38: the caller may pass its role hint through, explicitly."""
    backend = FakeBackend((_match("Play"),))
    grounder = _grounder(backend)
    (element,) = _elements(grounder, backend, role=UIRole.BUTTON)
    assert element.role is UIRole.BUTTON


def test_visual_grounding_never_emits_a_credential_element() -> None:
    """Visual grounding is never run over credential context, so it cannot
    produce a password element -- attempting to is a programming error."""
    backend = FakeBackend((_match("Play"),))
    grounder = _grounder(backend)
    result = grounder.ground(_frame(), query="Play", mode=PolicyMode.ASSIST)
    with pytest.raises(ValueError):
        grounder.as_elements(result, role=UIRole.PASSWORD_INPUT)


# -- Integration with the real resolver (section 43) -------------------------


def test_a_visual_element_is_resolvable_when_it_is_the_only_candidate() -> None:
    """The fallback must actually be usable, or it is a stub."""
    backend = FakeBackend((_match("Play"),))
    grounder = _grounder(backend)
    elements = _elements(grounder, backend)
    resolver = TargetResolver(ResolverSettings())

    from schemas.elements import ElementQuery

    resolution = resolver.resolve(ElementQuery(text="Play"), elements)
    assert resolution.status is ResolutionStatus.RESOLVED
    assert resolution.element is not None
    assert resolution.element.source is PerceptionSource.VISUAL


def test_two_identical_visual_candidates_are_ambiguous_not_a_silent_pick() -> None:
    """Section 43: a visual fallback never out-votes the absolute ambiguity rule."""
    backend = FakeBackend(
        (
            _match("Play", x=0.1, y=0.1),
            _match("Play", x=0.6, y=0.6),
        )
    )
    grounder = _grounder(backend)
    elements = _elements(grounder, backend)
    resolver = TargetResolver(ResolverSettings())

    from schemas.elements import ElementQuery

    resolution = resolver.resolve(ElementQuery(text="Play"), elements)
    assert resolution.status is ResolutionStatus.AMBIGUOUS
    assert resolution.best is None
    assert resolution.ambiguity is True


# -- Failure honesty (section 80) --------------------------------------------


def test_a_backend_failure_propagates_unchanged() -> None:
    """A structured backend error is not swallowed or re-labelled."""
    backend = FakeBackend(error=BlaxcyError(ErrorCode.RATE_LIMITED, "quota exhausted"))
    with pytest.raises(BlaxcyError) as excinfo:
        _grounder(backend).ground(_frame(), query="Play", mode=PolicyMode.ASSIST)
    assert excinfo.value.code is ErrorCode.RATE_LIMITED
    assert excinfo.value.message == "quota exhausted"


def test_an_unexpected_backend_failure_becomes_a_model_error_without_the_image() -> None:
    """A backend bug is reported as a model error and never carries the image."""
    backend = FakeBackend(error=RuntimeError("boom with secret pixels"))
    with pytest.raises(BlaxcyError) as excinfo:
        _grounder(backend).ground(_frame(), query="Play", mode=PolicyMode.ASSIST)
    assert excinfo.value.code is ErrorCode.MODEL_ERROR
    assert "RuntimeError" in excinfo.value.message
    assert "pixels" not in json.dumps(excinfo.value.details)


def test_a_region_outside_the_frame_is_a_capture_failure() -> None:
    """An uncroppable region is honest about which failure it is."""
    backend = FakeBackend((_match(),))
    with pytest.raises(BlaxcyError) as excinfo:
        _grounder(backend).ground(
            _frame(),
            query="Play",
            mode=PolicyMode.ASSIST,
            region=_rect(5000, 5000, 100, 100),
        )
    assert excinfo.value.code is ErrorCode.CAPTURE_FAILED
    assert backend.called is False


# -- Capability and diagnostics (sections 28, 80) ----------------------------


def test_capability_reports_the_disabled_switch_honestly() -> None:
    """Disabled is UNAVAILABLE with a reason and a fix hint, never a guess."""
    cap = _grounder(FakeBackend(), enabled=False).capability()
    assert cap.status is CapabilityStatus.UNAVAILABLE
    assert "disabled by configuration" in (cap.reason or "")
    assert cap.fix_hint


def test_capability_reports_a_missing_credential_honestly() -> None:
    """The SDK is present here but no key is: that is UNAVAILABLE, with evidence."""
    grounder = VisualGrounder(
        _settings(),
        backend=FakeBackend(),
        model="gemini-2.5-flash",
        key_manager=FakeKeyManager(None),  # type: ignore[arg-type]
    )
    cap = grounder.capability()
    assert cap.status is CapabilityStatus.UNAVAILABLE
    assert "no Gemini API key" in (cap.reason or "")
    assert cap.details["max_image_side"] == 1024
    assert cap.details["max_confidence"] == VISUAL_CONFIDENCE_CAP
    assert cap.details["key_present"] is False


def test_capability_reports_available_with_limits_and_no_network_call() -> None:
    """With a credential present the verdict is AVAILABLE and states its bounds."""
    cap = _grounder(FakeBackend()).capability()
    assert cap.status is CapabilityStatus.AVAILABLE
    assert cap.backend == "google-genai"
    assert cap.details["required_modes"] == ["ASSIST", "AUTONOMOUS"]
    assert cap.details["key_present"] is True


def test_diagnostics_count_calls_refusals_and_downscales() -> None:
    """Observability is measured, never asserted."""
    backend = FakeBackend((_match(),))
    grounder = _grounder(backend, max_image_side=512)
    grounder.ground(_frame(), query="Play", mode=PolicyMode.ASSIST)
    with pytest.raises(BlaxcyError):
        grounder.ground(_frame(), query="Play", mode=PolicyMode.OBSERVE)
    diag = grounder.diagnostics()
    assert diag["calls"] == 1
    assert diag["refusals"] == 1
    assert diag["matches"] == 1
    assert diag["downscales"] == 1
    assert diag["enabled"] is True


# -- The Gemini backend's translation (section 82: verified SDK surface) -----


class FakeModels:
    """A ``models`` resource recording requests and returning a scripted reply."""

    def __init__(self, response: Any = None, error: Exception | None = None) -> None:
        """Create the resource with one scripted response or error."""
        self.response = response
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def generate_content(self, *, model: str, contents: Any, config: Any) -> Any:
        """Record the request and return (or raise) the scripted result."""
        self.calls.append({"model": model, "contents": contents, "config": config})
        if self.error is not None:
            raise self.error
        return self.response


class FakeClient:
    """A minimal SDK client double."""

    def __init__(self, models: FakeModels) -> None:
        """Wrap the fake models resource."""
        self.models = models


def _sdk_backend(models: FakeModels, *, api_key: str | None = None) -> GeminiVisualBackend:
    """A Gemini backend over an injected client (no keyring, no network)."""
    return GeminiVisualBackend(
        model="gemini-2.5-flash",
        client=FakeClient(models),
        api_key=api_key,
        key_manager=FakeKeyManager(api_key),  # type: ignore[arg-type]
    )


def _image() -> npt.NDArray[np.uint8]:
    """A tiny BGRA image."""
    return np.zeros((8, 8, 4), dtype=np.uint8)


def test_the_gemini_backend_parses_a_json_answer() -> None:
    """The response is translated into normalized boxes, and nothing else."""
    payload = {
        "matches": [
            {"label": "Play", "confidence": 0.9, "x": 0.1, "y": 0.2, "width": 0.3, "height": 0.4}
        ]
    }
    models = FakeModels(SimpleNamespace(text=json.dumps(payload)))
    matches = _sdk_backend(models).locate(
        _image(), query="Play", max_results=3, timeout_ms=5000
    )
    assert len(matches) == 1
    assert matches[0].label == "Play"
    assert (matches[0].x, matches[0].y) == (0.1, 0.2)
    assert models.calls[0]["model"] == "gemini-2.5-flash"


def test_the_gemini_backend_asks_for_strict_json_and_sends_the_image() -> None:
    """The request shape is part of the contract with the model."""
    models = FakeModels(SimpleNamespace(text='{"matches": []}'))
    _sdk_backend(models).locate(_image(), query="Play", max_results=2, timeout_ms=7000)
    config = models.calls[0]["config"]
    assert config.response_mime_type == "application/json"
    assert config.http_options.timeout == 7000
    (content,) = models.calls[0]["contents"]
    assert len(content.parts) == 2
    assert content.parts[0].inline_data.mime_type == "image/png"
    assert "Play" in content.parts[1].text


def test_a_malformed_answer_is_a_model_error_not_an_empty_success() -> None:
    """Malformed output must be distinguishable from 'the control is not there'."""
    models = FakeModels(SimpleNamespace(text="not json at all"))
    with pytest.raises(BlaxcyError) as excinfo:
        _sdk_backend(models).locate(_image(), query="Play", max_results=1, timeout_ms=1000)
    assert excinfo.value.code is ErrorCode.MODEL_ERROR


def test_a_missing_answer_is_a_model_error() -> None:
    """An empty response is a failure, never an implied 'not found'."""
    models = FakeModels(SimpleNamespace(text=None))
    with pytest.raises(BlaxcyError) as excinfo:
        _sdk_backend(models).locate(_image(), query="Play", max_results=1, timeout_ms=1000)
    assert excinfo.value.code is ErrorCode.MODEL_ERROR


def test_the_gemini_backend_without_a_credential_fails_before_any_call() -> None:
    """No key (and no SDK) is an honest structured failure, never a silent no-op.

    The assertion holds on either host state: without the SDK the import fails
    with the same code, and with it the client cannot be built without a key.
    """
    backend = GeminiVisualBackend(
        model="gemini-2.5-flash", key_manager=FakeKeyManager(None)  # type: ignore[arg-type]
    )
    with pytest.raises(BlaxcyError) as excinfo:
        backend.locate(_image(), query="Play", max_results=1, timeout_ms=1000)
    assert excinfo.value.code is ErrorCode.BACKEND_UNAVAILABLE
    assert excinfo.value.details


def test_the_gemini_backend_redacts_the_key_from_transport_errors() -> None:
    """Section 70: an SDK that echoes the key must not put it in a log or envelope."""
    models = FakeModels(error=RuntimeError(f"invalid key {API_KEY} for request"))
    with pytest.raises(BlaxcyError) as excinfo:
        _sdk_backend(models, api_key=API_KEY).locate(
            _image(), query="Play", max_results=1, timeout_ms=1000
        )
    assert API_KEY not in excinfo.value.message
    assert "***" in excinfo.value.message
    assert API_KEY not in json.dumps(excinfo.value.details)


def test_the_gemini_backend_classifies_a_rate_limit() -> None:
    """A quota failure is reported as RATE_LIMITED, not a generic model error."""
    models = FakeModels(error=RuntimeError("429 RESOURCE_EXHAUSTED: quota"))
    with pytest.raises(BlaxcyError) as excinfo:
        _sdk_backend(models, api_key=API_KEY).locate(
            _image(), query="Play", max_results=1, timeout_ms=1000
        )
    assert excinfo.value.code is ErrorCode.RATE_LIMITED
