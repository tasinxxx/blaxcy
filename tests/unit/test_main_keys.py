"""The ``main.py keys`` subcommand (specification section 69).

Three properties are checked: the status output carries no secret, the key is
accepted only from a hidden prompt (never an argument), and an empty answer is
refused rather than stored. The keyring is replaced with an in-memory store so
the test never touches the developer's real credentials.
"""

from __future__ import annotations

import getpass

import pytest

import main
from security.keyring_manager import ApiKeyManager

SECRET = "AIzaSy-not-a-real-key-0000000000000000000"


class FakeStore:
    """An in-memory secret store."""

    def __init__(self) -> None:
        """Create the empty store."""
        self.data: dict[tuple[str, str], str] = {}

    def get(self, service: str, key: str) -> str | None:
        """Read a stored secret."""
        return self.data.get((service, key))

    def set(self, service: str, key: str, value: str) -> None:
        """Store a secret."""
        self.data[(service, key)] = value

    def delete(self, service: str, key: str) -> bool:
        """Delete a secret."""
        return self.data.pop((service, key), None) is not None


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> FakeStore:
    """Redirect ``main``'s key manager onto an in-memory store."""
    fake = FakeStore()
    monkeypatch.setattr(main, "ApiKeyManager", lambda: ApiKeyManager(fake))
    return fake


def test_status_reports_presence_without_the_secret(
    store: FakeStore, capsys: pytest.CaptureFixture[str]
) -> None:
    """``keys`` is loggable: it says whether a key exists, not what it is."""
    store.data[("blaxcy", "gemini_api_key")] = SECRET
    assert main.main(["keys"]) == 0
    output = capsys.readouterr().out
    assert '"key_present": true' in output
    assert SECRET not in output


def test_set_gemini_reads_the_key_from_a_hidden_prompt(
    store: FakeStore, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The key is never an argument, so it cannot reach argv or history."""
    asked: list[str] = []

    def fake_getpass(prompt: str = "") -> str:
        asked.append(prompt)
        return SECRET

    monkeypatch.setattr(getpass, "getpass", fake_getpass)
    assert main.main(["keys", "set-gemini"]) == 0
    assert asked, "the key must be requested interactively"
    assert store.data[("blaxcy", "gemini_api_key")] == SECRET
    output = capsys.readouterr().out
    assert SECRET not in output


def test_set_gemini_refuses_an_empty_answer(
    store: FakeStore, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An empty key would look configured while authenticating nothing."""
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": "   ")
    assert main.main(["keys", "set-gemini"]) == 2
    assert store.data == {}
    assert "refusing to store an empty key" in capsys.readouterr().err


def test_clear_gemini_reports_whether_anything_was_removed(
    store: FakeStore, capsys: pytest.CaptureFixture[str]
) -> None:
    """Clearing is honest about whether a key was actually there."""
    assert main.main(["keys", "clear-gemini"]) == 0
    assert "no Gemini API key was stored" in capsys.readouterr().out

    store.data[("blaxcy", "gemini_api_key")] = SECRET
    assert main.main(["keys", "clear-gemini"]) == 0
    assert "removed the stored Gemini API key" in capsys.readouterr().out
    assert store.data == {}
