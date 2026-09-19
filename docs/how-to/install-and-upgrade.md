# Install and upgrade the orchestrator itself

This guide is about the `projects-orchestrator` tool on your machine. It is not
about the projects the tool watches.

> **Not `upgrade-plan`.** `projects-orchestrator upgrade-plan` reports which
> *child projects* are behind upstream project-init, and `--apply` dispatches
> *their* upgrade workflows. It never touches the orchestrator itself, and it
> says nothing about this tool's own version.

## Where it installs from

The tool is **not on PyPI yet**. `pypi.org/pypi/projects-orchestrator` answers
404, and the release workflow's publish step is off until the repository
variable `PUBLISH_ENABLED` is set (see the header of
`.github/workflows/release.yml`). Until then, the install source is a checkout of
this repository.

## Install

```sh
git clone https://github.com/VytCepas/projects-orchestrator.git
uv tool install ./projects-orchestrator
projects-orchestrator --version
```

`uv tool install` builds a copy of the checkout into its own environment. The
command on your `PATH` runs that copy, never the working tree. See the next
section for what that means.

## Upgrade

```sh
git -C projects-orchestrator pull --ff-only
uv tool install --reinstall ./projects-orchestrator
projects-orchestrator --version
```

**`--reinstall` is not optional.** A merged fix is not a deployed fix. Pulling
updates the checkout, but the installed copy keeps running the old code until it
is rebuilt. So `git log` shows the fix, the tests pass, and the command on your
`PATH` still behaves as before. Check `--version` after reinstalling. Between
releases the version does not move, so also confirm the fix's behaviour.

No state migration is needed in either direction. The cache is versioned, and a
build refuses to overwrite a newer build's file rather than discarding it (see
[Operate the orchestrator's own state](operations.md#migrating-between-versions)).

## What changed between versions

[CHANGELOG.md](../../CHANGELOG.md) lists the changes per release. Unreleased
work is at the top.

## The version

The version has one source, `__version__` in
`src/projects_orchestrator/__init__.py`. `pyproject.toml` declares it `dynamic`,
so the built wheel reads the same literal that `--version` prints. To cut a
release:

1. Move the `Unreleased` entries in `CHANGELOG.md` under the new version heading.
2. Set `__version__` to that version. `tests/test_version.py` fails if the two
   disagree.
3. Merge, then `git tag vX.Y.Z && git push --tags`. The release workflow builds
   the package and cuts a GitHub Release.
