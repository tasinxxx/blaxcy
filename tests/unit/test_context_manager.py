"""Context budget and the delta-oriented state view (sections 68, 68.1).

The point of these tests is what the Brain does *not* receive: an unchanged
screen it already saw, a credential value, or element content from an
application the operator marked protected. The budget side is checked for the
opposite reason -- the newest turn must always survive, or the trim would remove
the very thing the next call needs.
"""

from __future__ import annotations

from ai.brain_adapter import ConversationTurn, ModelToolCall, ToolResultPayload
from ai.context_manager import ContextManager
from config.settings import GeminiSettings, SafetySettings, Settings
from core.state_cache import StateCache
from schemas.elements import UIElement
from schemas.enums import UIRole
from tests.harness.phase8 import make_element, make_state


def _cache_with(*elements: UIElement, active_app: str = "fixture") -> StateCache:
    """A cache seeded with one observation of ``elements``."""
    cache = StateCache()
    cache.update_screen_state(make_state(elements=tuple(elements), active_app=active_app))
    return cache


# -- State context ------------------------------------------------------------

def test_the_first_snapshot_is_full() -> None:
    """The first state the Brain sees carries its stamps and elements."""
    manager = ContextManager(Settings())
    cache = _cache_with(make_element("e1", text="Send"))
    payload = manager.state_context(cache)
    assert payload["observed"] is True
    assert payload["unchanged"] is False
    assert payload["frame_id"] == 5
    assert len(payload["elements"]) == 1


def test_an_unchanged_snapshot_becomes_a_marker() -> None:
    """Section 68.1: do not re-send a screen BLAXCY already reported."""
    manager = ContextManager(Settings())
    cache = _cache_with(make_element("e1", text="Send"))
    manager.state_context(cache)
    again = manager.state_context(cache)
    assert again["unchanged"] is True
    assert "elements" not in again
    current = cache.current
    assert current is not None
    assert again["state_version"] == current.state_version


def test_a_new_state_version_is_sent_in_full_again() -> None:
    """A real change is a new snapshot, not another marker."""
    manager = ContextManager(Settings())
    cache = _cache_with(make_element("e1", text="Send"))
    manager.state_context(cache)
    cache.update_screen_state(
        make_state(frame_id=9, elements=(make_element("e2", text="Play"),))
    )
    payload = manager.state_context(cache)
    assert payload["unchanged"] is False
    assert payload["elements"][0]["text"] == "Play"


def test_forget_state_forces_a_full_snapshot() -> None:
    """After a takeover resume the Brain must not be told 'unchanged'."""
    manager = ContextManager(Settings())
    cache = _cache_with(make_element("e1", text="Send"))
    manager.state_context(cache)
    manager.forget_state()
    payload = manager.state_context(cache)
    assert payload["unchanged"] is False
    assert payload["elements"]


def test_force_attaches_the_snapshot_even_when_unchanged() -> None:
    """A caller can deliberately re-send the full view."""
    manager = ContextManager(Settings())
    cache = _cache_with(make_element("e1", text="Send"))
    manager.state_context(cache)
    payload = manager.state_context(cache, force=True)
    assert payload["unchanged"] is False
    assert payload["elements"]


def test_no_observation_is_reported_as_not_observed() -> None:
    """Nothing perceived is not the same as an empty desktop."""
    manager = ContextManager(Settings())
    assert manager.state_context(StateCache()) == {"observed": False}


def test_elements_are_capped_and_the_truncation_is_declared() -> None:
    """The declared cap is honoured and disclosed."""
    manager = ContextManager(Settings(), max_elements=2)
    cache = _cache_with(*(make_element(f"e{index}", text=f"b{index}") for index in range(5)))
    payload = manager.state_context(cache)
    assert len(payload["elements"]) == 2
    assert payload["elements_truncated"] is True


def test_elements_can_be_omitted_entirely() -> None:
    """A stamps-only call attaches no element payload at all."""
    manager = ContextManager(Settings())
    cache = _cache_with(make_element("e1", text="Send"))
    payload = manager.state_context(cache, include_elements=False)
    assert payload["elements"] == []


def test_a_protected_application_suppresses_all_element_content() -> None:
    """Sections 42/55/68: protected context never leaves, not even its labels."""
    settings = Settings().model_copy(
        update={"safety": SafetySettings(protected_applications=("keepassxc",))}
    )
    manager = ContextManager(settings)
    cache = _cache_with(make_element("e1", text="Secret Button"), active_app="KeePassXC")
    payload = manager.state_context(cache)
    assert payload["protected"] is True
    assert payload["elements"] == []
    assert "keepassxc" in payload["protected_reason"]


def test_an_unprotected_application_is_not_affected() -> None:
    """The protected list only suppresses the applications it names."""
    settings = Settings().model_copy(
        update={"safety": SafetySettings(protected_applications=("keepassxc",))}
    )
    manager = ContextManager(settings)
    cache = _cache_with(make_element("e1", text="Send"), active_app="firefox")
    payload = manager.state_context(cache)
    assert "protected" not in payload
    assert payload["elements"]


def test_a_password_value_never_reaches_the_model_context() -> None:
    """The credential boundary is structural, not a formatting choice."""
    manager = ContextManager(Settings())
    cache = _cache_with(
        make_element(
            "pw",
            role=UIRole.PASSWORD_INPUT,
            password=True,
            text=None,
            accessible_name="Password",
        )
    )
    payload = manager.state_context(cache)
    entry = payload["elements"][0]
    assert entry["password"] is True
    assert entry["text"] is None


# -- Conversation budget ------------------------------------------------------

def _tool_turn(name: str, *, ok: bool, code: str | None = None) -> ConversationTurn:
    """A model turn calling ``name`` plus the matching tool result turn."""
    envelope: dict[str, object] = {"ok": ok, "verification": "VERIFIED"}
    if code:
        envelope["error_code"] = code
    return ConversationTurn(
        role="tool",
        tool_results=(ToolResultPayload(name=name, envelope=envelope),),
    )


def test_a_short_conversation_is_kept_verbatim_with_no_summary() -> None:
    """Nothing is condensed while the conversation is small."""
    manager = ContextManager(Settings(), keep_recent_turns=4)
    turns = (
        ConversationTurn(role="user", text="open the app"),
        ConversationTurn(
            role="model",
            text="clicking",
            tool_calls=(ModelToolCall(name="click", args={"target": "Send"}),),
        ),
        _tool_turn("click", ok=True),
    )
    fitted = manager.fit(turns)
    assert fitted.turns == turns
    assert fitted.summary is None
    assert fitted.report.turns_summarized == 0
    assert fitted.report.turns_kept == 3


def test_older_turns_are_condensed_into_a_factual_summary() -> None:
    """Condensation records what happened, without a second model call."""
    manager = ContextManager(Settings(), keep_recent_turns=2)
    turns = (
        ConversationTurn(role="user", text="task"),
        _tool_turn("click", ok=False, code="TARGET_AMBIGUOUS"),
        ConversationTurn(role="user", text="middle"),
        ConversationTurn(role="user", text="latest"),
    )
    fitted = manager.fit(turns)
    assert fitted.report.turns_summarized == 2
    assert fitted.report.turns_kept == 2
    assert fitted.summary is not None
    assert "TARGET_AMBIGUOUS" in fitted.summary
    assert "click" in fitted.summary


def test_the_budget_drops_the_oldest_turns_but_keeps_the_newest() -> None:
    """Trimming must never remove the information the next call needs."""
    settings = Settings(gemini=GeminiSettings(context_token_budget=128))
    manager = ContextManager(settings, keep_recent_turns=8)
    turns = [
        ConversationTurn(role="user", text="x" * 400),
        ConversationTurn(role="user", text="y" * 400),
        ConversationTurn(role="user", text="newest"),
    ]
    fitted = manager.fit(turns)
    assert fitted.report.turns_dropped >= 1
    assert fitted.turns[-1].text == "newest"
    assert fitted.report.estimated_tokens <= fitted.report.token_budget


def test_an_empty_conversation_is_handled() -> None:
    """Fitting nothing reports nothing, rather than raising."""
    manager = ContextManager(Settings())
    fitted = manager.fit(())
    assert fitted.turns == ()
    assert fitted.report.turns_considered == 0


def test_the_last_report_is_retrievable() -> None:
    """The GUI/benchmarks can read back what the last fit did."""
    manager = ContextManager(Settings())
    manager.fit((ConversationTurn(role="user", text="hi"),))
    assert manager.last_report is not None
    assert manager.last_report.turns_kept == 1
