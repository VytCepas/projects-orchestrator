---
name: add_adr
description: Records an architectural decision as a new ADR in .claude/docs/adr/ using the MADR template. Use when a non-obvious design choice is made so future agents understand why.
when_to_use: Use when the user says "record this decision", "write an ADR", or when you make an architectural choice (library, pattern, boundary, trade-off) that a future session would otherwise re-litigate. Do not use for trivial or easily reversed choices.
user-invocable: true
effort: low
allowed-tools: Read Write Glob Bash(git *)
---

Record an architectural decision record (ADR).

## When an ADR is warranted

- The choice is hard to reverse, or expensive to re-derive
- Multiple plausible options existed and one was deliberately picked
- A future agent or developer would ask "why is it like this?"

Not warranted: formatting choices, renames, anything fully explained by code.

## Steps

1. Find the next free number: list `.claude/docs/adr/adr-*.md` and take the
   highest `NNN` + 1.
2. Copy `.claude/docs/adr/adr-template.md` to
   `.claude/docs/adr/adr-NNN-<kebab-slug>.md`.
3. Fill every section. Keep it short — context (2-5 sentences), the options
   actually considered, the decision with its justification, and honest
   consequences (including the bad ones). Delete the template's comments.
4. Set Status to `accepted` if the decision is already in effect, otherwise
   `proposed`.
5. If the ADR supersedes an older one, mark the old ADR
   `superseded by ADR-NNN` — do not delete it.
6. Mention the new ADR in the commit/PR that implements the decision.
