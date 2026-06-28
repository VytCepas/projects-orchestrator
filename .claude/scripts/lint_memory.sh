#!/usr/bin/env bash
# lint_memory.sh — validate memory files against SCHEMA.md conventions.
# Agent-agnostic: any agent or hook can call this directly.
# Exit 0 on clean, exit 1 with actionable messages.

set -euo pipefail

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
MEMORY_DIR="$ROOT/.claude/memory"
INDEX="$MEMORY_DIR/MEMORY.md"

ERRORS=0
WARNINGS=0

error() { echo "ERROR: $1" >&2; ERRORS=$((ERRORS + 1)); }
warn()  { echo "WARN:  $1" >&2; WARNINGS=$((WARNINGS + 1)); }

SKIP_FILES="MEMORY.md SCHEMA.md README.md"

is_skipped() {
  local name="$1"
  for skip in $SKIP_FILES; do
    [ "$name" = "$skip" ] && return 0
  done
  return 1
}

# --- Validate memory file frontmatter ---

for file in "$MEMORY_DIR"/*.md; do
  [ -f "$file" ] || continue
  name="$(basename "$file")"
  is_skipped "$name" && continue

  # Extract YAML frontmatter (between --- delimiters)
  if ! head -1 "$file" | grep -q '^---$'; then
    error "$name: missing YAML frontmatter (no opening ---)"
    continue
  fi

  # Get frontmatter block (between first and second --- delimiters, POSIX-portable)
  frontmatter="$(awk 'NR==1{next} /^---$/{exit} {print}' "$file")"

  # Check required fields
  for field in name description type; do
    if ! echo "$frontmatter" | grep -q "^${field}:"; then
      error "$name: missing required field '$field'"
    fi
  done

  # Validate type value
  type_val="$(echo "$frontmatter" | grep '^type:' | sed 's/^type:[[:space:]]*//' | tr -d '[:space:]')"
  if [ -n "$type_val" ]; then
    case "$type_val" in
      user|feedback|project|reference) ;;
      *) error "$name: invalid type '$type_val' (must be user|feedback|project|reference)" ;;
    esac
  fi
done

# --- Check index completeness ---

if [ ! -f "$INDEX" ]; then
  error "MEMORY.md index not found"
else
  # Check every memory file appears in the index
  for file in "$MEMORY_DIR"/*.md; do
    [ -f "$file" ] || continue
    name="$(basename "$file")"
    is_skipped "$name" && continue

    if ! grep -q "$name" "$INDEX"; then
      error "$name: not listed in MEMORY.md index"
    fi
  done

  # Check every file referenced in index actually exists
  while read -r ref; do
    if [ ! -f "$MEMORY_DIR/$ref" ]; then
      error "MEMORY.md references '$ref' but file does not exist"
    fi
  done < <(sed -n 's/.*\[[^]]*\](\([^)]*\)).*/\1/p' "$INDEX" 2>/dev/null || true)
fi

# --- Dangling path references (warnings only) ---
# A fact that names a `path/like/this.ext` which no longer exists is the most
# common mechanically-detectable staleness (ADR-024). Deterministic, no LLM:
# only flags backtick tokens that look like repo-relative paths (a slash plus a
# file extension), so prose, commands, and bare names don't trip it. Semantic
# contradiction detection is intentionally out of scope here — that needs a
# model, so it belongs to a consolidate-memory agent pass, not this gate.

for file in "$MEMORY_DIR"/*.md; do
  [ -f "$file" ] || continue
  name="$(basename "$file")"
  is_skipped "$name" && continue

  # word-splitting is safe: the charclass excludes whitespace, so no path has spaces.
  for ref in $(grep -oE '`[A-Za-z0-9_./-]+`' "$file" 2>/dev/null | tr -d '`'); do
    case "$ref" in
      http*|/*|*'*'*) continue ;;  # URLs, absolute system paths, globs
      */*.*) ;;                     # looks like a repo-relative path with an extension
      *) continue ;;
    esac
    [ -e "$ROOT/$ref" ] || warn "$name: references \`$ref\` which does not exist (stale?)"
  done
done

# --- Stale memory facts (warnings only) ---
# Flag facts not touched in over STALE_DAYS days — a deterministic nudge to
# review/refresh them (ADR-024). Only meaningful inside a git repo with history;
# uncommitted files (a fresh scaffold) and non-git checkouts are skipped.

STALE_DAYS="${LINT_MEMORY_STALE_DAYS:-180}"
if git -C "$ROOT" rev-parse --git-dir >/dev/null 2>&1; then
  cutoff=$(( $(date +%s) - STALE_DAYS * 86400 ))
  for file in "$MEMORY_DIR"/*.md; do
    [ -f "$file" ] || continue
    name="$(basename "$file")"
    is_skipped "$name" && continue
    ts="$(git -C "$ROOT" log -1 --format=%ct -- "$file" 2>/dev/null || true)"
    [ -n "$ts" ] || continue  # uncommitted / no history → not stale
    if [ "$ts" -lt "$cutoff" ]; then
      warn "$name: not updated in over $STALE_DAYS days (review for staleness)"
    fi
  done
fi

# --- Report orphaned vault notes (warnings only) ---

VAULT_DIR="$ROOT/.claude/vault"
if [ -d "$VAULT_DIR" ]; then
  # Collect all wikilink targets from vault notes
  all_links="$(grep -roh '\[\[[^]]*\]\]' "$VAULT_DIR" 2>/dev/null | sed 's/\[\[//;s/\]\]//' | sort -u || true)"

  while IFS= read -r -d '' file; do
    name="$(basename "$file" .md)"
    # Check if any other note links to this one
    if [ -n "$all_links" ]; then
      if ! echo "$all_links" | grep -q "$name"; then
        warn "$(basename "$file"): no inbound wikilinks (orphan note)"
      fi
    fi
  done < <(find "$VAULT_DIR" -name '*.md' -not -path '*/.obsidian/*' -not -path '*/templates/*' -not -name 'README.md' -not -name 'log.md' -print0)
fi

# --- Summary ---

if [ "$ERRORS" -gt 0 ]; then
  echo >&2
  echo "lint_memory: $ERRORS error(s), $WARNINGS warning(s)" >&2
  exit 1
fi

if [ "$WARNINGS" -gt 0 ]; then
  echo "lint_memory: clean ($WARNINGS warning(s))" >&2
fi

exit 0
