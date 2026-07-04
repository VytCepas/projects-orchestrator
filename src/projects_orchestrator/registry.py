"""Discover the fleet: which projects the orchestrator governs.

Sources, in precedence order:

1. An explicit fleet file (``fleet.yaml``) listing project paths and/or
   scan roots.
2. Fallback: scan the parent directory of the orchestrator checkout —
   the conventional ``~/projects/<name>`` sibling layout.

Discovery never raises: unreadable directories are skipped, non-projects
(no ``.claude/config.yaml``) are ignored, duplicates collapse by resolved
path, and the result is sorted by name for stable rendering.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import Path

import yaml

from projects_orchestrator.adapters.generic import infer_descriptor
from projects_orchestrator.descriptor import ProjectDescriptor, load_descriptor

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


def default_fleet_config(cwd: Path | None = None) -> FleetConfig:
    """Build the config used when no fleet file exists.

    Args:
        cwd: Directory to anchor discovery at (defaults to the process cwd).

    Returns:
        ``fleet.yaml`` in ``cwd`` when present, else a config scanning the
        parent directory of ``cwd`` (the sibling-checkout convention).
    """
    cwd = (cwd or Path.cwd()).resolve()
    fleet_file = cwd / FLEET_FILENAME
    if fleet_file.is_file():
        return load_fleet_config(fleet_file)
    return FleetConfig(roots=(cwd.parent,))


def _excluded(name: str, patterns: tuple[str, ...]) -> bool:
    """Return whether a directory name matches any exclude pattern."""
    return any(fnmatch.fnmatch(name, pattern) for pattern in patterns)


def _scan_root(root: Path, config: FleetConfig, warnings: list[str]) -> list[Path]:
    """List candidate project directories one level under ``root``."""
    try:
        entries = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError as exc:
        warnings.append(f"cannot scan root {root}: {exc}")
        return []
    return [p for p in entries if not _excluded(p.name, config.exclude)]


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
