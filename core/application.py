"""The BLAXCY composition root (specification sections 1-3, 12, 32, 65, 71).

Every phase before this one built a part of the Body and tested it in isolation.
This module is the single place where those parts are *constructed together*: the
capture engine, the accessibility owner, OCR, the visual grounder, the resolver
and its cache, the input controllers and clipboard paster, the policy gate, the
executor, the sequence runner, the tool dispatcher, the Brain loop, the watchdog
and the crash guard. It is also what finally makes the OCR engine and the visual
grounder reachable, through :class:`~core.perception.PerceptionOrchestrator`.

Why it lives here rather than in ``gui/``: the same object has to work with no Qt
at all. ``python main.py status`` and the tests need the Body without a window,
and the GUI must be a view onto this rather than the thing that owns it.

Three properties are deliberate and load-bearing:

1. **One pipeline.** The executor is the only route to physical input, the tool
   dispatcher is the only door from a Brain call, and the sequence runner is a
   container over that same dispatcher. Composition adds no second path.
2. **Fail-closed composition.** A component that cannot work is wired in a state
   that *refuses* rather than one that pretends: no input backend yields
   :class:`~control.backends.unavailable.UnavailableBackend`, no Brain key yields
   no adapter (with the reason kept), and a capability that is not AVAILABLE
   blocks the action that needs it.
3. **Honest startup.** :meth:`BlaxcyApplication.start` records what actually came
   up. It never reports a component as ready because it was constructed.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from ai.brain_adapter import AgentLoop, AgentRunResult, BrainAdapter, ToolResultPayload
from ai.context_manager import ContextManager
from ai.gemini_adapter import GeminiAdapter
from ai.tool_protocol import ToolCall, ToolDispatcher, tool_declarations
from config.settings import Settings
from control.action_tracker import ActionTracker
from control.backends import resolve_backend
from control.backends.base import InputBackend
from control.clipboard import select_clipboard_paster
from control.emergency_stop import EmergencyStop, EmergencyStopResult, combine_abort_checks
from control.executor import Executor
from control.keyboard import KeyboardController
from control.mouse import MouseController
from control.recovery import RecoveryController
from control.sequence_runner import SequenceRunner
from control.takeover import ResumeResult, TakeoverController
from control.verifier import Verifier
from control.window_manager import WindowManager
from core.accessibility import AccessibilityService
from core.calibration import identity_geometry_map, layout_from_topology
from core.capability_probe import probe_all
from core.event_bus import EventBus
from core.frame_engine import FrameEngine
from core.logging_setup import get_logger
from core.ocr import OcrEngine
from core.perception import PerceptionOrchestrator
from core.resolver_cache import ResolverCache, scorer_or_none
from core.session_detector import SessionInfo, detect_session
from core.speculative_perceiver import SpeculativePerceiver, build_speculative_perceiver
from core.state_cache import StateCache, StateSnapshot
from core.target_resolver import TargetResolver
from core.visual_grounder import VisualGrounder
from policy.modes import ModeController
from policy.permissions import PermissionEngine
from schemas.actions import ActivationOutcome
from schemas.capability import CapabilityReport
from schemas.elements import UIElement
from schemas.enums import CapabilityName, CapabilityStatus, ErrorCode, PolicyMode
from schemas.errors import BlaxcyError
from schemas.events import Event, EventType
from schemas.geometry import MonitorGeometry, MonitorLayout
from schemas.screen_state import ScreenState
from watchdog.watchdog import CrashGuard, Watchdog

#: How a human confirmation is obtained. The executor asks for one for a
#: destructive action; the Brain loop asks when a tool result needs it.
ConfirmationFn = Callable[[str, dict[str, Any]], bool]

#: The geometry used when the capture backend cannot report a topology at all.
#:
#: This is a placeholder, not a guess dressed as a measurement, and it cannot
#: produce a wrong click: with no usable capture there is no observation, and the
#: executor refuses to act without a fresh state, so nothing can reach the input
#: transform. It exists only because the input controllers need *a* layout at
#: construction time; :meth:`BlaxcyApplication.start` records why it was used.
_PLACEHOLDER_LAYOUT: Final[MonitorLayout] = MonitorLayout(
    monitors=(MonitorGeometry(monitor_id=0, width=1, height=1, is_primary=True),)
)


_log = get_logger(__name__)


@dataclass
class StartupReport:
    """What actually came up, recorded rather than assumed (section 80)."""

    started: bool = False
    frame_engine: bool = False
    accessibility: bool = False
    input_backend: str | None = None
    clipboard: bool = False
    window_manager: bool = False
    brain: str | None = None
    watchdog: bool = False
    crash_guard: bool = False
    sequence_enabled: bool = False
    resolver_cache_enabled: bool = False
    visual_grounding_enabled: bool = False
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """JSON-shaped startup evidence."""
        return {
            "started": self.started,
            "frame_engine": self.frame_engine,
            "accessibility": self.accessibility,
            "input_backend": self.input_backend,
            "clipboard": self.clipboard,
            "window_manager": self.window_manager,
            "brain": self.brain,
            "watchdog": self.watchdog,
            "crash_guard": self.crash_guard,
            "sequence_enabled": self.sequence_enabled,
            "resolver_cache_enabled": self.resolver_cache_enabled,
            "visual_grounding_enabled": self.visual_grounding_enabled,
            "notes": list(self.notes),
        }


class BlaxcyApplication:
    """The assembled Body: every component, wired, plus the lifecycle around it.

    Args:
        settings: The validated configuration (sections 72/4 rule 25).
        session: Detected session facts. ``None`` detects them.
        confirmation: How to ask a human for confirmation. ``None`` is the honest
            headless default: a destructive action is refused
            (``CONFIRMATION_REQUIRED``) and a Brain task that needs one halts
            rather than proceeding unattended (sections 54, 56, 71).
        capability_report: An already-probed report. ``None`` probes at
            :meth:`start`.
        collect_capabilities: Whether to probe capabilities at construction.
        with_watchdog: Whether to run the section 64 watchdog and crash guard.
        frame_engine: Capture engine override. ``None`` builds the real one.
        accessibility: Accessibility owner override. ``None`` builds the real one.
        ocr: OCR engine override. ``None`` builds the real one.
        visual: Visual grounder override. ``None`` builds the real one.
        backend: Input backend override. ``None`` resolves a real one through its
            functional probe (falling back to the honest unavailable backend).
        clipboard: Clipboard paster override. ``None`` selects a real one.
        window_manager: Window controller override. ``None`` builds the real one.

        watchdog_path: Where the section 64 heartbeat record is written. ``None``
            uses the default runtime location, which is deliberately outside the
            project directory; tests direct it to a temporary directory so they
            never touch the user's state.

        The component overrides exist so the assembled Body is testable without a
        desktop. They are substitutes for the *transport*, never for a safety
        decision: every injected component still runs inside the same executor,
        lease and revalidation pipeline.
        clock: Monotonic clock, injectable for deterministic tests.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        session: SessionInfo | None = None,
        confirmation: ConfirmationFn | None = None,
        capability_report: CapabilityReport | None = None,
        collect_capabilities: bool = True,
        with_watchdog: bool = True,
        frame_engine: FrameEngine | None = None,
        accessibility: AccessibilityService | None = None,
        ocr: OcrEngine | None = None,
        visual: VisualGrounder | None = None,
        backend: InputBackend | None = None,
        clipboard: Any = None,
        window_manager: Any = None,
        watchdog_path: Path | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.session = session if session is not None else detect_session()
        self._clock = clock
        self.startup = StartupReport()
        self._confirmation = confirmation

        # -- Shared plumbing ---------------------------------------------------
        self.bus = EventBus()
        self.cache = StateCache(event_bus=self.bus)
        self.modes = ModeController(settings.safety, event_bus=self.bus, clock=clock)
        self.permissions = PermissionEngine(settings)
        self.recovery = RecoveryController(settings.recovery)
        self.tracker = ActionTracker(event_bus=self.bus, clock=clock)
        self.verifier = Verifier(settings.verification)

        # -- Perception --------------------------------------------------------
        self.frame_engine = frame_engine if frame_engine is not None else FrameEngine(settings.capture)
        #: The monitor layout the input transform is built from. On X11/XTest the
        #: INPUT space coincides with DESKTOP, which is why an identity geometry
        #: map is the right answer here and not a failed calibration degraded to
        #: one (section 31).
        self.layout_notes: list[str] = []
        self.layout, layout_note = self._initial_layout()
        if layout_note is not None:
            self.layout_notes.append(layout_note)
        self.geometry = identity_geometry_map(self.layout)
        self.accessibility = (
            accessibility
            if accessibility is not None
            else AccessibilityService(settings.accessibility, clock=clock)
        )
        self.ocr = ocr if ocr is not None else OcrEngine(settings.ocr, clock=clock)
        self.visual = (
            visual
            if visual is not None
            else VisualGrounder(settings.visual, model=settings.gemini.model, clock=clock)
        )
        self.windows: Any = window_manager if window_manager is not None else WindowManager()
        self.resolver_cache: ResolverCache | None = (
            ResolverCache(settings.resolver_cache, clock=clock)
            if settings.resolver_cache.enabled
            else None
        )
        self.resolver = TargetResolver(
            settings.resolver,
            cache=self.resolver_cache,
            semantic_scorer=scorer_or_none(),
            semantic_match_floor=float(settings.resolver_cache.semantic_match_floor),
        )
        self.perceiver = PerceptionOrchestrator(
            settings,
            frame_engine=self.frame_engine,
            state_cache=self.cache,
            accessibility=self.accessibility,
            ocr=self.ocr,
            visual=self.visual,
            resolver=self.resolver,
            window_manager=self.windows,
            event_bus=self.bus,
            clock=clock,
        )

        # -- Input -------------------------------------------------------------
        self.backend: InputBackend = backend if backend is not None else resolve_backend()
        self.clipboard: Any = (
            clipboard if clipboard is not None else select_clipboard_paster(settings.clipboard)
        )
        self.mouse = MouseController(self.backend, self.geometry, settings.input)
        self.keyboard = KeyboardController(
            self.backend, settings.input, clipboard=self.clipboard
        )

        # -- Safety ------------------------------------------------------------
        self.stop = EmergencyStop(event_bus=self.bus, mode_controller=self.modes, clock=clock)
        self.takeover = TakeoverController(
            event_bus=self.bus, mode_controller=self.modes, clock=clock
        )
        self.stop.register_input(self.mouse)
        self.stop.register_input(self.keyboard)
        self.takeover.register_input(self.mouse)
        self.takeover.register_input(self.keyboard)
        # Section 62: a resume must invalidate every lease and re-perceive before
        # automation may act again -- an interrupted plan is never resumed.
        self.takeover.register_reperceive(self._reperceive)
        self.takeover.register_lease_invalidator(self.cache.invalidate_leases)
        # An emergency stop is the stronger statement, so it is reported first.
        self.abort_check = combine_abort_checks(self.stop.abort_code, self.takeover.abort_code)

        # -- Capabilities ------------------------------------------------------
        self.capabilities: CapabilityReport | None = (
            capability_report
            if capability_report is not None
            else (probe_all(settings, self.session) if collect_capabilities else None)
        )
        if self.capabilities is not None:
            self.cache.set_capabilities(self.capabilities)

        # -- Execution ---------------------------------------------------------
        self.executor = Executor(
            settings,
            permissions=self.permissions,
            mode_controller=self.modes,
            state_cache=self.cache,
            resolver=self.resolver,
            mouse=self.mouse,
            keyboard=self.keyboard,
            verifier=self.verifier,
            tracker=self.tracker,
            event_bus=self.bus,
            window_manager=self.windows,
            perceive=self.perceiver.perceive,
            abort_check=self.abort_check,
            confirmation=self._confirm_action if confirmation is not None else None,
            capabilities=self.capabilities,
            calibration=None,
            recovery=self.recovery,
            resolve_fallback=self._resolve_fallback,
            activate=self._activate_element,
        )
        self.dispatcher = ToolDispatcher(
            settings,
            executor=self.executor,
            state_cache=self.cache,
            resolver=self.resolver,
            event_bus=self.bus,
            window_manager=self.windows,
            capabilities=lambda: self.capabilities,
            perceive=self.perceiver.perceive,
            emergency_stop=self.stop,
            abort_check=self.abort_check,
            target_fallback=self._resolve_fallback,
            clock=clock,
        )
        self.speculative: SpeculativePerceiver | None = (
            build_speculative_perceiver(settings.sequence, clock=clock)
            if settings.sequence.enabled and settings.sequence.speculative_perception
            else None
        )
        self.sequence_runner = SequenceRunner(
            settings,
            dispatcher=self.dispatcher,
            state_cache=self.cache,
            permissions=self.permissions,
            mode_controller=self.modes,
            event_bus=self.bus,
            recovery=self.recovery,
            cache=self.resolver_cache,
            speculative=self.speculative,
            capture_boost=(
                self.frame_engine.active_window if settings.sequence.capture_boost else None
            ),
            abort_check=self.abort_check,
            clock=clock,
            calibration=None,
        )
        if settings.sequence.enabled:
            self.dispatcher.attach_sequence_runner(self.sequence_runner)

        # -- Brain -------------------------------------------------------------
        self.context_manager = ContextManager(settings)
        self._adapter: BrainAdapter | None = None
        self._brain_reason: str | None = None
        self._loops: list[AgentLoop] = []
        self.stop.register_cancel(self.cancel_loops)
        self.takeover.register_cancel(self.cancel_loops)

        # -- Crash safety ------------------------------------------------------
        self.watchdog: Watchdog | None = (
            Watchdog(path=watchdog_path) if with_watchdog else None
        )
        self.crash_guard: CrashGuard | None = (
            CrashGuard(release_input=self._release_held_input, watchdog=self.watchdog)
            if with_watchdog
            else None
        )

    # -- Lifecycle -------------------------------------------------------------

    def start(self) -> StartupReport:
        """Bring the Body up and record what actually came up."""
        try:
            self.perceiver.start()
            self.startup.frame_engine = not self.frame_engine.capture_failed
        except BlaxcyError as exc:
            self.startup.notes.append(f"capture unavailable: {exc.code.value}")
        self.startup.notes.extend(self.layout_notes)
        self.startup.accessibility = self.accessibility.is_running
        if not self.startup.accessibility:
            self.startup.notes.append(
                "the accessibility tree is not available on this session, so BLAXCY "
                "cannot observe UI controls"
            )

        probe = self.backend.probe()
        self.startup.input_backend = probe.name if probe.available else None
        if not probe.available:
            self.startup.notes.append(
                f"input backend unavailable ({probe.reason}); the Body runs read-only "
                "and refuses physical input"
            )
        self.startup.clipboard = self.clipboard is not None
        try:
            self.startup.window_manager = self.windows.probe().available
        except BlaxcyError:
            self.startup.window_manager = False

        self._ensure_brain()
        self.startup.brain = self._adapter.name if self._adapter is not None else None
        self.startup.sequence_enabled = self.settings.sequence.enabled
        self.startup.resolver_cache_enabled = self.resolver_cache is not None
        self.startup.visual_grounding_enabled = self.visual.enabled

        if self.watchdog is not None:
            self.watchdog.start()
            self.startup.watchdog = self.watchdog.running
        if self.crash_guard is not None:
            self.crash_guard.install()
            self.startup.crash_guard = self.crash_guard.installed
        self.startup.started = True
        # Section 70: what actually came up, recorded in the log as well as in the
        # returned report. The backend is the one fact a later investigation needs
        # most, and it is safe metadata.
        _log.info(
            "body started",
            extra={
                "action_type": "startup",
                "ok": True,
                "backend": self.startup.input_backend,
                "state_version": None,
            },
        )
        return self.startup

    def _activate_element(self, element: UIElement) -> ActivationOutcome:
        """Perform the target's accessibility action (section 66).

        This is the wiring behind ``activate_element``: the executor owns policy,
        the lease and revalidation, and hands the already-revalidated element here
        so the accessibility service -- the single AT-SPI owner (section 35) -- can
        ask the application to perform its own action. A target with no
        accessibility path (an OCR or visual element, for example) cannot be
        activated this way and says so rather than falling back to a click the
        caller did not ask for.
        """
        if not self.accessibility.is_running:
            return ActivationOutcome(
                ok=False, reason="the accessibility service is not running on this host"
            )
        if not element.atspi_path:
            return ActivationOutcome(
                ok=False,
                reason="the target has no accessibility path, so it has no accessibility action",
            )
        return self.accessibility.activate(element.atspi_path)

    def shutdown(self) -> None:
        """Release held input, stop every thread and mark the run clean."""
        self.cancel_loops()
        if self.speculative is not None:
            self.speculative.shutdown()
        if self.crash_guard is not None:
            # Cleanup releases held input and marks the run clean; uninstall then
            # removes the handlers so a shutdown Body leaves no signal hooks (or
            # ``atexit`` entry) behind for a later test or restart to trip over.
            self.crash_guard.cleanup()
            self.crash_guard.uninstall()
        else:
            self._release_held_input()
        if self.watchdog is not None:
            self.watchdog.stop(clean=True)
        self.perceiver.stop()
        if self.clipboard is not None:
            self.clipboard.close()
        if self._adapter is not None:
            self._adapter.close()
        self._loops.clear()
        self.startup.started = False
        _log.info("body stopped", extra={"action_type": "shutdown", "ok": True})

    def __enter__(self) -> BlaxcyApplication:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.shutdown()

    # -- Perception and state --------------------------------------------------

    def perceive(self) -> ScreenState | None:
        """Observe the desktop once (the one perception entry point)."""
        return self.perceiver.perceive()

    def snapshot(self) -> StateSnapshot:
        """A consistent read of the whole current state, for the GUI."""
        return self.cache.snapshot()

    def status(self) -> dict[str, Any]:
        """A JSON-shaped status snapshot for the GUI and the CLI (section 71)."""
        state = self.cache.current
        sequence = self.cache.sequence
        readiness = None if state is None else state.is_fresh(
            float(self.settings.verification.max_action_state_age_ms), self._clock()
        )
        return {
            "started": self.startup.started,
            "session_type": self.session.session_type.value,
            "mode": self.modes.mode.value,
            "mode_status": self.modes.status().to_dict(),
            "emergency_stop": {
                "latched": self.stop.latched,
                "triggers": self.stop.trigger_count,
                "last_latency_ms": self.stop.last_latency_ms,
            },
            "takeover": self.takeover.status(),
            "sequence": None if sequence is None else sequence.to_dict(),
            "brain": {
                "adapter": None if self._adapter is None else self._adapter.name,
                "reason": self._brain_reason,
            },
            "state": {
                "frame_id": None if state is None else state.frame_id,
                "state_version": self.cache.state_version,
                "generation": self.cache.generation,
                "age_ms": None if state is None else state.age_ms(self._clock()),
                "fresh": readiness,
                "element_count": None if state is None else len(state.elements),
                "active_window_id": None if state is None else state.active_window_id,
                "active_app": None if state is None else state.active_app,
                "accepted_updates": self.cache.accepted_updates,
                "rejected_updates": self.cache.rejected_updates,
            },
            "startup": self.startup.to_dict(),
            "perception": self.perceiver.diagnostics(),
            "capabilities": (
                None
                if self.capabilities is None
                else {
                    "AVAILABLE": _count(self.capabilities, CapabilityStatus.AVAILABLE),
                    "DEGRADED": _count(self.capabilities, CapabilityStatus.DEGRADED),
                    "UNAVAILABLE": _count(self.capabilities, CapabilityStatus.UNAVAILABLE),
                }
            ),
        }

    def capability(self, name: CapabilityName) -> Any:
        """One capability verdict, or ``None`` when it was not probed."""
        return None if self.capabilities is None else self.capabilities.get(name)

    def declared_tools(self) -> tuple[str, ...]:
        """The tool names advertised to the Brain (section 66)."""
        return tuple(declaration.name for declaration in tool_declarations())

    # -- Safety operations -----------------------------------------------------

    def trigger_emergency_stop(self, reason: str = "user request") -> EmergencyStopResult:
        """Run the latched section 63 stop over the *wired* Body."""
        return self.stop.trigger(reason)

    def reset_emergency_stop(self, *, reason: str = "explicit re-arm") -> bool:
        """Clear the stop latch. Explicit, never automatic (section 63)."""
        return self.stop.reset(reason=reason)

    def begin_takeover(self, reason: str = "human took control") -> Any:
        """Hand the desktop back to the human (section 62)."""
        return self.takeover.begin(reason)

    def resume_takeover(self) -> ResumeResult:
        """Hand control back explicitly; invalidates leases and re-perceives."""
        return self.takeover.resume()

    def set_mode(self, mode: PolicyMode, *, reason: str | None = None) -> Any:
        """Change the policy mode, emitting ``MODE_CHANGED`` (section 56)."""
        return self.modes.set_mode(mode, reason=reason)

    def cancel_loops(self) -> None:
        """Ask every live Brain loop to stop at its next checkpoint."""
        for loop in list(self._loops):
            loop.cancel()

    # -- Brain -----------------------------------------------------------------

    def _ensure_brain(self) -> BrainAdapter | None:
        """Build the Brain adapter once, keeping the honest reason when it cannot."""
        if self._adapter is not None or self._brain_reason is not None:
            return self._adapter
        try:
            self._adapter = GeminiAdapter.from_keyring(self.settings.gemini)
        except BlaxcyError as exc:
            self._brain_reason = f"{exc.code.value}: {exc.message}"
            self._adapter = None
        return self._adapter

    @property
    def brain_reason(self) -> str | None:
        """Why no Brain is connected, or ``None`` when one is."""
        return self._brain_reason

    def build_loop(self, adapter: BrainAdapter | None = None) -> AgentLoop:
        """Construct the manual tool loop over the wired dispatcher (section 67).

        The loop is registered with the emergency stop and human takeover, so a
        latched stop cancels the model round-trip rather than waiting behind it.
        """
        brain = adapter if adapter is not None else self._ensure_brain()
        if brain is None:
            raise BlaxcyError(
                ErrorCode.BACKEND_UNAVAILABLE,
                f"no Brain is connected: {self._brain_reason or 'no adapter was built'}",
                details={"keyring_service": "blaxcy", "key": "gemini_api_key"},
            )
        loop = AgentLoop(
            brain,
            dispatcher=self.dispatcher,
            context_manager=self.context_manager,
            settings=self.settings,
            event_bus=self.bus,
            abort_check=self.abort_check,
            state_cache=self.cache,
            confirmation_prompt=(
                self._confirm_payload if self._confirmation is not None else None
            ),
            mode_provider=lambda: self.modes.mode.value,
            clock=self._clock,
        )
        self._loops.append(loop)
        return loop

    def run_task(
        self,
        task: str,
        *,
        task_id: str | None = None,
        adapter: BrainAdapter | None = None,
    ) -> AgentRunResult:
        """Run one task through the Brain and the real Body (section 67).

        Args:
            adapter: An explicit Brain. ``None`` uses the connected one (and
                raises with the honest reason when there is none).
        """
        loop = self.build_loop(adapter)
        try:
            return loop.run(task, task_id=task_id)
        finally:
            if loop in self._loops:
                self._loops.remove(loop)

    def dispatch(self, call: ToolCall) -> Any:
        """Dispatch one tool call directly (used by tests and the GUI console)."""
        return self.dispatcher.dispatch(call)

    # -- Confirmation (sections 54, 56, 71) ------------------------------------

    def _confirm_action(self, message: str, details: dict[str, Any]) -> bool:
        """Ask the human, publishing the request so a GUI can show it."""
        request_id = uuid.uuid4().hex
        self._publish(
            EventType.CONFIRMATION_REQUESTED,
            {"request_id": request_id, "message": message, "details": details},
        )
        granted = False
        callback = self._confirmation
        if callback is not None:
            try:
                granted = bool(callback(message, details))
            except Exception as exc:  # a broken prompt must never grant anything
                self.startup.notes.append(f"confirmation callback failed: {exc!r}")
                granted = False
        self._publish(
            EventType.CONFIRMATION_RESOLVED,
            {"request_id": request_id, "granted": granted, "message": message},
        )
        return granted

    def _confirm_payload(self, payload: ToolResultPayload) -> bool:
        """Adapt the executor-shaped confirmation to the Brain loop's payload."""
        return self._confirm_action(f"{payload.name} requires confirmation", payload.to_dict())

    # -- The section 43 fallback ----------------------------------------------

    def _resolve_fallback(self, query: Any) -> bool:
        """Run the perception fallback cascade for an unresolved target.

        Wired into both the executor and the dispatcher, so a target only OCR or
        (gated) visual grounding can find is reachable through the ordinary
        resolve -> lease -> revalidate -> execute -> verify pipeline -- never
        around it (sections 38, 41, 43).
        """
        return self.perceiver.enrich(query, mode=self.modes.mode).enriched

    # -- Internals -------------------------------------------------------------

    def _initial_layout(self) -> tuple[MonitorLayout, str | None]:
        """A real monitor layout, or a documented placeholder when none exists."""
        try:
            self.frame_engine.open()
        except BlaxcyError as exc:
            return _PLACEHOLDER_LAYOUT, (
                f"capture unavailable at construction ({exc.code.value}); the input "
                "geometry is a placeholder and no input can be injected without a "
                "captured state"
            )
        layout = layout_from_topology(self.frame_engine.monitor_topology)
        if layout is not None:
            return layout, None
        return _PLACEHOLDER_LAYOUT, (
            "the capture backend reported no monitor topology; the input geometry is "
            "a placeholder"
        )

    def _reperceive(self) -> None:
        """Full re-perception hook for a takeover resume (section 62).

        Registered as a no-argument callback, so the state it produces is offered
        to the cache rather than returned to the takeover controller.
        """
        self.perceiver.perceive()

    def _release_held_input(self) -> None:
        """Release everything BLAXCY holds, for the crash guard (section 64)."""
        try:
            self.backend.release_all()
        except Exception:  # pragma: no cover - a release must never raise out
            return

    def _publish(self, event_type: EventType, payload: dict[str, Any]) -> None:
        """Publish one event, never letting an event failure break an operation."""
        try:
            self.bus.publish(Event.create(event_type, payload))
        except Exception:  # pragma: no cover - the bus isolates subscriber errors
            return


def _count(report: CapabilityReport, status: CapabilityStatus) -> int:
    """How many capabilities report ``status``."""
    return len(report.by_status(status))


__all__ = [
    "BlaxcyApplication",
    "ConfirmationFn",
    "StartupReport",
]
