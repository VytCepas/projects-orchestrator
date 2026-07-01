"""Discover and index project-init projects beneath a root directory.

The registry is the orchestrator's "kernel view" of a fleet: it walks a tree,
finds every ``.claude/config.yaml``, and exposes the parsed descriptors for
lookup and filtering. Malformed projects are skipped rather than aborting the
whole scan, so one broken descriptor never blinds the orchestrator to the rest.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from projects_orchestrator.descriptor import (
    DescriptorError,
    ProjectDescriptor,
    load_descriptor,
)

_CONFIG_GLOB = "**/.claude/config.yaml"


def discover_projects(root: Path) -> list[ProjectDescriptor]:
    """Find every project-init project at or beneath ``root``.

    Args:
        root: Directory to scan recursively.

    Returns:
        Descriptors for each discoverable project, sorted by name. Projects with
        a missing or malformed descriptor are silently skipped.
    """
    descriptors: list[ProjectDescriptor] = []
    for config_path in root.glob(_CONFIG_GLOB):
        try:
            descriptors.append(load_descriptor(config_path))
        except DescriptorError:
            continue
    return sorted(descriptors, key=lambda d: d.name)


@dataclass
class Registry:
    """An indexed collection of discovered project descriptors.

    Attributes:
        projects: Descriptors, conventionally sorted by name.
    """

    projects: list[ProjectDescriptor]

    @classmethod
    def discover(cls, root: Path) -> Registry:
        """Build a registry by discovering projects beneath ``root``.

        Args:
            root: Directory to scan recursively.

        Returns:
            A populated :class:`Registry`.
        """
        return cls(discover_projects(root))

    def names(self) -> list[str]:
        """Return the names of all indexed projects.

        Returns:
            Project names in registry order.
        """
        return [d.name for d in self.projects]

    def get(self, name: str) -> ProjectDescriptor | None:
        """Look up a project by name.

        Args:
            name: Project name to find.

        Returns:
            The matching descriptor, or ``None`` if no project has that name.
        """
        return next((d for d in self.projects if d.name == name), None)

    def by_language(self, language: str) -> list[ProjectDescriptor]:
        """Filter projects by primary language.

        Args:
            language: Language to match (e.g. ``python``).

        Returns:
            Descriptors whose ``language`` equals ``language``.
        """
        return [d for d in self.projects if d.language == language]

    def __len__(self) -> int:
        """Return the number of indexed projects."""
        return len(self.projects)

    def __iter__(self) -> Iterator[ProjectDescriptor]:
        """Iterate over indexed projects."""
        return iter(self.projects)
