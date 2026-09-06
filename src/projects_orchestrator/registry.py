"""Discover the fleet: which projects the orchestrator governs.

Sources, in precedence order:

1. An explicit fleet file (``fleet.yaml``) listing project paths and/or
   scan roots.
2. Fallback: scan the parent directory of the orchestrator checkout —
   the conventional ``~/projects/<name>`` sibling layout.

Discovery never raises: unreadable directories are skipped, non-projects
(no ``.agents/config.yaml``, nor a legacy ``.claude/`` one) are ignored,
duplicates collapse by resolved
path, and the result is sorted by name for stable rendering.
"""

from __future__ import annotations

import fnmatch
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml

from projects_orchestrator.adapters.generic import infer_descriptor
from projects_orchestrator.descriptor import ProjectDescriptor, load_descriptor, resolve_config

FLEET_FILENAME = "fleet.yaml"


@dataclass(frozen=True)
class FleetConfig:
    """Where to look for projects.

    Attributes:
        roots: Directories scanned one level deep for projects.
        projects: Explicit project paths (used even when not under a root).
        exclude: ``fnmatch`` patterns on directory names to skip.
        include_plain_repos: Also govern git repos without a project-init
            descriptor, via conservative inference (off by default).
        source: The fleet file this config came from, if any.
    """

    roots: tuple[Path, ...] = ()
    projects: tuple[Path, ...] = ()
    exclude: tuple[str, ...] = ()
    include_plain_repos: bool = False
    source: Path | None = None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class Fleet:
    """The discovered fleet.

    Attributes:
        descriptors: One descriptor per discovered project, sorted by name.
        config: The configuration used for discovery.
        warnings: Non-fatal discovery problems worth showing the operator.
    """

    descriptors: tuple[ProjectDescriptor, ...]
    config: FleetConfig
    warnings: tuple[str, ...] = ()

    def get(self, name: str) -> ProjectDescriptor | None:
        """Return the project named ``name`` (case-insensitive), if present."""
        lowered = name.lower()
        for descriptor in self.descriptors:
            if descriptor.name.lower() == lowered:
                return descriptor
        return None

    @property
    def names(self) -> tuple[str, ...]:
        """Names of all discovered projects."""
        return tuple(d.name for d in self.descriptors)


def _resolve(base: Path, value: str) -> Path:
    """Resolve ``value`` (may be ``~`` or relative) against ``base``."""
    path = Path(value).expanduser()
    return path if path.is_absolute() else (base / path).resolve()


def load_fleet_config(fleet_file: Path) -> FleetConfig:
    """Parse a fleet file; never raises.

    Args:
        fleet_file: Path to a ``fleet.yaml``.

    Returns:
        The parsed config; unreadable/invalid files yield an empty config
        whose ``source`` is still set so callers can report it.
    """
    base = fleet_file.parent.resolve()
    warnings: tuple[str, ...] = ()
    try:
        raw = yaml.safe_load(fleet_file.read_text(encoding="utf-8", errors="replace"))
    except OSError as exc:
        # A misspelled or unreadable --fleet path otherwise looks exactly like
        # an empty fleet (no projects, exit 0). Surface it instead.
        raw, warnings = None, (f"cannot read fleet file {fleet_file}: {exc}",)
    except yaml.YAMLError as exc:
        raw, warnings = None, (f"invalid fleet file {fleet_file}: {exc}",)
    raw = raw if isinstance(raw, dict) else {}

    def paths(key: str) -> tuple[Path, ...]:
        values = raw.get(key)
        if not isinstance(values, list):
            return ()
        return tuple(_resolve(base, str(v)) for v in values if isinstance(v, (str, Path)))

    exclude = raw.get("exclude")
    return FleetConfig(
        roots=paths("roots"),
        projects=paths("projects"),
        exclude=tuple(str(p) for p in exclude) if isinstance(exclude, list) else (),
        include_plain_repos=bool(raw.get("include_plain_repos", False)),
        source=fleet_file,
        warnings=warnings,
    )


FLEET_ROOT_ENV = "PO_FLEET_ROOT"


def default_fleet_config(
    cwd: Path | None = None, env: Mapping[str, str] | None = None
) -> FleetConfig:
    """Build the config used when no fleet file exists.

    Args:
        cwd: Directory to anchor discovery at (defaults to the process cwd).
        env: Environment to read ``PO_FLEET_ROOT`` from (defaults to the real one).

    Returns:
        ``fleet.yaml`` in ``cwd`` when present; else ``$PO_FLEET_ROOT`` when set
        to a directory; else a config scanning the parent directory of ``cwd``
        (the sibling-checkout convention).

    ``PO_FLEET_ROOT`` IS READ HERE BECAUSE IT WAS ALREADY ADVERTISED (#204). The
    `watch` failure message told the operator to check it and nothing in the
    codebase read it — it existed only in the docs' cron recipes, where the
    SHELL expands it into `--root "$PO_FLEET_ROOT"`. So the one piece of advice
    offered at the moment of confusion was a dead end.

    It also fixes the cwd-dependence that made the advice necessary. With the
    orchestrator installed as a `uv tool` there is no checkout to sit beside, so
    a flat `$HOME/<repo>` layout resolved the whole fleet from inside any
    governed repo and NOTHING from `$HOME` itself, where `cwd.parent` is
    `/Users` or `/home`. An exported root is cwd-independent, which is the
    property that was missing.

    Precedence puts the file first on purpose: a `fleet.yaml` in the directory
    is a more specific statement than an environment default.
    """
    cwd = (cwd or Path.cwd()).resolve()
    fleet_file = cwd / FLEET_FILENAME
    if fleet_file.is_file():
        return load_fleet_config(fleet_file)
    source = os.environ if env is None else env
    declared = (source.get(FLEET_ROOT_ENV) or "").strip()
    if declared:
        candidate = Path(declared).expanduser()
        if candidate.is_dir():
            return FleetConfig(roots=(candidate.resolve(),))
        # Set but unusable. Saying so beats silently scanning somewhere else and
        # reporting an empty fleet the operator cannot explain.
        return FleetConfig(
            roots=(cwd.parent,),
            warnings=(f"{FLEET_ROOT_ENV}={declared!r} is not a directory — ignoring it",),
        )
    return FleetConfig(roots=(cwd.parent,))


def _excluded(name: str, patterns: tuple[str, ...]) -> bool:
    """Return whether a directory name matches any exclude pattern."""
    return any(fnmatch.fnmatch(name, pattern) for pattern in patterns)


#: How many levels BELOW a scanned root the skipped-project hint looks.
#:
#: DISCOVERY ITSELF STAYS ONE LEVEL DEEP. That is the documented contract
#: (:attr:`FleetConfig.roots`), and widening it would silently change fleet
#: membership on every box that has a root with repos underneath — an unreviewed
#: change to *what the fleet is*. #215's own falsifier says so: "refuted if
#: depth-1 is documented as a hard contract — in which case the skipped count is
#: still the fix, because silence is indistinguishable from absence." So this
#: adds the accounting, not the recursion.
_HINT_DEPTH = 3

#: Directories the hint visits before it stops looking. The accounting must never
#: cost more than the scan it annotates, and one `node_modules` would see to that.
#: On exhaustion the count is reported as a lower bound rather than a total — an
#: undercount that says it is one is honest; one that does not is worse than none.
_HINT_BUDGET = 2000

#: Never descended into by the hint: build output and vendored trees, which hold
#: no governed project and plenty of directories. Dotted names are skipped too
#: (so `.git` and `.venv` need no entry here), which does mean a project hidden
#: under a dotted directory goes uncounted — stated rather than papered over.
_HINT_SKIP_DIRS = frozenset(
    {"node_modules", "venv", "target", "dist", "build", "__pycache__", "site-packages"}
)


def _is_project_dir(path: Path, config: FleetConfig) -> bool:
    """Whether :func:`discover` would admit ``path`` as a project (same test)."""
    if resolve_config(path) is not None:
        return True
    return config.include_plain_repos and (path / ".git").is_dir()


def _nested_projects(root: Path, config: FleetConfig) -> tuple[list[Path], bool]:
    """Governed projects under ``root`` that the one-level scan cannot reach.

    Returns the paths found (sorted) and whether the visit budget ran out, so a
    caller can report a lower bound instead of claiming a total.
    """
    found: list[Path] = []
    # (directory, depth-below-root). Depth 1 is what discovery already scans, so
    # the hint starts by descending FROM depth 1 and reports depth >= 2.
    frontier: list[tuple[Path, int]] = [(root, 0)]
    visited = 0
    while frontier:
        current, depth = frontier.pop()
        if depth >= _HINT_DEPTH:
            continue
        try:
            children = sorted(c for c in current.iterdir() if c.is_dir())
        except OSError:
            continue  # unreadable subtree is not this function's problem to report
        for child in children:
            if visited >= _HINT_BUDGET:
                return sorted(found), True
            visited += 1
            name = child.name
            if name.startswith(".") or name in _HINT_SKIP_DIRS:
                continue
            if _excluded(name, config.exclude):
                continue
            if depth + 1 >= 2 and _is_project_dir(child, config):
                found.append(child)
            frontier.append((child, depth + 1))
    return sorted(found), False


def _scan_root(root: Path, config: FleetConfig, warnings: list[str]) -> list[Path]:
    """List candidate project directories one level under ``root``.

    Also ACCOUNTS for governed projects nested deeper than that. They stay
    undiscovered — see :data:`_HINT_DEPTH` for why the depth is not widened — but
    they are no longer invisible, which was the actual defect (#215): a
    well-formed project reachable via ``projects:`` appeared in none of the
    verbs and produced no warning, so an operator could not tell "not there"
    from "not looked at".
    """
    try:
        entries = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError as exc:
        warnings.append(f"cannot scan root {root}: {exc}")
        return []
    candidates = [p for p in entries if not _excluded(p.name, config.exclude)]
    nested, truncated = _nested_projects(root, config)
    if nested:
        shown = ", ".join(str(p) for p in nested[:5])
        more = f" (+{len(nested) - 5} more)" if len(nested) > 5 else ""
        count = f"at least {len(nested)}" if truncated else str(len(nested))
        warnings.append(
            f"{count} project(s) under {root} are nested deeper than the one level "
            f"discovery scans and were NOT discovered — list them under `projects:` "
            f"to govern them: {shown}{more}"
        )
    elif truncated:
        # NOTHING FOUND *AND* THE SEARCH WAS INCOMPLETE. Reporting nothing here
        # would put the operator back in exactly the state #215 describes:
        # silence indistinguishable from absence. Having stopped looking is not
        # the same fact as there being nothing to find, so it is said out loud.
        warnings.append(
            f"stopped looking for nested projects under {root} after "
            f"{_HINT_BUDGET} directories — there may be projects nested deeper "
            f"than the one level discovery scans; list any under `projects:`"
        )
    return candidates


def discover(config: FleetConfig) -> Fleet:
    """Discover every project the config points at; never raises.

    Args:
        config: Roots, explicit paths, and exclusions to use.

    Returns:
        The fleet, with warnings for paths that were configured but are
        not usable projects.
    """
    warnings: list[str] = list(config.warnings)
    candidates: list[Path] = list(config.projects)
    for root in config.roots:
        candidates.extend(_scan_root(root, config, warnings))

    seen: set[Path] = set()
    found: list[ProjectDescriptor] = []
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        descriptor = load_descriptor(resolved)
        if descriptor is None and config.include_plain_repos:
            descriptor = infer_descriptor(resolved)
        if descriptor is None:
            if candidate in config.projects:
                warnings.append(f"not a project-init project: {resolved}")
            continue
        found.append(descriptor)

    found.sort(key=lambda d: d.name.lower())
    warnings.extend(_duplicate_name_warnings(found))
    return Fleet(descriptors=tuple(found), config=config, warnings=tuple(warnings))


@dataclass(frozen=True)
class RegisterOutcome:
    """The result of registering a project path into a fleet file.

    Attributes:
        fleet_file: The fleet file that was written (or would have been).
        project: The resolved project path.
        added: Whether the path was newly added (``False`` when already listed).
        warnings: Non-fatal problems (an unreadable existing fleet file).
    """

    fleet_file: Path
    project: Path
    added: bool
    warnings: tuple[str, ...] = ()


def register_project(fleet_file: Path, project: Path) -> RegisterOutcome:
    """Add a project path to a fleet file's ``projects:`` list; never raises.

    Consumes the ``scaffold --json`` seam: a freshly-scaffolded project is
    registered into the orchestrator's own fleet file (not the child tree —
    ADR-003 forbids writing to children, not to the orchestrator's registry)
    so the next ``discover`` governs it without a manual edit. Idempotent: a
    path already listed is left as-is.

    Args:
        fleet_file: The fleet file to write (created when absent).
        project: The project root to register.

    Returns:
        A :class:`RegisterOutcome`; a write failure surfaces as a warning with
        ``added=False`` rather than an exception.
    """
    resolved = project.resolve()
    existing = load_fleet_config(fleet_file) if fleet_file.is_file() else None
    warnings = existing.warnings if existing is not None else ()
    listed = {p.resolve() for p in (existing.projects if existing is not None else ())}
    if resolved in listed:
        return RegisterOutcome(fleet_file, resolved, added=False, warnings=warnings)

    projects = sorted({*listed, resolved}, key=str)
    document: dict[str, object] = {
        "projects": [str(p) for p in projects],
        "roots": [str(p) for p in (existing.roots if existing is not None else ())],
    }
    # Preserve fields the loader treats as first-class but this rewrite would
    # otherwise silently drop — an omitted `exclude` re-admits excluded repos and
    # a dropped `include_plain_repos` flips discovery, both invisibly.
    if existing is not None and existing.exclude:
        document["exclude"] = list(existing.exclude)
    if existing is not None and existing.include_plain_repos:
        document["include_plain_repos"] = existing.include_plain_repos
    try:
        fleet_file.parent.mkdir(parents=True, exist_ok=True)
        fleet_file.write_text(yaml.safe_dump(document, sort_keys=True), encoding="utf-8")
    except OSError as exc:
        return RegisterOutcome(
            fleet_file,
            resolved,
            added=False,
            warnings=(*warnings, f"cannot write {fleet_file}: {exc}"),
        )
    return RegisterOutcome(fleet_file, resolved, added=True, warnings=warnings)


def _duplicate_name_warnings(found: list[ProjectDescriptor]) -> list[str]:
    """Warn when two discovered projects share a name.

    Discovery dedupes by resolved path, but the cache, supervisor state, and
    name lookups are all keyed by name — so a collision silently merges two
    projects' results and makes only one addressable. Surface it.
    """
    counts: dict[str, int] = {}
    for descriptor in found:
        counts[descriptor.name] = counts.get(descriptor.name, 0) + 1
    return [
        f"duplicate project name '{name}' ({count} paths) — only one is addressable by name"
        for name, count in sorted(counts.items())
        if count > 1
    ]
