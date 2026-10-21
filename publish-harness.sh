#!/usr/bin/env bash
set -euo pipefail

REMOTE_URL="git@github.com:DavisStanko/battlesnake-ab-harness.git"
REMOTE_BRANCH="main"

# Ensure working tree is clean before publishing
if ! git diff-index --quiet HEAD --; then
    echo "Error: Working tree has uncommitted changes. Commit or stash before publishing." >&2
    exit 1
fi

CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)

echo "--> Splitting harness subtree..."
SPLIT_COMMIT=$(git subtree split --prefix=harness)

echo "--> Fetching remote $REMOTE_URL ($REMOTE_BRANCH)..."
TMP_REMOTE="remote_harness_tmp_$$"
git fetch "$REMOTE_URL" "$REMOTE_BRANCH":"$TMP_REMOTE"

SPLIT_TREE=$(git rev-parse "$SPLIT_COMMIT^{tree}")
REMOTE_TREE=$(git rev-parse "$TMP_REMOTE^{tree}")

if [ "$SPLIT_TREE" = "$REMOTE_TREE" ]; then
    echo "--> Remote tree is already completely up to date with harness/ ($SPLIT_TREE)."
    git branch -D "$TMP_REMOTE" > /dev/null 2>&1 || true
    echo "Done!"
    exit 0
fi

if git merge-base --is-ancestor "$TMP_REMOTE" "$SPLIT_COMMIT"; then
    echo "--> Fast-forward pushing split commit $SPLIT_COMMIT..."
    git push "$REMOTE_URL" "$SPLIT_COMMIT:$REMOTE_BRANCH"
else
    echo "--> Aligning subtree history with remote..."
    TMP_BRANCH="harness_publish_tmp_$$"
    git checkout -b "$TMP_BRANCH" "$SPLIT_COMMIT"
    git merge -s ours "$TMP_REMOTE" --allow-unrelated-histories -m "chore: align harness subtree publish"
    git push "$REMOTE_URL" "$TMP_BRANCH:$REMOTE_BRANCH"
    git checkout "$CURRENT_BRANCH"
    git branch -D "$TMP_BRANCH" > /dev/null 2>&1 || true
fi

git branch -D "$TMP_REMOTE" > /dev/null 2>&1 || true
echo "--> Successfully published harness to $REMOTE_URL ($REMOTE_BRANCH)."
