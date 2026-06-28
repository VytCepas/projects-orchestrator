#!/usr/bin/env bash
# Configure or check GitHub repository governance for the scaffolded workflow.
#
# Usage: setup_github.sh [branch] [--protect]
#   --protect  apply baseline branch protection to the default branch
#              (require CI green, require PR review, block force-push).
#              Idempotent: the PUT endpoint replaces the existing config.
#
# Requires: gh and admin permission on the repository.

set -euo pipefail

# This script hard-requires the GitHub CLI (PI-362).
command -v gh >/dev/null 2>&1 || { echo "error: GitHub CLI (gh) not found — install: https://cli.github.com" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
# shellcheck source=/dev/null
. "$SCRIPT_DIR/gh_host.sh"

BRANCH="main"
PROTECT=0
for arg in "$@"; do
  case "$arg" in
    --protect) PROTECT=1 ;;
    --*) echo "Unknown option: $arg" >&2; exit 1 ;;
    *) BRANCH="$arg" ;;
  esac
done

HOST="$(gh_host)"
if ! gh auth status -h "$HOST" >/dev/null 2>&1; then
  echo "ERROR: gh is not authenticated for $HOST. Run: gh auth login --hostname $HOST" >&2
  exit 1
fi

REPO=$(gh repo view --json nameWithOwner -q .nameWithOwner)
OWNER=${REPO%/*}
NAME=${REPO#*/}
WEB_BASE="https://$HOST"

echo "Configuring GitHub governance for $REPO ($BRANCH)"
# Default endpoint: repos/$OWNER/$NAME/branches/main/protection

if [ "$PROTECT" = 1 ]; then
# Repo merge policy: squash-only + delete-branch-on-merge. Squash keeps history
# linear (one commit per PR) and reuses the PR title (ADR-006); deleting the head
# branch on merge keeps the branch list clean.
if gh api "repos/$OWNER/$NAME" -X PATCH \
     -F allow_squash_merge=true -F allow_merge_commit=false \
     -F allow_rebase_merge=false -F delete_branch_on_merge=true >/dev/null 2>&1; then
  echo "Repo merge policy: squash-only + delete-branch-on-merge"
else
  echo "WARNING: could not set repo merge policy (admin permission?)." >&2
fi

PROTECTION=$(mktemp)
trap 'rm -f "$PROTECTION"' EXIT

# Contexts must match the scaffolded workflows ("<workflow> / <job name>").
# "CI / Integration tests" is omitted on purpose — that job is documented as
# user-adjustable; add it here once your integration suite is stable.
cat > "$PROTECTION" <<'JSON'
{
  "required_status_checks": {
    "strict": true,
    "contexts": [
      "CI / Lint and test",
      "CI / Secret scan (gitleaks)",
      "Validate PR / Check PR title, branch, and linked issue",
      "review/decision"
    ]
  },
  "enforce_admins": false,
  "required_pull_request_reviews": {
    "required_approving_review_count": 1,
    "dismiss_stale_reviews": true,
    "require_code_owner_reviews": false,
    "require_last_push_approval": false
  },
  "restrictions": null,
  "required_conversation_resolution": true,
  "allow_force_pushes": false,
  "allow_deletions": false
}
JSON

if gh api "repos/$OWNER/$NAME/branches/$BRANCH/protection" -X PUT --input "$PROTECTION" >/dev/null; then
  echo "Branch protection applied to $BRANCH"
else
  echo "WARNING: could not apply branch protection. Check admin permissions and repository plan." >&2
fi

# Repository ruleset (#251): the org profile's "hard" enforcement layer. A
# ruleset with an empty bypass_actors binds *everyone* (owners/admins included),
# so it cannot be admin-bypassed — unlike classic branch protection
# (enforce_admins=false above). Applied ONLY under the org profile;
# individual/standalone keep advisory branch protection (admin escape hatch
# intact), per ADR-013. Forks do NOT inherit branch/tag rulesets, so the org
# applies it directly. Feature-probe first and warn (never fail) without rulesets.
if [ "$(gh_profile)" != "org" ]; then
  echo "Profile is not 'org' — keeping advisory branch protection only (no owner-binding ruleset)."
elif gh api "repos/$OWNER/$NAME/rulesets" >/dev/null 2>&1; then
  RULESET=$(mktemp)
  trap 'rm -f "$PROTECTION" "$RULESET"' EXIT
  cat > "$RULESET" <<'RULESET_JSON'
{
  "name": "project-init-baseline",
  "target": "branch",
  "enforcement": "active",
  "conditions": { "ref_name": { "include": ["~DEFAULT_BRANCH"], "exclude": [] } },
  "rules": [
    { "type": "non_fast_forward" },
    { "type": "deletion" },
    { "type": "pull_request", "parameters": {
        "required_approving_review_count": 1,
        "dismiss_stale_reviews_on_push": true,
        "require_code_owner_review": false,
        "require_last_push_approval": false,
        "required_review_thread_resolution": true } },
    { "type": "required_status_checks", "parameters": {
        "strict_required_status_checks_policy": true,
        "required_status_checks": [
          { "context": "CI / Lint and test" },
          { "context": "CI / Secret scan (gitleaks)" } ] } }
  ],
  "bypass_actors": []
}
RULESET_JSON
  if gh api "repos/$OWNER/$NAME/rulesets" -X POST --input "$RULESET" >/dev/null 2>&1; then
    echo "Repository ruleset 'project-init-baseline' applied (binds everyone — empty bypass)"
  else
    echo "WARNING: could not create the repository ruleset (it may already exist, or the plan/permission is insufficient)." >&2
  fi
else
  echo "Rulesets API unavailable on this host/plan — relying on branch protection only." >&2
fi
else
  echo "Skipping branch protection (pass --protect to apply: require CI green, require review, block force-push)"
fi

if gh api "repos/$OWNER/$NAME/code-review-settings" -X PUT -f copilot_code_review_enabled=true >/dev/null 2>&1; then
  echo "Copilot code review enabled"
else
  echo "WARNING: Enable Copilot code review manually if your plan supports it:" >&2
  echo "  $WEB_BASE/$OWNER/$NAME/settings/code_review" >&2
fi

# --- GitHub Project board field provisioning ---
# Creates the single-select metadata fields used by board-automation.yml.
# Requires a token with 'project' scope (set PROJECT_TOKEN env var, or ensure
# gh auth token has project scope). Skips fields that already exist.
echo ""
echo "Provisioning GitHub Project board fields..."

PROJECT_NUMBER="${PROJECT_NUMBER:-1}"

# Use PROJECT_TOKEN if set, otherwise fall through to default gh auth
if [ -n "${PROJECT_TOKEN:-}" ]; then
  export GH_TOKEN="$PROJECT_TOKEN"
fi

# Query the project board with gh's built-in gojq (-q) instead of an external
# jq, so the script has no jq dependency (PI-362). Two short round-trips on a
# one-time admin script is negligible.
PROJECT_QUERY='
  query($owner: String!, $number: Int!) {
    user(login: $owner) {
      projectV2(number: $number) {
        id
        fields(first: 50) { nodes { ... on ProjectV2SingleSelectField { name } } }
      }
    }
    organization(login: $owner) {
      projectV2(number: $number) {
        id
        fields(first: 50) { nodes { ... on ProjectV2SingleSelectField { name } } }
      }
    }
  }'

PROJECT_ID=$(gh api graphql -f query="$PROJECT_QUERY" \
  -f owner="$OWNER" -F number="$PROJECT_NUMBER" \
  -q '(.data.user.projectV2 // .data.organization.projectV2 // {}).id // empty' \
  2>/dev/null || echo '')

if [ -z "$PROJECT_ID" ]; then
  echo "WARNING: Project #$PROJECT_NUMBER not found for $REPO." >&2
  echo "  Ensure PROJECT_TOKEN has 'project' scope, or create these fields manually:" >&2
  echo "  • Priority     — options: high, medium, low" >&2
  echo "  • Size         — options: XS, S, M, L, XL" >&2
  echo "  • Agent ready  — options: Yes, No" >&2
  echo "  • Confidence   — options: high, medium, low, unknown" >&2
  echo "  • Type         — options: feature, bug, chore, documentation, test" >&2
  echo "  Settings: $WEB_BASE/users/$OWNER/projects/$PROJECT_NUMBER/settings/fields" >&2
else
  EXISTING_FIELDS=$(gh api graphql -f query="$PROJECT_QUERY" \
    -f owner="$OWNER" -F number="$PROJECT_NUMBER" \
    -q '((.data.user.projectV2 // .data.organization.projectV2).fields.nodes // [])[] | .name // empty' \
    2>/dev/null || echo '')

  ensure_single_select_field() {
    local field_name="$1"
    local mutation="$2"
    if printf '%s\n' "$EXISTING_FIELDS" | grep -Fxq "$field_name"; then
      echo "  '$field_name' already exists — skipping"
      return 0
    fi
    if gh api graphql -f query="$mutation" -f projectId="$PROJECT_ID" >/dev/null 2>&1; then
      echo "  Created '$field_name'"
    else
      # Repo: $REPO  Project: #$PROJECT_NUMBER
      echo "  WARNING: could not create '$field_name' for $REPO — add it manually:" >&2
      echo "    $WEB_BASE/users/$OWNER/projects/$PROJECT_NUMBER/settings/fields" >&2
    fi
  }

  ensure_single_select_field "Priority" '
    mutation($projectId: ID!) {
      createProjectV2Field(input: {
        projectId: $projectId
        dataType: SINGLE_SELECT
        name: "Priority"
        singleSelectOptions: [
          { name: "high",   color: RED,    description: "" }
          { name: "medium", color: YELLOW, description: "" }
          { name: "low",    color: GRAY,   description: "" }
        ]
      }) { projectV2Field { ... on ProjectV2SingleSelectField { id } } }
    }'

  ensure_single_select_field "Size" '
    mutation($projectId: ID!) {
      createProjectV2Field(input: {
        projectId: $projectId
        dataType: SINGLE_SELECT
        name: "Size"
        singleSelectOptions: [
          { name: "XS", color: BLUE,   description: "" }
          { name: "S",  color: GREEN,  description: "" }
          { name: "M",  color: YELLOW, description: "" }
          { name: "L",  color: ORANGE, description: "" }
          { name: "XL", color: RED,    description: "" }
        ]
      }) { projectV2Field { ... on ProjectV2SingleSelectField { id } } }
    }'

  ensure_single_select_field "Agent ready" '
    mutation($projectId: ID!) {
      createProjectV2Field(input: {
        projectId: $projectId
        dataType: SINGLE_SELECT
        name: "Agent ready"
        singleSelectOptions: [
          { name: "Yes", color: GREEN, description: "" }
          { name: "No",  color: GRAY,  description: "" }
        ]
      }) { projectV2Field { ... on ProjectV2SingleSelectField { id } } }
    }'

  ensure_single_select_field "Confidence" '
    mutation($projectId: ID!) {
      createProjectV2Field(input: {
        projectId: $projectId
        dataType: SINGLE_SELECT
        name: "Confidence"
        singleSelectOptions: [
          { name: "high",    color: GREEN,  description: "" }
          { name: "medium",  color: YELLOW, description: "" }
          { name: "low",     color: ORANGE, description: "" }
          { name: "unknown", color: GRAY,   description: "" }
        ]
      }) { projectV2Field { ... on ProjectV2SingleSelectField { id } } }
    }'

  ensure_single_select_field "Type" '
    mutation($projectId: ID!) {
      createProjectV2Field(input: {
        projectId: $projectId
        dataType: SINGLE_SELECT
        name: "Type"
        singleSelectOptions: [
          { name: "feature",       color: BLUE,   description: "" }
          { name: "bug",           color: RED,    description: "" }
          { name: "chore",         color: GRAY,   description: "" }
          { name: "documentation", color: PURPLE, description: "" }
          { name: "test",          color: YELLOW, description: "" }
        ]
      }) { projectV2Field { ... on ProjectV2SingleSelectField { id } } }
    }'
fi

