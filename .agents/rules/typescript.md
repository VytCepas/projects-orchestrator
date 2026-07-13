---
description: TypeScript strict type-checking conventions
globs: ["**/*.ts", "**/*.tsx", "tsconfig.json", "tsconfig.base.json"]
alwaysApply: false
---

## TypeScript only — no plain JavaScript

Project source is **TypeScript, not JavaScript**. Write `.ts`/`.tsx`; do not add
`.js`/`.jsx` source files. This is a deliberate safety choice: the strict gates
below only see files `tsconfig.json` includes (`**/*.ts`, `**/*.tsx`), so a plain
`.js` file silently escapes `tsc`, the type-aware eslint rules, and the coverage
gate entirely — untyped, unchecked code hiding inside a "green" build.

Do **not** re-open that hole: never set `allowJs` in `tsconfig*.json`, and never
widen the project `include` (or an eslint `files` block) to `**/*.js`. The only
`.js`/`.mjs`/`.cjs` files in a scaffold are tooling configs (e.g.
`eslint.config.mjs`), which `eslint.config.mjs` lints with type-checking
disabled on purpose — that carve-out is for config, not for product code.

## TypeScript environment

```bash
bunx tsc --noEmit   # type check (strict mode, per tsconfig.base.json)
bunx eslint .        # lint + SECURITY gate (#729), blocking. Pinned to `error` in eslint.config.mjs:
                     #   security/detect-eval-with-expression, detect-child-process,
                     #   detect-non-literal-fs-filename, detect-unsafe-regex
                     #   no-unsanitized/method, no-unsanitized/property   (DOM XSS sinks)
                     # and the type-aware rules no-floating-promises, no-misused-promises,
                     # no-unsafe-* — pinned explicitly, NOT inherited from strictTypeChecked,
                     # so an upstream preset change cannot silently downgrade them.
                     # eslint-plugin-security's own preset sets these to `warn`, and eslint
                     # exits 0 on warnings — a gate that never blocks. Hence the pins.
bunx @biomejs/biome format .   # format gate — exits 1 on unformatted, leaves files untouched.
                               # Part of `just lint` (#726). There is NO `--check` flag: biome
                               # rejects it ("`--check` is not expected in this context") — the
                               # no-flag form IS the check. `--write` (i.e. `just format`) fixes.
bun test             # tests + coverage gate (threshold per bunfig.toml) — always on, no extra flag needed
bun audit            # dependency CVE/advisory scan — CI always runs this
```

`tsconfig.base.json` is a direct structural analog to `mypy --strict` /
`.golangci.yml`'s strict linters — `strict`, `noUncheckedIndexedAccess`,
`exactOptionalPropertyTypes`, `noImplicitOverride`,
`noPropertyAccessFromIndexSignature`, `noFallthroughCasesInSwitch`,
`noImplicitReturns`, and `allowUnreachableCode: false` all on. `tsconfig.json`
extends it; keep your own paths/target edits there, not in the base file.

`eslint.config.mjs` uses `strictTypeChecked` + `stylisticTypeChecked`
(type-aware linting, wired via `tsconfig.json`) — this is the layer that
catches an un-awaited `Promise` in an async function or an `any` value's
unsafety propagating through a return, neither of which `tsc` alone flags.

Use [Zod](https://zod.dev/) for runtime boundary validation (parsing
untyped `JSON.parse`/API-response data into a typed shape) — the
TypeScript analog to pydantic/serde. TypeScript's type system is
compile-time only; it does not validate data at runtime on its own.
