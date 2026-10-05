#!/usr/bin/env bash
set -euo pipefail

# Ensure working tree is clean before publishing
if ! git diff-index --quiet HEAD --; then
    echo "Error: Working tree has uncommitted changes. Commit or stash before publishing." >&2
    exit 1
fi

git subtree push --prefix=harness git@github.com:DavisStanko/battlesnake-ab-harness.git main
