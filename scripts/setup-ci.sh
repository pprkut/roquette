#!/bin/bash
# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

# Install what CI and cloud sessions need to run `just check`: roquette's
# dependencies and the development tools (pyproject.toml's `dev` group).
# Not for the development machine, where they are system packages.
#
#   scripts/setup-ci.sh [--oldest]
#
# --oldest installs the oldest supported versions (ci/oldest.txt).
#
# In CI, pip installs into the Python that actions/setup-python set up.
# In a cloud session (CLAUDE_CODE_REMOTE=true), Ubuntu's own Python
# doesn't take packages from pip (PEP 668), so they go into a venv in the
# session's container, which is put on the PATH of the session's commands
# through CLAUDE_ENV_FILE.

set -euo pipefail

cd "$(dirname "$0")/.."

constraints=()
if [ "${1:-}" = --oldest ]; then
    constraints=(--constraint ci/oldest.txt)
fi

# System libraries, added with the code that needs them:
# - PyGObject is built from source: GObject Introspection (1.0 for the
#   oldest supported PyGObject, 2.0 for newer ones) and cairo, for
#   pycairo.
# - GStreamer and its introspection data, with the base and good plugins.
# - What the slint wheel loads at import (libinput) and for X11 windows.
packages=(
    libcairo2-dev
    libgirepository-2.0-dev
    libgirepository1.0-dev
    pkg-config
    gir1.2-gst-plugins-base-1.0
    gir1.2-gstreamer-1.0
    gstreamer1.0-plugins-base
    gstreamer1.0-plugins-good
    libinput10
    libxkbcommon-x11-0
)
sudo=()
if [ "$(id -u)" != 0 ]; then
    sudo=(sudo)
fi
"${sudo[@]}" apt-get update --quiet --quiet
"${sudo[@]}" apt-get install --quiet --quiet --yes --no-install-recommends "${packages[@]}"

python=python3
if [ "${CLAUDE_CODE_REMOTE:-}" = true ]; then
    venv="$HOME/.venvs/roquette"
    if [ ! -x "$venv/bin/python" ]; then
        python3 -m venv "$venv"
    fi
    python="$venv/bin/python"
    if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
        echo "export VIRTUAL_ENV=\"$venv\" PATH=\"$venv/bin:\$PATH\"" >> "$CLAUDE_ENV_FILE"
    fi
fi

# Dependency groups need pip 25.1 or later.
"$python" -m pip install --quiet --upgrade pip
"$python" -m pip install --quiet --editable . --group dev "${constraints[@]}"
