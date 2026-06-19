#!/usr/bin/env bash
#
# dev_setup.sh — one-command local dev environment setup for wherefore.
#
# Usage:
#   ./dev_setup.sh
#
# What it does:
#   1. Checks for a usable Python 3.10+ interpreter
#   2. Creates a virtual environment in .venv/ (skips if one exists)
#   3. Installs the package in editable mode with dev dependencies
#   4. Runs the test suite to confirm everything works
#
# Safe to re-run — it won't recreate an existing .venv, and pip install
# is idempotent.

set -euo pipefail

REQUIRED_MAJOR=3
REQUIRED_MINOR=10

echo "==> Checking for Python..."

if command -v python3 &>/dev/null; then
    PYTHON_BIN="python3"
else
    echo "ERROR: python3 not found on PATH. Install Python ${REQUIRED_MAJOR}.${REQUIRED_MINOR}+ and re-run." >&2
    exit 1
fi

PY_VERSION=$("$PYTHON_BIN" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)

if [ "$PY_MAJOR" -lt "$REQUIRED_MAJOR" ] || { [ "$PY_MAJOR" -eq "$REQUIRED_MAJOR" ] && [ "$PY_MINOR" -lt "$REQUIRED_MINOR" ]; }; then
    echo "ERROR: Found Python ${PY_VERSION}, but wherefore requires ${REQUIRED_MAJOR}.${REQUIRED_MINOR}+." >&2
    exit 1
fi

echo "    Found Python ${PY_VERSION} (${PYTHON_BIN})"

if [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -ge 14 ]; then
    echo "    NOTE: You're on Python ${PY_VERSION}, newer than this project has been"
    echo "    tested against. pandas/numpy support 3.14+, but if 'pip install' fails"
    echo "    below, a transitive dependency lagging on 3.14 wheels is the likely"
    echo "    cause — see README troubleshooting notes."
fi

echo "==> Setting up virtual environment..."

if [ -d ".venv" ]; then
    echo "    .venv already exists, skipping creation."
else
    "$PYTHON_BIN" -m venv .venv
    echo "    Created .venv"
fi

# shellcheck disable=SC1091
source .venv/bin/activate

echo "==> Upgrading pip..."
pip install -q --upgrade pip

echo "==> Installing wherefore in editable mode with dev dependencies..."
pip install -e ".[dev]"

echo "==> Running test suite..."
pytest tests/ -v

echo ""
echo "==> Done. Virtual environment is active in this shell."
echo "    Next time, just run: source .venv/bin/activate"
