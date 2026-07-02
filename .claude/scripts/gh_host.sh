#!/usr/bin/env bash
# gh_host.sh — resolve the active GitHub host for host-aware links and URLs.
#
# Sourced by lifecycle scripts so manual links, clone URLs, and curl-based API
# calls work on github.com, GHE.com (data residency, *.ghe.com), and GitHub
# Enterprise Server (GHES) — not just public github.com (ADR-013, spike #254).
#
# `gh api` already targets the repo's host automatically, so these helpers are
# only needed for hardcoded URLs and non-gh callers.
#
# On Enterprise Managed Users (EMU), external forks are blocked — an org mirrors
# or imports the upstream instead (see the org fork lifecycle runbook). These
# helpers resolve the host the same way regardless of fork-vs-import.
#
# Resolution order: PROJECT_INIT_HOST → GH_HOST → current repo remote → github.com.

# Strip scheme, userinfo, path, and port from a URL or host string → bare host.
# Handles https://, http://, ssh://, scp-style git@host:owner/repo, and bare hosts.
_gh_host_normalize() {
  printf '%s\n' "$1" | sed -E 's#^[a-zA-Z][a-zA-Z0-9+.-]*://##; s#^[^@/]*@##; s#[/:].*$##'
}

gh_host() {
  local raw=""
  if [ -n "${PROJECT_INIT_HOST:-}" ]; then
    raw="$PROJECT_INIT_HOST"
  elif [ -n "${GH_HOST:-}" ]; then
    raw="$GH_HOST"
  else
    raw=$(gh repo view --json url -q .url 2>/dev/null || true)
    [ -z "$raw" ] && raw=$(git config --get remote.origin.url 2>/dev/null || true)
  fi
  local host
  host="$(_gh_host_normalize "$raw")"
  printf '%s\n' "${host:-github.com}"
}

# Web base for browser links, e.g. https://github.com or https://ghes.example.com
gh_web_base() { printf 'https://%s\n' "$(gh_host)"; }

# REST API base for curl-based callers. Prefer `gh api` where possible (it
# resolves the host itself). github.com & *.ghe.com use api.<host>; GHES uses
# <host>/api/v3. Override explicitly with PROJECT_INIT_API_BASE.
gh_api_base() {
  if [ -n "${PROJECT_INIT_API_BASE:-}" ]; then
    printf '%s\n' "$PROJECT_INIT_API_BASE"
    return
  fi
  local host
  host="$(gh_host)"
  case "$host" in
  "") printf 'https://api.github.com\n' ;;
  github.com | *.ghe.com) printf 'https://api.%s\n' "$host" ;;
  *) printf 'https://%s/api/v3\n' "$host" ;;
  esac
}

# Distribution profile recorded by project-init in .claude/config.yaml (#247);
# defaults to individual when unset. Gates org-only hard enforcement (#251).
gh_profile() {
  local cfg=".claude/config.yaml" prof=""
  [ -f "$cfg" ] && prof=$(sed -nE 's/^[[:space:]]*profile:[[:space:]]*([a-z]+).*/\1/p' "$cfg" | head -1)
  printf '%s\n' "${prof:-individual}"
}

# Base branch for feature PRs. Single trunk: the scaffolder pins the rendered
# workflows (ci.yml, validate-pr.yml) to 'main', so this MUST return 'main' too —
# resolving the live default branch instead would let start_issue.sh target a
# branch the CI/validation workflows don't cover (PR #330 review). Used by
# start_issue.sh.
base_branch() {
  printf 'main\n'
}
