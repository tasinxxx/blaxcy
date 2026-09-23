"""Configuration schema migration (specification section 72).

The migration path is deliberately small and explicit. It never guesses: an
unknown *newer* schema is refused rather than silently coerced, and a migration
that does not exist is reported as an error, not skipped.
"""

from __future__ import annotations

from typing import Any

#: The configuration schema version this build understands.
CURRENT_SCHEMA_VERSION = 1


class MigrationError(RuntimeError):
    """Raised when configuration cannot be migrated to the current schema."""


def migrate(raw: dict[str, Any], *, current: int = CURRENT_SCHEMA_VERSION) -> dict[str, Any]:
    """Return ``raw`` migrated up to the current schema version.

    Args:
        raw: Parsed TOML configuration mapping.
        current: Target schema version (defaults to the build's version).

    Returns:
        A new mapping at ``current`` schema version. The input is not mutated.

    Raises:
        MigrationError: If the file is newer than this build, or if no
            migration step exists for a gap in versions.
    """
    data: dict[str, Any] = dict(raw)
    version = data.get("schema_version")

    if version is None:
        # A missing version is treated as the first schema that had this field;
        # it is recorded explicitly rather than left absent.
        version = 1
        data["schema_version"] = version

    if not isinstance(version, int) or isinstance(version, bool):
        raise MigrationError(f"schema_version must be an integer, got {version!r}")

    if version > current:
        raise MigrationError(
            f"config schema_version {version} is newer than this build's {current}; "
            "refusing to coerce it. Update BLAXCY or restore an older config."
        )

    while version < current:
        step = _MIGRATIONS.get(version)
        if step is None:
            raise MigrationError(
                f"no migration registered from schema_version {version} to {version + 1}"
            )
        data = step(data)
        version += 1
        data["schema_version"] = version

    return data


#: Migration steps keyed by the *source* version they upgrade from.
#: Version 1 is the first schema, so there is legitimately nothing to migrate
#: yet -- this registry exists so future versions have a defined home.
_MIGRATIONS: dict[int, Any] = {}
