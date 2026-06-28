---
name: status
description: Show project status — git state, recent commits, open tasks, and memory summary
when_to_use: Use when you want a quick snapshot of the project — current branch, uncommitted changes, recent work, active memory, and open TODOs.
allowed-tools: Bash(git *) Read Grep Glob
---

Give me a concise project status report:

1. **Git state** — current branch, uncommitted changes, commits ahead/behind remote
2. **Recent work** — last 5 commits (one line each)
3. **Memory** — if `.claude/memory/MEMORY.md` exists, read it and list the key facts; otherwise skip this section (the project has no memory tier)
4. **Open items** — scan for TODO/FIXME/HACK in the codebase (top 10)

Keep the report under 30 lines. Use markdown formatting.
