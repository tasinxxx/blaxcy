"""Fixture application tests (specification section 74).

These tests launch the real fixture as a child process on the Qt offscreen
platform and drive it over its control channel. They prove the fixture exposes
every required control and that each control genuinely changes state -- the
fixture must not merely *look* complete.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from typing import Any

import pytest

from tests.harness import FixtureApp

#: Controls section 74 requires the fixture to expose.
REQUIRED_CONTROLS = {
    "btn_alpha",
    "btn_alpha_dup_1",
    "btn_alpha_dup_2",
    "btn_beta",
    "text_input",
    "password_input",
    "menu_file",
    "canvas",
    "icon_only",
    "movable",
    "destroyed_target",
    "occluder",
    "state_checkbox",
}

#: The five-control workflow fixture, in pipeline order.
WORKFLOW_CONTROLS = [
    "search_icon",
    "search_input",
    "submit_button",
    "result_list",
    "play_button",
]


@pytest.fixture(scope="module")
def fixture() -> Iterator[FixtureApp]:
    """One fixture process shared by the module (launch cost is not free)."""
    with FixtureApp() as app:
        yield app


def _names(app: FixtureApp) -> set[str]:
    """Current inventory object names."""
    return {str(entry["object_name"]) for entry in app.inventory()}


def _entry(app: FixtureApp, object_name: str) -> dict[str, Any]:
    """Return the inventory entry for ``object_name`` or fail the test."""
    for entry in app.inventory():
        if entry["object_name"] == object_name:
            return dict(entry)
    raise AssertionError(f"{object_name!r} is not present in the fixture inventory")


def test_ready_reports_inventory(fixture: FixtureApp) -> None:
    """The fixture announces readiness with its full inventory, not a bare ping."""
    assert fixture.ready_payload["event"] == "ready"
    assert fixture.ready_payload["inventory"]
    assert fixture.ready_payload["title"] == "BLAXCY Test Fixture"


def test_all_required_controls_present(fixture: FixtureApp) -> None:
    """Every control section 74 requires is actually present."""
    assert _names(fixture) >= REQUIRED_CONTROLS


def test_password_input_is_masked(fixture: FixtureApp) -> None:
    """The password control must genuinely be a masked input (section 55)."""
    assert _entry(fixture, "password_input")["password"] is True
    assert _entry(fixture, "text_input")["password"] is False


def test_duplicate_alpha_controls_share_accessible_name(fixture: FixtureApp) -> None:
    """The ambiguity fixture needs same-name, same-role duplicates (section 43)."""
    duplicates = [
        entry for entry in fixture.inventory() if entry["accessible_name"] == "Button Alpha"
    ]
    assert len(duplicates) == 3
    assert len({str(entry["class"]) for entry in duplicates}) == 1


def test_click_changes_state(fixture: FixtureApp) -> None:
    """A click must be observably state-changing, not a no-op."""
    before = fixture.stats()["counters"].get("btn_beta", 0)
    fixture.click("btn_beta")
    fixture.click("btn_beta")
    after = fixture.stats()["counters"]["btn_beta"]
    assert after == before + 2


def test_set_text_roundtrips(fixture: FixtureApp) -> None:
    """Typed text is readable back from the fixture state."""
    fixture.set_text("text_input", "hello blaxcy")
    assert fixture.stats()["text_input"] == "hello blaxcy"


def test_toggle_changes_checkbox_state(fixture: FixtureApp) -> None:
    """A toggle control changes its own checked state."""
    initial = fixture.stats()["checkbox_checked"]
    fixture.toggle("state_checkbox")
    assert fixture.stats()["checkbox_checked"] is not initial


def test_move_relocates_target(fixture: FixtureApp) -> None:
    """The movable target actually moves, so staleness tests have a moving part."""
    before = _entry(fixture, "movable")["global"]["x"]
    fixture.move("movable", dx=37, dy=11)
    after = _entry(fixture, "movable")["global"]["x"]
    assert after == before + 37


def test_destroy_removes_target(fixture: FixtureApp) -> None:
    """Destroying a target is observable and marks it destroyed."""
    fixture.destroy("destroyed_target")
    assert "destroyed_target" in fixture.stats()["destroyed"]

    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and "destroyed_target" in _names(fixture):
        time.sleep(0.05)
    assert "destroyed_target" not in _names(fixture)


def test_occluder_can_be_shown_and_hidden(fixture: FixtureApp) -> None:
    """Occlusion is a real, controllable state (section 46)."""
    fixture.set_occluder(True)
    assert fixture.stats()["occluder_visible"] is True
    fixture.set_occluder(False)
    assert fixture.stats()["occluder_visible"] is False


def test_focus_can_be_set(fixture: FixtureApp) -> None:
    """Focus changes are driveable for section 51 focus-guard tests."""
    fixture.set_focus("text_input")
    assert fixture.stats()["focused"] == "text_input"
    fixture.set_focus("search_input")
    assert fixture.stats()["focused"] == "search_input"


def test_dialog_can_be_opened(fixture: FixtureApp) -> None:
    """The dialog is real, openable, and contains its own control."""
    response = fixture.open_dialog()
    assert response.payload["dialog_visible"] is True
    assert "fixture_dialog" in _names(fixture)


def test_workflow_fixture_end_to_end(fixture: FixtureApp) -> None:
    """The 5-control workflow fixture behaves as a real search->play pipeline."""
    assert _names(fixture) >= set(WORKFLOW_CONTROLS)
    assert fixture.stats()["workflow_present"] is True

    fixture.set_text("search_input", "")
    fixture.click("search_icon")
    assert fixture.stats()["focused"] == "search_input"

    fixture.set_text("search_input", "a song")
    fixture.click("submit_button")
    stats = fixture.stats()
    assert stats["result_count"] == 3
    assert stats["play_enabled"] is True
    assert fixture.click("play_button").ok


def test_unknown_target_is_reported_not_ignored(fixture: FixtureApp) -> None:
    """Bad commands produce structured errors; the fixture never silently no-ops."""
    response = fixture.command("click", target="does_not_exist")
    assert response.ok is False
    assert "not clickable" in response.message

    unknown = fixture.command("definitely_not_a_command")
    assert unknown.ok is False
    assert "unknown command" in unknown.message


def test_fixture_stays_alive_after_bad_commands(fixture: FixtureApp) -> None:
    """Recovering from a bad command must not require relaunching the fixture."""
    assert fixture.command_ok("ping").payload["pong"] is True


def test_stop_terminates_the_process() -> None:
    """Stopping the fixture must actually reap the child process."""
    app = FixtureApp(start_timeout=30.0).start()
    process = app._process  # the test is specifically about process lifecycle
    assert process is not None
    app.stop()
    assert app._process is None
    assert process.poll() is not None


@pytest.mark.skipif(
    not os.environ.get("BLAXCY_FIXTURE_REAL_DISPLAY"),
    reason="real-display fixture run is opt-in (it opens a visible window)",
)
def test_fixture_launches_on_real_display() -> None:
    """When explicitly requested, the fixture starts on the real display."""
    with FixtureApp(platform=None) as app:
        assert app.ready_payload["event"] == "ready"
        assert app.command_ok("ping").payload["pong"] is True
