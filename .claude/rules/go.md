---
description: Go environment and tooling
globs: ["**/*.go", "go.mod", "go.sum"]
alwaysApply: false
---

## Go environment

```bash
go build ./...
go test ./... -count=1
just test-cov       # tests + coverage gate (>= 70%, per justfile) — CI always runs this
just audit          # dependency CVE/advisory scan (govulncheck) — CI always runs this
just sbom           # CycloneDX SBOM via cyclonedx-gomod (#574) — release.yml attaches it to Releases
just license        # dependency license scan (#579) — deny copyleft; tune --disallowed_types
just fuzz           # replay fuzz-test seed corpora (#580) — native go test -fuzz, CI-safe
golangci-lint run   # revive, godoclint, gocognit, cyclop, dupl, errcheck, govet, staticcheck, gosec — see .golangci.yml
golangci-lint fmt   # gofumpt (stricter than gofmt) — no separate binary needed
```

## Fuzzing (native `go test -fuzz`, #580)

Built into the toolchain (go 1.18+), no dependency. Write a `FuzzXxx(f *testing.F)`
with `f.Add(seed)` + `f.Fuzz(func(t, in){...})`. `just fuzz` replays the seed
corpora deterministically (CI-safe); active fuzzing targets one function:

```bash
go test -run='^$' -fuzz=FuzzMyTarget -fuzztime=30s ./...
```

Pattern/tooling, **not** a blocking gate — the CI `fuzz` job runs seed-corpus
replay only and is non-blocking.

`govulncheck` (`go install golang.org/x/vuln/cmd/govulncheck@latest`) only
reports vulnerabilities actually reachable from your code, not every
advisory touching a transitive dependency — a lower false-positive rate than
a plain dependency-list scan.
