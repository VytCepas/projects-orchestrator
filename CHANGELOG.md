# Changelog

Notable changes to the `projects-orchestrator` tool itself. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

A release moves the `Unreleased` entries under a new version heading, and that
heading must equal `__version__` in `src/projects_orchestrator/__init__.py`, the
version's only source. `tests/test_version.py` fails when they disagree.

## [Unreleased]

### Added

- `--json` output is a frozen seam: versioned schemas under `schemas/`, golden
  fixtures, and producer-side validation (#230).
- `watch` reports when its own timer stops, instead of going quiet (#244).
- The never-raise contract is tested module by module (#243). The nightly fuzz
  gate now explores inputs rather than passing on none (#245).
- `tech-debt` and `spike` are first-class issue types (#250).
- `--verbose` (or `PROJECTS_ORCHESTRATOR_VERBOSE=1`) logs every error a
  degraded path swallowed, and `upgrade-plan` names the reason for each
  `unknown` row. An unexpected internal error exits 70 with one line instead of
  a traceback (#269).
- A scheduled heal posts what it did, or why it failed, to `PO_HEAL_WEBHOOK`.
  A clean pass posts nothing, and the healed PR stays a draft (#276).
- The integration job runs real tests: the installed CLI over real git
  repositories (#274).

### Changed

- The version is single-sourced from `__init__.py` (#191).
- `memory` search ranks hits by BM25 and matches query terms separately, rather
  than scoring every substring hit alike (#264).
- Fleet discovery no longer counts a linked worktree as a separate project when
  its repository is already in the fleet (#261).
- The memory tier is derived from `memory.stack`. A contract-v1+ descriptor with
  no `memory:` block reads as memory declined (#258).
- Every cache and registry write goes through one hardened write path, which
  closes a registry race (#240). A cache written by a newer build is no longer
  read as empty and overwritten (#242).
- Every `gcloud` subprocess is pinned to a named identity rather than the ambient
  account.
- The scaffold is re-rendered from current project-init, taking its upstream
  security fixes (#202, #226, #233).

### Fixed

- An unresolved fleet reported clean and exited 0 (#239).
- One unreadable `.agents` directory blacked out the whole fleet (#234). A
  scanned project that stopped resolving dropped out silently (#235). A project
  nested more than one level deep was invisible (#227).
- A memory file in the documented format was typed `unknown` (#229).
- `doctor` named the wrong problem for a malformed contract version (#228), and
  hardening told a `memory: none` project to build memory (#238).
- Hook health read `missing` on every worktree while the hooks fired (#223).
- The review gate could not see a review that arrived as a comment (#231).
- The `work` briefing promised a gate re-run that `work` never runs (#256).
- Four Linux-only tests failed on macOS (#206). `PO_FLEET_ROOT` is read, and
  discovery says where it looked when it finds nothing (#207).
- The golden fixtures are regenerated with project-init 1.2.2 (#259).
- A retrieval surface declared below its memory-tier gate was dropped without a
  warning, and a descriptor path that could not be resolved aborted discovery
  of the whole fleet (#262).
- `doctor` called an explicit contract version of `0` absent (#265).
- An installed git hook older than its tracked source read `ok`. It reads
  `stale` now, and `doctor`, the hardening checklist and `watch` report it (#272).
- The URL guard let the IPv4-mapped cloud metadata address through on CPython
  3.12.3. It now unwraps the mapped address itself. The nightly mutation job
  copies every file the tests read (#273).
- A descriptor refused because it is a symlink was reported as "not a
  project-init project". The warning now names the refused link (#220).

### Security

- Descriptor-declared URLs pass a scheme and host allowlist (#225).
- A symlinked descriptor is refused (#200). A redirection bypass is closed, and
  the projection no longer deletes unmanaged files (#203).
- The Read-deny rules cover what the Bash guard already covers (#224).
- A test fails when the shipped or tracked tree names a private upstream
  repository; the shipped set is read from `pyproject.toml` (#267).

### Documentation

- The README documents every registered command, and a test keeps it that way
  (#246).
- Where the fleet's schedulers run, and that nothing schedules them on macOS
  (#252).
- Installing and upgrading the tool itself:
  [docs/how-to/install-and-upgrade.md](docs/how-to/install-and-upgrade.md)
  (#191).
- ADR citations say which repository owns the ADR, and a test fails on a
  citation that resolves to no ADR (#266).
- The descriptor contract no longer claims `config.yaml` is hash-covered (#275).

## [0.2.0] - 2026-07-13

The first release: the fleet engine (descriptor registry, checks, drift, memory
search, controller, TUI) and the project-init scaffold. Full notes:
[GitHub release v0.2.0](https://github.com/VytCepas/projects-orchestrator/releases/tag/v0.2.0).

[Unreleased]: https://github.com/VytCepas/projects-orchestrator/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/VytCepas/projects-orchestrator/releases/tag/v0.2.0
