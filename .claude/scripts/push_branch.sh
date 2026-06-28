#!/usr/bin/env bash
# Thin shim — actual logic lives in .claude/hooks/dag_workflow.py.
# Kept under .claude/scripts/ so existing skill paths and agent muscle
# memory keep working.
exec "$(dirname "$0")/../hooks/_py.sh" "$(dirname "$0")/../hooks/dag_workflow.py" push "$@"
