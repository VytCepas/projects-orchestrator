---
name: plan
description: TDD-style planning — understand requirements, write tests first, then implement with numbered steps
when_to_use: Use for non-trivial features or bug fixes. Plan before code to reduce rework and improve test coverage.
allowed-tools: Read Grep Glob Bash(git *)
---

Plan this work in 5 phases. Format output clearly so it's actionable.

## Phase 1 — Understand the requirement

Ask clarifying questions if needed. Answer: What is the core change? Why now? What's the acceptance criteria?

## Phase 2 — Acceptance tests (write them first)

Write the test assertions **before** implementation code:

```bash
# Example: test that a function returns X when given Y
assert_equals(parse_config("key=val"), {"key": "val"})
assert_throws(parse_config("broken"), ParseError)
```

## Phase 3 — Implementation plan

Numbered steps for implementation. Link to files (`path:line_number`). Estimate scope (1-2h, half-day, day+).

## Phase 4 — Risks and scope

- What could go wrong?
- Is this change backward compatible?
- Do other parts of the system depend on this behavior?

## Phase 5 — Definition of done

When is this complete?
- Tests pass
- Behavior works end-to-end
- Error cases handled
- Documentation updated if needed
