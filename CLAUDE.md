# projects-orchestrator — Claude Code entry point

Canonical agent instructions live in [AGENTS.md](AGENTS.md) — the
Linux Foundation standard file most coding agents read natively. Read it
before working in this codebase; it is the source of truth for workflow,
conventions, memory, tools, and branch naming. Its "Claude Code specifics"
section applies to this session.

## Compact Instructions

When compacting this conversation, preserve:
- The project ticket key and GitHub issue number being worked on (e.g. `PI-42`, GitHub `#42`)
- Files modified in this session (list by path)
- Test results: pass/fail count and any failing test names
- Unresolved errors or lint failures
- Any decisions made that aren't yet committed to `.agents/docs/adr/`
