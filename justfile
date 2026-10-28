# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: CC0-1.0

# List the recipes
default:
    @just --list

# Run roquette from the checkout, on the development copy of the beets data
run *args:
    BEETSDIR=dev/beets python3 -m roquette {{args}}

# Copy the beets config and library into dev/beets (read-only on the live data); --force replaces the copy
dev-library *args:
    python3 -I scripts/dev-library.py {{args}}

# Make a fake beets library in build/fake-library, for running roquette without the dev library (BEETSDIR=build/fake-library/beets)
fake-library *args:
    python3 -I scripts/fake-library.py {{args}}

# Measure startup, memory and CPU of the window (needs a graphical session); variants: femtovg software skia skia-software
measure *variants:
    scripts/measure.sh {{variants}}

# Install what CI and cloud sessions need (system libraries with apt, Python packages with pip); not for the development machine
setup-ci:
    scripts/setup-ci.sh

# Run the checks CI runs, except the ones that need a display; tools that aren't installed are skipped outside CI
check: reuse lint format-check typecheck test
    @{{just_executable()}} _optional typos typos
    @{{just_executable()}} _optional editorconfig-checker editorconfig
    @{{just_executable()}} _optional shellcheck shellcheck
    @{{just_executable()}} _optional zizmor zizmor
    @{{just_executable()}} _optional deptry deptry
    @{{just_executable()}} _optional twine package
    @{{just_executable()}} _ci-only licenses
    @{{just_executable()}} _ci-only advisories

# Run a recipe if its tool is installed; in CI (CI=true) a missing tool fails
[private]
_optional tool recipe:
    #!/usr/bin/env bash
    set -euo pipefail
    if command -v {{tool}} >/dev/null; then
        exec {{just_executable()}} {{recipe}}
    elif [ "${CI:-}" = true ]; then
        echo "{{tool}} isn't installed" >&2
        exit 1
    else
        echo "{{tool}} isn't installed; skipped {{recipe}} (CI runs it)"
    fi

# Run a recipe that checks the installed Python packages only in CI and cloud sessions, which install exactly roquette's
[private]
_ci-only recipe:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ "${CI:-}" = true ] || [ "${CLAUDE_CODE_REMOTE:-}" = true ]; then
        exec {{just_executable()}} {{recipe}}
    else
        echo "skipped {{recipe}}: it checks the installed packages, CI's are roquette's alone"
    fi

# Check the REUSE compliance (licences and copyright of every file)
reuse:
    reuse lint

# Look for misspelt words in the code and the docs (typos-cli; typos.toml)
typos:
    typos

# Check the files against .editorconfig: line ends, final newlines, trailing whitespace, tabs (editorconfig-checker)
editorconfig:
    git ls-files --exclude-standard -z | xargs -0 "$(command -v editorconfig-checker || echo ec)"

# Check the shell scripts (shellcheck)
shellcheck:
    shellcheck scripts/*.sh

# Check the GitHub workflows and actions (zizmor)
zizmor:
    zizmor --offline .github

# Lint the Python code (ruff)
lint:
    ruff check .

# Check the formatting of the Python code (ruff)
format-check:
    ruff format --check .

# Format the Python code (ruff)
format:
    ruff format .

# Check the types (mypy, strict)
typecheck:
    mypy

# Run the tests
test *args:
    python3 -m pytest {{args}}

# Run the tests with coverage, as CI does: JUnit results in build/junit.xml, coverage in build/coverage.xml
test-coverage *args:
    python3 -m pytest --cov --cov-report=term --cov-report=xml:build/coverage.xml --junitxml=build/junit.xml {{args}}

# Look for unused, missing and transitive-only dependencies (deptry)
deptry:
    deptry .

# Build the sdist and the wheel into build/dist and check them (build, twine, validate-pyproject)
package:
    validate-pyproject pyproject.toml
    rm -rf build/dist
    python3 -m build --outdir build/dist .
    twine check --strict build/dist/*

# Check the licences of the installed packages against the allowed ones (pip-licenses; see "Licensing" in docs/PLAN.md); Slint's licences are decided there
licenses:
    pip-licenses --from=mixed --partial-match --ignore-packages roquette slint --allow-only="MIT;BSD;Apache;ISC;Mozilla Public License 2.0;MPL-2.0;GNU Lesser General Public License;LGPL;Python Software Foundation;PSF-2.0;Zlib;Unlicense;CC0;GPL-2.0-or-later;GPL-3.0-or-later;GNU General Public License v2 or later (GPLv2+);GNU General Public License v3 or later (GPLv3+)"

# Check the installed packages for security advisories (pip-audit, OSV and PyPI)
advisories:
    pip-audit --skip-editable
