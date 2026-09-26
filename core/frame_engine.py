"""Frame engine -- bounded, adaptive screen capture (specification section 33).

The frame engine is the only place in BLAXCY that grabs pixels from the desktop.
It is deliberately small and honest about its limits:

* **Bounded memory.** A rolling window of at most ``capture.max_full_frames``
  full frames (2 by default) and ``capture.max_thumbnails`` thumbnails (8 by
  default) is retained. There is no unbounded image queue (section 33). Every
  frame is an immutable snapshot, so a stale frame can never be mutated into a
  current one.
* **Adaptive capture targets.** ``idle`` (2 FPS), ``normal`` (10 FPS) and
  ``active`` (30 FPS) profiles set a *target* interval. These are targets, not
  guarantees: the engine measures its own latency and reports effective FPS
  rather than claiming a rate it did not achieve (sections 23, 33).
* **Task-active boost.** :meth:`FrameEngine.active_window` temporarily raises
  the profile for the duration of a multi-step task (section 33.1). It is a
  scheduling change only; it alters no safety check.
* **Honest failure.** Three consecutive capture errors reinitialize the
  backend; repeated reinitialization failure raises ``CAPTURE_FAILED`` and
  disarms input. A capture that cannot be performed is never reported as a
  success.

Capture backend selection: the specification makes ``mss`` the primary X11
path. This engine captures the virtual union of all monitors as a single
desktop frame (``mss`` monitor 0); per-monitor frames are not required for
change detection (section 34) and are deferred until a phase needs them.
"""

from __future__ import annotations

import importlib
import time
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, NoReturn, Protocol

import cv2
import numpy as np
import numpy.typing as npt

from config.settings import CaptureSettings
from schemas.enums import CoordinateSpace, ErrorCode
from schemas.errors import BlaxcyError
from schemas.geometry import Rect

#: Canonical thumbnail geometry (section 33): 480x270 grayscale.
THUMBNAIL_WIDTH: Final[int] = 480
THUMBNAIL_HEIGHT: Final[int] = 270

#: Three consecutive capture errors trigger a backend reinitialization, and
#: three consecutive reinitialization failures are terminal (section 33).
MAX_CONSECUTIVE_CAPTURE_ERRORS: Final[int] = 3
MAX_REINITIALIZE_FAILURES: Final[int] = 3

#: Backend identifier reported in capability/benchmark output.
CAPTURE_BACKEND_NAME: Final[str] = "mss"

_CAPTURE_FIX_HINT: Final[str] = (
    "Verify the X display / compositor state; on native Wayland capture must go "
    "through the XDG ScreenCast portal + PipeWire (section 30)."
)


class CaptureProfile(StrEnum):
    """Adaptive capture profiles (section 33). Targets, not guarantees."""

    IDLE = "idle"
    NORMAL = "normal"
    ACTIVE = "active"


@dataclass(frozen=True, eq=False)
class Frame:
    """One immutable captured frame plus its derived thumbnail.

    ``eq=False`` on purpose: two frames containing different pixel buffers are
    never "equal", and an array-valued ``__eq__`` would be ambiguous.
    """

    frame_id: int
    generation: int
    captured_at_monotonic: float
    captured_at: float
    desktop_rect: Rect
    image: npt.NDArray[np.uint8]
    thumbnail: npt.NDArray[np.uint8]
    latency_ms: float
    backend: str

    @property
    def width(self) -> int:
        """Pixel width of the full frame."""
        return int(self.image.shape[1])

    @property
    def height(self) -> int:
        """Pixel height of the full frame."""
        return int(self.image.shape[0])

    @property
    def thumbnail_size(self) -> tuple[int, int]:
        """``(width, height)`` of the derived thumbnail."""
        return (int(self.thumbnail.shape[1]), int(self.thumbnail.shape[0]))


class CaptureBackend(Protocol):
    """The minimal contract the frame engine needs from a capture backend."""

    def grab(self) -> tuple[bytes, int, int]:
        """Grab one frame, returning ``(raw_bgra_bytes, width, height)``."""
        ...

    def monitor_bounds(self) -> tuple[int, int, int, int]:
        """Return the captured region as ``(left, top, width, height)`` in DESKTOP space."""
        ...

    def monitors(self) -> tuple[dict[str, int], ...]:
        """Physical monitors as reported by the backend (never the virtual union)."""
        ...

    def close(self) -> None:
        """Release backend resources."""
        ...


class MssBackend:
    """Real capture via ``mss`` -- the primary X11 path (section 33).

    The backend is created lazily by the engine; importing/instantiating it here
    raises if ``mss`` is missing or no display is available, which the engine
    surfaces as ``CAPTURE_FAILED`` rather than a fabricated success.
    """

    #: Reported in the frame and stats, so a frame is never mislabelled as coming
    #: from a backend that did not produce it.
    name: str = CAPTURE_BACKEND_NAME

    def __init__(self) -> None:
        mss = importlib.import_module("mss")
        # ``mss.mss`` is deprecated in mss 10.x in favour of ``mss.MSS``; the
        # current factory is used deliberately (section 82: verify the installed
        # version's API rather than relying on older documentation).
        factory = getattr(mss, "MSS", None)
        if factory is None:  # pragma: no cover - defensive for older mss
            raise RuntimeError("installed mss exposes no MSS factory")
        self._capture = factory()
        # Monitor 0 is the virtual union of every monitor; the rest are physical.
        raw_monitors = list(self._capture.monitors)
        self._monitor: dict[str, int] = dict(raw_monitors[0])
        physical = raw_monitors[1:] if len(raw_monitors) > 1 else raw_monitors
        self._monitors: tuple[dict[str, int], ...] = tuple(
            {
                "left": int(monitor["left"]),
                "top": int(monitor["top"]),
                "width": int(monitor["width"]),
                "height": int(monitor["height"]),
            }
            for monitor in physical
        )

    def grab(self) -> tuple[bytes, int, int]:
        """Grab the virtual desktop once, returning BGRA bytes and dimensions."""
        shot = self._capture.grab(self._monitor)
        width = int(shot.size.width)
        height = int(shot.size.height)
        return bytes(shot.bgra), width, height

    def monitor_bounds(self) -> tuple[int, int, int, int]:
        """The desktop region this backend captures, in DESKTOP coordinates."""
        return (
            int(self._monitor.get("left", 0)),
            int(self._monitor.get("top", 0)),
            int(self._monitor["width"]),
            int(self._monitor["height"]),
        )

    def monitors(self) -> tuple[dict[str, int], ...]:
        """The physical monitors ``mss`` reports for this display."""
        return self._monitors

    def close(self) -> None:
        """Close the underlying mss instance."""
        closer = getattr(self._capture, "close", None)
        if callable(closer):
            closer()


class FrameEngine:
    """Bounded, adaptive capture of the desktop with honest failure reporting."""

    def __init__(
        self,
        settings: CaptureSettings,
        *,
        backend_factory: Callable[[], CaptureBackend] | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        thumbnail_size: tuple[int, int] = (THUMBNAIL_WIDTH, THUMBNAIL_HEIGHT),
    ) -> None:
        """Create an engine.

        Args:
            settings: The ``[capture]`` configuration section.
            backend_factory: Override for tests; defaults to :class:`MssBackend`.
            clock: Monotonic clock (injectable for deterministic tests).
            wall_clock: Wall clock used for the human-facing timestamp only.
            thumbnail_size: ``(width, height)`` of the derived grayscale thumbnail.
        """
        self._settings = settings
        self._backend_factory = backend_factory or MssBackend
        self._clock = clock
        self._wall_clock = wall_clock
        self._thumbnail_size = thumbnail_size

        self._backend: CaptureBackend | None = None
        self._backend_label: str = CAPTURE_BACKEND_NAME
        self._frames: deque[Frame] = deque(maxlen=settings.max_full_frames)
        self._thumbnails: deque[npt.NDArray[np.uint8]] = deque(maxlen=settings.max_thumbnails)

        self._profile: CaptureProfile = CaptureProfile.IDLE
        self._generation = 0
        self._next_frame_id = 0
        self._consecutive_errors = 0
        self._reinitialize_failures = 0
        self._capture_failed = False
        self._total_captures = 0
        self._total_errors = 0
        self._last_latency_ms: float | None = None
        self._capture_times: deque[float] = deque(maxlen=64)

    # -- Lifecycle ------------------------------------------------------------

    def open(self) -> None:
        """Initialize the capture backend.

        Raises:
            BlaxcyError: ``CAPTURE_FAILED`` when the backend cannot be created.
        """
        if self._backend is not None:
            return
        try:
            self._backend = self._backend_factory()
            # A backend that names itself is reported by that name (mss, the
            # ScreenCast portal, ...); an anonymous one keeps the default.
            self._backend_label = str(getattr(self._backend, "name", CAPTURE_BACKEND_NAME))
        except Exception as exc:
            self._capture_failed = True
            raise BlaxcyError(
                ErrorCode.CAPTURE_FAILED,
                f"could not initialize the capture backend: {exc!r}",
                details={"backend": self._backend_label, "fix_hint": _CAPTURE_FIX_HINT},
            ) from exc
        self._capture_failed = False
        self._consecutive_errors = 0

    def close(self) -> None:
        """Release the capture backend. Idempotent."""
        backend = self._backend
        self._backend = None
        if backend is None:
            return
        # Closing is best-effort; a failed close must not mask capture state.
        with suppress(Exception):
            backend.close()

    def __enter__(self) -> FrameEngine:
        self.open()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- Profiles (section 33 / 33.1) -----------------------------------------

    @property
    def profile(self) -> CaptureProfile:
        """The current capture profile."""
        return self._profile

    def set_profile(self, profile: CaptureProfile) -> None:
        """Set the capture profile. This is a scheduling change only."""
        self._profile = profile

    @property
    def target_interval_ms(self) -> float:
        """The target interval between captures for the current profile."""
        fps = {
            CaptureProfile.IDLE: self._settings.idle_fps,
            CaptureProfile.NORMAL: self._settings.normal_fps,
            CaptureProfile.ACTIVE: self._settings.active_fps,
        }[self._profile]
        return 1000.0 / fps

    @contextmanager
    def active_window(self) -> Iterator[CaptureProfile]:
        """Temporarily switch to the ``active`` profile (section 33.1).

        On exit -- normally, on failure, or on abort -- the previous profile is
        restored unconditionally, so a task that raises cannot leave the engine
        capturing at 30 FPS forever.
        """
        previous = self._profile
        self.set_profile(CaptureProfile.ACTIVE)
        try:
            yield previous
        finally:
            self.set_profile(previous)

    # -- Generation -----------------------------------------------------------

    @property
    def generation(self) -> int:
        """The current generation counter (section 32)."""
        return self._generation

    def bump_generation(self) -> int:
        """Advance the generation, invalidating older leases (section 32)."""
        self._generation += 1
        return self._generation

    # -- Capture --------------------------------------------------------------

    def grab(self) -> Frame:
        """Capture one frame.

        Returns:
            The captured, immutable :class:`Frame`.

        Raises:
            BlaxcyError: ``CAPTURE_FAILED`` on any capture failure. After the
                configured number of consecutive failures the backend is
                reinitialized; after repeated reinitialization failure input is
                disarmed and every later call fails fast.
        """
        if self._capture_failed:
            raise BlaxcyError(
                ErrorCode.CAPTURE_FAILED,
                "screen capture has failed and input is disarmed until the backend recovers",
                details={"backend": self._backend_label},
            )

        if self._backend is None:
            self.open()
        backend = self._backend
        if backend is None:  # pragma: no cover - open() guarantees a backend
            raise BlaxcyError(
                ErrorCode.CAPTURE_FAILED,
                "capture backend is unavailable",
                details={"backend": self._backend_label},
            )

        started = self._clock()
        try:
            raw, width, height = backend.grab()
            left, top, monitor_width, monitor_height = backend.monitor_bounds()
            latency_ms = (self._clock() - started) * 1000.0
            frame = self._build_frame(
                raw, width, height, left, top, monitor_width, monitor_height, latency_ms
            )
        except Exception as exc:
            self._fail_grab(exc)

        self._on_success(frame)
        return frame

    @property
    def full_frames(self) -> tuple[Frame, ...]:
        """The retained full frames, oldest first (bounded, section 33)."""
        return tuple(self._frames)

    @property
    def thumbnails(self) -> tuple[npt.NDArray[np.uint8], ...]:
        """The retained thumbnails, oldest first (bounded, section 33)."""
        return tuple(self._thumbnails)

    @property
    def monitor_topology(self) -> tuple[dict[str, int], ...]:
        """Physical monitors reported by the backend, or ``()`` when unknown.

        This is informational evidence for the capability report; it is never
        used to select a backend or to inject input.
        """
        backend = self._backend
        if backend is None:
            return ()
        try:
            return backend.monitors()
        except Exception:
            # Optional evidence: an unreadable topology is reported as unknown
            # rather than crashing a read-only capability probe.
            return ()

    @property
    def capture_failed(self) -> bool:
        """True when capture failed terminally and input is disarmed."""
        return self._capture_failed

    @property
    def backend_name(self) -> str:
        """The backend actually in use, by its own name (never a guess).

        ``mss`` while the X11 backend is up; the portal capture backend's own
        name when a Wayland session is being captured through ScreenCast. Before
        any backend is opened this is the default label, which is exactly what a
        caller should then display.
        """
        return self._backend_label

    @property
    def input_disarmed(self) -> bool:
        """Alias for :attr:`capture_failed` -- the section 33 disarm signal."""
        return self._capture_failed

    @property
    def measured_fps(self) -> float | None:
        """Effective capture rate over the retained window, or ``None``.

        Reported as a measurement, never as a promise (sections 23, 76).
        """
        times = self._capture_times
        if len(times) < 2:
            return None
        span = times[-1] - times[0]
        if span <= 0.0:
            return None
        return (len(times) - 1) / span

    def stats(self) -> dict[str, Any]:
        """A JSON-shaped health/performance snapshot for logs and benchmarks."""
        return {
            "backend": self._backend_label,
            "profile": self._profile.value,
            "target_interval_ms": self.target_interval_ms,
            "total_captures": self._total_captures,
            "total_errors": self._total_errors,
            "consecutive_errors": self._consecutive_errors,
            "reinitialize_failures": self._reinitialize_failures,
            "capture_failed": self._capture_failed,
            "input_disarmed": self._capture_failed,
            "last_latency_ms": self._last_latency_ms,
            "measured_fps": self.measured_fps,
            "retained_frames": len(self._frames),
            "retained_thumbnails": len(self._thumbnails),
        }

    # -- Internals ------------------------------------------------------------

    def _build_frame(
        self,
        raw: bytes,
        width: int,
        height: int,
        left: int,
        top: int,
        monitor_width: int,
        monitor_height: int,
        latency_ms: float,
    ) -> Frame:
        """Decode a raw BGRA buffer into a frame plus grayscale thumbnail."""
        if width <= 0 or height <= 0:
            raise ValueError(f"capture backend reported a non-positive size ({width}x{height})")
        expected = width * height * 4
        if len(raw) != expected:
            raise ValueError(
                f"capture backend returned {len(raw)} bytes, expected {expected} "
                f"for a {width}x{height} BGRA frame"
            )
        # mss returns BGRA bytes; the buffer is read-only, which is exactly what
        # an immutable frame wants.
        image: npt.NDArray[np.uint8] = np.frombuffer(raw, dtype=np.uint8).reshape(
            height, width, 4
        )
        gray = cv2.cvtColor(image, cv2.COLOR_BGRA2GRAY)
        thumbnail: npt.NDArray[np.uint8] = np.asarray(
            cv2.resize(gray, self._thumbnail_size, interpolation=cv2.INTER_AREA),
            dtype=np.uint8,
        )
        frame = Frame(
            frame_id=self._next_frame_id,
            generation=self._generation,
            captured_at_monotonic=self._clock(),
            captured_at=self._wall_clock(),
            desktop_rect=Rect(
                x=float(left),
                y=float(top),
                width=float(monitor_width),
                height=float(monitor_height),
                space=CoordinateSpace.DESKTOP,
            ),
            image=image,
            thumbnail=thumbnail,
            latency_ms=latency_ms,
            backend=self._backend_label,
        )
        self._next_frame_id += 1
        return frame

    def _on_success(self, frame: Frame) -> None:
        """Record a successful capture and clear the error counters."""
        self._consecutive_errors = 0
        self._reinitialize_failures = 0
        self._total_captures += 1
        self._last_latency_ms = frame.latency_ms
        self._capture_times.append(frame.captured_at_monotonic)
        self._frames.append(frame)
        self._thumbnails.append(frame.thumbnail)

    def _fail_grab(self, cause: BaseException) -> NoReturn:
        """Record a capture failure, maybe reinitialize, then raise."""
        self._consecutive_errors += 1
        self._total_errors += 1
        if self._consecutive_errors >= MAX_CONSECUTIVE_CAPTURE_ERRORS:
            self._reinitialize(cause)
        raise BlaxcyError(
            ErrorCode.CAPTURE_FAILED,
            f"screen capture failed: {cause!r}",
            details={
                "backend": self._backend_label,
                "consecutive_errors": self._consecutive_errors,
                "fix_hint": _CAPTURE_FIX_HINT,
            },
        )

    def _reinitialize(self, cause: BaseException) -> None:
        """Replace a failing backend; disarm input after repeated failures."""
        try:
            replacement = self._backend_factory()
        except Exception as exc:
            self._reinitialize_failures += 1
            # Start a fresh error streak: reinitialization is the response to a
            # streak, so a failed one must not be retried on every later grab.
            self._consecutive_errors = 0
            if self._reinitialize_failures >= MAX_REINITIALIZE_FAILURES:
                self._capture_failed = True
                raise BlaxcyError(
                    ErrorCode.CAPTURE_FAILED,
                    "capture backend repeatedly failed to reinitialize; input is disarmed",
                    details={
                        "backend": self._backend_label,
                        "reinitialize_failures": self._reinitialize_failures,
                        "last_cause": repr(cause),
                    },
                ) from exc
            return

        previous = self._backend
        self._backend = replacement
        self._reinitialize_failures = 0
        self._consecutive_errors = 0
        if previous is not None:
            with suppress(Exception):
                previous.close()
