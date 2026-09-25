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
    # Section 44's bound had to be raised from 800 ms: section 45 revalidates a
    # lease against a *fresh* observation, and a real re-perception on this host
    # measures 0.68-1.36 s, so the tighter bound expired during the very
    # re-perception meant to validate it. See ``docs/benchmark_report.md``.
    assert settings.lease.ttl_ms == 3000
    assert settings.verification.max_action_state_age_ms == 1500
    assert settings.resolver.act_threshold == 0.65
    assert settings.recovery.automatic_attempts_per_tool == 2
    assert settings.safety.mode is PolicyMode.OBSERVE


def test_batching_layer_is_enabled_by_default() -> None:
    """Sections 83/84: the layer is on now that the 66.1/75 test list is green."""
    settings = Settings()
    assert settings.resolver_cache.enabled is True
    assert settings.sequence.enabled is True
    assert settings.sequence.capture_boost is True
    assert settings.sequence.speculative_perception is True


def test_batching_layer_can_be_disabled_by_user_config(tmp_path: Path) -> None:
    """Each flag stays a switch: an operator can turn the layer off in config."""
    path = tmp_path / "config.toml"
    path.write_text(
        "[resolver_cache]\nenabled = false\n\n[sequence]\nenabled = false\n"
        "capture_boost = false\nspeculative_perception = false\n",
        encoding="utf-8",
    )
    settings = load_settings(path)
    assert settings.resolver_cache.enabled is False
    assert settings.sequence.enabled is False
    assert settings.sequence.capture_boost is False
    assert settings.sequence.speculative_perception is False


def test_visual_grounding_defaults_match_the_specification() -> None:
    """Section 41's limits are the packaged defaults, and the ceiling is a bound."""
    settings = Settings()
    assert settings.visual.enabled is True
    assert settings.visual.max_image_side == 1024
    assert settings.visual.max_confidence == 0.80
    assert settings.visual.max_results == 3


def test_the_visual_confidence_ceiling_cannot_be_raised(tmp_path: Path) -> None:
    """A config may lower section 41's 0.80 ceiling, never raise it."""
    path = tmp_path / "config.toml"
    path.write_text("[visual]\nmax_confidence = 0.99\n", encoding="utf-8")
    with pytest.raises(BlaxcyError):
        load_settings(path)


def test_the_visual_fallback_can_be_disabled_by_config(tmp_path: Path) -> None:
    """The switch removes the capability without weakening any check."""
    path = tmp_path / "config.toml"
    path.write_text("[visual]\nenabled = false\n", encoding="utf-8")
    settings = load_settings(path)
    assert settings.visual.enabled is False
    assert settings.visual.max_image_side == 1024


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
