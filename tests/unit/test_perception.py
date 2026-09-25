"""Perception orchestration (specification sections 33-43, 65).

The orchestrator is exercised with a *real* frame engine over a scripted capture
backend, a real accessibility service over a scripted AT-SPI backend, and
scripted OCR / visual backends. That is deliberate: the change detector must
classify real pixels, because which regions the OCR stage gets to see is decided
by the section 34 classification, not by a hand-written delta.

The properties asserted here are the ones that make the layer honest:

* one observation produces exactly one accepted, versioned state;
* a real change is classified from real pixels and accepted with its delta;
* the OCR stage only ever sees *relevant* regions, and its output is never
  treated as clickable;
* the section 43 fallback runs only after a deterministic miss, never on an
  ambiguous target, and the visual stage is refused below ASSIST and over
  credential context without the model being called at all;
* a failed capture never yields a synthesized state.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest

from config.settings import Settings
from core.perception import PerceptionOrchestrator
from schemas.elements import ElementQuery, UIElement
from schemas.enums import (
    SIGNIFICANT_CHANGE_CLASSES,
    ChangeClass,
    PerceptionSource,
    PolicyMode,
    UIRole,
)
from schemas.events import EventType
from tests.harness.perception import (
    PerceptionEnv,
    build_perception_env,
    button,
    desktop_box,
    visual_match,
)

#: How a test asks for a wired perception environment. The keywords mirror
#: :func:`build_perception_env`.
EnvFactory = Callable[..., PerceptionEnv]


@pytest.fixture
def env_factory() -> Iterator[EnvFactory]:
    """Build perception environments and stop them afterwards."""
    built: list[PerceptionEnv] = []

    def build(**kwargs: Any) -> PerceptionEnv:
        env = build_perception_env(**kwargs)
        built.append(env)
        return env

    yield build
    for env in built:
        env.orchestrator.stop()


def _send_button() -> UIElement:
    """An AT-SPI button with a small, realistic box."""
    box = desktop_box(200.0, 300.0, 120.0, 32.0)
    return button("b1", "Send", box=box)


def _play_on_a_button() -> ElementQuery:
    """A *partial* match: the right role, the wrong label.

    This is the shape that reaches the OCR stage. A text-only query scores an
    element only through a matched constrained dimension, so a text miss with no
    role hint is a plain ``NOT_FOUND`` with no candidate and therefore no region
    to read. When the role matches and the label does not, the resolver returns a
    below-threshold candidate -- and that candidate's box is exactly the
    "relevant region" section 40 asks OCR to look at.
    """
    return ElementQuery(text="Play", role_hint=UIRole.BUTTON)


# -- The ordinary cycle -------------------------------------------------------


def test_perceive_assembles_one_accepted_state_from_the_accessibility_tree(
    env_factory: EnvFactory,
) -> None:
    """One cycle yields one versioned state, built from the observed elements."""
    env = env_factory(elements=[_send_button()])

    state = env.orchestrator.perceive()

    assert state is not None
    assert env.cache.current is state
    assert [element.element_id for element in state.elements] == ["b1"]
    assert state.frame_id == 0
    assert env.cache.accepted_updates == 1
    # The layout comes from the capture backend's own monitor report, not a guess.
    assert state.layout.monitors[0].width == 1920
    assert state.layout.monitors[0].height == 1080
    assert state.capture_backend


def test_the_first_observation_has_no_delta_because_there_is_nothing_to_compare(
    env_factory: EnvFactory,
) -> None:
    """A delta describes a change; the first frame has no predecessor."""
    env = env_factory(elements=[_send_button()])

    env.orchestrator.perceive()

    assert env.cache.delta is None


def test_a_real_patch_change_is_classified_and_accepted_with_its_delta(
    env_factory: EnvFactory,
) -> None:
    """Section 34: the change detector sees real pixels and the cache keeps the delta."""
    env = env_factory(elements=[_send_button()])
    env.orchestrator.perceive()

    env.capture.add_patch(400, 500, 400, 150, level=220)
    state = env.orchestrator.perceive()

    assert state is not None
    delta = env.cache.delta
    assert delta is not None
    assert delta.regions
    assert delta.change_class in SIGNIFICANT_CHANGE_CLASSES
    assert env.cache.accepted_updates == 2
    assert any(event.event_type is EventType.SCREEN_CHANGED for event in env.events)


def test_a_failed_capture_never_produces_a_synthesized_state(env_factory: EnvFactory) -> None:
    """A capture failure is not an observation; nothing is invented to fill the gap."""
    env = env_factory(elements=[_send_button()], capture_fail=True)

    assert env.orchestrator.perceive() is None
    assert env.cache.current is None
    assert env.orchestrator.diagnostics()["capture_failures"] == 1


def test_browser_elements_carry_the_url_that_was_actually_discovered(env_factory: EnvFactory) -> None:
    """Section 36: a discovered URL annotates that browser's elements, and only those."""
    chrome_button = button(
        "chrome-button",
        "Sign in",
        box=desktop_box(10.0, 10.0, 100.0, 30.0),
    ).model_copy(update={"owner_app": "google-chrome"})
    document = _send_button().model_copy(
        update={
            "element_id": "doc",
            "role": UIRole.DOCUMENT,
            "text": None,
            "accessible_name": "Example Page",
            "owner_app": "google-chrome",
        }
    )
    address_bar = _send_button().model_copy(
        update={
            "element_id": "addr",
            "role": UIRole.TEXT_INPUT,
            "text": "https://example.com/",
            "accessible_name": "Address and search bar",
            "owner_app": "google-chrome",
        }
    )
    other = _send_button()  # a non-browser element must not be annotated

    env = env_factory(elements=[chrome_button, document, address_bar, other])

    state = env.orchestrator.perceive()

    assert state is not None
    by_id = {element.element_id: element for element in state.elements}
    assert by_id["chrome-button"].browser_url == "https://example.com/"
    assert by_id["addr"].browser_url == "https://example.com/"
    assert by_id["b1"].browser_url is None
    assert env.orchestrator.diagnostics()["browser_contexts"] >= 1


# -- The section 43 fallback cascade -----------------------------------------


def test_the_fallback_refuses_an_empty_description_without_calling_any_model(
    env_factory: EnvFactory,
) -> None:
    """There is nothing to look for, so nothing is spent looking."""
    env = env_factory(elements=[_send_button()])
    env.orchestrator.perceive()

    report = env.orchestrator.enrich(ElementQuery(text="   "), mode=PolicyMode.ASSIST)

    assert report.enriched is False
    assert report.refused == "empty target description"
    assert env.ocr_backend.calls == []
    assert env.visual_backend.called is False


def test_the_fallback_does_nothing_when_the_deterministic_cascade_resolves(
    env_factory: EnvFactory,
) -> None:
    """A resolved target needs no fallback: the cascade stops at the first hit."""
    env = env_factory(elements=[_send_button()])
    env.orchestrator.perceive()

    report = env.orchestrator.enrich(ElementQuery(text="Send"), mode=PolicyMode.ASSIST)

    assert report.enriched is False
    assert report.refused == "already resolved"
    assert env.ocr_backend.calls == []
    assert env.visual_backend.called is False


def test_an_ambiguous_target_is_never_enriched(env_factory: EnvFactory) -> None:
    """Section 43's absolute rule: ambiguity is a decision, not a perception gap."""
    first = _send_button().model_copy(update={"text": "Open"})
    second = _send_button().model_copy(
        update={
            "element_id": "b2",
            "text": "Open",
            "bbox": desktop_box(400.0, 300.0, 120.0, 32.0),
        }
    )
    env = env_factory(elements=[first, second])
    env.orchestrator.perceive()

    report = env.orchestrator.enrich(ElementQuery(text="Open"), mode=PolicyMode.ASSIST)

    assert report.enriched is False
    assert report.refused == "ambiguous; never enriched"
    assert env.ocr_backend.calls == []
    assert env.visual_backend.called is False


def test_the_ocr_stage_reads_the_near_miss_region_and_accepts_the_result(
    env_factory: EnvFactory,
) -> None:
    """A near-miss candidate is the section 40 'relevant region' for the OCR stage."""
    env = env_factory(elements=[_send_button()], ocr_text="Play")
    env.orchestrator.perceive()

    report = env.orchestrator.enrich(_play_on_a_button(), mode=PolicyMode.ASSIST)

    assert report.enriched is True
    assert report.ocr_elements == 1
    assert env.ocr_backend.calls

    state = env.cache.current
    assert state is not None
    fragments = [
        element for element in state.elements if element.source is PerceptionSource.OCR
    ]
    assert len(fragments) == 1
    # Section 38: OCR proves text is visible, never that it is clickable.
    assert fragments[0].role is UIRole.TEXT_FRAGMENT
    assert fragments[0].clickable is False
    assert fragments[0].effective_clickable is False

    # And the resolver still refuses to make it the target: an element whose
    # clickability cannot be proven is never clicked, whatever it says. The
    # cascade therefore continues to the visual stage rather than acting on it.
    resolution = env.resolver.resolve(
        _play_on_a_button(), state.elements, state=state
    )
    assert not resolution.is_resolved
    assert resolution.element is None or resolution.element.source is not PerceptionSource.OCR


def test_resolve_target_falls_back_to_a_visual_match_on_the_read_path(
    env_factory: EnvFactory,
) -> None:
    """The read path runs the same cascade, and injects nothing doing it.

    Visual grounding is the section 38 last-resort *clickability* source, which is
    why it is the stage that can turn an unfindable description into a resolved
    target for ``find_element``.
    """
    env = env_factory(
        elements=[], visual_matches=[visual_match("Play", x=0.4, y=0.4, width=0.05, height=0.05)]
    )

    resolution = env.orchestrator.resolve_target(
        ElementQuery(text="Play"), mode=PolicyMode.ASSIST
    )

    assert resolution.is_resolved
    assert resolution.element is not None
    assert resolution.element.source is PerceptionSource.VISUAL
    assert env.visual_backend.calls
    assert env.capture.grabs >= 2  # the fallback observed a fresh frame


# -- The visual stage ---------------------------------------------------------


def test_the_visual_stage_is_refused_below_assist_and_the_model_is_not_called(
    env_factory: EnvFactory,
) -> None:
    """Section 41: raising the mode is the operator's decision, made before pixels move."""
    env = env_factory(elements=[], visual_matches=[visual_match("Play")])
    env.orchestrator.perceive()

    report = env.orchestrator.enrich(ElementQuery(text="Play"), mode=PolicyMode.OBSERVE)

    assert report.enriched is False
    assert report.refused is not None and "ASSIST" in report.refused
    assert env.visual_backend.called is False


def test_the_visual_stage_grounds_in_assist_and_accepts_the_observation(
    env_factory: EnvFactory,
) -> None:
    """In ASSIST, a located control is merged into the state as a real VISUAL element."""
    match = visual_match("Play", x=0.20, y=0.30, width=0.05, height=0.05)
    env = env_factory(elements=[], visual_matches=[match])
    env.orchestrator.perceive()

    report = env.orchestrator.enrich(ElementQuery(text="Play"), mode=PolicyMode.ASSIST)

    assert report.enriched is True
    assert report.visual_elements == 1
    assert env.visual_backend.calls
    assert env.visual_backend.calls[0]["query"] == "Play"

    state = env.cache.current
    assert state is not None
    visual = [
        element for element in state.elements if element.source is PerceptionSource.VISUAL
    ]
    assert len(visual) == 1
    assert visual[0].accessible_name == "Play"
    # Visual grounding proves *where*, never *what* (section 38).
    assert visual[0].role is UIRole.UNKNOWN
    assert visual[0].effective_clickable is False


def test_credential_context_is_never_uploaded_and_never_ocrd(env_factory: EnvFactory) -> None:
    """Sections 42/55: a password field blocks the upload and is excluded from OCR."""
    password = _send_button().model_copy(
        update={"element_id": "pw", "role": UIRole.PASSWORD_INPUT, "text": None, "password": True}
    )
    env = env_factory(elements=[password], visual_matches=[visual_match("Play")])
    env.orchestrator.perceive()

    report = env.orchestrator.enrich(ElementQuery(text="Play"), mode=PolicyMode.ASSIST)

    assert report.enriched is False
    assert report.refused == "VISUAL_FALLBACK_DENIED"
    # The model was never called, and the credential region never reached Tesseract.
    assert env.visual_backend.called is False
    assert env.ocr_backend.calls == []


# -- Diagnostics --------------------------------------------------------------


def test_diagnostics_report_measurements_not_assumptions(env_factory: EnvFactory) -> None:
    """The layer can say what it actually did, including what it could not do."""
    env = env_factory(elements=[_send_button()], capture_fail=True)

    env.orchestrator.perceive()
    report = env.orchestrator.diagnostics()

    assert report["cycles"] == 0
    assert report["capture_failures"] == 1
    assert report["ocr_available"] is True
    assert report["accessibility"] == "wired"
    assert report["enrichments"] == 0


def test_a_change_older_than_the_ocr_deadline_is_still_bounded(env_factory: EnvFactory) -> None:
    """A wall-to-wall change is too large for section 40, so OCR correctly skips it."""
    env = env_factory(elements=[_send_button()], ocr_text="Play")
    env.orchestrator.perceive()

    # Every pixel moves: the changed region covers the desktop, which section 40's
    # max_area_ratio excludes. The honest outcome is that OCR contributes nothing.
    env.capture.base = 250
    env.orchestrator.perceive()

    delta = env.cache.delta
    assert delta is not None
    assert delta.change_class is ChangeClass.MAJOR
    assert all(
        element.source is not PerceptionSource.OCR
        for element in (env.cache.current.elements if env.cache.current else ())
    )


def test_change_class_of_a_small_patch_is_meaningful_not_major(env_factory: EnvFactory) -> None:
    """A modest patch is a MEANINGFUL change; only a large one is MAJOR (section 34)."""
    env = env_factory(elements=[_send_button()])
    env.orchestrator.perceive()

    env.capture.add_patch(400, 500, 400, 150, level=220)
    env.orchestrator.perceive()

    assert env.cache.delta is not None
    assert env.cache.delta.change_class is ChangeClass.MEANINGFUL


def test_orchestrator_is_usable_directly_as_the_perceive_hook(env_factory: EnvFactory) -> None:
    """The hook signature the executor and dispatcher require: ``() -> state | None``."""
    env = env_factory(elements=[_send_button()])
    hook: object = env.orchestrator
    assert isinstance(hook, PerceptionOrchestrator)

    state = env.orchestrator()

    assert state is not None
    assert env.cache.current is state


def test_settings_are_not_mutated_by_a_perception_cycle(env_factory: EnvFactory) -> None:
    """Perception observes; it never rewrites configuration."""
    settings = Settings()
    before = settings.model_dump(mode="json")
    env = env_factory(elements=[_send_button()], settings=settings)

    env.orchestrator.perceive()

    assert settings.model_dump(mode="json") == before
