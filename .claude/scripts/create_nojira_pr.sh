#!/usr/bin/env bash
# Thin shim — actual logic lives in .claude/hooks/dag_workflow.py.
exec "$(dirname "$0")/../hooks/_py.sh" "$(dirname "$0")/../hooks/dag_workflow.py" create-pr-nojira "$@"
