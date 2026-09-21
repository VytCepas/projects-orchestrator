# projects-orchestrator — Claude Code entry point

@AGENTS.md

That bare line is an IMPORT and has to stay bare. Claude Code expands `@path`
and nothing else, so a markdown link to the same file loads nothing — and this
file existing at all stops Claude reading `AGENTS.md` natively. Backticks or a
code fence disable the import the same way.

`AGENTS.md` is the source of truth: workflow, conventions, memory, tools, branch
naming, and the Claude-only details in its `## Claude Code specifics` section.
Claude-specific content belongs there, not here — only what `AGENTS.md` cannot
carry, like the compaction directives below, lives in this file.

## Compact Instructions

When compacting this conversation, preserve:
- The project ticket key and GitHub issue number being worked on (e.g. `PI-42`, GitHub `#42`)
- Files modified in this session (list by path)
- Test results: pass/fail count and any failing test names
- Unresolved errors or lint failures
- Any decisions made that aren't yet committed to `.agents/docs/adr/`
