"""Clipboard-assisted typing (specification sections 30, 50, 52, 55, 76).

Section 50 needs a way to produce text the current keyboard mapping cannot
express. The only honest mechanism on X11 is the CLIPBOARD selection: put the
text there and inject Ctrl+V, so the *target application* inserts the characters
it can render. BLAXCY never approximates non-ASCII text by typing a lookalike.

Why this module is not just a call to ``xclip``
-----------------------------------------------

An X selection is served **lazily**. Setting the selection owner is not
"putting text on the clipboard": the data only moves when some other client asks
the owner for it, which happens *after* the paste keystroke is injected and the
target application processes it. A component that merely ran ``xclip -i`` and
returned would therefore be racing -- it would usually work and occasionally
paste nothing. So this paster takes ownership and then **serves** requests, from
its own thread, until the transfer has had its bounded moment to complete.

That gives three real obligations, all handled here:

* **Answer, then let go.** After the paste is injected the paster keeps serving
  for ``paste_grace_ms``; only then does it stop. Without that window the target's
  request can arrive at an owner that has already walked away.
* **Do not eat the user's clipboard.** The previous content and the previous
  owner window are captured *before* BLAXCY takes ownership, and the previous
  content is served for ``restore_grace_ms`` on the way out, which is what lets a
  clipboard manager (xfce4-clipman and friends) capture it. Ownership is then
  handed back to the previous owner window when there was one, or released when
  there was none. This is honestly a *best-effort* restore: X gives no way to
  write into another client's clipboard, so a previous owner that no longer
  answers for its own data cannot be repaired by BLAXCY -- re-owning its window
  only gives it the chance, and the grace window is what makes the content
  survive on a desktop that runs a clipboard manager.
* **Never fight for the selection.** If another client takes ownership
  (``SelectionClear``), this paster stops serving immediately rather than
  re-claiming it. Two programs arguing over the clipboard is a worse outcome than
  a lost paste, which the executor already reports honestly.

Targets offered are ``UTF8_STRING``, ``STRING`` and ``TEXT``; ``STRING`` is
refused (with the protocol's own ``property = None``) when the text is not
Latin-1, because sending mangled bytes would look like a successful transfer of
text the target cannot actually represent. A ``TARGETS`` request is answered with
the real list, and any other target is refused the same way: this paster never
claims a format it did not provide.

Nothing here holds a lock the executor needs, and
:meth:`X11ClipboardPaster.close` is safe to call from the emergency-stop thread.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from config.settings import ClipboardSettings
from schemas.enums import ErrorCode
from schemas.errors import BlaxcyError

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime import edge
    # The *consumer* (control.keyboard) owns the plug-in interface, so this module
    # depends on it and never the other way round. The composition root wires the
    # concrete paster in, which is what keeps the two modules acyclic.
    from control.keyboard import ClipboardPaster

#: How long the serve loop sleeps between polls of its own connection. Not a
#: config knob: it is an implementation detail of this mechanism and it costs
#: nothing when there is nothing to serve.
SERVE_POLL_SECONDS: float = 0.005

#: Poll granularity for the bounded read of the previous clipboard content.
WAIT_POLL_SECONDS: float = 0.002

#: Longest single sleep slice while waiting out a grace window.
GRACE_SLICE_SECONDS: float = 0.05

#: Selection and target names. X atoms are interned from these by name: none of
#: them is predefined by the protocol, so none of them may be assumed.
SELECTION_CLIPBOARD: str = "CLIPBOARD"
TARGET_UTF8_STRING: str = "UTF8_STRING"
TARGET_STRING: str = "STRING"
TARGET_TEXT: str = "TEXT"
TARGET_TARGETS: str = "TARGETS"

#: Property name used when a client asks for the data with ``property = None``
#: (some legacy clients do; the protocol requires the owner to choose a name and
#: report that choice in the ``SelectionNotify``).
BLAXCY_PROPERTY: str = "BLAXCY_CLIPBOARD_TRANSFER"


@dataclass(frozen=True)
class ClipboardProbe:
    """Result of the functional clipboard probe (section 28 rule 12).

    The probe deliberately does **not** take ownership of the selection: stealing
    the user's clipboard to prove the clipboard works would be a real side effect
    on a shared resource, and it would inject a transient empty entry into a
    clipboard manager's history. Instead it performs every real operation that
    serving the selection depends on -- connecting, interning the selection and
    target atoms, creating a serviceable owner window, and reading who currently
    owns the clipboard -- and reports that honestly. ``takes_ownership`` in
    ``details`` records which of those it did, so no caller can mistake the probe
    for the transfer itself.
    """

    name: str
    available: bool
    reason: str | None = None
    latency_ms: float | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RestoreReport:
    """What :meth:`X11ClipboardPaster.restore` actually did (section 50).

    Every field is observed, never assumed: ``previous_text`` is what the paster
    really read off the selection, ``handed_back`` records whether ownership was
    returned to the previous owner (or released when there was none), and
    ``reasons`` carries anything that legitimately could not be done.
    """

    restored: bool
    previous_owner: int | None
    previous_text: str | None
    handed_back: bool
    served_requests: int
    served_targets: tuple[str, ...]
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """JSON-shaped report for logs and the GUI."""
        return {
            "restored": self.restored,
            "previous_owner": self.previous_owner,
            "previous_text_chars": None if self.previous_text is None else len(self.previous_text),
            "handed_back": self.handed_back,
            "served_requests": self.served_requests,
            "served_targets": list(self.served_targets),
            "reasons": list(self.reasons),
        }


class X11ClipboardPaster:
    """Owns the X CLIPBOARD selection and serves it, so Ctrl+V produces text.

    Args:
        settings: The ``[clipboard]`` configuration.
        display_name: X display to use; ``None`` uses ``$DISPLAY``.
        sleep: Sleep function, injectable so tests never wait for real.
        clock: Monotonic clock, injectable for deterministic tests.

    Satisfies :class:`control.keyboard.ClipboardPaster` structurally
    (``set_text`` / ``restore``). It is constructed by the composition root and
    handed to the keyboard controller, which is why this module never imports the
    controller at runtime.
    """

    def __init__(
        self,
        settings: ClipboardSettings | None = None,
        *,
        display_name: str | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings if settings is not None else ClipboardSettings()
        self._display_name = display_name
        self._sleep = sleep
        self._clock = clock

        self._lock = threading.RLock()
        self._connection: Any = None
        self._owner_window: Any = None
        self._atoms: dict[str, int] = {}

        #: The text currently offered, or ``None`` when nothing is served. Read by
        #: the serve thread without the lock (a plain reference read) and replaced
        #: under it.
        self._serving: str | None = None
        self._previous_text: str | None = None
        self._previous_owner_id: int | None = None
        self._owns_selection = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        self._served_requests = 0
        self._served_targets: set[str] = set()
        self._set_calls = 0
        self._restore_calls = 0
        self._last_error: str | None = None
        self._last_report: RestoreReport | None = None

    @property
    def enabled(self) -> bool:
        """True when configuration permits borrowing the clipboard (section 50)."""
        return bool(self._settings.enabled)

    # -- Probing --------------------------------------------------------------

    def probe(self) -> ClipboardProbe:
        """Functionally probe clipboard ownership support (section 28 rule 12).

        Performs the real operations serving the selection requires, and takes
        ownership of nothing (see :class:`ClipboardProbe`). Availability is never
        inferred from an import or from an installed executable.
        """
        started = self._clock()
        try:
            self._connect()
        except BlaxcyError as exc:
            return ClipboardProbe(
                "xlib-selection",
                False,
                reason=exc.message,
                latency_ms=self._elapsed_ms(started),
                details=dict(exc.details),
            )
        return ClipboardProbe(
            "xlib-selection",
            True,
            latency_ms=self._elapsed_ms(started),
            details={
                "selection": SELECTION_CLIPBOARD,
                "targets": [TARGET_UTF8_STRING, TARGET_STRING, TARGET_TEXT],
                "owner_window": self._owner_window.id if self._owner_window is not None else None,
                "selection_owned": self._current_owner_id() is not None,
                "takes_ownership": False,
                "serves_lazily": True,
            },
        )

    # -- The ClipboardPaster interface ----------------------------------------

    def set_text(self, text: str) -> bool:
        """Take the selection and serve ``text`` until :meth:`restore`.

        Returns:
            ``True`` only when BLAXCY really owns the selection and is serving
            ``text``; ``False`` (with the reason kept in :meth:`status`) when the
            clipboard cannot be used. It never reports ``True`` for a clipboard
            that is not actually being served.
        """
        text = str(text)
        with self._lock:
            self._set_calls += 1
            self._last_error = None
            if not self.enabled:
                self._last_error = "clipboard typing is disabled by configuration"
                return False
            encoded = text.encode("utf-8")
            if len(encoded) > self._settings.max_bytes:
                self._last_error = (
                    f"text is {len(encoded)} bytes, above the {self._settings.max_bytes} "
                    "byte clipboard limit"
                )
                return False
            try:
                self._connect()
            except BlaxcyError as exc:
                self._last_error = exc.message
                return False

            # Capture the previous state before taking ownership: afterwards it is
            # unrecoverable, because an X selection is served lazily and
            # exclusively. A repeated set_text keeps the *original* capture.
            if not self._owns_selection:
                self._previous_owner_id = self._current_owner_id()
                self._previous_text = self._read_selection_text()

            if not self._take_ownership():
                return False

            self._serving = text
            self._owns_selection = True
            self._stop.clear()
            thread = self._thread
            if thread is None or not thread.is_alive():
                # Discard anything already queued before the serve loop starts. An
                # X client receives a ``SelectionClear`` whenever it *loses* the
                # selection, including when a previous restore handed it away, and
                # such a stale event would otherwise stop the next transfer the
                # moment it began.
                self._drain_events()
                self._start_serve_thread()
            # Otherwise the existing loop is already serving, and it now serves the
            # new text. Starting a second loop would mean two threads reading one
            # X connection -- a race over the event queue, not an optimisation.
            return True

    def restore(self) -> None:
        """Stop serving BLAXCY's text and hand the selection back (section 50).

        Keeps answering for ``paste_grace_ms`` (the in-flight paste request),
        serves the captured previous content for ``restore_grace_ms``, stops, then
        hands ownership back to the previous owner window -- or releases the
        selection when there was none. Idempotent, and part of the
        :class:`control.keyboard.ClipboardPaster` protocol, so it returns nothing;
        the details are kept in :attr:`last_report`.
        """
        report = self._restore_once()
        with self._lock:
            self._last_report = report

    @property
    def last_report(self) -> RestoreReport | None:
        """The most recent :meth:`restore` report, or ``None``."""
        with self._lock:
            return self._last_report

    # -- Diagnostics ----------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """A JSON-shaped snapshot for logs, the GUI and benchmarks."""
        with self._lock:
            return {
                "backend": "xlib-selection",
                "connected": self._connection is not None,
                "owns_selection": self._owns_selection,
                "serving_chars": None if self._serving is None else len(self._serving),
                "previous_text_chars": (
                    None if self._previous_text is None else len(self._previous_text)
                ),
                "previous_owner": self._previous_owner_id,
                "served_requests": self._served_requests,
                "served_targets": sorted(self._served_targets),
                "set_calls": self._set_calls,
                "restore_calls": self._restore_calls,
                "last_error": self._last_error,
            }

    def close(self) -> None:
        """Restore the clipboard, destroy the owner window and disconnect.

        Never raises: teardown runs on error paths, including crash cleanup.
        """
        with contextlib.suppress(Exception):
            if self._owns_selection or self._serving is not None:
                self.restore()
        with self._lock:
            self._stop.set()
            self._serving = None
            window, connection = self._owner_window, self._connection
            self._owner_window = None
            self._connection = None
            self._atoms = {}
            self._owns_selection = False
        if window is not None:
            with contextlib.suppress(Exception):
                window.destroy()
        if connection is not None:
            with contextlib.suppress(Exception):
                connection.close()

    # -- restore internals ----------------------------------------------------

    def _restore_once(self) -> RestoreReport:
        """Perform the restore sequence once and describe what happened."""
        with self._lock:
            self._restore_calls += 1
            serving = self._owns_selection or self._serving is not None
            previous_text = self._previous_text
            previous_owner = self._previous_owner_id
            if not serving:
                return RestoreReport(
                    restored=False,
                    previous_owner=previous_owner,
                    previous_text=previous_text,
                    handed_back=False,
                    served_requests=self._served_requests,
                    served_targets=tuple(sorted(self._served_targets)),
                    reasons=("nothing was being served",),
                )

        # 1. Keep answering for the paste that was already injected. No lock is
        #    held here: the serve thread needs to run.
        self._grace(self._settings.paste_grace_ms)

        # 2. Offer the previous content briefly, so a clipboard manager can take
        #    it. This is the only way the user's clipboard survives at all.
        if previous_text is not None:
            self._serving = previous_text
            self._grace(self._settings.restore_grace_ms)

        # 3. Stop serving, then hand the selection back.
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)
        with self._lock:
            self._thread = None
            self._serving = None
            owns = self._owns_selection
            self._owns_selection = False

        reasons: list[str] = []
        handed_back = self._hand_back(previous_owner, reasons) if owns else False
        report = RestoreReport(
            restored=True,
            previous_owner=previous_owner,
            previous_text=previous_text,
            handed_back=handed_back,
            served_requests=self._served_requests,
            served_targets=tuple(sorted(self._served_targets)),
            reasons=tuple(reasons),
        )
        with self._lock:
            self._previous_text = None
            self._previous_owner_id = None
        return report

    # -- Connection and atoms -------------------------------------------------

    def _connect(self) -> None:
        """Open the display and create the window that will own the selection.

        Raises:
            BlaxcyError: ``CLIPBOARD_FAILED`` with the real reason when the
                display, the atoms or the owner window cannot be obtained.
        """
        with self._lock:
            if self._connection is not None:
                return
            try:
                from Xlib import X, display
            except Exception as exc:
                raise BlaxcyError(
                    ErrorCode.CLIPBOARD_FAILED,
                    f"python-xlib is not importable: {exc!r}",
                    details={"fix_hint": "pip install python-xlib"},
                ) from exc
            connection: Any = None
            try:
                connection = (
                    display.Display() if self._display_name is None else display.Display(self._display_name)
                )
                screen = connection.screen()
                window = screen.root.create_window(
                    0,
                    0,
                    1,
                    1,
                    0,
                    screen.root_depth,
                    X.InputOutput,
                    X.CopyFromParent,
                    background_pixel=screen.white_pixel,
                    event_mask=X.PropertyChangeMask,
                )
                atoms = {
                    "selection": connection.intern_atom(SELECTION_CLIPBOARD),
                    "utf8": connection.intern_atom(TARGET_UTF8_STRING),
                    "string": connection.intern_atom(TARGET_STRING),
                    "text": connection.intern_atom(TARGET_TEXT),
                    "targets": connection.intern_atom(TARGET_TARGETS),
                    "property": connection.intern_atom(BLAXCY_PROPERTY),
                }
                connection.sync()
            except Exception as exc:
                if connection is not None:
                    with contextlib.suppress(Exception):
                        connection.close()
                raise BlaxcyError(
                    ErrorCode.CLIPBOARD_FAILED,
                    f"cannot serve the X CLIPBOARD selection: {exc!r}",
                    details={"display": self._display_name or "DISPLAY"},
                ) from exc
            self._connection = connection
            self._owner_window = window
            self._atoms = atoms

    def _owns_selection_checked(self) -> bool:
        """True when the X server still says this paster owns the selection."""
        window = self._owner_window
        if window is None:
            return False
        return self._current_owner_id() == int(window.id)

    def _drain_events(self) -> None:
        """Discard events already queued on this connection.

        Called immediately before the serve loop starts, so the loop only ever
        sees events that arrived after BLAXCY took ownership. Never raises: a
        connection that cannot be read will fail honestly in the loop itself.
        """
        with contextlib.suppress(Exception):
            while self._connection.pending_events():
                self._connection.next_event()

    def _current_owner_id(self) -> int | None:
        """The window id owning CLIPBOARD, or ``None`` when nobody does."""
        try:
            owner = self._connection.get_selection_owner(self._atoms["selection"])
        except Exception:
            return None
        if owner is None:
            return None
        return int(getattr(owner, "id", owner) or 0) or None

    def _take_ownership(self) -> bool:
        """Become the selection owner, verifying that it really happened."""
        from Xlib import X
        from Xlib.protocol import request as req

        try:
            req.SetSelectionOwner(
                self._connection.display,
                window=self._owner_window,
                selection=self._atoms["selection"],
                time=X.CurrentTime,
            )
            self._connection.sync()
        except Exception as exc:
            self._last_error = f"could not take the CLIPBOARD selection: {exc!r}"
            return False
        if self._current_owner_id() != int(self._owner_window.id):
            # Somebody else owns it (a clipboard manager racing us, or a request
            # the server rejected). Say so rather than pretending the text is on
            # the clipboard and letting the paste silently do nothing.
            self._last_error = "another client owns the CLIPBOARD selection"
            return False
        return True

    def _hand_back(self, previous_owner: int | None, reasons: list[str]) -> bool:
        """Return ownership to ``previous_owner``, or release the selection."""
        from Xlib import X
        from Xlib.protocol import request as req

        try:
            req.SetSelectionOwner(
                self._connection.display,
                window=previous_owner if previous_owner else X.NONE,
                selection=self._atoms["selection"],
                time=X.CurrentTime,
            )
            self._connection.sync()
        except Exception as exc:
            reasons.append(f"could not hand the selection back: {exc!r}")
            return False
        return True

    # -- Reading the previous content ------------------------------------------

    def _read_selection_text(self) -> str | None:
        """Best-effort read of the current CLIPBOARD text, bounded in time.

        Returns ``None`` when there is no owner, when the owner does not answer
        within ``read_timeout_ms``, or when what it returns cannot be decoded.
        ``None`` is an honest "could not capture it", never an empty string
        standing in for content BLAXCY did not actually read.
        """
        from Xlib import X
        from Xlib.protocol import request as req

        if self._previous_owner_id is None:
            return None
        property_atom = self._atoms["property"]
        try:
            req.ConvertSelection(
                self._connection.display,
                requestor=self._owner_window,
                selection=self._atoms["selection"],
                target=self._atoms["utf8"],
                property=property_atom,
                time=X.CurrentTime,
            )
            self._connection.sync()
        except Exception:
            return None

        deadline = self._clock() + self._settings.read_timeout_ms / 1000.0
        while self._clock() < deadline:
            if not self._connection.pending_events():
                self._sleep(WAIT_POLL_SECONDS)
                continue
            event = self._connection.next_event()
            if event.type != X.SelectionNotify:
                continue
            if event.property == X.NONE:
                return None
            try:
                data = self._owner_window.get_full_property(property_atom, X.AnyPropertyType)
            except Exception:
                return None
            if data is None:
                return None
            value = data.value
            if isinstance(value, bytes):
                return self._decode_previous(value, getattr(data, "property_type", None))
            return str(value)
        return None

    def _decode_previous(self, value: bytes, property_type: int | None) -> str | None:
        """Decode a previous owner's payload by the type it *actually* set.

        An owner may answer ``STRING`` even when asked for ``UTF8_STRING`` (legacy
        clients do). Decoding Latin-1 bytes as UTF-8 would either fail -- losing
        content that was perfectly capturable -- or, worse, produce mojibake that
        BLAXCY would then hand back as if it were the user's text. So the type the
        owner reported decides the codec, and text that cannot be decoded is
        reported as not captured.
        """
        from Xlib import Xatom

        if property_type is not None and int(property_type) not in (
            int(self._atoms["utf8"]),
            int(Xatom.STRING),
            int(self._atoms["text"]),
        ):
            return None
        if property_type is not None and int(property_type) != int(self._atoms["utf8"]):
            try:
                return value.decode("latin-1")
            except UnicodeDecodeError:  # pragma: no cover - Latin-1 never fails
                return None
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return None

    # -- Serving ---------------------------------------------------------------

    def _start_serve_thread(self) -> None:
        """Start the (daemon) thread that answers selection requests."""
        thread = threading.Thread(target=self._serve_loop, name="blaxcy-clipboard", daemon=True)
        self._thread = thread
        thread.start()

    def _serve_loop(self) -> None:
        """Answer selection requests until stopped.

        Runs on its own thread because the transfer happens *after* the paste was
        injected: the executor's typing call returns while the target application
        is still asking for the data. Only this thread pumps this connection's
        events while it runs.
        """
        from Xlib import X

        connection = self._connection
        if connection is None:  # pragma: no cover - only reachable if closed first
            return
        while not self._stop.is_set():
            try:
                if not connection.pending_events():
                    self._sleep(SERVE_POLL_SECONDS)
                    continue
                event = connection.next_event()
            except Exception as exc:
                self._last_error = f"clipboard serve loop stopped: {exc!r}"
                return
            if event.type == X.SelectionClear:
                # Losing the selection normally means somebody else took the
                # clipboard, and BLAXCY stops rather than re-claiming it. The
                # owner is re-checked first, because the client also receives a
                # clear when *it* hands the selection away, and that stale event
                # must not be mistaken for a rival taking over.
                if self._owns_selection_checked():
                    continue
                with self._lock:
                    self._owns_selection = False
                    self._serving = None
                self._stop.set()
                return
            if event.type == X.SelectionRequest:
                self._answer_request(event, X)

    def _answer_request(self, event: Any, x: Any) -> None:
        """Write the requested data into the requestor's property and notify it.

        The data goes into a property of the **requestor's** window -- that is the
        protocol -- and an unsupported or unrepresentable target is refused with
        ``property = None`` rather than answered with mangled bytes.
        """
        from Xlib import Xatom
        from Xlib.protocol import event as xevent

        payload = self._serving
        property_atom = event.property if event.property != x.NONE else self._atoms["property"]
        target = event.target
        served: str | None = None
        try:
            if payload is None:
                raise ValueError("nothing is being served")
            if target == self._atoms["targets"]:
                event.requestor.change_property(
                    property_atom,
                    Xatom.ATOM,
                    32,
                    [self._atoms["utf8"], self._atoms["string"], self._atoms["text"]],
                )
                served = TARGET_TARGETS
            elif target == self._atoms["utf8"]:
                event.requestor.change_property(property_atom, target, 8, payload.encode("utf-8"))
                served = TARGET_UTF8_STRING
            elif target == self._atoms["text"]:
                event.requestor.change_property(property_atom, target, 8, payload.encode("utf-8"))
                served = TARGET_TEXT
            elif target == self._atoms["string"]:
                # STRING is Latin-1 by convention: text that cannot be represented
                # is refused rather than truncated into mojibake (section 50).
                event.requestor.change_property(property_atom, target, 8, payload.encode("latin-1"))
                served = TARGET_STRING
        except (Exception, UnicodeEncodeError):
            served = None

        try:
            self._connection.send_event(
                event.requestor,
                xevent.SelectionNotify(
                    time=event.time,
                    requestor=event.requestor,
                    selection=event.selection,
                    target=event.target,
                    property=property_atom if served is not None else x.NONE,
                ),
            )
            self._connection.flush()
        except Exception as exc:
            self._last_error = f"could not answer a selection request: {exc!r}"
            return
        with self._lock:
            self._served_requests += 1
            if served is not None:
                self._served_targets.add(served)

    # -- Helpers ---------------------------------------------------------------

    def _grace(self, milliseconds: int) -> None:
        """Wait out a grace window in slices, so the wait stays interruptible."""
        remaining = max(0, int(milliseconds)) / 1000.0
        while remaining > 0:
            slice_seconds = min(remaining, GRACE_SLICE_SECONDS)
            self._sleep(slice_seconds)
            remaining -= slice_seconds

    def _elapsed_ms(self, started: float) -> float:
        """Milliseconds since ``started`` on the injected monotonic clock."""
        return max(0.0, (self._clock() - started) * 1000.0)


def probe_x11_clipboard(settings: ClipboardSettings | None = None) -> ClipboardProbe:
    """Probe clipboard support on a throwaway paster, then clean up.

    Used by the capability probe, so the reported status comes from the same code
    path the typing layer will use (sections 28, 50).
    """
    paster = X11ClipboardPaster(settings)
    try:
        return paster.probe()
    finally:
        paster.close()


def select_clipboard_paster(
    settings: ClipboardSettings | None = None,
) -> X11ClipboardPaster | None:
    """Return a probed clipboard paster, or ``None`` when clipboard use is impossible.

    ``None`` is a real answer: the keyboard controller then refuses long or
    non-ASCII text with ``UNICODE_UNSUPPORTED`` instead of typing an
    approximation (section 50). That includes the deliberate case where the
    operator disabled clipboard typing -- a disabled mechanism is not handed to
    the typing layer at all, so the failure is reported as "no clipboard" rather
    than discovered later mid-paste.
    """
    paster = X11ClipboardPaster(settings)
    if not paster.enabled:
        paster.close()
        return None
    if paster.probe().available:
        return paster
    paster.close()
    return None


#: Checked by mypy: this class really satisfies the consumer's plug-in interface
#: (``control.keyboard.ClipboardPaster``) without a runtime import edge.
if TYPE_CHECKING:  # pragma: no cover - typing/verification only
    _conforms: type[ClipboardPaster] = X11ClipboardPaster


__all__ = [
    "BLAXCY_PROPERTY",
    "SERVE_POLL_SECONDS",
    "ClipboardProbe",
    "RestoreReport",
    "X11ClipboardPaster",
    "probe_x11_clipboard",
    "select_clipboard_paster",
]
