"""BLAXCY configuration package.

Configuration is TOML with a ``schema_version``. Every operational limit is
configurable; security invariants are not (specification section 72).
"""

from config.settings import (
    SCHEMA_VERSION,
    Settings,
    effective_settings_path,
    load_settings,
    save_settings,
)

__all__ = [
    "SCHEMA_VERSION",
    "Settings",
    "effective_settings_path",
    "load_settings",
    "save_settings",
]
