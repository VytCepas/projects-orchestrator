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
just sbom         # CycloneDX SBOM via cdxgen (#574) — release.yml attaches it to Releases
just license      # dependency license scan (#579) — deny GPL/AGPL; tune the recipe's --failOn list
just fuzz         # property-based tests with fast-check (#580) — runs under `bun test`
```

## Property-based testing (fast-check, #580)

Add once with `bun add -d fast-check`; opt-in per file, runs under `bun test`
(`just fuzz`). Generates edge-case inputs a hand-written test wouldn't try:

```ts
import fc from "fast-check";
import { test } from "bun:test";

test("clamp stays within bounds", () => {
  fc.assert(fc.property(fc.integer(), fc.integer(), fc.integer(), (a, b, x) => {
    const [lo, hi] = a <= b ? [a, b] : [b, a];
    const r = clamp(x, lo, hi);
    return lo <= r && r <= hi;
  }));
});
```

Pattern/tooling, **not** a blocking gate — property tests live alongside unit tests.
