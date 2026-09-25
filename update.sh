#!/bin/sh
# BLAXCY updater (specification section 73).
#
# Re-installs over an existing installation and keeps the previous application tree
# until the new one has passed runtime validation. If any step fails, the rollback
# restores the previous tree and the previous desktop entry, so an update is
# all-or-nothing rather than a half-new install.
#
# A prefix with no manifest is refused: updating something that was never installed
# would mean guessing what to replace.
#
#   ./update.sh
#   ./update.sh --dry-run
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PY=${BLAXCY_INSTALLER_PYTHON:-python3}

if ! command -v "$PY" >/dev/null 2>&1; then
    printf '%s\n' "update.sh: $PY not found (BLAXCY needs Python 3.12+)" >&2
    exit 2
fi

PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONPATH

exec "$PY" -m installer update "$@"
