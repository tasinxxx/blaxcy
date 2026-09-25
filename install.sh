#!/bin/sh
# BLAXCY installer (specification section 73).
#
# A thin wrapper on purpose: the whole process -- preflight, package-manager
# detection, venv, dependencies, application files, icon, desktop entry, manifest
# with checksums, runtime validation, rollback -- lives in installer/installer.py
# so that it can be typed, unit-tested, and exercised end to end by the test suite
# against a throwaway prefix. This script only finds the checkout, picks an
# interpreter, and forwards every argument.
#
#   ./install.sh                              # ~/.local, no root, system packages untouched
#   ./install.sh --dry-run                    # print the plan, change nothing
#   ./install.sh --system-deps                # also install section 27's system packages (sudo)
#   ./install.sh --prefix /tmp/blaxcy-test --python "$PWD/.venv/bin/python"
#
# The default prefix is the user-level XDG prefix, which is what makes this a menu
# application without root: the desktop entry lands in ~/.local/share/applications,
# which the desktop environment already reads.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PY=${BLAXCY_INSTALLER_PYTHON:-python3}

if ! command -v "$PY" >/dev/null 2>&1; then
    printf '%s\n' "install.sh: $PY not found (BLAXCY needs Python 3.12+)" >&2
    exit 2
fi

PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONPATH

exec "$PY" -m installer install "$@"
