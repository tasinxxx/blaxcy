"""Section 76 read-only benchmark integration tests.

``bench/real_desktop.py`` measures the Phase 14 numbers over the live desktop.
Three of them -- the warm and cold perception cycles (sections 33-43/65) and the
section 71 GUI refresh -- inject nothing, so unlike the click/keyboard/sequence
benchmarks they are verifiable in the ordinary gate rather than behind the
``--confirm-real-input`` opt-in.

These tests prove each really measures the assembled Body (a number comes out,
with the sample count asked for) rather than returning an empty distribution that
would make the benchmark look green while measuring nothing, and they prove the
warm/cold distinction is real: a cold cycle pays an AT-SPI traversal the warm one
skips (section 35's cache must never be presented as the live tree). No action is
ever dispatched and the Body stays in its safe default mode, so no input reaches
the desktop; the Body is assembled the production way, not mocked.
"""

from __future__ import annotations

import os
import statistics
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest

from bench.real_desktop import (
    bench_gui_refresh,
    bench_perceive_cycle,
    bench_perceive_cycle_cold,
)
from config.settings import load_settings
from core.application import BlaxcyApplication
from gui.app import self_excluded_settings

pytestmark = [
    pytest.mark.gui,
    pytest.mark.skipif(
        not os.environ.get("DISPLAY"),
        reason="real-display benchmark test requires an X display (DISPLAY unset)",
    ),
]


@dataclass
class ReadOnlyHarness:
    """The one attribute the read-only benchmarks touch (``AppOwner``)."""

    app: BlaxcyApplication | None


@pytest.fixture(scope="module")
def harness(tmp_path_factory: pytest.TempPathFactory) -> Iterator[ReadOnlyHarness]:
    """One assembled Body for the module, or an honest skip."""
    settings = self_excluded_settings(load_settings(None))
    application = BlaxcyApplication(
        settings,
        # Never take the operator's CLIPBOARD selection, and keep the section 64
        # heartbeat out of their runtime directory.
        clipboard=None,
        watchdog_path=tmp_path_factory.mktemp("bench-readonly") / "watchdog.json",
    )
    startup = application.start()
    if not startup.started:
        application.shutdown()
        pytest.skip(f"the Body could not start on this host: {startup.notes}")
    try:
        yield ReadOnlyHarness(application)
    finally:
        application.shutdown()


def test_warm_perceive_cycle_measures_the_real_body(harness: ReadOnlyHarness) -> None:
    """One full perception cycle over the live desktop yields ``samples`` timings."""
    result = bench_perceive_cycle(harness, samples=3)

    assert len(result.values_ms) == 3, "the perception cycle did not run the requested samples"
    assert result.summary()["n"] == 3
    assert "cache" in result.note


def test_cold_perceive_cycle_pays_the_traversal_the_warm_one_skips(
    harness: ReadOnlyHarness,
) -> None:
    """The cold cycle is a different measurement, not a relabelled warm one."""
    assert harness.app is not None

    warm = bench_perceive_cycle(harness, samples=3)
    cold = bench_perceive_cycle_cold(harness, samples=3)

    assert len(cold.values_ms) == 3
    assert "fresh AT-SPI traversal" in cold.note

    if not harness.app.accessibility.is_running:
        pytest.skip("the accessibility tree is unavailable, so there is no traversal to pay for")
    # A live traversal over a real desktop tree dwarfs the warm cache read; if the
    # cold benchmark were not actually traversing, the two would be indistinguishable.
    assert statistics.median(cold.values_ms) > statistics.median(warm.values_ms)


def test_gui_refresh_measures_the_real_window(harness: ReadOnlyHarness, qt_app: Any) -> None:
    """The section 71 refresh really re-reads and re-renders over the live Body."""
    result = bench_gui_refresh(harness, samples=3)

    assert len(result.values_ms) == 3, "the GUI refresh did not run the requested samples"
    assert result.summary()["n"] == 3
    assert "offscreen Qt" in result.note
    assert "not measured" not in result.note
