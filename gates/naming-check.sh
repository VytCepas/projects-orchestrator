#!/bin/sh
# naming-check.sh — the box's naming lint over this repo, with this repo's exemption rows applied.
#
#   NAMING_STANDARD_REPO=<checkout> sh gates/naming-check.sh [lint-name.sh args]   e.g. --format findings
#
# The judge is bin/lint-name.sh in the checkout that owns the naming standard and the registry is its
# standards/naming/. This repo adds rows to exemptions.tsv only, from naming-exemptions.tsv at the root,
# in a temporary copy of the registry. Rules that judge the registry itself (name.registry-*,
# exemption-*, control-*, surface-*, alias-expired) read that checkout's tree, not this one, so their
# findings are dropped here. No checkout: exit 3.
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd)
STANDARD="${NAMING_STANDARD_REPO:-}"
LINT="$STANDARD/bin/lint-name.sh"
[ -n "$STANDARD" ] && [ -f "$LINT" ] || { printf 'naming-check: UNCHECKED, set NAMING_STANDARD_REPO to the checkout that owns the naming standard\n' >&2; exit 3; }
T=$(mktemp -d "${TMPDIR:-/tmp}/naming-check.XXXXXX")
trap 'rm -rf "$T"' EXIT INT TERM HUP
cp -R "$STANDARD/standards/naming" "$T/naming"
# grep exits 1 when no row survives (a header-only file): that is an empty exemption list, not a failure. Any other grep failure (exit 2, unreadable file) stops the gate with a message.
grep -v -e '^#' -e '^id	' "$ROOT/naming-exemptions.tsv" >> "$T/naming/exemptions.tsv" ||
    [ "$?" -eq 1 ] || { printf 'naming-check: cannot read naming-exemptions.tsv (grep failed)\n' >&2; exit 2; }
rc=0
sh "$LINT" --root "$ROOT" --registry "$T/naming" --format findings "$@" > "$T/out" 2> "$T/err" || rc=$?
[ "$rc" -le 1 ] || { cat "$T/err" >&2; exit "$rc"; }
awk -F'\t' 'NF >= 3 && $2 !~ /^name\.(registry-|exemption-|alias-expired|control-|surface-)/' "$T/out" > "$T/mine"
cat "$T/mine"
n=$(wc -l < "$T/mine" | tr -d ' ')
printf 'naming-check: %s finding(s) on this tree, after naming-exemptions.tsv\n' "$n" >&2
[ "$n" -eq 0 ]
