# `.agents/agents/`

Claude Code subagent definitions — specialized personas for repeatable tasks.
They're version-controlled, team-shared, and double as teammate roles for agent
teams.

No specs ship here by default (#848): read-only codebase research is the
built-in `Explore` agent, and code review comes from the pre-enabled
`pr-review-toolkit` plugin — a local copy would register twice in the agent
index every session. A `--no-egress` scaffold (no marketplace plugins) does
ship **`code-reviewer.md`** as the fallback reviewer.

## How to create a subagent

Create a `<name>.md` file with YAML frontmatter, then a markdown body that is the
agent's system prompt. **Only `name` and `description` are required.**

```yaml
---
name: agent-name                 # lowercase + hyphens; this is the subagent_type
description: What this agent does and when to delegate to it
model: inherit                   # sonnet | opus | haiku | fable | <full-id> | inherit (default)
tools: Read, Grep, Glob, Bash    # comma-separated; inherits all if omitted
# --- all optional below ---
# disallowedTools: Write, Edit   # deny specific tools
# permissionMode: default        # default|acceptEdits|auto|dontAsk|bypassPermissions|plan
# maxTurns: 15                   # cap agentic turns
# skills: [name, ...]            # preload skill content into context
# mcpServers: [name, ...]        # MCP servers available to this agent
# hooks: { ... }                 # lifecycle hooks scoped to this agent
# memory: project                # user|project|local — cross-session memory
# background: true               # always run as a background task
# effort: medium                 # low|medium|high|xhigh|max
# isolation: worktree            # run in a throwaway git worktree
# color: cyan                    # task-list display color
# initialPrompt: "..."           # first user turn when run as the main agent
---

<Detailed instructions for the agent's role, behavior, and approach.>
```

**Plugin caveat:** subagents distributed via a plugin ignore `hooks`,
`mcpServers`, and `permissionMode` for security. The shipped specs omit those so
they behave identically whether scaffolded directly or via the plugin.

Invoke from a skill or the CLI: `Agent({"description": "...", "subagent_type":
"agent-name", "prompt": "..."})`.

## Reference

[Claude Code subagents documentation](https://code.agents.com/docs/en/sub-agents)
— full field reference, tool access, and agent-team usage.
