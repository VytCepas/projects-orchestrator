---
description: Node.js / Bun environment and tooling
globs: ["**/*.ts", "**/*.tsx", "**/*.js", "package.json", "bun.lockb"]
alwaysApply: false
---

## Node.js environment

Uses [`bun`](https://bun.sh). Never `npm` or `npx`.

```bash
bun install       # install deps
bun run <script>  # run a package.json script
bunx <package>    # run a binary (replaces npx)
bun run lint      # lint
bun test          # tests
```
