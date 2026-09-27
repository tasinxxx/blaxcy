"""Gemini Brain integration (specification sections 67, 69).

The initial official remote Brain. Three implementation decisions are worth
stating, because each one is a place this could have gone wrong:

* **Manual function calling only.** ``automatic_function_calling`` is explicitly
  *disabled* in the request config. If it were left on, the SDK would execute
  BLAXCY's tools itself and BLAXCY would lose the policy/confirmation/verification
  control section 67 requires -- the model would be calling functions directly.
  With it off, the SDK returns the calls and :class:`~ai.brain_adapter.AgentLoop`
  walks them, one at a time, through the dispatcher.
* **The SDK is imported lazily.** ``google.genai`` is probed at call time, so
  importing this module (and therefore BLAXCY) works on a machine without the
  SDK, and a missing SDK is an honest ``BACKEND_UNAVAILABLE`` rather than an
  ImportError at startup.
* **The API key never appears in a message.** It is read from the keyring by
  :class:`~security.keyring_manager.ApiKeyManager`, and every third-party error
  string is passed through :func:`~security.keyring_manager.redact_secret`
  before it becomes a :class:`~ai.brain_adapter.BrainError`. An SDK that echoes
  the key in an exception would otherwise put it in BLAXCY's logs.

The API shape used here was verified against the installed ``google-genai``
version (2.20.0): ``FunctionDeclaration.parameters_json_schema`` accepts the
JSON schema BLAXCY already declares, function responses are sent back as
``Content(role="user", parts=[Part.from_function_response(...)])``, and a
model's ``thought_signature`` lives on the ``Part`` that wraps each
``FunctionCall`` -- so it is captured per part here and echoed back on the
replayed call, as Gemini 3 models require.

One consequence of that verification: the aggregated
``GenerateContentResponse.function_calls`` property flattens each part down to
its bare ``FunctionCall`` and silently drops the ``thought_signature``, so
:meth:`GeminiAdapter._to_model_turn` walks the candidate's parts itself instead.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, Final

from ai.brain_adapter import (
    ROLE_MODEL,
    ROLE_TOOL,
    BrainAdapter,
    BrainError,
    ConversationTurn,
    ModelToolCall,
    ModelTurn,
)
from ai.tool_protocol import ToolDeclaration
from config.settings import GeminiSettings
from schemas.enums import ErrorCode
from security.keyring_manager import ApiKeyManager, KeyringError, redact_secret

#: Google's documented escape hatch for a replayed ``functionCall`` part that
#: arrived without a ``thought_signature``. Gemini 3 models enforce the signature
#: and answer a signed call replayed without one with ``400 INVALID_ARGUMENT``
#: whose text talks about function-call/response ordering rather than the
#: signature, so the cause is opaque from the error alone. Google advises the
#: sentinel only as a last resort because it costs some model performance, so it
#: is used only when the provider genuinely returned no signature to echo.
#: https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/thinking/thought-signatures
_SKIP_SIGNATURE_SENTINEL: Final[bytes] = b"skip_thought_signature_validator"

#: Detected once at import, reported honestly, never assumed. Held as ``Any``
#: so an absent SDK really is ``None`` rather than a module object.
_genai: Any = None
_types: Any = None
_SDK_ERROR: str | None = None
try:  # pragma: no cover - exercised by the SDK-present installation path
    from google import genai as _genai_module
    from google.genai import types as _types_module

    _genai = _genai_module
    _types = _types_module
except Exception as exc:  # pragma: no cover - depends on the host
    _SDK_ERROR = f"{type(exc).__name__}: {exc}"


def sdk_available() -> bool:
    """Whether the ``google-genai`` SDK can be imported here."""
    return _genai is not None


class GeminiAdapter(BrainAdapter):
    """A Gemini-backed Brain connection (sections 67, 69).

    Args:
        settings: The ``[gemini]`` configuration (model and limits).
        client: An injected client (tests, or a caller that manages credentials
            itself). When omitted, one is built from the keyring API key.
        api_key: An explicit key, for a caller that already resolved it. Never
            read from a CLI argument (section 69).
        key_manager: Keyring reader; defaults to :class:`ApiKeyManager`.
        temperature: Optional sampling temperature. Left unset by default so
            BLAXCY does not impose a value the operator did not choose.
    """

    name = "gemini"

    def __init__(
        self,
        settings: GeminiSettings,
        *,
        client: Any = None,
        api_key: str | None = None,
        key_manager: ApiKeyManager | None = None,
        temperature: float | None = None,
    ) -> None:
        if _genai is None and client is None:
            raise BrainError(
                ErrorCode.BACKEND_UNAVAILABLE,
                "the google-genai SDK is not importable",
                details={"sdk_error": _SDK_ERROR},
            )
        self._settings = settings
        self._key_manager = key_manager if key_manager is not None else ApiKeyManager()
        self._api_key = api_key
        self._temperature = temperature
        self._client = client if client is not None else self._build_client()

    # -- Construction ---------------------------------------------------------

    @classmethod
    def from_keyring(
        cls,
        settings: GeminiSettings,
        *,
        key_manager: ApiKeyManager | None = None,
    ) -> GeminiAdapter:
        """Build an adapter whose credential comes from the OS keyring.

        Raises:
            BrainError: With ``BACKEND_UNAVAILABLE`` when no key is stored, and
                with the same code when the keyring backend itself is broken --
                the two cases carry different details, because the fix differs.
        """
        manager = key_manager if key_manager is not None else ApiKeyManager()
        try:
            key = manager.gemini_key()
        except KeyringError as exc:
            raise BrainError(exc.code, exc.message, details=exc.details) from exc
        if not key:
            raise BrainError(
                ErrorCode.BACKEND_UNAVAILABLE,
                "no Gemini API key is stored in the keyring",
                details=manager.status(),
            )
        return cls(settings, api_key=key, key_manager=manager)

    def _build_client(self) -> Any:
        """Construct the SDK client from the resolved key."""
        if not self._api_key:
            raise BrainError(
                ErrorCode.BACKEND_UNAVAILABLE,
                "no Gemini API key was supplied",
                details=self._key_manager.status(),
            )
        try:
            return _genai.Client(api_key=self._api_key)
        except Exception as exc:
            raise BrainError(
                ErrorCode.MODEL_ERROR,
                "the Gemini client could not be constructed",
                details={"error": self._redact(str(exc))},
            ) from exc

    # -- The one operation ----------------------------------------------------

    def generate(
        self,
        *,
        system_instruction: str,
        turns: Sequence[ConversationTurn],
        tools: Sequence[ToolDeclaration],
    ) -> ModelTurn:
        """Send the conversation and return the model's next turn.

        Raises:
            BrainError: On a transport failure, an authentication failure, or a
                response whose shape cannot be understood. Never returns an
                empty turn to signal failure.
        """
        contents = self._to_contents(turns)
        config = self._build_config(system_instruction, tools)
        try:
            response = self._client.models.generate_content(
                model=self._settings.model, contents=contents, config=config
            )
        except BrainError:
            raise
        except Exception as exc:
            raise self._classify(exc) from exc
        return self._to_model_turn(response)

    def close(self) -> None:
        """Close the underlying client if it offers a close operation."""
        closer = getattr(self._client, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:  # pragma: no cover - close is best-effort
                return

    def describe(self) -> dict[str, Any]:
        """Safe, loggable metadata about this adapter: never the key."""
        return {
            "adapter": self.name,
            "model": self._settings.model,
            "sdk_available": sdk_available(),
            "max_model_turns": self._settings.max_model_turns,
            "max_task_wall_clock_seconds": self._settings.max_task_wall_clock_seconds,
            "context_token_budget": self._settings.context_token_budget,
            **self._key_manager.status(),
        }

    # -- Translation ----------------------------------------------------------

    def _build_config(self, system_instruction: str, tools: Sequence[ToolDeclaration]) -> Any:
        """Build the request config with automatic function calling disabled."""
        declarations = [
            _types.FunctionDeclaration(
                name=declaration.name,
                description=declaration.description,
                parameters_json_schema=declaration.parameters,
            )
            for declaration in tools
        ]
        kwargs: dict[str, Any] = {
            "system_instruction": system_instruction,
            "tools": [_types.Tool(function_declarations=declarations)],
            # Section 67: BLAXCY walks the tool loop, not the SDK.
            "automatic_function_calling": _types.AutomaticFunctionCallingConfig(disable=True),
        }
        if self._temperature is not None:
            kwargs["temperature"] = self._temperature
        return _types.GenerateContentConfig(**kwargs)

    def _to_contents(self, turns: Sequence[ConversationTurn]) -> list[Any]:
        """Translate neutral turns into SDK content objects."""
        contents: list[Any] = []
        for turn in turns:
            if turn.role == ROLE_MODEL:
                parts: list[Any] = []
                if turn.text:
                    parts.append(_types.Part(text=turn.text))
                for call in turn.tool_calls:
                    call_kwargs: dict[str, Any] = {"name": call.name}
                    if call.args:
                        call_kwargs["args"] = dict(call.args)
                    if call.call_id:
                        call_kwargs["id"] = call.call_id
                    part_kwargs: dict[str, Any] = {
                        "function_call": _types.FunctionCall(**call_kwargs)
                    }
                    # Gemini 3 models validate that the thought_signature which
                    # rode in with a function call is echoed back on the same
                    # part; a replayed call without one is a 400 INVALID_ARGUMENT.
                    # When the provider returned no signature to echo, the
                    # documented sentinel keeps the replay valid instead of
                    # halting the task.
                    part_kwargs["thought_signature"] = (
                        call.thought_signature or _SKIP_SIGNATURE_SENTINEL
                    )
                    parts.append(_types.Part(**part_kwargs))
                contents.append(_types.Content(role="model", parts=parts or [_types.Part(text="")]))
                continue
            if turn.role == ROLE_TOOL:
                # A function response goes back with role "user" (verified against
                # google-genai 2.20.0's own automatic-function-calling path). The
                # call's ``id`` travels with the response too: a multi-step turn
                # (several function calls and responses) is matched by id, and a
                # response missing its id is answered with 400 INVALID_ARGUMENT
                # ("function response turn comes immediately after a function
                # call turn"). ``Part.from_function_response`` cannot carry it, so
                # the part is built directly.
                responses: list[Any] = []
                for result in turn.tool_results:
                    response_kwargs: dict[str, Any] = {
                        "name": result.name,
                        "response": dict(result.envelope),
                    }
                    if result.call_id:
                        response_kwargs["id"] = result.call_id
                    responses.append(
                        _types.Part(
                            function_response=_types.FunctionResponse(**response_kwargs)
                        )
                    )
                if responses:
                    contents.append(_types.Content(role="user", parts=responses))
                continue
            # Anything else is an ordinary user message (the default role).
            contents.append(_types.Content(role="user", parts=[_types.Part(text=turn.text or "")]))
        return contents

    def _to_model_turn(self, response: Any) -> ModelTurn:
        """Parse the SDK response into a :class:`ModelTurn`."""
        text = self._response_text(response)
        calls: list[ModelToolCall] = []
        for call, signature in self._function_call_parts(response):
            name = getattr(call, "name", None)
            if not name:
                continue
            raw_args = getattr(call, "args", None)
            if isinstance(raw_args, str):
                try:
                    raw_args = json.loads(raw_args)
                except json.JSONDecodeError:
                    raw_args = {"_raw": raw_args}
            calls.append(
                ModelToolCall(
                    name=str(name),
                    args=dict(raw_args) if isinstance(raw_args, dict) else {},
                    call_id=getattr(call, "id", None),
                    thought_signature=signature,
                )
            )
        return ModelTurn(
            text=text,
            tool_calls=tuple(calls),
            finish_reason=self._finish_reason(response),
        )

    def _function_call_parts(self, response: Any) -> list[tuple[Any, bytes | None]]:
        """The response's function calls, each paired with its signature.

        This walks the first candidate's parts exactly the way the SDK's
        aggregated ``response.function_calls`` property does, but keeps the
        wrapping ``Part`` around each call -- which is where the
        ``thought_signature`` lives (google-genai 2.20.0). The aggregate drops
        it, and a replayed call without its signature is rejected by Gemini 3
        models with 400 INVALID_ARGUMENT.
        """
        candidates = getattr(response, "candidates", None) or ()
        for candidate in candidates[:1]:
            content = getattr(candidate, "content", None)
            parts = getattr(content, "parts", None) or ()
            found: list[tuple[Any, bytes | None]] = []
            for part in parts:
                call = getattr(part, "function_call", None)
                if call is not None:
                    found.append((call, getattr(part, "thought_signature", None) or None))
            return found
        return []

    def _response_text(self, response: Any) -> str | None:
        """Best-effort plain text, or ``None`` when the turn carried no text.

        The SDK raises when a response has no text part at all (a pure
        function-call turn); that is a normal case here, not an error, so it is
        translated to ``None`` rather than propagated.
        """
        try:
            text = response.text
        except Exception:
            text = None
        if text:
            return str(text)
        candidates = getattr(response, "candidates", None) or ()
        for candidate in candidates:
            content = getattr(candidate, "content", None)
            for part in getattr(content, "parts", None) or ():
                value = getattr(part, "text", None)
                if value:
                    return str(value)
        return None

    def _finish_reason(self, response: Any) -> str | None:
        """The first candidate's finish reason, when present."""
        candidates = getattr(response, "candidates", None) or ()
        for candidate in candidates:
            reason = getattr(candidate, "finish_reason", None)
            if reason is not None:
                return getattr(reason, "name", None) or str(reason)
        return None

    def _classify(self, exc: BaseException) -> BrainError:
        """Map an SDK failure onto the taxonomy, with the key redacted."""
        message = self._redact(str(exc)) or type(exc).__name__
        lowered = message.casefold()
        code = ErrorCode.MODEL_ERROR
        if "429" in message or "resource_exhausted" in lowered or "rate limit" in lowered:
            code = ErrorCode.RATE_LIMITED
        elif "api key" in lowered or "unauthenticated" in lowered or "permission_denied" in lowered:
            code = ErrorCode.BACKEND_UNAVAILABLE
        return BrainError(
            code,
            f"the Gemini request failed: {message[:300]}",
            details={"model": self._settings.model},
        )

    def _redact(self, text: str) -> str:
        """Remove the API key from any string before it leaves this object."""
        return redact_secret(text, self._api_key)


__all__ = ["GeminiAdapter", "sdk_available"]
