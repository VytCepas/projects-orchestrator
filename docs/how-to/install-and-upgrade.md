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
cd projects-orchestrator
just install            # dry run: source, commit, target, command, and what --apply would refuse
just install --apply    # uv tool install --reinstall <this checkout>, then --check
projects-orchestrator --version
```

`just install` with no flag writes nothing. `--apply` installs only from the
main worktree, on a clean `main` in sync with `origin/main` (it fetches first),
and not from inside a Claude Code session. A branch or an uncommitted edit is
code nobody reviewed, and uv records the source path in its receipt, so a linked
worktree's path would outlive its branch. After installing, `--apply` runs the
check below and fails if the build does not match `HEAD`.

`uv tool install` builds a copy of the checkout into its own environment. The
command on your `PATH` runs that copy, never the working tree. See the next
section for what that means.

## Upgrade

```sh
git -C projects-orchestrator pull --ff-only
just install --apply
```

**The reinstall is not optional.** A merged fix is not a deployed fix. Pulling
updates the checkout, but the installed copy keeps running the old code until it
is rebuilt. So `git log` shows the fix, the tests pass, and the command on your
`PATH` still behaves as before. `just install --apply` always passes
`--reinstall`, so uv rebuilds even when the version has not moved.

No state migration is needed in either direction. The cache is versioned, and a
build refuses to overwrite a newer build's file rather than discarding it (see
[Operate the orchestrator's own state](operations.md#migrating-between-versions)).

## Check the installed build

```sh
just install --check
```

This compares the installed package files with the checkout's `HEAD` and exits 1
on drift, naming each file: `modified`, `missing`, or `not in tree`. It also
fails when uv's receipt names another checkout, or when the entrypoint on `PATH`
does not resolve into the tool's environment. It compares against `HEAD`, not
the working tree, so an uncommitted edit is not drift but a pull without a
reinstall is. `--version` cannot tell you this: it does not move between
releases.

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
3. Merge, then `git tag vX.Y.Z && git push origin vX.Y.Z`. Push that one tag, not
   `--tags`: every `v*` tag that reaches GitHub starts a release run. The release
   workflow builds the package and cuts a GitHub Release.
