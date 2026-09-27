"""The Gemini adapter (specification sections 67, 69).

No network and no API key are involved: the SDK client is injected, so these
tests assert the two things that actually matter -- that BLAXCY's tool loop stays
manual (automatic function calling is disabled), and that the credential can
never escape through an error message or a status payload.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, cast

import pytest

from ai.brain_adapter import (
    BrainError,
    ConversationTurn,
    ModelToolCall,
    ToolResultPayload,
)
from ai.gemini_adapter import GeminiAdapter, sdk_available
from ai.tool_protocol import tool_declarations
from config.settings import GeminiSettings
from schemas.actions import ToolName
from schemas.enums import ErrorCode, VerificationState
from security.keyring_manager import ApiKeyManager

API_KEY = "AIzaSy-not-a-real-key-0000000000000000000"

requires_sdk = pytest.mark.skipif(
    not sdk_available(), reason="google-genai is not importable on this host"
)


class FakeModels:
    """A ``models`` resource that records requests and returns a scripted reply."""

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
        self.closed = False

    def close(self) -> None:
        """Record that the client was closed."""
        self.closed = True


class FakeKeyManager:
    """A keyring manager double."""

    def __init__(self, key: str | None = API_KEY, *, error: Exception | None = None) -> None:
        """Create the manager, optionally with a stored key or a failure."""
        self._key = key
        self._error = error
        self.service = "blaxcy"

    def gemini_key(self) -> str | None:
        """Return the scripted key."""
        if self._error is not None:
            raise self._error
        return self._key

    def status(self) -> dict[str, Any]:
        """A loggable status without the secret."""
        return {"keyring_service": self.service, "keyring_key": "gemini_api_key", "key_present": bool(self._key)}


def _adapter(
    response: Any = None,
    *,
    error: Exception | None = None,
    key: str | None = API_KEY,
    **kwargs: Any,
) -> tuple[GeminiAdapter, FakeModels, FakeClient]:
    """Build an adapter over a fake client."""
    models = FakeModels(response, error)
    client = FakeClient(models)
    kwargs.setdefault("key_manager", FakeKeyManager(key=key))
    adapter = GeminiAdapter(GeminiSettings(), client=client, api_key=key, **kwargs)
    return adapter, models, client


def _response(
    *,
    text: str | None = None,
    calls: tuple[Any, ...] = (),
    text_raises: bool = False,
) -> Any:
    """A stand-in for a GenerateContentResponse.

    Calls are embedded in ``candidates[0].content.parts`` the way the real SDK
    delivers them, each part carrying the call and its ``thought_signature``.
    """
    parts: list[Any] = [
        SimpleNamespace(function_call=call, thought_signature=None) for call in calls
    ]
    candidate = SimpleNamespace(content=SimpleNamespace(parts=parts), finish_reason=None)
    if text_raises:

        class _Raising:
            @property
            def text(self) -> str:
                raise ValueError("no text part in this response")

            candidates: tuple[Any, ...] = (candidate,)

        obj = _Raising()
        obj.function_calls = calls  # type: ignore[attr-defined]
        return obj
    return SimpleNamespace(text=text, function_calls=calls, candidates=(candidate,))


# -- SDK availability ---------------------------------------------------------

def test_sdk_availability_is_reported_honestly() -> None:
    """The flag reflects the real import, whatever it is on this host."""
    assert isinstance(sdk_available(), bool)


def test_a_missing_sdk_is_a_structured_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A machine without the SDK gets BACKEND_UNAVAILABLE, not an ImportError."""
    import ai.gemini_adapter as module

    monkeypatch.setattr(module, "_genai", None)
    with pytest.raises(BrainError) as excinfo:
        GeminiAdapter(GeminiSettings(), api_key=API_KEY)
    assert excinfo.value.code is ErrorCode.BACKEND_UNAVAILABLE


# -- Request shape ------------------------------------------------------------

@requires_sdk
def test_automatic_function_calling_is_disabled() -> None:
    """Section 67: BLAXCY walks the tool loop, not the SDK."""
    adapter, models, _client = _adapter(_response(text="hi"))
    adapter.generate(
        system_instruction="be careful",
        turns=(ConversationTurn(role="user", text="hi"),),
        tools=tool_declarations(),
    )
    config = models.calls[0]["config"]
    assert config.automatic_function_calling.disable is True


@requires_sdk
def test_every_tool_is_declared_to_the_model() -> None:
    """The provider sees BLAXCY's real schemas."""
    adapter, models, _client = _adapter(_response(text="hi"))
    adapter.generate(
        system_instruction="x",
        turns=(ConversationTurn(role="user", text="hi"),),
        tools=tool_declarations(),
    )
    config = models.calls[0]["config"]
    declared = {d.name for d in config.tools[0].function_declarations}
    assert declared == {declaration.name for declaration in tool_declarations()}
    click = next(d for d in config.tools[0].function_declarations if d.name == ToolName.CLICK)
    # The schema BLAXCY declares is the schema the provider receives, and the
    # result is not JSON-serialisable (it is an Any for dump purposes).
    assert click.parameters_json_schema is not None
    json_form = json.loads(click.model_dump_json())
    assert json_form["parameters_json_schema"]["required"] == ["target"]
    assert json_form["parameters_json_schema"]["additionalProperties"] is False


@requires_sdk
def test_the_model_and_instruction_are_forwarded() -> None:
    """The configured model name and the instruction reach the request."""
    adapter, models, _client = _adapter(_response(text="hi"))
    adapter.generate(
        system_instruction="be careful",
        turns=(ConversationTurn(role="user", text="hi"),),
        tools=tool_declarations(),
    )
    assert models.calls[0]["model"] == GeminiSettings().model
    assert models.calls[0]["config"].system_instruction == "be careful"


@requires_sdk
def test_temperature_is_only_set_when_chosen() -> None:
    """BLAXCY does not impose a sampling temperature the operator did not pick."""
    default_adapter, models, _client = _adapter(_response(text="hi"))
    default_adapter.generate(
        system_instruction="x", turns=(ConversationTurn(role="user", text="hi"),), tools=()
    )
    assert models.calls[0]["config"].temperature is None

    chosen, models2, _client2 = _adapter(_response(text="hi"), temperature=0.0)
    chosen.generate(
        system_instruction="x", turns=(ConversationTurn(role="user", text="hi"),), tools=()
    )
    assert models2.calls[0]["config"].temperature == 0.0


# -- Contents translation -----------------------------------------------------

@requires_sdk
def test_turns_translate_to_user_model_and_function_response_contents() -> None:
    """The neutral conversation becomes the SDK's expected roles and parts."""
    adapter, models, _client = _adapter(_response(text="hi"))
    adapter.generate(
        system_instruction="x",
        turns=(
            ConversationTurn(role="user", text="click send"),
            ConversationTurn(
                role="model",
                text="on it",
                tool_calls=(ModelToolCall(name=ToolName.CLICK, args={"target": "Send"}, call_id="c1"),),
            ),
            ConversationTurn(
                role="tool",
                tool_results=(
                    ToolResultPayload(
                        name=ToolName.CLICK,
                        envelope={"ok": True, "verification": VerificationState.VERIFIED.value},
                        call_id="c1",
                    ),
                ),
            ),
        ),
        tools=tool_declarations(),
    )
    contents = models.calls[0]["contents"]
    assert [content.role for content in contents] == ["user", "model", "user"]
    call_part = contents[1].parts[-1]
    assert call_part.function_call.name == ToolName.CLICK
    assert call_part.function_call.args == {"target": "Send"}
    response_part = contents[2].parts[0]
    assert response_part.function_response.name == ToolName.CLICK
    assert response_part.function_response.response["ok"] is True
    # The call's id rides back on its response: a multi-step turn is matched by
    # id, and a response missing it is rejected with 400 INVALID_ARGUMENT.
    assert response_part.function_response.id == "c1"


@requires_sdk
def test_a_function_response_without_a_call_id_carries_no_id() -> None:
    """A call that arrived without an id must not gain a fabricated one."""
    adapter, models, _client = _adapter(_response(text="hi"))
    adapter.generate(
        system_instruction="x",
        turns=(
            ConversationTurn(
                role="tool",
                tool_results=(
                    ToolResultPayload(name=ToolName.CLICK, envelope={"ok": True}),
                ),
            ),
        ),
        tools=tool_declarations(),
    )
    response_part = models.calls[0]["contents"][0].parts[0]
    assert response_part.function_response.name == ToolName.CLICK
    assert response_part.function_response.id is None


# -- Response parsing ---------------------------------------------------------

@requires_sdk
def test_function_calls_are_parsed_for_the_loop() -> None:
    """The adapter returns the model's calls; it never executes them."""
    raw_call = SimpleNamespace(name=ToolName.CLICK, args={"target": "Send"}, id="c1")
    adapter, _models, _client = _adapter(_response(calls=(raw_call,)))
    turn = adapter.generate(
        system_instruction="x", turns=(ConversationTurn(role="user", text="hi"),), tools=()
    )
    assert turn.has_tool_calls is True
    assert turn.tool_calls[0].name == ToolName.CLICK
    assert turn.tool_calls[0].args == {"target": "Send"}
    assert turn.tool_calls[0].call_id == "c1"


@requires_sdk
def test_the_thought_signature_is_captured_from_the_call_part() -> None:
    """Gemini 3 signs each function-call part; the capture keeps it with the call."""
    candidate = SimpleNamespace(
        content=SimpleNamespace(
            parts=[
                SimpleNamespace(
                    function_call=SimpleNamespace(
                        name=ToolName.CLICK, args={"target": "Send"}, id=None
                    ),
                    thought_signature=b"sig-abc",
                ),
            ]
        ),
        finish_reason=None,
    )
    adapter, _models, _client = _adapter(
        SimpleNamespace(text=None, function_calls=(), candidates=(candidate,))
    )
    turn = adapter.generate(
        system_instruction="x", turns=(ConversationTurn(role="user", text="hi"),), tools=()
    )
    assert turn.has_tool_calls is True
    assert turn.tool_calls[0].name == ToolName.CLICK
    assert turn.tool_calls[0].thought_signature == b"sig-abc"


@requires_sdk
def test_a_call_without_a_signature_captures_none() -> None:
    """A part that only signs emptyly yields no signature, not an empty string."""
    candidate = SimpleNamespace(
        content=SimpleNamespace(
            parts=[
                SimpleNamespace(
                    function_call=SimpleNamespace(name=ToolName.CLICK, args={}, id=None),
                    thought_signature=b"",
                ),
            ]
        ),
        finish_reason=None,
    )
    adapter, _models, _client = _adapter(
        SimpleNamespace(text=None, function_calls=(), candidates=(candidate,))
    )
    turn = adapter.generate(
        system_instruction="x", turns=(ConversationTurn(role="user", text="hi"),), tools=()
    )
    assert turn.tool_calls[0].thought_signature is None


@requires_sdk
def test_a_replayed_call_echoes_the_thought_signature_on_its_part() -> None:
    """The signature must ride back on the same part, or Gemini 3 rejects the request."""
    adapter, models, _client = _adapter(_response(text="hi"))
    adapter.generate(
        system_instruction="x",
        turns=(
            ConversationTurn(role="user", text="click send"),
            ConversationTurn(
                role="model",
                tool_calls=(
                    ModelToolCall(
                        name=ToolName.CLICK,
                        args={"target": "Send"},
                        call_id="c1",
                        thought_signature=b"sig-abc",
                    ),
                ),
            ),
        ),
        tools=tool_declarations(),
    )
    call_part = models.calls[0]["contents"][1].parts[-1]
    assert call_part.function_call.name == ToolName.CLICK
    assert call_part.function_call.args == {"target": "Send"}
    assert call_part.thought_signature == b"sig-abc"


@requires_sdk
def test_a_call_that_arrived_without_a_signature_replays_with_the_sentinel() -> None:
    """With no signature to echo, the documented sentinel keeps Gemini 3 from 400ing."""
    adapter, models, _client = _adapter(_response(text="hi"))
    adapter.generate(
        system_instruction="x",
        turns=(
            ConversationTurn(
                role="model",
                tool_calls=(ModelToolCall(name=ToolName.CLICK, args={"target": "Send"}),),
            ),
        ),
        tools=tool_declarations(),
    )
    call_part = models.calls[0]["contents"][0].parts[-1]
    assert call_part.thought_signature == b"skip_thought_signature_validator"


@requires_sdk
def test_a_pure_tool_call_turn_has_no_text_and_is_not_an_error() -> None:
    """The SDK raises when a response has no text part; that is normal here."""
    raw_call = SimpleNamespace(name=ToolName.CLICK, args={}, id=None)
    adapter, _models, _client = _adapter(_response(calls=(raw_call,), text_raises=True))
    turn = adapter.generate(
        system_instruction="x", turns=(ConversationTurn(role="user", text="hi"),), tools=()
    )
    assert turn.text is None
    assert turn.has_tool_calls is True


@requires_sdk
def test_text_is_recovered_from_candidates_when_the_property_is_empty() -> None:
    """A response with parts but no ``text`` property still yields its text."""
    part = SimpleNamespace(text="hello there")
    candidate = SimpleNamespace(content=SimpleNamespace(parts=[part]), finish_reason=None)
    adapter, _models, _client = _adapter(
        SimpleNamespace(text=None, function_calls=(), candidates=(candidate,))
    )
    turn = adapter.generate(
        system_instruction="x", turns=(ConversationTurn(role="user", text="hi"),), tools=()
    )
    assert turn.text == "hello there"


@requires_sdk
def test_arguments_arriving_as_a_json_string_are_parsed() -> None:
    """Some SDK paths hand back serialized arguments rather than a mapping."""
    raw_call = SimpleNamespace(name=ToolName.CLICK, args='{"target": "Send"}', id=None)
    adapter, _models, _client = _adapter(_response(calls=(raw_call,)))
    turn = adapter.generate(
        system_instruction="x", turns=(ConversationTurn(role="user", text="hi"),), tools=()
    )
    assert turn.tool_calls[0].args == {"target": "Send"}


# -- Failure classification and credential safety -----------------------------

@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("429 RESOURCE_EXHAUSTED: quota", ErrorCode.RATE_LIMITED),
        ("API key not valid. Please pass a valid API key.", ErrorCode.BACKEND_UNAVAILABLE),
        ("503 service unavailable", ErrorCode.MODEL_ERROR),
    ],
)
def test_sdk_failures_map_onto_the_taxonomy(message: str, expected: ErrorCode) -> None:
    """A failure is classified, so callers can act on the code, not the text."""
    adapter, _models, _client = _adapter(error=RuntimeError(message))
    with pytest.raises(BrainError) as excinfo:
        adapter.generate(
            system_instruction="x", turns=(ConversationTurn(role="user", text="hi"),), tools=()
        )
    assert excinfo.value.code is expected


def test_the_api_key_is_redacted_from_an_error_message() -> None:
    """An SDK that echoes the key must not put it into BLAXCY's logs."""
    adapter, _models, _client = _adapter(error=RuntimeError(f"bad request key={API_KEY}"))
    with pytest.raises(BrainError) as excinfo:
        adapter.generate(
            system_instruction="x", turns=(ConversationTurn(role="user", text="hi"),), tools=()
        )
    assert API_KEY not in str(excinfo.value)
    assert API_KEY not in repr(excinfo.value.details)


def test_describe_never_contains_the_key() -> None:
    """The loggable description reports presence, not the secret."""
    adapter, _models, _client = _adapter()
    description = adapter.describe()
    assert description["key_present"] is True
    assert API_KEY not in repr(description)


def test_close_releases_the_client() -> None:
    """Closing the adapter closes the SDK client."""
    adapter, _models, client = _adapter()
    adapter.close()
    assert client.closed is True


# -- Keyring integration ------------------------------------------------------

def test_from_keyring_builds_from_a_stored_key() -> None:
    """The credential comes from the keyring, never a config file or argv."""
    manager = cast(ApiKeyManager, FakeKeyManager())
    adapter = GeminiAdapter.from_keyring(GeminiSettings(), key_manager=manager)
    assert adapter.describe()["key_present"] is True


def test_from_keyring_without_a_key_is_structured() -> None:
    """An unconfigured Brain is BACKEND_UNAVAILABLE with the keyring status."""
    manager = cast(ApiKeyManager, FakeKeyManager(key=None))
    with pytest.raises(BrainError) as excinfo:
        GeminiAdapter.from_keyring(GeminiSettings(), key_manager=manager)
    assert excinfo.value.code is ErrorCode.BACKEND_UNAVAILABLE
    assert "keyring_key" in excinfo.value.details


def test_from_keyring_propagates_a_keyring_failure() -> None:
    """A broken keyring is reported as such, not as a missing key."""
    from security.keyring_manager import ApiKeyManager, KeyringError

    manager = cast(ApiKeyManager, FakeKeyManager(error=KeyringError("keyring is broken")))
    with pytest.raises(BrainError) as excinfo:
        GeminiAdapter.from_keyring(GeminiSettings(), key_manager=manager)
    assert excinfo.value.code is ErrorCode.BACKEND_UNAVAILABLE
