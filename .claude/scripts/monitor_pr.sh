#!/usr/bin/env bash
# Wait for all CI checks on a PR, then optionally merge.
# Only prints failures or the final pass line — no per-refresh noise.
# Requires: gh, a Python 3 (resolved via ../hooks/_py.sh; stdlib only — no jq).
#
# Usage:
#   .claude/scripts/monitor_pr.sh <pr-number> [--merge] [--review-cycle N] [--no-review]
#
# --merge: squash-merge and delete branch automatically when all checks pass.
# --review-cycle N: current review fix cycle count (0-based, default 0).
#   When N >= MAX_REVIEW_CYCLES and review/decision is still failing or pending,
#   force-merges with --admin.
# --no-review: skip all review waiting and admin-merge after CI passes.
#   Use ONLY for solo-dev PRs where no human reviewer will ever respond.
#   Do NOT use to avoid addressing legitimate review feedback.
#
# Full lifecycle for agents:
#   1. .claude/scripts/monitor_pr.sh <n> --merge
#   2. Exit 2 -> review comments printed -> read and address them, push
#   3. Re-run with --review-cycle 1
#   4. Exit 2 again -> address remaining comments, push
#   5. Re-run with --review-cycle 2 -> admin-merge fires if still blocked
#
# Review cycle policy:
#   Two fix cycles are required before admin-merge is allowed. This ensures
#   review feedback (including Copilot comments) is read and addressed at
#   least once before force-merging.

set -euo pipefail

# This script hard-requires the GitHub CLI (PI-362).
command -v gh >/dev/null 2>&1 || { echo "error: GitHub CLI (gh) not found — install: https://cli.github.com" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
# shellcheck source=/dev/null
. "$SCRIPT_DIR/gh_host.sh"

# Resolve the Python interpreter through the canonical helper (PI-361).
PY="$SCRIPT_DIR/../hooks/_py.sh"

PR_NUMBER="${1:-}"
MODE="${2:-}"
REVIEW_CYCLE=0
MAX_REVIEW_CYCLES=2
NO_REVIEW=0

if [ -z "$PR_NUMBER" ]; then
  echo "Usage: monitor_pr.sh <pr-number> [--merge] [--review-cycle N] [--no-review]" >&2
  exit 1
fi

if [ -n "$MODE" ] && [ "$MODE" != "--merge" ]; then
  echo "Unknown option: $MODE" >&2
  exit 2
fi

# Parse remaining flags (order-independent after position 2).
# Shift past <pr-number> and optional --merge; remaining args are flags.
shift 1  # drop PR_NUMBER
[ "$MODE" = "--merge" ] && shift 1  # drop --merge if present
while [ $# -gt 0 ]; do
  case "$1" in
    --review-cycle)   REVIEW_CYCLE="${2:-0}"; shift 2 ;;
    --review-cycle=*) REVIEW_CYCLE="${1#*=}"; shift ;;
    --no-review)      NO_REVIEW=1; shift ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

_count_pending() {
  echo "$1" | "$PY" -c "
import json, sys
data = json.load(sys.stdin)
# Exclude review/decision; it is a derived commit status that only appears
# after a review event. We detect review state directly via reviewDecision.
# Gate on gh's authoritative 'bucket' rollup (pass/fail/pending/skipping/
# cancel), not a hand-rolled state allowlist: just-queued Actions report
# state=QUEUED (and WAITING/REQUESTED for env-gated jobs), all bucket=pending.
# An allowlist that omitted those let the CI-wait break before CI ran (#428).
print(sum(1 for c in data if c.get('name') != 'review/decision' and c.get('bucket') == 'pending'))
"
}

_print_failures() {
  echo "$1" | "$PY" -c "
import json, sys
data = json.load(sys.stdin)
bad = [
    c for c in data
    if c.get('name') != 'review/decision'
    and (
        c.get('bucket') in ('fail', 'cancel')
        or c.get('state') in ('FAILURE', 'CANCELLED', 'TIMED_OUT', 'ERROR')
    )
]
for c in bad:
    print(f\"  {c['name']}: {c.get('state') or c.get('bucket')}\")
sys.exit(len(bad))
"
}

# Print review feedback — inline comments first, falls back to full PR comments view.
_print_review_comments() {
  local inline
  inline=$(
    gh api "repos/{owner}/{repo}/pulls/$PR_NUMBER/comments" \
      --jq '.[] | "  \(.path):\(.line // "?") [\(.user.login)]\n  \(.body)\n"' \
      2>/dev/null || true
  )
  if [ -n "$inline" ]; then
    printf '%s\n' "$inline"
  else
    gh pr view "$PR_NUMBER" --comments 2>/dev/null || true
  fi
  echo "  Full PR: $(gh pr view "$PR_NUMBER" --json url -q '.url' 2>/dev/null || true)"
}

_run_gh() {
  local output
  local status

  set +e
  output=$(GH_PROMPT_DISABLED=1 gh "$@" 2>&1)
  status=$?
  set -e

  if [ -n "$output" ]; then
    printf '%s\n' "$output" | grep -v "^$" || true
  fi

  return "$status"
}

_admin_merge() {
  # Hard enforcement must bind under the org profile (ADR-013/#251): admin-merge
  # bypasses the server-side rules, so it is refused — use auto-merge / the merge
  # queue under the required checks instead. gh_profile comes from gh_host.sh.
  if [ "$(gh_profile)" = "org" ]; then
    echo "ERROR: admin-merge is refused under the org profile (#251) — hard" >&2
    echo "  enforcement must bind. Use auto-merge or the merge queue under the" >&2
    echo "  required checks, or resolve the blocking review/checks." >&2
    return 1
  fi
  if _run_gh pr merge "$PR_NUMBER" --squash --delete-branch --admin; then
    echo "Merged PR #$PR_NUMBER (admin)"
  else
    echo "ERROR: admin merge failed for PR #$PR_NUMBER" >&2
    return 1
  fi
}

# Query the PR's aggregate review decision directly — source of truth regardless
# of whether the review/decision commit status has been posted yet.
# Returns: APPROVED | CHANGES_REQUESTED | REVIEW_REQUIRED | (empty = no review policy)
# Returns UNKNOWN on API failure — callers must treat this as fail-closed.
_get_review_decision() {
  gh pr view "$PR_NUMBER" --json reviewDecision -q '.reviewDecision // ""' 2>/dev/null || echo "UNKNOWN"
}

# Check if any review activity exists (COMMENTED, APPROVED, or CHANGES_REQUESTED).
# Bot reviewers like Codex post COMMENTED reviews that don't change reviewDecision,
# so we use this as an early exit signal from the wait loop.
_has_review_activity() {
  local count
  count=$(gh pr view "$PR_NUMBER" --json reviews -q '.reviews | length' 2>/dev/null) || count=0
  [ "$count" -gt 0 ]
}

# --- Wait for all CI checks (excludes review/decision commit status) ---
# Guard: if checks haven't registered yet (empty list), keep polling.
# An empty list is indistinguishable from "all done" without this guard,
# which caused premature merges before CI even started.
# Bounded wait (PI-186): a required check that never leaves PENDING/EXPECTED
# (e.g. a workflow that never triggers on this branch) must not hang the
# script — and any autonomous caller — forever. Fail closed on timeout.
CI_TIMEOUT=900
CI_ELAPSED=0
while true; do
  CHECKS=$(gh pr checks "$PR_NUMBER" --json name,state,bucket 2>/dev/null) || CHECKS="[]"
  CHECK_COUNT=$(echo "$CHECKS" | "$PY" -c "import json,sys; print(len(json.load(sys.stdin)))")
  if [ "$CHECK_COUNT" -gt 0 ] && [ "$(_count_pending "$CHECKS")" -eq 0 ]; then
    break
  fi
  if [ "$CI_ELAPSED" -ge "$CI_TIMEOUT" ]; then
    echo "PR #$PR_NUMBER: CI did not settle within ${CI_TIMEOUT}s. Still pending:"
    echo "$CHECKS" | "$PY" -c "import json,sys
for c in json.load(sys.stdin):
    if c.get('name') != 'review/decision' and c.get('bucket') == 'pending':
        print('  -', c.get('name'))" 2>/dev/null || true
    echo "Re-run once the check registers, or investigate why it never started."
    exit 1
  fi
  sleep 10
  CI_ELAPSED=$((CI_ELAPSED + 10))
done

FAIL_CODE=0
_print_failures "$CHECKS" || FAIL_CODE=$?

if [ "$FAIL_CODE" -gt 0 ]; then
  echo "CI failed on PR #$PR_NUMBER — fix the issues, push, then re-run this script."
  exit 1
fi

# --no-review: explicit bypass — skip review gate entirely after CI passes.
# Use only for solo-dev PRs where no reviewer will ever respond.
if [ "$NO_REVIEW" -eq 1 ] && [ "$MODE" = "--merge" ]; then
  echo "PR #$PR_NUMBER: CI passed. --no-review specified — skipping review gate."
  _admin_merge; exit 0
fi

# --- Wait up to 6 min for a reviewer to act ---
# Query reviewDecision directly so this works before review-status.yml creates
# the derived review/decision commit status.
REVIEW_TIMEOUT=360
REVIEW_ELAPSED=0
REVIEW_DECISION=$(_get_review_decision)
if [ "$MODE" = "--merge" ] && [ "$REVIEW_CYCLE" -ge "$MAX_REVIEW_CYCLES" ] && [ "$REVIEW_DECISION" = "REVIEW_REQUIRED" ]; then
  echo "Max review cycles ($MAX_REVIEW_CYCLES) reached — skipping reviewer wait and force-merging with admin override."
  _admin_merge; exit 0
fi

if [ "$REVIEW_DECISION" = "REVIEW_REQUIRED" ] || [ "$REVIEW_DECISION" = "UNKNOWN" ]; then
  echo "Waiting for reviewer (up to ${REVIEW_TIMEOUT}s, polling every 30s) — reviewDecision: ${REVIEW_DECISION}"
fi
while { [ "$REVIEW_DECISION" = "REVIEW_REQUIRED" ] || [ "$REVIEW_DECISION" = "UNKNOWN" ]; } && [ "$REVIEW_ELAPSED" -lt "$REVIEW_TIMEOUT" ]; do
  sleep 30
  REVIEW_ELAPSED=$((REVIEW_ELAPSED + 30))
  REVIEW_DECISION=$(_get_review_decision)
  echo "  [${REVIEW_ELAPSED}s/${REVIEW_TIMEOUT}s] reviewDecision: ${REVIEW_DECISION:-none}"
  # Early exit: if any review activity exists (even COMMENTED), stop waiting.
  # Bot reviewers like Codex post comments without changing reviewDecision.
  if [ "$REVIEW_DECISION" = "REVIEW_REQUIRED" ] && _has_review_activity; then
    echo "  Review comments detected — proceeding without waiting for formal approval."
    break
  fi
  CHECKS=$(gh pr checks "$PR_NUMBER" --json name,state,bucket 2>/dev/null) || CHECKS="[]"
done

if [ "$REVIEW_DECISION" = "UNKNOWN" ]; then
  echo "ERROR: could not fetch reviewDecision for PR #$PR_NUMBER — cannot verify review state." >&2
  exit 2
fi

if [ "$REVIEW_DECISION" = "CHANGES_REQUESTED" ]; then
  echo "Review/decision failed on PR #$PR_NUMBER (cycle $REVIEW_CYCLE/$MAX_REVIEW_CYCLES):"
  _print_review_comments

  if [ "$MODE" = "--merge" ]; then
    if [ "$REVIEW_CYCLE" -ge "$MAX_REVIEW_CYCLES" ]; then
      echo "Max review cycles ($MAX_REVIEW_CYCLES) reached — force-merging with admin override."
      _admin_merge; exit 0
    else
      NEXT=$((REVIEW_CYCLE + 1))
      echo "Address the comments above, push your changes, then re-run:"
      echo "  .claude/scripts/monitor_pr.sh $PR_NUMBER --merge --review-cycle $NEXT"
      exit 2
    fi
  fi
  exit 1
fi

if [ "$REVIEW_DECISION" = "REVIEW_REQUIRED" ]; then
  echo "PR #$PR_NUMBER: review/decision still pending after ${REVIEW_TIMEOUT}s — no reviewer has acted."
  echo "  Full PR: $(gh pr view "$PR_NUMBER" --json url -q '.url' 2>/dev/null || true)"

  if [ "$MODE" = "--merge" ]; then
    if [ "$REVIEW_CYCLE" -ge "$MAX_REVIEW_CYCLES" ]; then
      echo "Max review cycles ($MAX_REVIEW_CYCLES) reached — force-merging with admin override."
      _admin_merge; exit 0
    else
      NEXT=$((REVIEW_CYCLE + 1))
      echo "Request a review or wait for a reviewer, then re-run:"
      echo "  .claude/scripts/monitor_pr.sh $PR_NUMBER --merge --review-cycle $NEXT"
      exit 2
    fi
  fi
  exit 1
fi

PR_URL=$(gh pr view "$PR_NUMBER" --json url -q '.url')
echo "PR #$PR_NUMBER passed: $PR_URL"

if [ "$MODE" = "--merge" ]; then
  MERGE_STATE=$(gh pr view "$PR_NUMBER" --json mergeStateStatus -q '.mergeStateStatus' 2>/dev/null || echo "UNKNOWN")

  if [ "$MERGE_STATE" = "CLEAN" ] || [ "$MERGE_STATE" = "UNSTABLE" ]; then
    if _run_gh pr merge "$PR_NUMBER" --squash --delete-branch; then
      echo "Merged PR #$PR_NUMBER"
    else
      echo "ERROR: merge failed for PR #$PR_NUMBER" >&2
      exit 1
    fi
  elif [ "$MERGE_STATE" = "BLOCKED" ]; then
    echo "PR is blocked by review protection — merging with admin override."
    _admin_merge
  else
    if ! _run_gh pr merge "$PR_NUMBER" --squash --delete-branch; then
      if ! _run_gh pr merge "$PR_NUMBER" --squash --delete-branch --auto; then
        echo "ERROR: could not merge or enable auto-merge for PR #$PR_NUMBER" >&2
        exit 1
      fi
      echo "Auto-merge enabled for PR #$PR_NUMBER — will merge once all requirements are met."
    else
      echo "Merged PR #$PR_NUMBER"
    fi
  fi
fi
