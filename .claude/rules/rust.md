---
description: Rust environment and tooling
globs: ["**/*.rs", "Cargo.toml", "Cargo.lock"]
alwaysApply: false
---

## Rust environment

```bash
cargo build
cargo test
cargo llvm-cov --fail-under-lines 70              # tests + coverage gate — CI always runs this (`just test-cov`)
cargo audit                                       # dependency CVE/advisory scan (`just audit`) — CI always runs this
cargo cyclonedx --format json                     # CycloneDX SBOM (`just sbom`, #574) — release.yml attaches it to Releases
cargo deny check licenses                         # license compliance (`just license`, #579) — allow-list in deny.toml (denies GPL/AGPL)
cargo test                                        # property-based tests with proptest (`just fuzz`, #580)
cargo clippy -- -D warnings -D clippy::pedantic   # pedantic + cognitive-complexity gate per clippy.toml
cargo fmt --check                                 # verifies only; `cargo fmt` (no flag) writes changes
```

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

`cargo llvm-cov` needs `cargo install cargo-llvm-cov` + `rustup component add
llvm-tools-preview` once locally; `cargo audit` only needs `cargo install
cargo-audit` (no llvm-tools-preview — that component is specific to
llvm-cov's instrumentation, not the advisory-database lookup `cargo audit`
does). CI installs both binaries per-run (prebuilt, not a source compile).

The compiler is the type checker — no separate strict-mode gate needed.
`-D warnings` (`.cargo/config.toml`) is the Rust analog to `mypy --strict` /
tsconfig `"strict": true`. Use [`serde`](https://serde.rs/) for structured
data validation — the Rust analog to pydantic/Zod.
