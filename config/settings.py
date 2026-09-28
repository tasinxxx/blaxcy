"""Typed BLAXCY settings (specification sections 72 and 4 rule 25).

Every operational limit is configurable. Security invariants -- terminal
confirmation, credential-context privacy, and no continuous screen streaming --
are *not* configurable into an unsafe state: loading a config that attempts to
disable them fails loudly instead of silently degrading into an unsafe runtime.
"""

from __future__ import annotations

import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

import tomlkit
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from schemas.enums import ErrorCode, PolicyMode
from schemas.errors import BlaxcyError

#: Configuration schema version. Bump only with a migration step in
#: :mod:`config.migration`.
SCHEMA_VERSION: Final[int] = 1

#: Default runtime configuration location (created on first save, not shipped
#: with secrets). Never put API keys here; those live in the OS keyring.
DEFAULT_CONFIG_PATH: Final[Path] = Path.home() / ".config" / "blaxcy" / "config.toml"

#: Packaged defaults, used as the merge base and as the reference document.
DEFAULT_SETTINGS_TOML: Final[Path] = Path(__file__).with_name("default_settings.toml")


class _Section(BaseModel):
    """Base for config sections: strict, so typos are errors, never ignored."""

    model_config = ConfigDict(extra="forbid")


class PerformanceSettings(_Section):
    """Scheduling knobs that must never weaken a safety check (section 32.1)."""

    max_concurrent_readonly_dispatch: int = Field(default=4, ge=1, le=64)


class CaptureSettings(_Section):
    """Frame engine capture profiles (section 33). Targets, not guarantees."""

    idle_fps: float = Field(default=2.0, gt=0.0, le=120.0)
    normal_fps: float = Field(default=10.0, gt=0.0, le=120.0)
    active_fps: float = Field(default=30.0, gt=0.0, le=120.0)
    max_full_frames: int = Field(default=2, ge=1, le=8)
    max_thumbnails: int = Field(default=8, ge=1, le=64)


class PerceptionSettings(_Section):
    """Change-detector defaults (section 34)."""

    pixel_threshold: int = Field(default=12, ge=0, le=255)
    meaningful_min_width: int = Field(default=80, ge=1)
    meaningful_min_height: int = Field(default=24, ge=1)
    meaningful_area_ratio: float = Field(default=0.015, gt=0.0, le=1.0)
    major_area_ratio: float = Field(default=0.30, gt=0.0, le=1.0)
    debounce_short_ms: int = Field(default=120, ge=0)
    debounce_major_ms: int = Field(default=300, ge=0)
    max_perception_jobs_per_second: int = Field(default=5, ge=1)
    animation_recheck_seconds: float = Field(default=3.0, gt=0.0)


class AccessibilitySettings(_Section):
    """AT-SPI timeouts and bounded traversal (section 35)."""

    method_timeout_ms: int = Field(default=250, ge=1)
    startup_timeout_ms: int = Field(default=1000, ge=1)
    max_depth: int = Field(default=12, ge=1)
    max_nodes: int = Field(default=1200, ge=1)
    deadline_ms: int = Field(default=1000, ge=1)
    cache_ttl_seconds: float = Field(default=2.0, gt=0.0)
    blacklist_after_failures: int = Field(default=3, ge=1)


class OcrSettings(_Section):
    """OCR is a fallback only (section 40)."""

    minimum_width: int = Field(default=40, ge=1)
    minimum_height: int = Field(default=14, ge=1)
    max_regions: int = Field(default=6, ge=1)
    max_area_ratio: float = Field(default=0.015, gt=0.0, le=1.0)
    deadline_ms: int = Field(default=300, ge=1)


class VisualSettings(_Section):
    """Visual grounding: the final perception fallback (section 41).

    Visual grounding is the *only* source that uploads screen pixels to a remote
    model, so its knobs are deliberately narrow:

    * ``max_confidence`` is capped at the section 41 ceiling (0.80). A visual
      result can be trusted *less* than that, never more -- the bound is
      enforced by the field itself, so a config cannot raise it.
    * ``enabled = false`` removes the capability entirely (``UNAVAILABLE``), it
      never creates another path to input.
    * ``model_timeout_ms`` bounds the one model call a grounding attempt makes.

    Enabling this does **not** let it run in OBSERVE: section 41 requires
    ``mode >= ASSIST``, which the grounder and
    :func:`policy.guards.check_visual_fallback` both enforce. Since section 56
    starts every fresh configuration in OBSERVE, raising the mode is the
    operator's explicit decision before any pixel can leave the machine.
    """

    enabled: bool = True
    #: Longest side of the image uploaded to the visual model (section 41).
    max_image_side: int = Field(default=1024, ge=64, le=4096)
    #: Section 41 confidence ceiling. Only lowerable: never above 0.80.
    max_confidence: float = Field(default=0.80, gt=0.0, le=0.80)
    #: Most candidate locations accepted from one call.
    max_results: int = Field(default=3, ge=1, le=20)
    #: Bounded wait for the model response.
    model_timeout_ms: int = Field(default=12000, ge=1000, le=60000)


class ResolverSettings(_Section):
    """Target resolver scoring and ambiguity rule (section 43)."""

    act_threshold: float = Field(default=0.65, ge=0.0, le=1.0)
    max_candidates: int = Field(default=5, ge=1, le=50)
    ambiguity_gap: float = Field(default=0.08, ge=0.0, le=1.0)
    weight_text: float = Field(default=0.40, ge=0.0, le=1.0)
    weight_source: float = Field(default=0.20, ge=0.0, le=1.0)
    weight_role: float = Field(default=0.15, ge=0.0, le=1.0)
    weight_context: float = Field(default=0.15, ge=0.0, le=1.0)
    weight_geometry: float = Field(default=0.10, ge=0.0, le=1.0)


class ResolverCacheSettings(_Section):
    """Resolver cache (section 43.1).

    Enabled by default: the section 43.1/75 test list is green, a hit is only a
    traversal-order hint that is re-queried and re-scored live, and disabling it
    produces identical (only slower) targeting decisions. It remains a
    configuration switch so an operator can turn it off without a code change.
    """

    enabled: bool = True
    max_entries: int = Field(default=256, ge=1)
    ttl_seconds: float = Field(default=10.0, gt=0.0)
    semantic_match_floor: int = Field(default=70, ge=0, le=100)


class SequenceSettings(_Section):
    """``run_sequence`` limits (sections 66.1, 33.1, 33.2, 32.1).

    Enabled by default: Phase 9's safety gate passed and the section 66.1/75
    batching test list is green (sections 83, 84). Batching removes Brain
    round-trips only -- it can never skip a policy, resolve, lease, revalidate,
    execute or verify step, and every step still runs the standalone pipeline.
    Each flag stays independently switchable, and disabling one can only remove
    capability, never remove a check.
    """

    enabled: bool = True
    max_sequence_steps: int = Field(default=12, ge=1, le=256)
    max_sequence_wall_clock_seconds: float = Field(default=60.0, gt=0.0, le=3600.0)
    capture_boost: bool = True
    speculative_perception: bool = True


class InputSettings(_Section):
    """Physical input behaviour (sections 48-52).

    These are behavioural timings, not safety switches. ``guard_focus`` only
    *adds* a pre-typing focus check (section 51); setting it false never lets
    typing reach an unverified target through some other path -- the focus
    guard is the caller's opt-in, and the executor still owns lease/revalidation.
    """

    #: Settle delay after moving the pointer and before pressing a button (sec 48).
    mouse_settle_ms: int = Field(default=40, ge=0)
    #: Settle delay after a click completes, before perception (section 48).
    post_click_settle_ms: int = Field(default=120, ge=0)
    #: How far a pointer readback may differ from the requested point (section 48).
    pointer_readback_tolerance_px: int = Field(default=4, ge=0)
    #: Bounded pointer corrections after a move; never an unbounded retry loop.
    max_pointer_corrections: int = Field(default=1, ge=0, le=3)
    #: Typing directly with XTEST is used for ASCII text up to this length (sec 50).
    ascii_direct_max: int = Field(default=200, ge=1)
    #: Inter-key delay for direct typing (section 50 target band is 8-12 ms).
    key_interval_ms: int = Field(default=10, ge=0, le=100)
    #: Enforce the section 51 focus guard before typing into a text-entry target.
    guard_focus: bool = True


class ClipboardSettings(_Section):
    """Clipboard-assisted typing (section 50).

    These knobs tune how BLAXCY *borrows* the clipboard; none of them is a
    safety switch. ``enabled = false`` only removes a capability: text the
    keyboard cannot produce then fails honestly with ``UNICODE_UNSUPPORTED``
    instead of being typed. Nothing here can add a path to unverified input --
    the executor still owns the lease and revalidation that precede a paste.

    The two grace windows exist because an X selection is served *lazily*: the
    target application asks the owner for the data after the paste keystroke, so
    BLAXCY must stay the owner and answer for a moment after injecting it, and
    must serve the clipboard's previous content for a moment before handing
    ownership back (which is what lets a clipboard manager capture it).
    """

    #: Borrow the X CLIPBOARD selection for long or non-ASCII text (section 50).
    enabled: bool = True
    #: Keep serving BLAXCY's text after the paste is injected, so the in-flight
    #: selection request is still answerable.
    paste_grace_ms: int = Field(default=400, ge=0, le=5000)
    #: Serve the captured previous clipboard text before handing ownership back.
    restore_grace_ms: int = Field(default=400, ge=0, le=5000)
    #: Bounded wait when reading the clipboard's previous content.
    read_timeout_ms: int = Field(default=300, ge=1, le=5000)
    #: Largest payload BLAXCY will place on the clipboard.
    max_bytes: int = Field(default=262144, ge=1, le=16777216)


class LeaseSettings(_Section):
    """Element lease lifetime (section 44).

    The bound must exceed one real perception cycle: section 45 revalidates a
    lease against a fresh observation, and that observation is produced inside the
    lease's lifetime. A real re-perception measures 0.68-1.36 s on the reference
    host (capture + AT-SPI traversal + OCR), so a tighter default made every target
    action expire while it was being revalidated.
    """

    ttl_ms: int = Field(default=3000, ge=1)


class VerificationSettings(_Section):
    """Verification and state-age policy (sections 45, 60, 76)."""

    max_action_state_age_ms: int = Field(default=1500, ge=1)
    require_verification_for_mutating: bool = True
    #: How long a *negative* verdict may be re-checked against fresh, live
    #: observations before it is reported (section 60). Section 60's
    #: ``CONTRADICTED`` requires positive evidence, and a single read taken
    #: immediately after injection is not positive evidence when the application
    #: may not have applied the input yet -- or when the element cache (section 35)
    #: served a pre-action read. A verdict of ``VERIFIED`` is never delayed by this
    #: window; it only gives a would-be failure a chance to be disproved.
    verify_settle_ms: int = Field(default=1000, ge=0, le=30_000)
    verify_poll_ms: int = Field(default=150, ge=1, le=5_000)


class RecoverySettings(_Section):
    """Bounded recovery budgets (section 61). Never unbounded."""

    automatic_attempts_per_tool: int = Field(default=2, ge=0, le=10)
    attempts_per_task_step: int = Field(default=6, ge=0, le=32)
    identical_call_loop_guard: int = Field(default=3, ge=2, le=10)


class SafetySettings(_Section):
    """Policy mode, session ceilings and application policy (sections 56-58, 63).

    ``blocked_applications`` is the section 58 deny-list: no visual upload, no
    input, no autonomous override, and any in-flight sequence halts when the
    active application matches. ``protected_applications`` is the section 42
    privacy list: content from these applications must never be uploaded to a
    visual model (a password manager is protected even before it is blocked).

    Both are *substring* matches, case-folded, against the owning application,
    the window title and the window class -- deliberately broad, because a
    deny-list that can be evaded by a renamed window is not a deny-list.
    """

    mode: PolicyMode = PolicyMode.OBSERVE
    autonomous_max_session_duration_minutes: int = Field(default=30, ge=1, le=1440)
    emergency_stop_target_ms: int = Field(default=150, ge=1)
    blocked_applications: tuple[str, ...] = ()
    protected_applications: tuple[str, ...] = ()


class TerminalSettings(_Section):
    """Terminal safety (section 54). Invariants below forbid disabling these."""

    submit_requires_confirmation: bool = True
    destructive_always_protected: bool = True


class PrivacySettings(_Section):
    """Privacy and redaction (sections 41, 42, 55)."""

    visual_upload_on_demand_only: bool = True
    never_upload_credential_context: bool = True
    never_ocr_password_fields: bool = True


class LoggingSettings(_Section):
    """Structured logging (section 70)."""

    level: str = "INFO"
    rotation_max_bytes: int = Field(default=10 * 1024 * 1024, ge=1024)
    rotation_backups: int = Field(default=5, ge=1, le=100)
    redact_on_root_logger: bool = True


class GeminiSettings(_Section):
    """Brain connection limits (sections 67, 68). Key lives in the keyring."""

    # Re-verified 2026-09-26 against the live API: gemini-2.5-flash returns 404
    # "no longer available to new users" and points at the 3.x family. The
    # default is the replacement the API itself names; any model the key can see
    # remains configurable here.
    model: str = "gemini-3.8-flash"
    max_model_turns: int = Field(default=40, ge=1)
    max_task_wall_clock_seconds: int = Field(default=300, ge=1)
    context_token_budget: int = Field(default=1200, ge=128)


class Settings(BaseModel):
    """The complete, validated BLAXCY configuration."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = SCHEMA_VERSION

    performance: PerformanceSettings = Field(default_factory=PerformanceSettings)
    capture: CaptureSettings = Field(default_factory=CaptureSettings)
    perception: PerceptionSettings = Field(default_factory=PerceptionSettings)
    accessibility: AccessibilitySettings = Field(default_factory=AccessibilitySettings)
    ocr: OcrSettings = Field(default_factory=OcrSettings)
    visual: VisualSettings = Field(default_factory=VisualSettings)
    resolver: ResolverSettings = Field(default_factory=ResolverSettings)
    resolver_cache: ResolverCacheSettings = Field(default_factory=ResolverCacheSettings)
    sequence: SequenceSettings = Field(default_factory=SequenceSettings)
    input: InputSettings = Field(default_factory=InputSettings)
    clipboard: ClipboardSettings = Field(default_factory=ClipboardSettings)
    lease: LeaseSettings = Field(default_factory=LeaseSettings)
    verification: VerificationSettings = Field(default_factory=VerificationSettings)
    recovery: RecoverySettings = Field(default_factory=RecoverySettings)
    safety: SafetySettings = Field(default_factory=SafetySettings)
    terminal: TerminalSettings = Field(default_factory=TerminalSettings)
    privacy: PrivacySettings = Field(default_factory=PrivacySettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    gemini: GeminiSettings = Field(default_factory=GeminiSettings)


# ---------------------------------------------------------------------------
# Security invariants (section 4 rule 25 / section 72).
# Each entry: (human path, predicate over Settings that must hold, explanation).
# ---------------------------------------------------------------------------
Invariant = tuple[str, Callable[[Settings], bool], str]


SECURITY_INVARIANTS: Final[tuple[Invariant, ...]] = (
    (
        "terminal.submit_requires_confirmation",
        lambda s: s.terminal.submit_requires_confirmation,
        "Terminal submission must never bypass confirmation policy (section 54).",
    ),
    (
        "terminal.destructive_always_protected",
        lambda s: s.terminal.destructive_always_protected,
        "Destructive terminal commands stay protected in every mode (section 54).",
    ),
    (
        "privacy.never_upload_credential_context",
        lambda s: s.privacy.never_upload_credential_context,
        "Protected/password context must never be uploaded to visual AI (section 42).",
    ),
    (
        "privacy.never_ocr_password_fields",
        lambda s: s.privacy.never_ocr_password_fields,
        "Password fields must never be OCR'd (section 55).",
    ),
    (
        "privacy.visual_upload_on_demand_only",
        lambda s: s.privacy.visual_upload_on_demand_only,
        "No continuous screen streaming; visual upload is on-demand only (section 42).",
    ),
    (
        "logging.redact_on_root_logger",
        lambda s: s.logging.redact_on_root_logger,
        "Log redaction must never be disabled: secrets must never reach a log (section 70).",
    ),
)


def invariant_violations(settings: Settings) -> list[str]:
    """Return human-readable descriptions of every violated invariant."""
    return [explanation for _path, holds, explanation in SECURITY_INVARIANTS if not holds(settings)]


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``override`` onto a copy of ``base``."""
    merged = dict(base)
    for key, value in override.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(existing, value)
        else:
            merged[key] = value
    return merged


def _read_toml(path: Path) -> dict[str, Any]:
    """Read a TOML file into a mapping, raising a structured error on failure."""
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise BlaxcyError(
            ErrorCode.INTERNAL_ERROR,
            f"could not read config file {path}: {exc}",
            details={"path": str(path)},
        ) from exc


def effective_settings_path(explicit: Path | None = None) -> Path:
    """Resolve which config file is authoritative for this run.

    An explicit path (e.g. ``--config``) wins; otherwise the user config is
    used when present, else the packaged defaults path is reported so callers
    can tell the user where a config *would* live.
    """
    if explicit is not None:
        return explicit
    if DEFAULT_CONFIG_PATH.exists():
        return DEFAULT_CONFIG_PATH
    return DEFAULT_SETTINGS_TOML


def load_settings(
    path: Path | None = None,
    *,
    validate_invariants: bool = True,
) -> Settings:
    """Load, migrate, validate and invariant-check the effective configuration.

    Args:
        path: Explicit config file. ``None`` uses the effective default path.
        validate_invariants: When True (always in production), a config that
            attempts to disable a security invariant raises rather than loads.

    Raises:
        BlaxcyError: On unreadable config, schema migration failure, validation
            failure, or violated security invariant.
    """
    # Imported lazily to avoid an import cycle at module load time.
    from config.migration import MigrationError, migrate

    effective = effective_settings_path(path)
    raw = _read_toml(effective)

    try:
        migrated = migrate(raw)
    except MigrationError as exc:
        raise BlaxcyError(
            ErrorCode.INTERNAL_ERROR,
            str(exc),
            details={"path": str(effective), "origin": "migration"},
        ) from exc

    merged = _deep_merge(_read_toml(DEFAULT_SETTINGS_TOML), migrated)

    try:
        settings = Settings.model_validate(merged)
    except ValidationError as exc:
        raise BlaxcyError(
            ErrorCode.INTERNAL_ERROR,
            f"invalid configuration in {effective}: {exc.error_count()} problem(s)",
            details={"path": str(effective), "errors": exc.errors(include_url=False)},
        ) from exc

    if validate_invariants:
        violations = invariant_violations(settings)
        if violations:
            raise BlaxcyError(
                ErrorCode.INTERNAL_ERROR,
                "refusing to load configuration that disables a security invariant",
                details={"path": str(effective), "violations": violations},
            )

    return settings


def save_settings(settings: Settings, path: Path | None = None) -> Path:
    """Write ``settings`` as TOML, returning the path written."""
    target = path if path is not None else DEFAULT_CONFIG_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = settings.model_dump(mode="json", exclude_none=True)
    target.write_text(tomlkit.dumps(payload), encoding="utf-8")
    return target
