---
description: Rust environment and tooling
globs: ["**/*.rs", "Cargo.toml", "Cargo.lock"]
alwaysApply: false
---

## Rust environment

```bash
cargo build
cargo test
just test-cov                                     # tests + coverage gate (threshold + --fail-under-lines per justfile) — bare `cargo llvm-cov` only reports, doesn't gate; CI always runs this
cargo audit                                       # dependency CVE/advisory scan (`just audit`) — CI always runs this
cargo cyclonedx --format json                     # CycloneDX SBOM (`just sbom`, #574) — release.yml attaches it to Releases
cargo deny check licenses                         # license compliance (`just license`, #579) — allow-list in deny.toml (denies GPL/AGPL)
cargo test                                        # property-based tests with proptest (`just fuzz`, #580)
cargo clippy --all-features -- -D warnings -D clippy::pedantic -D clippy::cognitive_complexity -D missing_docs   # lint + complexity + public-API docs (lib/bin)
cargo clippy --all-targets --all-features -- -D warnings -D clippy::pedantic -D clippy::cognitive_complexity     # also tests/, benches/, examples/ (#725)
cargo check --all-targets --all-features          # `just typecheck` — type-only pass, no codegen
cargo fmt --check                                 # format gate — part of `just lint`, CI runs it (#726); `cargo fmt` writes
```

## Enforced complexity and docs (parity with Python's ruff gate)

`just lint` denies two extra lints beyond `-D pedantic`:

- `clippy::cognitive_complexity` — a nursery lint (so **not** enabled by
  `-D pedantic` on its own) that activates the `cognitive-complexity-threshold`
  in `clippy.toml`. Mirrors ruff's `max-complexity = 10`.
- `missing_docs` — every public item needs a doc comment (`///`), and every
  crate needs a crate-level `//!` doc. This is the Rust analog to ruff's `D`
  gate (which requires module + public-symbol docstrings) — so a fresh
  `cargo init` project needs a `//! …` line at the top of `main.rs`/`lib.rs`
  before `just lint` passes, exactly as a new Python module needs a docstring.
  Private items are exempt.

## Property-based testing (proptest, #580)

Add once with `cargo add --dev proptest`; opt-in per file via the `proptest!`
macro, runs under `cargo test` (`just fuzz`). Generates edge-case inputs a
hand-written test wouldn't try:

```rust
proptest! {
    #[test]
    fn clamp_stays_within_bounds(x in any::<i32>(), lo in -1000i32..=0, hi in 1i32..=1000) {
        let r = clamp(x, lo, hi);
        prop_assert!(lo <= r && r <= hi);   // a true invariant; proptest probes x < lo
    }
}
```

Pattern/tooling, **not** a blocking gate — property tests live alongside unit tests.
For coverage-guided binary fuzzing, `cargo-fuzz` (libFuzzer, nightly) is the
heavier option; proptest is the stable-toolchain default.

**When it runs:** the CI `fuzz` job is schedule-only (nightly) and non-blocking —
never on a PR (#727). proptest draws fresh seeds each run, so nightly explores
inputs a per-PR run would only repeat. Run `just fuzz` locally at will.

`cargo llvm-cov` needs `cargo install cargo-llvm-cov` + `rustup component add
llvm-tools-preview` once locally; `cargo audit` only needs `cargo install
cargo-audit` (no llvm-tools-preview — that component is specific to
llvm-cov's instrumentation, not the advisory-database lookup `cargo audit`
does). CI installs both binaries per-run (prebuilt, not a source compile).

The compiler is the type checker — no separate strict-mode gate needed.
`-D warnings` (`.cargo/config.toml`) is the Rust analog to `mypy --strict` /
tsconfig `"strict": true`. Use [`serde`](https://serde.rs/) for structured
data validation — the Rust analog to pydantic/Zod.
