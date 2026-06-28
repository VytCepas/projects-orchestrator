#!/usr/bin/env bash
# push_wiki.sh — clone the GitHub wiki, update Home.md, and push.
# Usage: push_wiki.sh <repo-slug> <wiki-source-file>
#   repo-slug       e.g. owner/repo-name
#   wiki-source-file  path to the markdown file to write as Home.md
#
# The guard allowlists this script so it is not blocked by the git-push rule.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
# shellcheck source=/dev/null
. "$SCRIPT_DIR/gh_host.sh"

if [[ $# -lt 2 ]]; then
  echo "Usage: push_wiki.sh <repo-slug> <wiki-source-file>" >&2
  exit 1
fi

REPO_SLUG="$1"
SOURCE_FILE="$2"
WIKI_DIR="$(mktemp -d)"
trap 'rm -rf "$WIKI_DIR"' EXIT

echo "Cloning wiki for $REPO_SLUG..."
git clone "$(gh_web_base)/${REPO_SLUG}.wiki.git" "$WIKI_DIR"

cp "$SOURCE_FILE" "$WIKI_DIR/Home.md"

cd "$WIKI_DIR"
git add Home.md
if git diff --cached --quiet; then
  echo "Wiki already up to date."
  exit 0
fi
git commit -m "Update Home page"
git push
echo "Wiki updated."
