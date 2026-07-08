"""Fleet setup-readiness checklist with concrete next actions.

``audit`` answers whether governance signals need attention. ``hardening`` is
more operator-focused: it groups the common onboarding gaps that keep a local
fleet from being governable at all — inactive git hooks, absent memory, and no
cached gate results — and prints the command or file action that moves each
project forward. It is read-only and never raises, like the rest of the engine.
"""

from __future__ import annotations

from dataclasses import dataclass

from projects_orchestrator.checks import DEFAULT_TASKS, CheckResult
from projects_orchestrator.descriptor import ProjectDescriptor
from projects_orchestrator.drift import hook_health
from projects_orchestrator.memory import load_project_memory

WARN = "warn"


@dataclass(frozen=True)
class HardeningItem:
    """One setup gap and its suggested next action."""

    category: str
    status: str
    detail: str
    action: str


@dataclass(frozen=True)
class HardeningReport:
    """Hardening checklist for one project."""

    project: str
    items: tuple[HardeningItem, ...] = ()

    @property
    def needs_attention(self) -> bool:
        """Whether this project has any setup gaps."""
        return bool(self.items)


def _hook_item(descriptor: ProjectDescriptor) -> HardeningItem | None:
    """Return a hook-installation item when enforcement is inactive."""
    health = hook_health(descriptor)
    if health not in {"missing", "partial"}:
        return None
    return HardeningItem(
        category="hooks",
        status=WARN,
        detail=f"git hooks {health}; local enforcement is inactive",
        action=f"run {descriptor.path / '.claude/scripts/install_hooks.sh'}",
    )


def _memory_items(descriptor: ProjectDescriptor) -> list[HardeningItem]:
    """Return memory setup items for one project."""
    memory = load_project_memory(descriptor)
    if memory.memory_path is None or not memory.memory_path.is_dir():
        return [
            HardeningItem(
                category="memory",
                status=WARN,
                detail="memory directory is missing",
                action=f"create {descriptor.path / '.claude/memory'} with MEMORY.md",
            )
        ]
    if not memory.index_present:
        return [
            HardeningItem(
                category="memory",
                status=WARN,
                detail="MEMORY.md index is missing",
                action=f"add {memory.memory_path / 'MEMORY.md'}",
            )
        ]
    return []


def _checks_item(
    descriptor: ProjectDescriptor, cached: dict[str, CheckResult] | None
) -> HardeningItem | None:
    """Return a check-cache item when no gate result has ever been recorded."""
    if cached and any(task in cached for task in DEFAULT_TASKS):
        return None
    return HardeningItem(
        category="checks",
        status=WARN,
        detail="no cached lint/test gate results",
        action=f"run projects-orchestrator checks {descriptor.name}",
    )


def project_checklist(
    descriptor: ProjectDescriptor, cached: dict[str, CheckResult] | None = None
) -> HardeningReport:
    """Build the hardening checklist for one project."""
    items: list[HardeningItem] = []
    if (hook_item := _hook_item(descriptor)) is not None:
        items.append(hook_item)
    items.extend(_memory_items(descriptor))
    if (checks_item := _checks_item(descriptor, cached)) is not None:
        items.append(checks_item)
    return HardeningReport(project=descriptor.name, items=tuple(items))


def checklist(
    descriptors: list[ProjectDescriptor],
    cached: dict[str, dict[str, CheckResult]],
) -> list[HardeningReport]:
    """Build hardening checklists for the fleet."""
    return [project_checklist(descriptor, cached.get(descriptor.name)) for descriptor in descriptors]


def render_text(reports: list[HardeningReport]) -> str:
    """Render reports as grouped text with concrete next actions."""
    if not reports:
        return "no projects discovered"
    if not any(report.needs_attention for report in reports):
        return "hardening checklist clean — fleet is ready"
    lines: list[str] = []
    for report in reports:
        if not report.items:
            lines.append(f"{report.project}: ok")
            continue
        lines.append(f"{report.project}:")
        lines.extend(
            f"  {item.category}: {item.detail} — {item.action}" for item in report.items
        )
    return "\n".join(lines)
