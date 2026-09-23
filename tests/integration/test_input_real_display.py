"""Real-display input-backend integration test (specification sections 30, 48).

This runs against the live X11 display and verifies the parts of the input layer
that can be checked **without injecting any input at all**: the functional probe,
backend selection, pointer readback, and keysym/keycode resolution. It never
moves the pointer and never presses a key -- injecting into the live desktop is
gated behind policy, resolution, a lease and revalidation (sections 13, 44, 45),
none of which exist in Phase 7.

If the display or XTEST is unavailable the test skips with an honest reason
rather than fabricating a result.
"""

from __future__ import annotations

import os

import pytest

from control.backends import XtestBackend, keysym_for_char, select_backend

pytestmark = pytest.mark.skipif(
    not os.environ.get("DISPLAY"),
    reason="real-display input test requires an X display (DISPLAY unset)",
)


def _require_backend() -> XtestBackend:
    """Return an XTEST backend, or skip when XTEST is not functionally present."""
    backend = XtestBackend()
    probe = backend.probe()
    if not probe.available:
        pytest.skip(f"XTEST is not available: {probe.reason}")
    return backend


def test_xtest_probe_is_functional() -> None:
    """The probe reports an actual XTEST version, not merely an import."""
    probe = XtestBackend().probe()
    if not probe.available:
        pytest.skip(f"XTEST is not available: {probe.reason}")
    assert probe.name == "xtest"
    assert probe.details.get("xtest_version")


def test_select_backend_picks_xtest_on_a_live_display() -> None:
    """On this host the functional probe selects the XTEST backend."""
    if not XtestBackend().probe().available:
        pytest.skip("XTEST is not available on this display")
    backend = select_backend()
    assert isinstance(backend, XtestBackend)


def test_pointer_readback_is_a_real_read_only_query() -> None:
    """Pointer readback returns real integer coordinates without moving it."""
    backend = _require_backend()
    try:
        position = backend.get_pointer_position()
        assert position is not None
        assert isinstance(position[0], int) and isinstance(position[1], int)
        assert backend.supports_pointer_readback is True
    finally:
        backend.close()


def test_keysym_resolution_matches_the_live_keyboard_mapping() -> None:
    """Printable ASCII resolves, and Shift is detected where the mapping needs it."""
    backend = _require_backend()
    try:
        lower = backend.resolve_key(keysym_for_char("a") or 0)
        upper = backend.resolve_key(keysym_for_char("A") or 0)
        assert lower is not None and lower.keycode > 0 and lower.shift is False
        assert upper is not None and upper.keycode > 0 and upper.shift is True
    finally:
        backend.close()
