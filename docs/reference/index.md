# Reference

Information-oriented technical description: API signatures, configuration
keys, CLI flags. Reference states facts — it does not explain or instruct.

For Python projects, render API docs from docstrings with a
[mkdocstrings](https://mkdocstrings.github.io/) directive:

```markdown
::: your_package.your_module
```

## Pages

- [Descriptor contract v1](descriptor-contract-v1.md) — the project-init
  surfaces the orchestrator reads (config, scaffold manifest, git hooks,
  memory), pinned as a versioned boundary.

*Add reference pages as `docs/reference/<area>.md` and link them here.*
