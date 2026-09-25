"""The Brain boundary and the manual tool loop (specification section 67).

BLAXCY is a BODY, not a Brain. This module is the seam: :class:`BrainAdapter` is
whatever reasoning model is connected, and :class:`AgentLoop` is the only thing
allowed to drive it.

Section 67 requires **manual** tool calling. That is not a style preference --
it is what keeps policy, confirmation, execution, verification, error handling,
cancellation and ``NOT_EXECUTED`` semantics in BLAXCY's hands instead of the
SDK's. So the loop below does the walking itself:

``send context + declarations -> receive a turn -> inspect its function calls ->
validate -> dispatch each call through ToolDispatcher -> feed the structured
results back -> repeat``

Four limits are enforced here rather than hoped for:

* ``max_model_turns`` -- a fixed number of model turns per task, no infinite
  exchange.
* ``max_task_wall_clock_seconds`` -- a fixed wall-clock ceiling per task.
* **Cancellation** -- the emergency stop and human takeover register
  :meth:`AgentLoop.cancel`, and the loop checks its token before every model
  turn and before every tool call, so a latched stop cannot be waiting behind a
  model round-trip.
* **No self-confirmation.** A ``CONFIRMATION_REQUIRED`` result is surfaced to a
  human through an injected prompt. Without one, the loop stops and says so: it
  never sets ``confirmed=True`` itself (section 56).
"""

from __future__ import annotations

import threading
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from ai.context_manager import ContextManager, compact_sequence_result
from ai.prompt_builder import system_instruction
from ai.tool_protocol import ToolCall, ToolDeclaration, ToolDispatcher, tool_declarations
from config.settings import Settings
from core.event_bus import EventBus
from core.state_cache import StateCache
from schemas.actions import ToolName
from schemas.enums import ErrorCode, VerificationState
from schemas.errors import BlaxcyError
from schemas.events import Event, EventType

#: Roles a :class:`ConversationTurn` can carry. The transport format (Gemini
#: ``Content`` objects, OpenAI messages, ...) belongs to the adapter, not here.
ROLE_USER = "user"
ROLE_MODEL = "model"
ROLE_TOOL = "tool"


class BrainError(BlaxcyError):
    """A Brain-side failure (model error, rate limit, malformed response).

    ``MODEL_ERROR`` and ``RATE_LIMITED`` are the two taxonomy codes used here.
    An adapter must never place the API key in ``message`` or ``details``
    (section 69); :func:`security.keyring_manager.redact_secret` is the tool for
    sanitising a third-party error string before it gets here.
    """


@dataclass(frozen=True)
class ModelToolCall:
    """One function call the model asked for, before BLAXCY validates it."""

    name: str
    args: dict[str, Any] = field(default_factory=dict)
    call_id: str | None = None


@dataclass(frozen=True)
class ToolResultPayload:
    """One tool result as it goes back to the model (section 66 envelope)."""

    name: str
    envelope: dict[str, Any]
    call_id: str | None = None

    @property
    def ok(self) -> bool:
        """Whether the tool call succeeded."""
        return bool(self.envelope.get("ok"))

    @property
    def error_code(self) -> str | None:
        """The structured error code, when the call failed."""
        value = self.envelope.get("error_code")
        return None if value is None else str(value)

    def to_dict(self) -> dict[str, Any]:
        """JSON-shaped payload."""
        return {"name": self.name, "call_id": self.call_id, "envelope": self.envelope}


@dataclass(frozen=True)
class ConversationTurn:
    """One neutral turn in the conversation BLAXCY and the Brain share."""

    role: str
    text: str | None = None
    tool_calls: tuple[ModelToolCall, ...] = ()
    tool_results: tuple[ToolResultPayload, ...] = ()


@dataclass(frozen=True)
class ModelTurn:
    """What one model call produced."""

    text: str | None = None
    tool_calls: tuple[ModelToolCall, ...] = ()
    finish_reason: str | None = None

    @property
    def has_tool_calls(self) -> bool:
        """True when the model asked BLAXCY to do something."""
        return bool(self.tool_calls)


class BrainAdapter(ABC):
    """The abstract Brain connection (specification section 67).

    An adapter translates a neutral conversation into its provider's request and
    translates the reply back into :class:`ModelTurn`. It never executes a tool,
    never confirms anything, and never sees a credential.
    """

    #: A short name for logs and capability reporting.
    name: str = "brain"

    @abstractmethod
    def generate(
        self,
        *,
        system_instruction: str,
        turns: Sequence[ConversationTurn],
        tools: Sequence[ToolDeclaration],
    ) -> ModelTurn:
        """Produce the next model turn for ``turns``.

        Raises:
            BrainError: On any transport, authentication or response-shape
                failure. An adapter must not return an empty :class:`ModelTurn`
                to mean "it failed" -- that would be indistinguishable from a
                model choosing to say nothing.
        """

    def close(self) -> None:
        """Release any client resources. Safe to call more than once."""
        return None


@dataclass(frozen=True)
class AgentRunResult:
    """The outcome of one :meth:`AgentLoop.run`."""

    task_id: str
    turns: int
    tool_calls: int
    final_text: str | None
    halted: bool
    halt_reason: str | None
    elapsed_ms: float
    envelopes: tuple[ToolResultPayload, ...] = ()
    confirmations: tuple[dict[str, Any], ...] = ()
    conversation: tuple[ConversationTurn, ...] = ()

    @property
    def verified(self) -> int:
        """How many tool results came back ``VERIFIED``."""
        return sum(
            1
            for payload in self.envelopes
            if payload.envelope.get("verification") == VerificationState.VERIFIED.value
        )

    @property
    def failed(self) -> int:
        """How many tool results came back not-ok."""
        return sum(1 for payload in self.envelopes if not payload.ok)

    def to_dict(self) -> dict[str, Any]:
        """JSON-shaped result for the GUI and the report."""
        return {
            "task_id": self.task_id,
            "turns": self.turns,
            "tool_calls": self.tool_calls,
            "final_text": self.final_text,
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "elapsed_ms": self.elapsed_ms,
            "verified": self.verified,
            "failed": self.failed,
            "envelopes": [payload.to_dict() for payload in self.envelopes],
            "confirmations": list(self.confirmations),
        }


class AgentLoop:
    """Drives one task through the Brain, one manual tool call at a time.

    Args:
        adapter: The connected Brain.
        dispatcher: BLAXCY's tool dispatcher -- the only route to the desktop.
        context_manager: Builds the budgeted context (section 68).
        settings: Full configuration; ``[gemini]`` sets the loop's limits.
        event_bus: Optional bus, for confirmation and error visibility.
        abort_check: The combined stop/takeover hook (section 63/62).
        state_cache: Optional, so the GUI can see the active task (section 65).
        confirmation_prompt: Optional ``(payload) -> bool`` asked when a human
            confirmation is required. Without it, the loop halts and reports
            that a confirmation is pending rather than proceeding.
        on_tool_result: Optional observer called after each tool result.
        mode_provider: Optional ``() -> str`` returning the current policy mode,
            so the instruction describes the mode actually in force (section 56).
        clock: Monotonic clock, injectable for deterministic tests.
    """

    def __init__(
        self,
        adapter: BrainAdapter,
        *,
        dispatcher: ToolDispatcher,
        context_manager: ContextManager,
        settings: Settings,
        event_bus: EventBus | None = None,
        abort_check: Callable[[], ErrorCode | None] | None = None,
        state_cache: StateCache | None = None,
        confirmation_prompt: Callable[[ToolResultPayload], bool] | None = None,
        on_tool_result: Callable[[ToolResultPayload], None] | None = None,
        mode_provider: Callable[[], str] | None = None,
        clock: Callable[[], float] = time.monotonic,
        max_model_turns: int | None = None,
        max_task_wall_clock_seconds: float | None = None,
    ) -> None:
        self._adapter = adapter
        self._dispatcher = dispatcher
        self._context = context_manager
        self._settings = settings
        self._bus = event_bus
        self._abort_check = abort_check
        self._cache = state_cache
        self._confirmation_prompt = confirmation_prompt
        self._on_tool_result = on_tool_result
        self._mode_provider = mode_provider
        self._clock = clock
        self._max_turns = (
            max_model_turns if max_model_turns is not None else settings.gemini.max_model_turns
        )
        self._max_seconds = (
            max_task_wall_clock_seconds
            if max_task_wall_clock_seconds is not None
            else float(settings.gemini.max_task_wall_clock_seconds)
        )
        self._cancel = threading.Event()
        self._running = False

    # -- Cancellation ---------------------------------------------------------

    def cancel(self) -> None:
        """Ask the loop to stop at the next checkpoint.

        Registered as an emergency-stop / takeover cancel callback, so a latched
        stop takes effect before the next model round-trip rather than after it.
        """
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        """Whether cancellation has been requested."""
        return self._cancel.is_set()

    # -- The loop -------------------------------------------------------------

    def run(self, task: str, *, task_id: str | None = None) -> AgentRunResult:
        """Run one task to completion, halt, or a limit (section 67).

        Args:
            task: The user-facing instruction handed to the Brain.
            task_id: Optional task id; a fresh one is generated otherwise.
        """
        started = self._clock()
        resolved_task_id = task_id or uuid.uuid4().hex
        self._cancel.clear()
        self._running = True
        if self._cache is not None:
            self._cache.set_active_task(resolved_task_id)
        conversation: list[ConversationTurn] = [ConversationTurn(role=ROLE_USER, text=task)]
        envelopes: list[ToolResultPayload] = []
        confirmations: list[dict[str, Any]] = []
        turns = 0
        halted = False
        halt_reason: str | None = None
        final_text: str | None = None

        try:
            while True:
                reason = self._stop_reason(started, turns)
                if reason is not None:
                    halted = True
                    halt_reason = reason
                    break

                fitted = self._context.fit(conversation)
                instruction = system_instruction(
                    tool_names=[declaration.name for declaration in tool_declarations()],
                    mode=self._mode_provider() if self._mode_provider is not None else None,
                )
                if fitted.summary:
                    instruction = f"{instruction}\n\n{fitted.summary}"
                try:
                    model_turn = self._adapter.generate(
                        system_instruction=instruction,
                        turns=fitted.turns,
                        tools=tool_declarations(),
                    )
                except BrainError as exc:
                    halted = True
                    halt_reason = exc.code.value
                    self._emit(EventType.ERROR, {"where": "brain", "error": exc.to_dict()})
                    break
                turns += 1

                if model_turn.text:
                    final_text = model_turn.text
                conversation.append(
                    ConversationTurn(
                        role=ROLE_MODEL,
                        text=model_turn.text,
                        tool_calls=model_turn.tool_calls,
                    )
                )
                if not model_turn.has_tool_calls:
                    # The model is done talking: no tool call means the task ends
                    # here. BLAXCY does not invent a follow-up action.
                    break

                results: list[ToolResultPayload] = []
                for call in model_turn.tool_calls:
                    reason = self._stop_reason(started, turns)
                    if reason is not None:
                        halted = True
                        halt_reason = reason
                        break
                    payload = self._execute(call, task_id=resolved_task_id)
                    envelopes.append(payload)
                    results.append(payload)
                    if self._on_tool_result is not None:
                        self._on_tool_result(payload)
                    if payload.error_code == ErrorCode.CONFIRMATION_REQUIRED.value:
                        confirmed, exchange = self._ask_confirmation(payload, call, resolved_task_id)
                        confirmations.append(exchange)
                        if confirmed:
                            payload = self._execute(call, task_id=resolved_task_id, confirmed=True)
                            envelopes.append(payload)
                            results[-1] = payload
                            if self._on_tool_result is not None:
                                self._on_tool_result(payload)
                        else:
                            halted = True
                            halt_reason = ErrorCode.CONFIRMATION_DENIED.value
                            break
                conversation.append(
                    ConversationTurn(role=ROLE_TOOL, tool_results=tuple(results))
                )
                if halted:
                    break

            return AgentRunResult(
                task_id=resolved_task_id,
                turns=turns,
                tool_calls=len(envelopes),
                final_text=final_text,
                halted=halted,
                halt_reason=halt_reason,
                elapsed_ms=max(0.0, (self._clock() - started) * 1000.0),
                envelopes=tuple(envelopes),
                confirmations=tuple(confirmations),
                conversation=tuple(conversation),
            )
        finally:
            self._running = False
            if self._cache is not None:
                self._cache.set_active_task(None)

    # -- Internals ------------------------------------------------------------

    def _execute(
        self, call: ModelToolCall, *, task_id: str, confirmed: bool = False
    ) -> ToolResultPayload:
        """Dispatch one model tool call and wrap its envelope."""
        envelope = self._dispatcher.dispatch(
            ToolCall(name=call.name, arguments=dict(call.args), call_id=call.call_id),
            task_id=task_id,
            confirmed=confirmed,
        )
        payload = envelope.to_dict()
        if call.name == ToolName.RUN_SEQUENCE:
            # Section 68.1: a batch result goes back as the delta the Brain needs
            # to decide what is next, not as N copies of the same resolution
            # detail. Every outcome, code and verification verdict survives.
            payload = compact_sequence_result(payload)
        return ToolResultPayload(name=call.name, envelope=payload, call_id=call.call_id)

    def _ask_confirmation(
        self, payload: ToolResultPayload, call: ModelToolCall, task_id: str
    ) -> tuple[bool, dict[str, Any]]:
        """Ask a human about a destructive action (section 56).

        Without a prompt the answer is ``False``: BLAXCY does not confirm on the
        user's behalf, and it reports the refusal rather than silently skipping
        the step.
        """
        message = str(payload.envelope.get("message") or "confirmation required")
        self._emit(
            EventType.CONFIRMATION_REQUESTED,
            {"tool": call.name, "message": message, "task_id": task_id},
        )
        if self._confirmation_prompt is None:
            exchange = {
                "tool": call.name,
                "message": message,
                "asked": False,
                "granted": False,
                "reason": "no confirmation prompt is wired",
            }
            return False, exchange
        granted = bool(self._confirmation_prompt(payload))
        self._emit(
            EventType.CONFIRMATION_RESOLVED,
            {"tool": call.name, "granted": granted, "task_id": task_id},
        )
        return granted, {
            "tool": call.name,
            "message": message,
            "asked": True,
            "granted": granted,
            "reason": None,
        }

    def _stop_reason(self, started: float, turns: int) -> str | None:
        """Why the loop must halt before doing more work, or ``None``."""
        if self._cancel.is_set():
            return "cancelled"
        if self._abort_check is not None:
            code = self._abort_check()
            if code is not None:
                return code.value
        if turns >= self._max_turns:
            return "max_model_turns"
        if (self._clock() - started) > self._max_seconds:
            return "max_task_wall_clock_seconds"
        return None

    def _emit(self, event_type: EventType, payload: dict[str, Any]) -> None:
        """Publish an event, never letting a bus failure break the loop."""
        if self._bus is None:
            return
        try:
            self._bus.publish(Event.create(event_type, payload))
        except Exception:  # pragma: no cover - the bus is defensive already
            return


__all__ = [
    "ROLE_MODEL",
    "ROLE_TOOL",
    "ROLE_USER",
    "AgentLoop",
    "AgentRunResult",
    "BrainAdapter",
    "BrainError",
    "ConversationTurn",
    "ModelToolCall",
    "ModelTurn",
    "ToolResultPayload",
]
