"""Configuration defaults, merging, and security invariants (spec section 72)."""

from __future__ import annotations

from pathlib import Path

import pytest

from config.settings import (
    DEFAULT_SETTINGS_TOML,
    SCHEMA_VERSION,
    Settings,
    effective_settings_path,
    invariant_violations,
    load_settings,
    save_settings,
)
from schemas.enums import PolicyMode
from schemas.errors import BlaxcyError


def test_packaged_defaults_load_and_match_spec() -> None:
    """Packaged defaults must load and match the specification's numbers."""
    settings = load_settings(DEFAULT_SETTINGS_TOML)
    assert settings.schema_version == SCHEMA_VERSION
    assert settings.capture.idle_fps == 2.0
    assert settings.capture.normal_fps == 10.0
    assert settings.capture.active_fps == 30.0
    assert settings.perception.pixel_threshold == 12
    assert settings.lease.ttl_ms == 800
    assert settings.verification.max_action_state_age_ms == 1500
    assert settings.resolver.act_threshold == 0.65
    assert settings.recovery.automatic_attempts_per_tool == 2
    assert settings.safety.mode is PolicyMode.OBSERVE


def test_batching_layer_ships_disabled() -> None:
    """Section 4 rule 31: unproven optimizations default off."""
    settings = Settings()
    assert settings.resolver_cache.enabled is False
    assert settings.sequence.enabled is False
    assert settings.sequence.capture_boost is False
    assert settings.sequence.speculative_perception is False


def test_user_override_merges_over_defaults(tmp_path: Path) -> None:
    """A partial user config overrides only what it names."""
    path = tmp_path / "config.toml"
    path.write_text('[capture]\nidle_fps = 5.0\n', encoding="utf-8")
    settings = load_settings(path)
    assert settings.capture.idle_fps == 5.0
    assert settings.capture.normal_fps == 10.0


def test_unsafe_invariant_is_rejected(tmp_path: Path) -> None:
    """Disabling terminal confirmation must fail to load, not run unsafely."""
    path = tmp_path / "config.toml"
    path.write_text("[terminal]\nsubmit_requires_confirmation = false\n", encoding="utf-8")
    with pytest.raises(BlaxcyError) as excinfo:
        load_settings(path)
    assert "security invariant" in excinfo.value.message
    assert excinfo.value.details["violations"]


def test_invariant_violations_can_be_enumerated() -> None:
    """The invariant list is introspectable for tests and diagnostics."""
    settings = Settings()
    settings.terminal.submit_requires_confirmation = False
    violations = invariant_violations(settings)
    assert len(violations) == 1
    assert "confirmation" in violations[0]


def test_unknown_section_key_is_rejected(tmp_path: Path) -> None:
    """Strict models turn config typos into loud errors, not silent no-ops."""
    path = tmp_path / "config.toml"
    path.write_text("[safety]\nmode = 'OBSERVE'\nbogus = 1\n", encoding="utf-8")
    with pytest.raises(BlaxcyError):
        load_settings(path)


def test_invalid_mode_is_rejected(tmp_path: Path) -> None:
    """The policy mode must be one of the three defined modes."""
    path = tmp_path / "config.toml"
    path.write_text("[safety]\nmode = 'YOLO'\n", encoding="utf-8")
    with pytest.raises(BlaxcyError):
        load_settings(path)


def test_save_load_roundtrip(tmp_path: Path) -> None:
    """Settings survive a TOML write/read cycle unchanged."""
    original = Settings()
    out = save_settings(original, tmp_path / "out.toml")
    assert out.exists()
    assert load_settings(out) == original


def test_effective_path_prefers_explicit(tmp_path: Path) -> None:
    """An explicit --config path always wins."""
    explicit = tmp_path / "explicit.toml"
    assert effective_settings_path(explicit) == explicit
