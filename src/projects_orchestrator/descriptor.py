"""Read a child project's machine-readable self-description.

Every project scaffolded by project-init ships a ``config.yaml`` descriptor
(contract v1): name, language, tooling commands, memory tier and path. It lives
under ``.agents/`` on a current scaffold (project-init PI-627) and under
``.claude/`` on a legacy one — :func:`resolve_config` finds either. The
orchestrator is a *reader* of that contract — it never invents a parallel one.
Parsing never raises; malformed input degrades to defaults and is surfaced
through :attr:`ProjectDescriptor.warnings`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Scaffold layout roots, most-current first. project-init PI-627 relocated the
# canonical tree from ``.claude/`` to ``.agents/`` and its ``.claude/`` projection
# deliberately EXCLUDES config.yaml/CAPABILITIES/memory (single source of truth),
# so a modern scaffold declares itself under ``.agents/``. Pre-PI-627 projects
# still ship ``.claude/config.yaml`` — read it as a legacy fallback.
_LAYOUT_DIRS: tuple[str, ...] = (".agents", ".claude")
_CONFIG_BASENAME = "config.yaml"

# Legacy alias kept for backward compatibility; new code resolves the layout via
# :func:`resolve_config` and reads paths off :attr:`ProjectDescriptor.config_root`.
CONFIG_RELPATH = Path(".claude") / _CONFIG_BASENAME


def resolve_config(project_dir: Path) -> tuple[Path, str] | None:
    """Locate a project's descriptor across scaffold layouts.

    Args:
        project_dir: Candidate project root.

    Returns:
        ``(config_path, config_root)`` — the readable ``config.yaml`` and the
        layout dir it lives in (``.agents`` preferred, ``.claude`` legacy) — or
        ``None`` when neither layout has a config.
    """
    for root in _LAYOUT_DIRS:
        candidate = project_dir / root / _CONFIG_BASENAME
        if candidate.is_file():
            return candidate, root
    return None


_TOOLING_SUFFIX = "_command"

CONTRACT_V2 = 2

DEPLOY_NONE = "none"

# Memory tier at which each higher-tier retrieval surface first appears
# (ADR-024 tier model, ADR-025 §4). A child only *emits* the field at/above
# its tier, so the orchestrator reads it tier-gated: anchors never move, higher
# tiers only add surfaces, and a tier-0 read stays correct against a tier-3 child.
TIER_VAULT = 1
TIER_GRAPH = 2
TIER_RAG = 3


@dataclass(frozen=True)
class DeployConfig:
    """Contract-v2 ``deploy:`` block for ``delivery: service`` projects.

    Attributes:
        target: Deploy target (``none`` | ``cloud-run`` | ``fly`` | ``k8s`` | …).
        app: App/service name at the target.
        region: Target region, when the platform needs one.
        health_url: HTTP health-check URL, empty when undeclared.
        workflow: The child's ``workflow_dispatch`` deploy pipeline the
            orchestrator triggers for cloud actions (ADR-005); empty falls back
            to the ``deploy.yml`` convention. The orchestrator never runs a
            platform mutation itself — it only dispatches this workflow.
    """

    target: str = DEPLOY_NONE
    app: str = ""
    region: str = ""
    health_url: str = ""
    workflow: str = ""


@dataclass(frozen=True)
class CiConfig:
    """Contract ``ci:`` block — a non-forge CI status endpoint (project-init #828).

    Optional and additive within contract v2: a child that omits it (every child
    scaffolded before project-init 1.1.7) is probed through its forge exactly as
    before. Feature-detected, per ADR-025 §4 — not gated on a version bump.

    Attributes:
        status_url: JSON endpoint reporting the latest build; empty when the
            project's CI *is* the forge's and ``gh``/``glab`` should be used.
        status_field: Dot-path to the status value inside that JSON, for a
            response shape the auto-detection misses. Empty = auto-detect.
    """

    status_url: str = ""
    status_field: str = ""


@dataclass(frozen=True)
class ProjectDescriptor:
    """Everything the orchestrator knows about a project without running it.

    Attributes:
        name: Project name (directory name when the config omits it).
        path: Absolute path to the project root.
        language: Primary language declared at scaffold time.
        delivery: How the project ships (library | service | prototype).
        contract_version: Descriptor-contract schema version (0 when absent).
        project_init_version: Scaffold version the project was rendered with.
        memory_tier: Memory tier (0 auto … 3 obsidian-graphify-rag).
        memory_stack: Declared memory backend (``auto`` | ``obsidian-only`` |
            ``obsidian-graphify`` | ``obsidian-graphify-rag``); ``unknown`` when
            the config omits it.
        memory_path: Absolute path to the project's memory directory.
        vault_path: Obsidian vault directory; ``None`` below tier 1 or when
            undeclared (higher-tier retrieval surface, ADR-025 §4).
        graph_path: Graphify graph file; ``None`` below tier 2 or when
            undeclared.
        rag_endpoint: Tier-3 RAG query endpoint (URL or local address); empty
            below tier 3 or when the child has not run its RAG setup yet.
        tooling: Task name → shell command (lint, format, test, run, …).
        deploy: Contract-v2 deploy block; ``None`` below v2 or when absent.
        observability_path: Contract-v2 usage/guard-log directory; ``None``
            below v2 or when undeclared (callers fall back to convention).
        hooks_expected: Contract-v2 list of git hooks the scaffold ships;
            empty below v2 or when undeclared (callers fall back to globbing).
        host: Upstream forge host (``project.project_init_host``), e.g.
            ``github.com`` or ``gitlab.com``; empty when undeclared. Selects
            which forge adapter probes CI (``ci`` command).
        ci: Declared non-forge CI status endpoint; ``None`` when the child omits
            the block or leaves ``status_url`` empty — the overwhelmingly common
            case, in which the forge adapters probe CI as before.
        heal_mode: The project's declared heal-mode override (``fix`` |
            ``notify``); empty when undeclared, in which case the run-wide
            mode applies (ADR-008).
        warnings: Human-readable parse problems, empty when the config is clean.
    """

    name: str
    path: Path
    config_root: str = ".claude"
    language: str = "unknown"
    delivery: str = "unknown"
    contract_version: int = 0
    project_init_version: str = "unknown"
    memory_tier: int = 0
    memory_stack: str = "unknown"
    memory_path: Path | None = None
    vault_path: Path | None = None
    graph_path: Path | None = None
    rag_endpoint: str = ""
    tooling: dict[str, str] = field(default_factory=dict)
    deploy: DeployConfig | None = None
    observability_path: Path | None = None
    hooks_expected: tuple[str, ...] = ()
    host: str = ""
    ci: CiConfig | None = None
    heal_mode: str = ""
    warnings: tuple[str, ...] = ()

    def has_task(self, task: str) -> bool:
        """Return whether the project declares a runnable command for ``task``."""
        return bool(self.tooling.get(task, "").strip())

    @property
    def config_path(self) -> Path:
        """Absolute path to the descriptor this project was read from."""
        return self.path / self.config_root / _CONFIG_BASENAME


def _as_mapping(value: Any) -> dict[str, Any]:
    """Return ``value`` if it is a mapping, else an empty dict."""
    return value if isinstance(value, dict) else {}


def _as_int(value: Any, default: int = 0) -> int:
    """Coerce ``value`` to int, falling back to ``default``."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _extract_tooling(raw: dict[str, Any]) -> dict[str, str]:
    """Map ``<task>_command`` keys in the ``tooling`` block to task names."""
    tooling: dict[str, str] = {}
    for raw_key, value in _as_mapping(raw.get("tooling")).items():
        key = str(raw_key)
        if key.endswith(_TOOLING_SUFFIX) and isinstance(value, str) and value.strip():
            tooling[key.removesuffix(_TOOLING_SUFFIX)] = value.strip()
    return tooling


def _extract_deploy(raw: dict[str, Any]) -> DeployConfig | None:
    """Parse the v2 ``deploy:`` block; ``None`` when absent."""
    block = raw.get("deploy")
    if block is None:
        return None
    deploy = _as_mapping(block)
    return DeployConfig(
        target=str(deploy.get("target") or DEPLOY_NONE),
        app=str(deploy.get("app") or ""),
        region=str(deploy.get("region") or ""),
        health_url=str(deploy.get("health_url") or ""),
        workflow=str(deploy.get("workflow") or ""),
    )


def _extract_ci(raw: dict[str, Any]) -> CiConfig | None:
    """Parse the optional ``ci:`` block; ``None`` when absent or ``status_url`` is empty.

    An empty ``status_url`` is the scaffold default, and it means "my CI is the
    forge's" — so it collapses to ``None`` rather than an empty CiConfig, and
    callers can branch on presence alone.
    """
    block = _as_mapping(raw.get("ci"))
    status_url = str(block.get("status_url") or "").strip()
    if not status_url:
        return None
    return CiConfig(
        status_url=status_url,
        status_field=str(block.get("status_field") or "").strip(),
    )


#: Heal-mode values a child may declare (ADR-008): ``fix`` spawns the scoped
#: agent and lands a draft PR; ``notify`` reports the failure and spends nothing.
HEAL_MODES = ("fix", "notify")


def _extract_heal_mode(raw: dict[str, Any], warnings: list[str]) -> str:
    """Parse the optional ``heal.mode`` override; ``""`` when absent.

    Feature-detected like ``ci``, not version-gated. An unknown value is
    ignored WITH a warning rather than obeyed or guessed: a typo'd mode must
    not silently switch a project between "spends money and opens PRs" and
    "tells me and stops".
    """
    mode = str(_as_mapping(raw.get("heal")).get("mode") or "").strip()
    if not mode:
        return ""
    if mode not in HEAL_MODES:
        warnings.append(f"heal.mode '{mode}' is not one of {'|'.join(HEAL_MODES)} — ignored")
        return ""
    return mode


def _contained_path(project_dir: Path, relative: str) -> Path | None:
    """Join ``relative`` under ``project_dir``, or ``None`` if it escapes.

    A descriptor is data the orchestrator only reads, but a ``memory_path`` or
    ``observability.path`` of ``../../etc`` or ``/etc`` would resolve outside
    the project root (``Path('/proj') / '/etc'`` is ``/etc``). Reject any value
    whose resolved location is not the project dir or beneath it; contained
    values keep their plain (unresolved) join so callers compare cleanly.
    """
    resolved = (project_dir / relative).resolve()
    if resolved == project_dir or project_dir in resolved.parents:
        return project_dir / relative
    return None


def _extract_observability_path(
    raw: dict[str, Any], project_dir: Path, warnings: list[str]
) -> Path | None:
    """Resolve the v2 ``observability.path``; ``None`` when undeclared/escaping."""
    declared = _as_mapping(raw.get("observability")).get("path")
    if not isinstance(declared, str) or not declared.strip():
        return None
    contained = _contained_path(project_dir, declared.strip())
    if contained is None:
        warnings.append(
            f"observability.path '{declared.strip()}' escapes the project root — ignored"
        )
    return contained


@dataclass(frozen=True)
class _MemorySurface:
    """The parsed ``memory:`` block plus the context tier-gating needs."""

    block: dict[str, Any]
    tier: int
    project_dir: Path


def _tier_gated_path(
    memory: _MemorySurface, key: str, min_tier: int, warnings: list[str]
) -> Path | None:
    """Resolve a tier-gated memory path (``vault_path``/``graph_path``).

    Read only at/above ``min_tier`` — a lower-tier child never emits it, and
    ignoring a stray value keeps the anchors-never-move invariant (a value that
    only appears with its tier can never shift a lower-tier reader's behaviour).
    A path escaping the project root is dropped with a warning, exactly as
    ``memory_path`` is.
    """
    if memory.tier < min_tier:
        return None
    declared = memory.block.get(key)
    if not isinstance(declared, str) or not declared.strip():
        return None
    contained = _contained_path(memory.project_dir, declared.strip())
    if contained is None:
        warnings.append(f"memory.{key} '{declared.strip()}' escapes the project root — ignored")
    return contained


def _tier_gated_endpoint(memory: _MemorySurface) -> str:
    """Resolve the tier-3 ``rag_endpoint`` string; empty below tier 3/undeclared.

    Unlike the vault/graph *paths*, the endpoint is an opaque address (a URL or
    ``host:port``), so it is kept as a plain string rather than a contained path.
    """
    if memory.tier < TIER_RAG:
        return ""
    endpoint = memory.block.get("rag_endpoint")
    return endpoint.strip() if isinstance(endpoint, str) else ""


def _extract_hooks_expected(raw: dict[str, Any]) -> tuple[str, ...]:
    """Parse the v2 ``hooks.expected`` list; empty when undeclared."""
    expected = _as_mapping(raw.get("hooks")).get("expected")
    if not isinstance(expected, list):
        return ()
    return tuple(str(name) for name in expected if isinstance(name, str) and name.strip())


def parse_config(text: str, project_dir: Path, config_root: str = ".claude") -> ProjectDescriptor:
    """Build a descriptor from raw config text (pure; never raises).

    Args:
        text: Contents of the project's ``config.yaml``.
        project_dir: Project root the config belongs to.
        config_root: Layout dir the config was found in (``.agents`` for a
            PI-627 scaffold, ``.claude`` legacy). Read surfaces without an
            explicit path in the config (memory, capabilities, observability)
            default under this dir. Defaults to ``.claude`` for direct callers.

    Returns:
        A descriptor; parse failures degrade to defaults with a warning.
        Contract-v2 surfaces (deploy, observability, hooks) are parsed only
        when the config declares ``project_init_contract_version >= 2`` —
        additive fields on a v1 config are ignored, exactly as a v1 reader
        would ignore them.
    """
    project_dir = project_dir.resolve()
    warnings: list[str] = []
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raw = None
        warnings.append(f"config.yaml is not valid YAML: {exc}".splitlines()[0])
    raw = _as_mapping(raw)
    if not raw and not warnings:
        warnings.append("config.yaml is empty")

    project = _as_mapping(raw.get("project"))
    memory = _as_mapping(raw.get("memory"))
    memory_default = f"{config_root}/memory"
    memory_rel = str(memory.get("memory_path") or memory_default)
    memory_path = _contained_path(project_dir, memory_rel)
    if memory_path is None:
        warnings.append(
            f"memory_path '{memory_rel}' escapes the project root — using {memory_default}"
        )
        memory_path = project_dir / memory_default
    contract_version = _as_int(project.get("project_init_contract_version"))
    is_v2 = contract_version >= CONTRACT_V2
    memory_tier = _as_int(memory.get("tier"))
    surface = _MemorySurface(block=memory, tier=memory_tier, project_dir=project_dir)

    return ProjectDescriptor(
        name=str(project.get("name") or project_dir.name),
        path=project_dir,
        config_root=config_root,
        language=str(raw.get("language") or "unknown"),
        delivery=str(raw.get("delivery") or "unknown"),
        contract_version=contract_version,
        project_init_version=str(project.get("project_init_version") or "unknown"),
        memory_tier=memory_tier,
        memory_stack=str(memory.get("stack") or "unknown"),
        memory_path=memory_path,
        vault_path=_tier_gated_path(surface, "vault_path", TIER_VAULT, warnings),
        graph_path=_tier_gated_path(surface, "graph_path", TIER_GRAPH, warnings),
        rag_endpoint=_tier_gated_endpoint(surface),
        tooling=_extract_tooling(raw),
        deploy=_extract_deploy(raw) if is_v2 else None,
        observability_path=(
            _extract_observability_path(raw, project_dir, warnings) if is_v2 else None
        ),
        hooks_expected=_extract_hooks_expected(raw) if is_v2 else (),
        host=str(project.get("project_init_host") or ""),
        # Feature-detected, NOT version-gated (unlike deploy/observability/hooks,
        # which arrived *with* v2). `ci` is an additive field within v2, so the
        # contract version says nothing about whether a child emits it — ADR-025
        # §4's rule is to detect the surface, not infer it from a version. A v1
        # child that hand-adds the block is honoured too, which costs nothing.
        ci=_extract_ci(raw),
        heal_mode=_extract_heal_mode(raw, warnings),
        warnings=tuple(warnings),
    )


def parse_scaffold_version(value: str) -> tuple[int, int, int] | None:
    """Parse a ``MAJOR.MINOR.PATCH`` scaffold version into a comparable tuple.

    Args:
        value: A ``project_init_version`` string.

    Returns:
        The three numeric components, or ``None`` when the value is missing,
        ``unknown``, or not exactly three integer components. Malformed shapes
        (``0.6``, ``999``, ``1.2.beta``) degrade to "not comparable" rather
        than a misleading order that could mark valid projects as behind.
    """
    if not value or value == "unknown":
        return None
    parts = value.split(".")
    if len(parts) != 3:
        return None
    try:
        major, minor, patch = (int(part) for part in parts)
    except ValueError:
        return None
    return (major, minor, patch)


def load_descriptor(project_dir: Path) -> ProjectDescriptor | None:
    """Load the descriptor for one project directory.

    Args:
        project_dir: Candidate project root.

    Returns:
        The parsed descriptor, or ``None`` when the directory is not a
        project-init project (no readable ``.agents/config.yaml`` or legacy
        ``.claude/config.yaml``).
    """
    resolved = resolve_config(project_dir)
    if resolved is None:
        return None
    config_path, config_root = resolved
    try:
        # errors="replace": a config saved in a non-UTF-8 encoding degrades to
        # a slightly-garbled descriptor rather than dropping the project from
        # discovery entirely (the engine never raises — ADR-003).
        text = config_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return parse_config(text, project_dir, config_root)
