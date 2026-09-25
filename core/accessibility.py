"""Accessibility perception -- the primary perception source (specification section 35).

This module is the only place in BLAXCY that talks to AT-SPI. It exists to turn
the live accessibility tree into the same :class:`~schemas.elements.UIElement`
vocabulary every other perception source produces, so the resolver (section 43)
never needs to know how an element was observed.

Three rules shape the implementation, and each is load-bearing:

* **A dedicated thread owns AT-SPI.** GI/GLib objects are never touched
  concurrently from arbitrary threads. ``T-A11Y`` owns the backend, and every
  cross-thread request is marshalled to it through one queue. Callers get a
  structured ``A11Y_TIMEOUT`` when the thread cannot answer in time rather than
  a silent hang.
* **Traversal is bounded.** ``max_depth``, ``max_nodes`` and ``deadline_ms``
  cap every query. An accessibility tree can be enormous; an unbounded walk is
  an unbounded hang. ``Atspi.Collection`` is preferred when the installed
  implementation actually provides it, with the bounded BFS as a verified
  fallback -- the Collection interface is probed rather than assumed
  (section 82).
* **Credential content is never read.** A ``PASSWORD_INPUT`` element is
  reported with ``text=None`` and ``password=True``; its value is not read,
  logged, cached or uploaded (sections 42, 55). Editable text roles are
  likewise reported by their accessible name, not their current content, so a
  user's typed text is never swept into model context by perception alone.

The module is honest about what it cannot do: when ``gi``/AT-SPI is absent or
the accessibility bus is down, initialization raises and the capability report
says ``UNAVAILABLE`` with a reason. Nothing here fabricates a tree.
"""

from __future__ import annotations

import importlib
import queue
import threading
import time
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Final, Protocol

from config.settings import AccessibilitySettings
from schemas.actions import ActivationOutcome
from schemas.capability import Capability
from schemas.elements import UIElement, perception_confidence
from schemas.enums import (
    CapabilityName,
    CapabilityStatus,
    CoordinateSpace,
    ErrorCode,
    PerceptionSource,
    UIRole,
)
from schemas.errors import BlaxcyError
from schemas.geometry import Rect

#: Backend identifier reported in capability/benchmark output.
A11Y_BACKEND_NAME: Final[str] = "atspi"

#: Human-facing remediation hint for a failed AT-SPI initialization.
_A11Y_FIX_HINT: Final[str] = (
    "install python3-gi, gir1.2-atspi-2.0 and at-spi2-core, and ensure "
    "accessibility support is enabled for the session"
)

#: Default cooldown before a blacklisted object is retried (section 35).
_BLACKLIST_COOLDOWN_SECONDS: Final[float] = 30.0

#: How long the worker sleeps between queue polls while idle.
_QUEUE_POLL_SECONDS: Final[float] = 0.05

#: AT-SPI role name (as returned by ``get_role_name()``) to BLAXCY role. The
#: names are the documented AT-SPI "role name" strings, lower-cased; unknown
#: names map to ``UNKNOWN`` rather than being guessed into a wrong actionable
#: role (section 37).
ROLE_BY_NAME: Final[dict[str, UIRole]] = {
    "push button": UIRole.BUTTON,
    "button": UIRole.BUTTON,
    "toggle button": UIRole.TOGGLE,
    "check box": UIRole.CHECKBOX,
    "check menu item": UIRole.CHECKBOX,
    "radio button": UIRole.RADIO,
    "radio menu item": UIRole.RADIO,
    "link": UIRole.LINK,
    "entry": UIRole.TEXT_INPUT,
    "password text": UIRole.PASSWORD_INPUT,
    "combo box": UIRole.COMBO_BOX,
    "menu": UIRole.MENU,
    "menu item": UIRole.MENU_ITEM,
    "page tab": UIRole.TAB,
    "page tab list": UIRole.TAB,
    "list item": UIRole.LIST_ITEM,
    "tree item": UIRole.TREE_ITEM,
    "slider": UIRole.SLIDER,
    "dial": UIRole.SLIDER,
    "heading": UIRole.HEADING,
    "label": UIRole.LABEL,
    "caption": UIRole.LABEL,
    "accelerator label": UIRole.LABEL,
    "image": UIRole.IMAGE,
    "icon": UIRole.IMAGE,
    "video": UIRole.VIDEO,
    "canvas": UIRole.CANVAS,
    "drawing area": UIRole.CANVAS,
    "dialog": UIRole.DIALOG,
    "alert": UIRole.DIALOG,
    "file chooser": UIRole.DIALOG,
    "color chooser": UIRole.DIALOG,
    "tool bar": UIRole.TOOLBAR,
    "document frame": UIRole.DOCUMENT,
    "document text": UIRole.DOCUMENT,
    "document web": UIRole.DOCUMENT,
    "document email": UIRole.DOCUMENT,
    "document spreadsheet": UIRole.DOCUMENT,
    "document presentation": UIRole.DOCUMENT,
    "terminal": UIRole.TERMINAL,
    "text": UIRole.TEXT_FRAGMENT,
    "html container": UIRole.TEXT_FRAGMENT,
    "section": UIRole.TEXT_FRAGMENT,
    "paragraph": UIRole.TEXT_FRAGMENT,
    "form": UIRole.TEXT_FRAGMENT,
    "block quote": UIRole.TEXT_FRAGMENT,
    "comment": UIRole.TEXT_FRAGMENT,
}

#: BLAXCY role to the AT-SPI ``Role`` enum member names used by Collection.
#: Only used when the installed Collection interface actually works; a missing
#: member is skipped rather than assumed.
ROLE_TO_ATSPI_NAMES: Final[dict[UIRole, tuple[str, ...]]] = {
    UIRole.BUTTON: ("PUSH_BUTTON", "BUTTON"),
    UIRole.TOGGLE: ("TOGGLE_BUTTON",),
    UIRole.CHECKBOX: ("CHECK_BOX", "CHECK_MENU_ITEM"),
    UIRole.RADIO: ("RADIO_BUTTON", "RADIO_MENU_ITEM"),
    UIRole.LINK: ("LINK",),
    UIRole.TEXT_INPUT: ("ENTRY",),
    UIRole.PASSWORD_INPUT: ("PASSWORD_TEXT",),
    UIRole.COMBO_BOX: ("COMBO_BOX",),
    UIRole.MENU: ("MENU",),
    UIRole.MENU_ITEM: ("MENU_ITEM",),
    UIRole.TAB: ("PAGE_TAB",),
    UIRole.LIST_ITEM: ("LIST_ITEM",),
    UIRole.TREE_ITEM: ("TREE_ITEM",),
    UIRole.SLIDER: ("SLIDER",),
    UIRole.HEADING: ("HEADING",),
    UIRole.LABEL: ("LABEL",),
    UIRole.IMAGE: ("IMAGE", "ICON"),
    UIRole.VIDEO: ("VIDEO",),
    UIRole.CANVAS: ("CANVAS", "DRAWING_AREA"),
    UIRole.DIALOG: ("DIALOG",),
    UIRole.TOOLBAR: ("TOOL_BAR",),
    UIRole.DOCUMENT: (
        "DOCUMENT_FRAME",
        "DOCUMENT_TEXT",
        "DOCUMENT_WEB",
        "DOCUMENT_EMAIL",
        "DOCUMENT_SPREADSHEET",
        "DOCUMENT_PRESENTATION",
    ),
    UIRole.TERMINAL: ("TERMINAL",),
    UIRole.TEXT_FRAGMENT: ("TEXT",),
}

#: Roles that announce their label through the accessible name, so the name may
#: be surfaced as ``text``. Editable roles are deliberately excluded: their name
#: is a label, and their *content* is user data BLAXCY must not vacuum up.
_TEXT_BEARING_ROLES: Final[frozenset[UIRole]] = frozenset(
    {
        UIRole.BUTTON,
        UIRole.TOGGLE,
        UIRole.CHECKBOX,
        UIRole.RADIO,
        UIRole.LINK,
        UIRole.MENU_ITEM,
        UIRole.TAB,
        UIRole.LIST_ITEM,
        UIRole.TREE_ITEM,
        UIRole.COMBO_BOX,
        UIRole.HEADING,
        UIRole.LABEL,
        UIRole.DIALOG,
        UIRole.TEXT_FRAGMENT,
    }
)

#: Accessible-name hints that mark an editable element as a browser navigation
#: field (address/URL/location bar). Only such fields have their text read: a
#: URL is not a credential, but sweeping every text input's content into
#: perception would be, so the read is deliberately narrow (sections 36, 55).
NAVIGATION_FIELD_HINTS: Final[tuple[str, ...]] = (
    "address",
    "url",
    "location",
)

#: Editable roles that can host a navigation field.
_EDITABLE_ROLES: Final[frozenset[UIRole]] = frozenset({UIRole.TEXT_INPUT, UIRole.COMBO_BOX})

#: Roles that are actionable by UI convention even without an AT-SPI action.
_INHERENTLY_CLICKABLE_ROLES: Final[frozenset[UIRole]] = frozenset(
    {
        UIRole.BUTTON,
        UIRole.TOGGLE,
        UIRole.CHECKBOX,
        UIRole.RADIO,
        UIRole.LINK,
        UIRole.MENU_ITEM,
        UIRole.TAB,
        UIRole.LIST_ITEM,
        UIRole.TREE_ITEM,
        UIRole.COMBO_BOX,
    }
)

#: AT-SPI structural role names that start a new "owner window" context.
_WINDOW_ROLE_NAMES: Final[frozenset[str]] = frozenset(
    {"frame", "window", "dialog", "alert", "file chooser", "color chooser"}
)

#: The AT-SPI states BLAXCY reads. A state the installed typelib does not expose
#: is simply absent from the observed set (never assumed present).
_TRACKED_STATES: Final[tuple[str, ...]] = (
    "SHOWING",
    "VISIBLE",
    "ENABLED",
    "SENSITIVE",
    "FOCUSABLE",
    "FOCUSED",
    "EDITABLE",
    "SELECTED",
    "ACTIVE",
    "DEFUNCT",
    "CHECKED",
    "PRESSED",
    "EXPANDED",
    "COLLAPSED",
    "MODAL",
    "READ_ONLY",
    "REQUIRED",
)


def map_role_name(role_name: str) -> UIRole:
    """Map an AT-SPI role name onto the BLAXCY role vocabulary (section 37)."""
    return ROLE_BY_NAME.get(role_name.strip().casefold(), UIRole.UNKNOWN)


class NodeBlacklist:
    """A temporary blacklist for AT-SPI objects that repeatedly fail (section 35).

    A tree node that raises on access is quarantined for a cooldown instead of
    being retried on every traversal, which keeps one broken application from
    stalling perception forever. The block is temporary: after the cooldown the
    path is attempted again.
    """

    def __init__(self, threshold: int, *, cooldown_seconds: float = _BLACKLIST_COOLDOWN_SECONDS) -> None:
        self._threshold = threshold
        self._cooldown_seconds = cooldown_seconds
        self._failures: dict[str, int] = {}
        self._blocked_until: dict[str, float] = {}

    def record_failure(self, path: str, *, now: float | None = None) -> None:
        """Record one failure for ``path``, blocking it at the threshold."""
        clock = time.monotonic() if now is None else now
        count = self._failures.get(path, 0) + 1
        self._failures[path] = count
        if count >= self._threshold:
            self._blocked_until[path] = clock + self._cooldown_seconds

    def record_success(self, path: str) -> None:
        """Clear the failure history for a path that just answered."""
        self._failures.pop(path, None)
        self._blocked_until.pop(path, None)

    def is_blocked(self, path: str, *, now: float | None = None) -> bool:
        """True while ``path`` is inside its cooldown window."""
        clock = time.monotonic() if now is None else now
        until = self._blocked_until.get(path)
        if until is None:
            return False
        if clock >= until:
            self._blocked_until.pop(path, None)
            self._failures.pop(path, None)
            return False
        return True

    @property
    def blocked_paths(self) -> tuple[str, ...]:
        """Currently blocked paths, for diagnostics.

        The iteration runs over a snapshot: ``is_blocked`` removes entries whose
        cooldown has elapsed, so iterating the live mapping would raise
        "dictionary changed size during iteration".
        """
        now = time.monotonic()
        return tuple(p for p in list(self._blocked_until) if self.is_blocked(p, now=now))


class AccessibilityBackend(Protocol):
    """The minimal contract the accessibility service needs from a backend.

    The real backend is :class:`AtspiBackend`. Tests supply a scripted backend so
    the threading, caching and timeout behaviour can be exercised without a live
    accessibility bus.
    """

    def initialize(self) -> None:
        """Open the accessibility connection. Raises on any failure."""
        ...

    def shutdown(self) -> None:
        """Release the accessibility connection. Never raises."""
        ...

    def enumerate_elements(self) -> list[UIElement]:
        """Observe the current tree, bounded by the configured limits."""
        ...

    def query_roles(self, roles: Sequence[UIRole]) -> list[UIElement]:
        """Observe only elements whose role is in ``roles``."""
        ...

    def poll_events(self) -> bool:
        """Dispatch pending AT-SPI events; return True when one was observed."""
        ...

    def diagnostics(self) -> dict[str, Any]:
        """Backend-specific evidence for the capability report."""
        ...


@dataclass
class _Request:
    """One marshalled call from another thread to the T-A11Y worker."""

    kind: str
    payload: dict[str, Any]
    done: threading.Event
    result: Any = None
    error: BaseException | None = None


#: Sentinel pushed onto the queue to stop the worker promptly.
_STOP: Final[object] = object()


class AccessibilityService:
    """Owns the dedicated AT-SPI thread and the accessibility element cache.

    The service is deliberately passive about the desktop: it observes, caches,
    and invalidates. It never injects input, never changes focus, and never
    issues an AT-SPI action.
    """

    def __init__(
        self,
        settings: AccessibilitySettings,
        *,
        backend_factory: Callable[[], AccessibilityBackend] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self._backend_factory = backend_factory
        self._clock = clock
        self._queue: queue.Queue[_Request | object] = queue.Queue()
        self._worker_thread: threading.Thread | None = None
        self._backend: AccessibilityBackend | None = None
        self._ready = threading.Event()
        self._stopping = threading.Event()
        self._startup_error: BaseException | None = None
        self._cache_lock = threading.Lock()
        self._cache: list[UIElement] | None = None
        self._cache_at: float | None = None
        self._request_timeouts = 0
        self._refreshes = 0
        self._last_refresh_ms: float | None = None
        self._last_element_count = 0
        self._event_invalidations = 0
        self._request_timeout_s = (
            settings.deadline_ms + 2 * settings.method_timeout_ms
        ) / 1000.0

    # -- Lifecycle ------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        """True while the dedicated accessibility thread is alive."""
        thread = self._worker_thread
        return thread is not None and thread.is_alive()

    def start(self) -> None:
        """Start the accessibility thread and wait for initialization.

        Raises:
            BlaxcyError: ``BACKEND_UNAVAILABLE`` when the accessibility stack
                cannot initialize, or ``A11Y_TIMEOUT`` when startup does not
                complete within ``startup_timeout_ms``.
        """
        if self.is_running:
            return
        self._ready = threading.Event()
        self._stopping.clear()
        thread = threading.Thread(target=self._run, name="T-A11Y", daemon=True)
        self._worker_thread = thread
        thread.start()

        startup_timeout = self.settings.startup_timeout_ms / 1000.0
        if not self._ready.wait(startup_timeout):
            self._stopping.set()
            self._queue.put(_STOP)
            raise BlaxcyError(
                ErrorCode.A11Y_TIMEOUT,
                "the accessibility thread did not initialize within startup_timeout_ms",
                details={"startup_timeout_ms": self.settings.startup_timeout_ms},
            )
        if self._startup_error is not None:
            self._stopping.set()
            thread.join(timeout=1.0)
            self._worker_thread = None
            raise BlaxcyError(
                ErrorCode.BACKEND_UNAVAILABLE,
                f"AT-SPI initialization failed: {self._startup_error!r}",
                details={"backend": A11Y_BACKEND_NAME, "fix_hint": _A11Y_FIX_HINT},
            )

    def stop(self) -> None:
        """Stop the accessibility thread. Idempotent, and never raises."""
        thread = self._worker_thread
        if thread is None:
            return
        self._stopping.set()
        self._queue.put(_STOP)
        thread.join(timeout=2.0)
        self._worker_thread = None
        self._fail_pending_requests()

    def __enter__(self) -> AccessibilityService:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    # -- Queries --------------------------------------------------------------

    def elements(self, *, force: bool = False) -> list[UIElement]:
        """Return observed elements, reusing the cache while it is fresh.

        The cache exists to avoid redundant AT-SPI traversals within its TTL
        (section 35). It is only ever a hint: a caller acting on real input must
        still revalidate against live state (section 45).
        """
        if not force:
            with self._cache_lock:
                cache = self._cache
                age = self._cache_age_locked()
            if cache is not None and age <= self.settings.cache_ttl_seconds:
                return list(cache)
        return self.refresh()

    def refresh(self) -> list[UIElement]:
        """Force a live traversal on the accessibility thread and cache it."""
        started = self._clock()
        result = self._invoke("refresh")
        elements = list(result)
        elapsed_ms = (self._clock() - started) * 1000.0
        with self._cache_lock:
            self._cache = elements
            self._cache_at = self._clock()
        self._refreshes += 1
        self._last_refresh_ms = elapsed_ms
        self._last_element_count = len(elements)
        return elements

    def query(self, roles: Sequence[UIRole] | None = None) -> list[UIElement]:
        """Query elements by role, preferring ``Atspi.Collection`` (section 35).

        With no role filter this falls back to the cache-aware full enumeration.
        A role-filtered query runs live on the accessibility thread and does not
        disturb the shared full-tree cache.
        """
        if not roles:
            return self.elements()
        result = self._invoke("query_roles", {"roles": tuple(roles)})
        return list(result)

    def activate(self, path: str, action: str | None = None) -> ActivationOutcome:
        """Invoke an accessibility action on the element at ``path`` (section 66).

        Runs on the accessibility thread through the same request queue as every
        other AT-SPI interaction (section 35), so it can never become a second
        concurrent AT-SPI caller. The element is located by descending the path
        <em>live</em> rather than by reusing a cached node, because a node held
        from an earlier traversal is exactly the stale target section 45 exists
        to reject; the caller is still responsible for the full policy -> lease ->
        revalidation path before this is ever reached.
        """
        result = self._invoke("activate", {"path": path, "action": action})
        if isinstance(result, ActivationOutcome):
            return result
        return ActivationOutcome(
            ok=False,
            reason="the accessibility backend returned no activation result",
        )

    def snapshot(self) -> tuple[list[UIElement], float]:
        """Return the cached elements and their age in seconds (no backend call).

        A never-populated cache reports an infinite age, so callers cannot
        mistake "nothing observed yet" for "observed just now".
        """
        with self._cache_lock:
            if self._cache is None:
                return [], float("inf")
            return list(self._cache), self._cache_age_locked()

    def invalidate(self) -> None:
        """Drop the element cache; the next query re-observes the live tree."""
        self._invalidate()

    # -- Reporting ------------------------------------------------------------

    def capability(self) -> Capability:
        """Report the accessibility capability with the evidence behind it."""
        details: dict[str, Any] = {
            "running": self.is_running,
            "refreshes": self._refreshes,
            "request_timeouts": self._request_timeouts,
            "event_invalidations": self._event_invalidations,
            "elements_last_refresh": self._last_element_count,
            "cache_ttl_seconds": self.settings.cache_ttl_seconds,
        }
        backend = self._backend
        if backend is not None:
            details.update(backend.diagnostics())

        if self._startup_error is not None and not self.is_running:
            return Capability(
                name=CapabilityName.ACCESSIBILITY,
                status=CapabilityStatus.UNAVAILABLE,
                backend=A11Y_BACKEND_NAME,
                details=details,
                reason=f"AT-SPI initialization failed: {self._startup_error!r}",
                fix_hint=_A11Y_FIX_HINT,
            )
        if not self.is_running:
            return Capability(
                name=CapabilityName.ACCESSIBILITY,
                status=CapabilityStatus.UNAVAILABLE,
                backend=A11Y_BACKEND_NAME,
                details=details,
                reason="the accessibility service has not been started",
                fix_hint=_A11Y_FIX_HINT,
            )
        if self._request_timeouts > 0:
            return Capability(
                name=CapabilityName.ACCESSIBILITY,
                status=CapabilityStatus.DEGRADED,
                backend=A11Y_BACKEND_NAME,
                latency_ms=self._last_refresh_ms,
                details=details,
                reason=(
                    f"{self._request_timeouts} accessibility request(s) timed out; "
                    "the tree may be partially observable"
                ),
                fix_hint="check for a hung application exporting a broken AT-SPI tree",
            )
        return Capability(
            name=CapabilityName.ACCESSIBILITY,
            status=CapabilityStatus.AVAILABLE,
            backend=A11Y_BACKEND_NAME,
            latency_ms=self._last_refresh_ms,
            details=details,
        )

    def stats(self) -> dict[str, Any]:
        """A JSON-shaped health snapshot for logs and benchmarks."""
        return {
            "backend": A11Y_BACKEND_NAME,
            "running": self.is_running,
            "refreshes": self._refreshes,
            "request_timeouts": self._request_timeouts,
            "event_invalidations": self._event_invalidations,
            "cached_elements": 0 if self._cache is None else len(self._cache),
            "cache_age_seconds": self._cache_age_locked(),
            "last_refresh_ms": self._last_refresh_ms,
            "request_timeout_seconds": self._request_timeout_s,
        }

    # -- Internals ------------------------------------------------------------

    def _cache_age_locked(self) -> float:
        """Age of the cache in seconds; ``inf`` when nothing is cached yet."""
        if self._cache_at is None:
            return float("inf")
        return self._clock() - self._cache_at

    def _invalidate(self) -> None:
        with self._cache_lock:
            self._cache = None
            self._cache_at = None

    def _run(self) -> None:
        """The T-A11Y worker loop: the only thread that touches the backend."""
        # Cleared here rather than in start(): the worker owns this field, and
        # assigning it on the starting thread would also make static analysis
        # treat the post-start check as always-None.
        self._startup_error = None
        backend: AccessibilityBackend | None = None
        try:
            backend = (
                self._backend_factory()
                if self._backend_factory is not None
                else AtspiBackend(self.settings)
            )
            backend.initialize()
        except BaseException as exc:
            self._startup_error = exc
            self._ready.set()
            return

        self._backend = backend
        self._ready.set()
        try:
            while not self._stopping.is_set():
                try:
                    request = self._queue.get(timeout=_QUEUE_POLL_SECONDS)
                except queue.Empty:
                    self._poll_events(backend)
                    continue
                if request is _STOP:
                    break
                if isinstance(request, _Request):
                    try:
                        request.result = self._dispatch(backend, request.kind, request.payload)
                    except BaseException as exc:
                        request.error = exc
                    finally:
                        request.done.set()
        finally:
            self._backend = None
            # Shutdown is best-effort: a failed teardown must never mask the
            # worker's real state or raise out of a daemon thread.
            with suppress(BaseException):
                backend.shutdown()

    def _poll_events(self, backend: AccessibilityBackend) -> None:
        """Drain pending AT-SPI events, invalidating the cache when one arrives.

        Any observed event conservatively invalidates the cache: the event is
        evidence the tree moved, and re-observing is cheaper than serving a
        stale element to a caller that might act on it.
        """
        try:
            if backend.poll_events():
                self._event_invalidations += 1
                self._invalidate()
        except BaseException:
            pass

    def _dispatch(self, backend: AccessibilityBackend, kind: str, payload: dict[str, Any]) -> Any:
        """Execute one request on the worker thread."""
        if kind == "refresh":
            return backend.enumerate_elements()
        if kind == "query_roles":
            roles = tuple(payload.get("roles", ()))
            return backend.query_roles(roles)
        if kind == "activate":
            # Activation is an *optional* backend capability, deliberately not part
            # of the protocol above: a backend that cannot perform accessibility
            # actions must be able to say so rather than be forced to pretend. The
            # refusal is structured, so it surfaces as UNAVAILABLE, never as a
            # silently substituted click.
            perform = getattr(backend, "activate", None)
            if perform is None:
                raise BlaxcyError(
                    ErrorCode.BACKEND_UNAVAILABLE,
                    "this accessibility backend cannot perform accessibility actions",
                    details={"backend": A11Y_BACKEND_NAME},
                )
            return perform(path=str(payload.get("path", "")), action=payload.get("action"))
        raise BlaxcyError(ErrorCode.INTERNAL_ERROR, f"unknown accessibility request {kind!r}")

    def _invoke(self, kind: str, payload: dict[str, Any] | None = None) -> Any:
        """Marshall a request to the worker thread and wait for its answer."""
        if threading.current_thread() is self._worker_thread and self._backend is not None:
            return self._dispatch(self._backend, kind, payload or {})
        if not self.is_running:
            raise BlaxcyError(
                ErrorCode.BACKEND_UNAVAILABLE,
                "the accessibility thread is not running",
                details={"backend": A11Y_BACKEND_NAME, "fix_hint": _A11Y_FIX_HINT},
            )
        request = _Request(kind=kind, payload=payload or {}, done=threading.Event())
        self._queue.put(request)
        if not request.done.wait(self._request_timeout_s):
            self._request_timeouts += 1
            raise BlaxcyError(
                ErrorCode.A11Y_TIMEOUT,
                "the accessibility thread did not answer within the request timeout",
                details={
                    "backend": A11Y_BACKEND_NAME,
                    "timeout_seconds": self._request_timeout_s,
                    "deadline_ms": self.settings.deadline_ms,
                },
            )
        if request.error is not None:
            error = request.error
            if isinstance(error, BlaxcyError):
                raise error
            raise BlaxcyError(
                ErrorCode.A11Y_TIMEOUT,
                f"the accessibility query failed: {error!r}",
                details={"backend": A11Y_BACKEND_NAME},
            ) from error
        return request.result

    def _fail_pending_requests(self) -> None:
        """Unblock any caller still waiting after the worker stopped."""
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            if item is _STOP or not isinstance(item, _Request):
                continue
            item.error = BlaxcyError(
                ErrorCode.BACKEND_UNAVAILABLE,
                "the accessibility service stopped before the request completed",
            )
            item.done.set()


class AtspiBackend:
    """The real AT-SPI perception backend (specification sections 35, 37, 38).

    ``Atspi.Collection`` is preferred where the installed implementation really
    provides it; the installed interface is probed once and the bounded BFS is
    the fallback. The BFS path is the one exercised on hosts whose Collection
    interface is missing (which is common), so it is the *verified* path, not a
    theoretical one.
    """

    def __init__(self, settings: AccessibilitySettings) -> None:
        self._settings = settings
        self._atspi: Any = None
        self._glib: Any = None
        self._blacklist = NodeBlacklist(settings.blacklist_after_failures)
        self._collection_available: bool | None = None
        self._collection_errors = 0
        self._traversal_timeouts = 0
        self._traversals = 0
        self._truncated = False
        self._event_listeners: list[Any] = []
        self._events_seen = 0
        self._subscribe_error: str | None = None

    # -- Lifecycle ------------------------------------------------------------

    def initialize(self) -> None:
        """Import AT-SPI and verify it is functionally usable.

        Raises:
            BlaxcyError: ``BACKEND_UNAVAILABLE`` when PyGObject, the Atspi
                typelib, or the accessibility bus is missing, or when the
                desktop root cannot be enumerated.
        """
        try:
            gi = importlib.import_module("gi")
        except Exception as exc:
            raise BlaxcyError(
                ErrorCode.BACKEND_UNAVAILABLE,
                f"PyGObject (gi) is not importable: {exc!r}",
                details={"backend": A11Y_BACKEND_NAME, "fix_hint": _A11Y_FIX_HINT},
            ) from exc
        try:
            gi.require_version("Atspi", "2.0")
            atspi = importlib.import_module("gi.repository.Atspi")
            glib = importlib.import_module("gi.repository.GLib")
        except Exception as exc:
            raise BlaxcyError(
                ErrorCode.BACKEND_UNAVAILABLE,
                f"the Atspi 2.0 typelib is unavailable: {exc!r}",
                details={"backend": A11Y_BACKEND_NAME, "fix_hint": _A11Y_FIX_HINT},
            ) from exc

        try:
            atspi.init()
            desktop_count = int(atspi.get_desktop_count())
        except Exception as exc:
            raise BlaxcyError(
                ErrorCode.BACKEND_UNAVAILABLE,
                f"AT-SPI could not be initialized: {exc!r}",
                details={"backend": A11Y_BACKEND_NAME, "fix_hint": _A11Y_FIX_HINT},
            ) from exc

        if desktop_count < 1:
            raise BlaxcyError(
                ErrorCode.BACKEND_UNAVAILABLE,
                "AT-SPI responded but reported no desktops",
                details={"backend": A11Y_BACKEND_NAME, "fix_hint": _A11Y_FIX_HINT},
            )

        self._atspi = atspi
        self._glib = glib
        self._subscribe()

    def shutdown(self) -> None:
        """Deregister listeners and drop the AT-SPI handles."""
        atspi = self._atspi
        for listener in self._event_listeners:
            try:
                if atspi is not None:
                    atspi.deregister_event_listener(listener)
            except Exception:
                pass
        self._event_listeners.clear()
        self._atspi = None
        self._glib = None

    # -- Events ---------------------------------------------------------------

    def _subscribe(self) -> None:
        """Register AT-SPI event listeners, recording failure honestly.

        Event delivery depends on the GLib main context being iterated; that is
        done in :meth:`poll_events`. Registration failing is not fatal -- the
        cache TTL still bounds staleness -- but it is recorded so the capability
        report can report the limitation instead of implying events work.
        """
        atspi = self._atspi
        if atspi is None:
            return
        try:
            listener = atspi.EventListener.new(self._on_event, None)
        except Exception as exc:
            self._subscribe_error = repr(exc)
            return
        events = (
            "object:property-change:accessible-name",
            "object:state-changed:showing",
            "object:state-changed:visible",
            "object:children-changed",
            "object:defunct",
            "window:activate",
            "window:deactivate",
        )
        registered = 0
        for event in events:
            try:
                listener.register(event)
            except Exception:
                continue
            registered += 1
        if registered == 0:
            self._subscribe_error = "no AT-SPI event type could be registered"
            return
        self._event_listeners.append(listener)

    def _on_event(self, event: Any, *_extra: Any) -> None:
        """Record an AT-SPI event; the service treats it as a cache invalidation.

        The extra positional arguments are accepted deliberately. The registered
        callback is invoked by the accessibility stack, not by BLAXCY, and the
        number of arguments it passes depends on the installed ``Atspi``/GI
        version (some deliver ``(event, source, user_data)``). A single-argument
        signature raises ``TypeError`` on those versions, which would silently
        lose every event -- and a lost event is a stale element cache
        (section 35). Accepted and ignored, rather than guessed at.
        """
        del event, _extra
        self._events_seen += 1

    def poll_events(self) -> bool:
        """Iterate the GLib context so queued AT-SPI events dispatch (best effort)."""
        glib = self._glib
        if glib is None or not self._event_listeners:
            return False
        before = self._events_seen
        try:
            context = glib.MainContext.default()
            # Non-blocking: dispatch only what is already queued.
            while context.pending():
                context.iteration(False)
        except Exception:
            return False
        return self._events_seen > before

    # -- Traversal ------------------------------------------------------------

    def enumerate_elements(self) -> list[UIElement]:
        """Observe the live tree via the bounded depth-first traversal."""
        atspi = self._require_atspi()
        self._traversals += 1
        self._truncated = False
        deadline = time.monotonic() + self._settings.deadline_ms / 1000.0
        elements: list[UIElement] = []
        desktop_count = int(atspi.get_desktop_count())
        for desktop_index in range(desktop_count):
            try:
                root = atspi.get_desktop(desktop_index)
            except Exception:
                continue
            if root is None:
                continue
            self._walk(
                root,
                root_path=f"desktop[{desktop_index}]",
                deadline=deadline,
                out=elements,
            )
        return elements

    def query_roles(self, roles: Sequence[UIRole]) -> list[UIElement]:
        """Return elements of the given roles, preferring Collection (section 35)."""
        wanted = frozenset(roles)
        if not wanted:
            return self.enumerate_elements()
        collected = self._try_collection(wanted)
        if collected is not None:
            return collected
        return [element for element in self.enumerate_elements() if element.role in wanted]

    def activate(self, *, path: str, action: str | None = None) -> ActivationOutcome:
        """Perform an AT-SPI action on the live node at ``path`` (section 66).

        This is the real implementation behind the ``activate_element`` tool. It
        asks the application to perform its own action -- ``Atspi.Action`` --
        rather than injecting a pointer event, which is why it needs no
        coordinates and cannot be defeated by pointer occlusion. It performs no
        policy, lease or verification work itself: those belong to the executor,
        which reaches this only after the full section 59 pipeline.
        """
        atspi = self._require_atspi()
        node = _descend_path(atspi, path)
        if node is None:
            return ActivationOutcome(
                ok=False,
                reason="the target is no longer present in the accessibility tree",
                details={"path": path},
            )
        names = _read_actions(node)
        index = _select_action_index(names, action)
        if index is None:
            return ActivationOutcome(
                ok=False,
                reason=(
                    f"the element exposes no {action!r} action"
                    if action
                    else "the element exposes no activatable action"
                ),
                details={"path": path, "actions": list(names)},
            )
        if not _invoke_action(atspi, node, index):
            return ActivationOutcome(
                ok=False,
                action=names[index],
                reason="the application refused the accessibility action",
                details={"path": path, "actions": list(names)},
            )
        return ActivationOutcome(
            ok=True,
            action=names[index],
            details={"path": path, "actions": list(names)},
        )

    def _try_collection(self, roles: frozenset[UIRole]) -> list[UIElement] | None:
        """Attempt an ``Atspi.Collection`` query, or return ``None`` to fall back.

        The Collection interface is not universally implemented; the first
        failure marks it unavailable so later queries go straight to the
        bounded BFS instead of paying the failing round-trip every time.
        """
        if self._collection_available is False:
            return None
        atspi = self._require_atspi()
        role_members: list[Any] = []
        for role in roles:
            for name in ROLE_TO_ATSPI_NAMES.get(role, ()):
                member = getattr(atspi.Role, name, None)
                if member is not None:
                    role_members.append(member)
        if not role_members:
            return None
        try:
            root = atspi.get_desktop(0)
            rule = atspi.MatchRule.new(
                states=None,
                statematchtype=atspi.CollectionMatchType.ALL,
                attributes=None,
                attributematchtype=atspi.CollectionMatchType.ALL,
                roles=role_members,
                rolematchtype=atspi.CollectionMatchType.ANY,
                interfaces=None,
                interfacematchtype=atspi.CollectionMatchType.ALL,
                invert=False,
            )
            matches = root.get_matches(
                rule, atspi.CollectionSortOrder.CANONICAL, self._settings.max_nodes, True
            )
        except Exception:
            self._collection_errors += 1
            self._collection_available = False
            return None

        self._collection_available = True
        collected: list[UIElement] = []
        for match in matches or []:
            path = self._ancestry_path(match)
            try:
                element, _window, _app, _pid = self._observe(match, path, None, None, None)
            except Exception:
                self._blacklist.record_failure(path)
                continue
            if element is not None:
                collected.append(element)
        return collected

    def _ancestry_path(self, node: Any) -> str:
        """Best-effort stable path for a node discovered outside the BFS.

        Collection results arrive without the traversal path the BFS builds, so
        a path is reconstructed from role/index ancestry. It is used only as an
        identity hint; revalidation (section 45) is what actually decides
        whether the element is still the same one.
        """
        parts: list[str] = []
        current = node
        for _ in range(self._settings.max_depth + 2):
            if current is None:
                break
            role = _safe_str(current.get_role_name) or "unknown"
            try:
                index = int(current.get_index_in_parent())
            except Exception:
                index = -1
            parts.append(f"{role}[{index}]")
            try:
                current = current.get_parent()
            except Exception:
                break
        if not parts:
            return "unknown"
        return "/".join(reversed(parts))

    def _walk(
        self,
        root: Any,
        *,
        root_path: str,
        deadline: float,
        out: list[UIElement],
    ) -> None:
        """Bounded depth-first traversal of one desktop (section 35)."""
        settings = self._settings
        stack: list[tuple[Any, int, str, str | None, str | None, int | None]] = [
            (root, 0, root_path, None, None, None)
        ]
        visited = 0
        while stack:
            if visited >= settings.max_nodes:
                self._truncated = True
                break
            if time.monotonic() > deadline:
                self._traversal_timeouts += 1
                self._truncated = True
                break
            node, depth, path, window_name, app_name, app_pid = stack.pop()
            visited += 1
            if self._blacklist.is_blocked(path):
                continue
            try:
                element, new_window, new_app, new_pid = self._observe(
                    node, path, window_name, app_name, app_pid
                )
            except Exception:
                self._blacklist.record_failure(path)
                continue
            self._blacklist.record_success(path)
            if element is not None:
                out.append(element)
            if depth >= settings.max_depth:
                continue
            try:
                child_count = int(node.get_child_count())
            except Exception:
                self._blacklist.record_failure(path)
                continue
            for index in range(child_count - 1, -1, -1):
                child_path = f"{path}/{index}"
                if self._blacklist.is_blocked(child_path):
                    continue
                try:
                    child = node.get_child_at_index(index)
                except Exception:
                    self._blacklist.record_failure(child_path)
                    continue
                if child is None:
                    continue
                stack.append((child, depth + 1, child_path, new_window, new_app, new_pid))

    def _observe(
        self,
        node: Any,
        path: str,
        window_name: str | None,
        app_name: str | None,
        app_pid: int | None,
    ) -> tuple[UIElement | None, str | None, str | None, int | None]:
        """Turn one AT-SPI accessible into a :class:`UIElement`.

        Returns the element (or ``None`` for a node that carries no reportable
        information) plus the window/application context to propagate to
        children. That context includes the owning process id, which is the exact
        key that lets section 46's occlusion rule place an element in the window
        manager's stacking order (the window title beside it is only a heuristic).
        """
        atspi = self._require_atspi()
        role_name = _safe_str(node.get_role_name)
        name = _safe_str(node.get_name)
        role = map_role_name(role_name)

        if role_name.casefold() == "application":
            if name:
                app_name = name
            # Read even when the application reports no name: the pid is what the
            # occlusion join actually needs.
            app_pid = _safe_pid(node)
        if role_name.casefold() in _WINDOW_ROLE_NAMES and name:
            window_name = name

        states = _read_states(atspi, node)
        if "DEFUNCT" in states:
            return None, window_name, app_name, app_pid

        # Qt's AT-SPI bridge announces an editable text field with the plain role
        # "text" (GTK announces "entry"), so the role name alone would classify a
        # text field as a static fragment -- and section 51's focus guard, which
        # only accepts a text entry, would then refuse to type into it at all.
        # The EDITABLE state is what actually distinguishes an input from a label,
        # so it decides the role here (section 37). A non-editable "text" node is
        # untouched and stays a fragment.
        if role is UIRole.TEXT_FRAGMENT and "EDITABLE" in states:
            role = UIRole.TEXT_INPUT

        actions = _read_actions(node)
        password = role is UIRole.PASSWORD_INPUT
        if password:
            # Credential content is never read (sections 42, 55).
            text = None
        elif role in _EDITABLE_ROLES and _is_navigation_field(name):
            # A browser address/location bar: its content is a URL, not a secret.
            text = _read_text_content(atspi, node) or None
        elif role in _TEXT_BEARING_ROLES:
            text = name or None
        else:
            text = None

        # Section 36 stage 1: a document may expose its own URL as an attribute.
        browser_url = _read_document_url(node) if role is UIRole.DOCUMENT else None

        bbox = _read_extents(atspi, node)
        # With a readable state set, visibility means SHOWING *and* geometry.
        # Without one, geometry is the only evidence available.
        visible = ("SHOWING" in states and bbox is not None) if states else bbox is not None

        clickable = bool(actions) or role in _INHERENTLY_CLICKABLE_ROLES
        geometry_sanity = 1.0 if bbox is not None else 0.5
        element = UIElement(
            element_id=path,
            role=role,
            text=text,
            accessible_name=name or None,
            bbox=bbox,
            center=bbox.center if bbox is not None else None,
            coordinate_space=CoordinateSpace.DESKTOP if bbox is not None else None,
            clickable=clickable,
            effective_clickable=clickable,
            enabled="ENABLED" in states or "SENSITIVE" in states or not states,
            focusable="FOCUSABLE" in states,
            focused="FOCUSED" in states,
            visible=visible,
            occluded=False,
            password=password,
            owner_window_id=None,
            # The title of the window this node lives in (propagated from the frame
            # ancestor). It is what lets the section 46 occlusion rule ask "is this
            # window above the target's?" instead of guessing from geometry.
            owner_window_title=window_name,
            owner_app_pid=app_pid,
            owner_app=app_name,
            actions=actions,
            monitor_id=None,
            frame_id=None,
            timestamp=time.time(),
            source=PerceptionSource.ATSPI,
            confidence=perception_confidence(
                PerceptionSource.ATSPI, geometry_sanity=geometry_sanity
            ),
            patch_hash=None,
            atspi_path=path,
            dom_path=None,
            browser_url=browser_url,
            conflict=False,
        )
        if not _is_reportable(element):
            return None, window_name, app_name, app_pid
        return element, window_name, app_name, app_pid

    def _require_atspi(self) -> Any:
        """Return the Atspi module, or raise if the backend is not initialized."""
        if self._atspi is None:
            raise BlaxcyError(
                ErrorCode.BACKEND_UNAVAILABLE,
                "the AT-SPI backend is not initialized",
                details={"backend": A11Y_BACKEND_NAME},
            )
        return self._atspi

    # -- Diagnostics ----------------------------------------------------------

    def diagnostics(self) -> dict[str, Any]:
        """Backend-specific evidence for the capability report."""
        return {
            "backend": A11Y_BACKEND_NAME,
            "collection_available": self._collection_available,
            "collection_errors": self._collection_errors,
            "traversals": self._traversals,
            "traversal_timeouts": self._traversal_timeouts,
            "traversal_truncated": self._truncated,
            "events_seen": self._events_seen,
            "event_subscription_error": self._subscribe_error,
            "blocked_paths": list(self._blacklist.blocked_paths),
            "limits": {
                "max_depth": self._settings.max_depth,
                "max_nodes": self._settings.max_nodes,
                "deadline_ms": self._settings.deadline_ms,
            },
        }


def _is_reportable(element: UIElement) -> bool:
    """Whether an element carries information worth reporting.

    Pure structural containers with no role mapping, no label and no action are
    dropped so the state cache stays meaningful instead of being filled with the
    hundreds of anonymous frames a desktop exposes. The node is still traversed;
    only its *emission* is skipped.
    """
    return (
        element.role is not UIRole.UNKNOWN
        or bool(element.accessible_name)
        or bool(element.actions)
    )


def _is_navigation_field(name: str) -> bool:
    """True when an editable element's name marks it as a navigation field."""
    lowered = name.casefold()
    return any(hint in lowered for hint in NAVIGATION_FIELD_HINTS)


def _read_text_content(atspi: Any, node: Any) -> str:
    """Read an editable element's text; used only for navigation fields (section 36).

    The AT-SPI ``Text`` interface owns ``get_text(start, end)``. The deprecated
    ``Atspi.Accessible.get_text`` shim does **not** share that signature on every
    PyGObject version -- on the installed one it accepts a single argument and
    raises ``TypeError`` for ``(start, end)`` -- so reading through the shim
    silently produced nothing at all. The interface method is therefore tried
    first, and the node's own method is kept as a fallback for test doubles.
    """
    try:
        if not node.is_text():
            return ""
    except Exception:
        return ""

    count = -1
    try:
        count = int(node.get_character_count())
    except Exception:
        count = -1

    readers: list[Any] = []
    text_interface = getattr(atspi, "Text", None)
    if text_interface is not None:
        readers.append(lambda: text_interface.get_text(node, 0, count))
        readers.append(lambda: text_interface.get_text(node, 0, -1))
    readers.append(lambda: node.get_text(0, -1))

    for read in readers:
        try:
            value = read()
        except Exception:
            continue
        if value:
            return str(value)
    return ""


def _read_document_url(node: Any) -> str | None:
    """Read a document's URL from AT-SPI document attributes, if it exposes one.

    Browsers differ in whether (and under what attribute name) they publish a
    document URL. Every lookup is guarded: an unsupported attribute is absence
    of evidence, never a fabricated URL.
    """
    for attribute in ("DocURL", "DocUrl", "docurl", "URL", "url"):
        try:
            value = node.get_document_attribute_value(attribute)
        except Exception:
            value = None
        if value:
            return str(value)
    try:
        attributes = node.get_attributes()
    except Exception:
        return None
    if not isinstance(attributes, dict):
        return None
    for key, value in attributes.items():
        if str(key).casefold() in {"docurl", "url"} and value:
            return str(value)
    return None


def _safe_str(getter: Callable[[], Any]) -> str:
    """Call a zero-argument AT-SPI getter, returning ``""`` when it fails."""
    try:
        value = getter()
    except Exception:
        return ""
    return str(value) if value is not None else ""


def _safe_pid(node: Any) -> int | None:
    """The process id AT-SPI reports for a node, or ``None``.

    Never raises: an application that does not answer this query is a missing
    join, not a perception failure, and the occlusion rule treats a missing join
    conservatively.
    """
    try:
        pid = node.get_process_id()
    except Exception:
        return None
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    return int(pid)


def _read_states(atspi: Any, node: Any) -> frozenset[str]:
    """Read the AT-SPI state set as a frozen set of state names."""
    try:
        state_set = node.get_state_set()
    except Exception:
        return frozenset()
    if state_set is None:
        return frozenset()
    observed: set[str] = set()
    for state_name in _TRACKED_STATES:
        member = getattr(atspi.StateType, state_name, None)
        if member is None:
            continue
        try:
            if state_set.contains(member):
                observed.add(state_name)
        except Exception:
            continue
    return frozenset(observed)


#: Action names preferred when ``activate_element`` is asked to act without
#: naming one. Ordered most-specific first: an explicit ``click`` is what a
#: pointer click would have done, then the toolkit's generic activation verb.
_PREFERRED_ACTION_NAMES: Final[tuple[str, ...]] = ("click", "activate", "press", "toggle")


def _select_action_index(names: Sequence[str], requested: str | None) -> int | None:
    """Choose which of a node's actions to invoke, or ``None`` for none.

    A requested name must match exactly (case-insensitively); guessing a *close*
    name would be exactly the kind of invented behavior section 4 rule 4 forbids,
    so a miss is reported rather than approximated.
    """
    if requested is not None:
        wanted = requested.strip().lower()
        for index, name in enumerate(names):
            if name.lower() == wanted:
                return index
        return None
    lowered = [name.lower() for name in names]
    for preferred in _PREFERRED_ACTION_NAMES:
        if preferred in lowered:
            return lowered.index(preferred)
    return 0 if names else None


def _descend_path(atspi: Any, path: str) -> Any | None:
    """Follow an accessibility path (``desktop[0]/25/0``) to its live node.

    Descending is exact and costs one round-trip per path segment, so it is both
    cheaper and more honest than re-traversing the whole tree to find an element
    that may have moved. A path from a Collection query (which reconstructs paths
    from role ancestry, not indices) does not resolve here -- it returns ``None``
    and the caller reports the target as absent rather than acting on a guess.
    """
    if not path.startswith("desktop["):
        return None
    head, _, tail = path.partition("/")
    try:
        desktop_index = int(head[len("desktop[") : -1])
    except (ValueError, IndexError):
        return None
    try:
        node = atspi.get_desktop(desktop_index)
    except Exception:
        return None
    if node is None:
        return None
    for segment in (part for part in tail.split("/") if part):
        try:
            child_index = int(segment)
        except ValueError:
            return None
        try:
            node = node.get_child_at_index(child_index)
        except Exception:
            return None
        if node is None:
            return None
    return node


def _invoke_action(atspi: Any, node: Any, index: int) -> bool:
    """Perform an AT-SPI action, preferring the interface like other reads.

    The interface method is tried first and the node's own method is kept as a
    fallback, mirroring :func:`_read_text_content`: the deprecated shims do not
    share a signature across PyGObject versions, and a silently-swallowed
    failure there is how a real capability looks like an unsupported one.
    """
    action_interface = getattr(atspi, "Action", None)
    attempts: list[Callable[[], Any]] = []
    if action_interface is not None:
        attempts.append(lambda: action_interface.do_action(node, index))
    attempts.append(lambda: node.do_action(index))
    for attempt in attempts:
        try:
            performed = attempt()
        except Exception:
            continue
        if performed:
            return True
    return False


def _read_actions(node: Any) -> tuple[str, ...]:
    """Read the AT-SPI action names a node exposes (empty when it exposes none)."""
    try:
        count = int(node.get_n_actions())
    except Exception:
        return ()
    if count <= 0:
        return ()
    names: list[str] = []
    for index in range(count):
        try:
            name = node.get_action_name(index)
        except Exception:
            continue
        if name:
            names.append(str(name))
    return tuple(names)


def _read_extents(atspi: Any, node: Any) -> Rect | None:
    """Read screen-space extents as a DESKTOP-space rectangle.

    A component with non-positive width or height (a common AT-SPI idiom for
    "not currently laid out") has no usable geometry and is reported as ``None``
    rather than as an off-screen rectangle at a bogus coordinate.
    """
    try:
        extents = node.get_extents(atspi.CoordType.SCREEN)
    except Exception:
        return None
    if extents is None:
        return None
    width = float(extents.width)
    height = float(extents.height)
    if width <= 0.0 or height <= 0.0:
        return None
    return Rect(
        x=float(extents.x),
        y=float(extents.y),
        width=width,
        height=height,
        space=CoordinateSpace.DESKTOP,
    )
