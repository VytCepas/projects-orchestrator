---
description: Go environment and tooling
globs: ["**/*.go", "go.mod", "go.sum"]
alwaysApply: false
---

## Go environment

```bash
go build ./...
go test ./... -count=1
golangci-lint run
gofmt -w .
```
