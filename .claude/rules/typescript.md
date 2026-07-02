---
description: TypeScript strict type-checking conventions
globs: ["**/*.ts", "**/*.tsx", "tsconfig.json", "tsconfig.base.json"]
alwaysApply: false
---

## TypeScript environment

```bash
bunx tsc --noEmit   # type check (strict mode, per tsconfig.base.json)
bunx eslint .        # lint (type-aware: no-floating-promises, no-unsafe-*, per eslint.config.mjs)
bun test             # tests + coverage gate (>= 70%, per bunfig.toml) — always on, no extra flag needed
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
