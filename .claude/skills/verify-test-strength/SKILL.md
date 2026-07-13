---
name: verify-test-strength
description: Prove a load-bearing test can actually fail — run mutation testing on a target module, then iteratively strengthen its tests until surviving mutants are killed. The automated form of the break-it discipline, for guards that must not silently pass.
when_to_use: Use on a LOAD-BEARING test after writing or changing it — a contract test, guard hook, invariant check, or anything whose green must mean something. Trigger on "prove this test can fail", "is this test vacuous", "strengthen these tests", "mutation test this", "harden the guard", or right after adding a test you're about to rely on. NOT for the whole suite (it is expensive) and NOT for throwaway tests.
user-invocable: true
---

# verify-test-strength — make green mean something

A passing test proves nothing until you've seen it fail for the right reason.
Coverage is cheap and lies: an LLM-written suite can hit 100% coverage at ~4%
mutation score — it *executes* the code without *asserting* on it. The fix is a
**mutation-feedback loop**, not a one-off "try to break it": mutate the code,
see which mutants the tests fail to catch, strengthen the tests to catch them,
repeat. Static "write fault-detecting tests" guidance is the floor; the measured
gain comes from iterating on real survivors.

**Use this selectively.** The loop costs meaningfully more tokens and time than a
normal test run, so reach for it on the tests that *matter* — the contract test
guarding an invariant, the security/guard hook, the parser everything depends on
— not on every test. For a quick change with no load-bearing test, the plain
break-it rule (change the thing, watch the test fail, restore) is enough.

## 1. Scope it

Name the **one module (or a few functions) under test** and **its test file**.
Mutation testing is slow and noisy at scale, so a tight scope is what makes it
usable. Point the mutation tool at deterministic, pure-logic code — skip I/O,
network, template rendering, and hooks, where mutants are dominated by
false-equivalents.

## 2. Run the mutation tool

Only meaningful where a mutation runner is wired for the language:

| Language | Tool | Wired? |
|---|---|---|
| **Python** | `mutmut` — `just test-mutation` | ✅ shipped |
| Node/TS | StrykerJS | not scaffolded yet — fall back to §5 |
| Rust | `cargo-mutants` | not scaffolded yet — fall back to §5 |
| Go | `gremlins` / `go-mutesting` | not scaffolded yet — fall back to §5 |

For Python, target the run **without committing a narrower config**. The
nightly CI `mutation-tests` job reads `[tool.mutmut] source_paths` from
`pyproject.toml`, so committing a scoped-down `source_paths` silently shrinks
CI's mutation coverage to your one module. Instead:

- run mutmut with a per-run path filter — `uv run --with mutmut mutmut run <path/to/module>` — leaving the committed config alone, **or**
- temporarily narrow `source_paths` and **restore it before committing**.

`just test-mutation` runs the full configured scope (what CI runs). Either way,
mutmut reports **surviving mutants** — code changes the tests did NOT catch.
Each survivor is a hole in the suite.

If the language has no wired mutation tool, say so plainly and drop to §5 (the
manual loop) — do not pretend a score exists.

## 3. Kill the survivors, one at a time

For each surviving mutant:

1. Read the mutation — what did it change (a `>` to `>=`, a `+` to `-`, a
   returned value, a dropped call)?
2. Ask: **what real behaviour would that break, and why didn't a test notice?**
3. Add or strengthen a test that FAILS on the mutant and passes on the original.
   Assert on the *behaviour the mutant changed*, not on incidental output.
4. Prefer strengthening an existing test's assertions over adding a near-duplicate.

Re-run the mutation tool. Repeat. Convergence is typically ~4 rounds — if the
kill-rate stops improving, stop; grinding further mostly hits equivalent mutants.

## 4. Report honestly — never force green

Some survivors can't (or shouldn't) be killed:

- **Equivalent mutants** — the change doesn't alter observable behaviour. Note
  and move on; don't contort a test to "kill" a no-op.
- **Dead code** — nothing can reach the mutated line. That's a *finding*: the
  branch is a candidate for deletion, not a test to add.
- **Genuinely untestable** — a defensive `assert`/`raise` for an invariant the
  types already guarantee.

List the survivors you did not kill and WHY. A truthful "3 killed, 1 equivalent,
1 is dead code — delete it" is the deliverable. Forcing a green score by writing
a test that asserts the mutant's own behaviour is worse than the original hole.

## 5. No mutation tool? Manual break-it loop

Where no runner is wired, do the loop by hand on the load-bearing test:

1. In the code under test, deliberately break the exact thing the test claims to
   guard (flip a comparison, drop a guard clause, return a wrong constant).
2. Run the test. It **must** fail — if it passes, the test is vacuous; fix the
   assertion, not the code.
3. Restore the code; confirm green.
4. Repeat for each distinct thing the test claims to protect.

State in the PR that you did this and what you flipped — the same evidence the
mutation loop produces, just hand-rolled.
