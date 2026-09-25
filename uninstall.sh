#!/bin/sh
# BLAXCY uninstaller (specification section 73).
#
# Removes exactly what the install manifest lists -- and refuses to act on any path
# outside the prefix that manifest records -- then deletes the manifest. It restores
# nothing else and touches no other application's files: an uninstaller that swept
# the share directory would be a deletion tool.
#
#   ./uninstall.sh                 # remove the ~/.local install
#   ./uninstall.sh --prefix /tmp/blaxcy-test
#   ./uninstall.sh --dry-run       # report what would be removed
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PY=${BLAXCY_INSTALLER_PYTHON:-python3}

if ! command -v "$PY" >/dev/null 2>&1; then
    printf '%s\n' "uninstall.sh: $PY not found (BLAXCY needs Python 3.12+)" >&2
    exit 2
fi

PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONPATH

exec "$PY" -m installer uninstall "$@"
