#!/bin/bash
# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

# The SessionStart hook (.claude/settings.json): sets up a cloud session
# like CI (scripts/setup-ci.sh). Does nothing in local sessions, where
# the dependencies and tools are system packages.

set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != true ]; then
    exit 0
fi

cd "$CLAUDE_PROJECT_DIR"

# Skip the installation while nothing it depends on changed since the
# last successful one (the session resumed, or the container was cached).
venv="$HOME/.venvs/roquette"
hashes="$venv/.setup.sha256"
if sha256sum --check --status "$hashes" 2>/dev/null; then
    echo "export VIRTUAL_ENV=\"$venv\" PATH=\"$venv/bin:\$PATH\"" >> "${CLAUDE_ENV_FILE:-/dev/null}"
    exit 0
fi

rm -f "$hashes"
scripts/setup-ci.sh
sha256sum pyproject.toml ci/oldest.txt scripts/setup-ci.sh > "$hashes"
