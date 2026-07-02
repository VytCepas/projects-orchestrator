#!/usr/bin/env bash
# Start work on a GitHub issue: create branch, push, and open a draft PR.
#
# Usage:
#   .claude/scripts/start_issue.sh <issue-number> <type>
#
# Types: feat  fix  chore  docs  test
#
# Composable with create_issue.sh:
#   .claude/scripts/create_issue.sh feat "Add OAuth login" | xargs -I{} .claude/scripts/start_issue.sh {} feat

set -euo pipefail

# This script hard-requires the GitHub CLI (PI-362).
command -v gh >/dev/null 2>&1 || {
  echo "error: GitHub CLI (gh) not found — install: https://cli.github.com" >&2
  exit 1
}

# Resolve the base branch (ADR-014) from the promotion chain via gh_host.sh.
source "$(dirname "$0")/gh_host.sh"

VALID_TYPES="feat fix chore docs test"

usage() {
  echo "Usage: start_issue.sh <issue-number> <type>"
  echo ""
  echo "Types: $VALID_TYPES"
  echo ""
  echo "Examples:"
  echo "  start_issue.sh 42 feat"
  echo "  start_issue.sh 99 fix"
  exit 1
}

# --- Validate args ---
if [ $# -lt 2 ]; then
  usage
fi

ISSUE_NUMBER="$1"
TYPE="$2"

if ! echo "$VALID_TYPES" | grep -qw "$TYPE"; then
  echo "ERROR: invalid type '$TYPE'. Valid types: $VALID_TYPES" >&2
  exit 1
fi

if ! [[ "$ISSUE_NUMBER" =~ ^[0-9]+$ ]]; then
  echo "ERROR: issue number must be numeric, got '$ISSUE_NUMBER'" >&2
  exit 1
fi

# --- Resolve project key / abbreviation ---
# Set PROJECT_KEY env var, add `project_key: PI` to .claude/config.yaml,
# or let the script derive one from the repository directory name.
derive_project_key() {
  if [ -n "${PROJECT_KEY:-}" ]; then
    echo "$PROJECT_KEY"
    return
  fi

  local configured=""
  configured=$(grep '^[[:space:]]*project_key:' .claude/config.yaml 2>/dev/null |
    head -n 1 |
    cut -d: -f2- |
    sed 's/#.*$//' |
    tr -d '[:space:]"' |
    tr -d "'" || true)
  if [ -n "$configured" ]; then
    echo "$configured"
    return
  fi

  local repo_name=""
  repo_name=$(basename "$(git rev-parse --show-toplevel 2>/dev/null || pwd)")
  echo "$repo_name" |
    tr '[:lower:]' '[:upper:]' |
    tr -cs 'A-Z0-9' '\n' |
    awk 'NF { printf substr($0, 1, 1) }' |
    cut -c1-10
}

PROJECT_KEY=$(derive_project_key)
PROJECT_KEY=$(echo "$PROJECT_KEY" | tr '[:lower:]' '[:upper:]' | tr -cd 'A-Z0-9')
# A single-word repo name yields a 1-char initials key (e.g. "widget" -> "W").
# That passes the branch-name regex but the commit-msg hook then rejects every
# commit — its scope regex requires >=2 chars: [A-Z][A-Z0-9]{1,9}- (#432).
# Widen a too-short key to the repo name's leading alphanumerics first.
if [ "${#PROJECT_KEY}" -lt 2 ]; then
  PROJECT_KEY=$(basename "$(git rev-parse --show-toplevel 2>/dev/null || pwd)" |
    tr '[:lower:]' '[:upper:]' | tr -cd 'A-Z0-9' | cut -c1-4)
fi
# Final guard: the key must satisfy the shared key regex (>=2 chars, leading
# letter). Empty, still-too-short, or digit-leading -> stable PROJ fallback.
if ! echo "$PROJECT_KEY" | grep -qE '^[A-Z][A-Z0-9]{1,9}$'; then
  PROJECT_KEY="PROJ"
fi

# --- Fetch issue title ---
ISSUE_TITLE=$(gh issue view "$ISSUE_NUMBER" --json title -q '.title' 2>/dev/null)
if [ -z "$ISSUE_TITLE" ]; then
  echo "ERROR: issue #$ISSUE_NUMBER not found" >&2
  exit 1
fi

ISSUE_REF="${PROJECT_KEY}-${ISSUE_NUMBER}"

# --- Derive branch name: <issue_type>/<project_abbr>-<issue_number>-<kebab-slug>, max 80 chars total ---
# Matches convention: feat/PI-42-add-oauth-login, fix/API-99-null-pointer
# Strip leading [type] prefix from issue title if present (e.g. "[feat] Add OAuth" -> "Add OAuth")
CLEAN_TITLE=$(echo "$ISSUE_TITLE" | sed 's/^\[[^]]*\] *//')
SLUG=$(echo "$CLEAN_TITLE" |
  tr '[:upper:]' '[:lower:]' |
  tr -cs 'a-z0-9' '-' |
  sed 's/^-//;s/-$//')
PREFIX="${ISSUE_REF}-"
MAX_SLUG=$((80 - ${#TYPE} - 1 - ${#PREFIX})) # -1 for the /
if [ "$MAX_SLUG" -lt 12 ]; then
  MAX_SLUG=12
fi
SLUG="${SLUG:0:$MAX_SLUG}"
SLUG="${SLUG%-}" # trim trailing dash if truncated mid-word
# A title with no alphanumerics (e.g. "!!!") collapses to an empty slug, so the
# branch would be `feat/PI-42-` — pushed and PR'd before validate-pr.yml rejects
# it for a slug that doesn't start with [a-z0-9]. Fall back to a stable slug so
# no malformed branch is created (PI-206).
[ -z "$SLUG" ] && SLUG="issue"
BRANCH="${TYPE}/${PREFIX}${SLUG}"

echo "Branch: $BRANCH"

# --- Guard: already on this branch or it already exists ---
CURRENT=$(git branch --show-current)
if [ "$CURRENT" = "$BRANCH" ]; then
  echo "Already on branch $BRANCH"
elif git show-ref --verify --quiet "refs/heads/$BRANCH"; then
  echo "Branch $BRANCH already exists - switching"
  git checkout "$BRANCH"
else
  git checkout -b "$BRANCH"
fi

# --- Seed an empty commit so the draft PR has a diff base ---
# GitHub refuses a PR with no commits between base and head ("No commits between
# main and <branch>"), so a freshly-created branch cannot open the draft PR this
# script promises until it has >=1 commit. Seed one when the branch is level
# with the base; real work simply adds commits on top (#433).
#
# Build the seed from HEAD's own tree with commit-tree so it is empty by
# construction and cannot capture the user's staged index — a plain
# `git commit --allow-empty` still commits whatever is currently staged, which
# would silently fold unrelated work into the generated seed commit (#446).
BASE_BRANCH=$(base_branch)
if [ -z "$(git rev-list "${BASE_BRANCH}..HEAD" 2>/dev/null || true)" ]; then
  SEED_COMMIT=$(git commit-tree "HEAD^{tree}" -p HEAD \
    -m "chore(${ISSUE_REF}): start #${ISSUE_NUMBER} — ${CLEAN_TITLE}")
  git reset --soft "$SEED_COMMIT"
fi

# --- Push and set upstream (retry + remote-SHA verification) ---
.claude/scripts/push_branch.sh "$BRANCH"

# --- Open draft PR ---
# Conventional Commits with issue key as scope (ADR-006)
PR_TITLE="${TYPE}(${ISSUE_REF}): ${CLEAN_TITLE}"
PR_BODY="Closes #${ISSUE_NUMBER}"

PR_URL=$(gh pr create \
  --draft \
  --base "$BASE_BRANCH" \
  --title "$PR_TITLE" \
  --body "$PR_BODY")

echo "Draft PR: $PR_URL"
