---
description: Go environment and tooling
globs: ["**/*.go", "go.mod", "go.sum"]
alwaysApply: false
---

## Go environment

```bash
go build ./...
go test ./... -count=1
just test-cov       # tests + coverage gate (threshold per justfile) — CI always runs this
just audit          # dependency CVE/advisory scan (govulncheck) — CI always runs this
just sbom           # CycloneDX SBOM via cyclonedx-gomod (#574) — release.yml attaches it to Releases
just license        # dependency license scan (#579) — deny copyleft; tune --disallowed_types
just fuzz           # replay fuzz-test seed corpora: `go test -run=Fuzz` (#580) — NOT active `-fuzz`; CI runs it nightly, not per-PR
just typecheck      # `go vet ./...` — type-only pass; redundant with golangci-lint by design (#725)
golangci-lint run   # revive, godoclint, gocognit, cyclop, dupl, errcheck, govet, staticcheck, gosec — see .golangci.yml
                    # ALSO the format gate: `formatters: gofumpt` in .golangci.yml makes `run` fail on unformatted code (#726)
golangci-lint fmt   # WRITES gofumpt formatting (stricter than gofmt) — no separate binary needed
```

## Fuzzing (native `go test -fuzz`, #580)

Built into the toolchain (go 1.18+), no dependency. Write a `FuzzXxx(f *testing.F)`
with `f.Add(seed)` + `f.Fuzz(func(t, in){...})`. `just fuzz` replays the seed
corpora deterministically (CI-safe); active fuzzing targets one function:

```bash
go test -run='^$' -fuzz=FuzzMyTarget -fuzztime=30s ./...
```

Pattern/tooling, **not** a blocking gate. **When it runs:** the CI `fuzz` job is
schedule-only (nightly) and non-blocking — never on a PR (#727). Seed replay is
deterministic, so repeating it nightly surfaces no *new* inputs the way
Hypothesis/proptest/fast-check do — it still consumes CI minutes. It runs nightly
to keep the four languages uniform. Run `just fuzz` locally whenever you touch a
fuzz target.

`govulncheck` (`go install golang.org/x/vuln/cmd/govulncheck@latest`) only
reports vulnerabilities actually reachable from your code, not every
advisory touching a transitive dependency — a lower false-positive rate than
a plain dependency-list scan.
