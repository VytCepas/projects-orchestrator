"""Infer a minimal descriptor for plain git repos — no contract required.

The orchestrator is deliberately a *reader* of the project-init descriptor,
but most people's project directories are a mix. With
``include_plain_repos: true`` in ``fleet.yaml``, a scanned directory that
is a git repo without ``.claude/config.yaml`` gets a conservative inferred
descriptor: name from the directory, language from its manifest files, and
lint/test/run commands from a fixed table of well-known conventions.

Inference never guesses beyond that table: a ``justfile`` wins when its
recipes are declared (running the same commands a human would), otherwise
each ecosystem contributes only its canonical commands. No match means no
command — the gate stays ``skip``, exactly like an undeclared contract
task. Inferred descriptors carry ``contract_version 0`` and a warning, so
``doctor``/``audit`` keep reporting them as outside the contract.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from projects_orchestrator.descriptor import ProjectDescriptor

_MAX_MANIFEST_BYTES = 262_144

# (manifest file, language, {task: command}) — canonical, never guessed.
_ECOSYSTEMS: tuple[tuple[str, str, dict[str, str]], ...] = (
    ("pyproject.toml", "python", {"test": "pytest"}),
    ("package.json", "javascript", {}),  # commands come from declared scripts
    ("Cargo.toml", "rust", {"lint": "cargo clippy", "test": "cargo test"}),
    ("go.mod", "go", {"lint": "go vet ./...", "test": "go test ./..."}),
)

_INFERRED_TASKS = ("lint", "test", "run")


def _read_text(path: Path) -> str:
    """Read a small text file; empty string on any problem."""
    try:
        if path.stat().st_size > _MAX_MANIFEST_BYTES:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _just_recipes(project_dir: Path) -> set[str]:
    """Names of top-level recipes declared in the repo's justfile."""
    text = _read_text(project_dir / "justfile")
    return {
        match.group(1)
        for match in re.finditer(r"^([A-Za-z][\w-]*)(?:\s+[\w-]+)*\s*:(?!=)", text, re.MULTILINE)
    }


def _npm_scripts(project_dir: Path) -> set[str]:
    """Script names declared in package.json (tolerant of bad JSON)."""
    try:
        raw = json.loads(_read_text(project_dir / "package.json") or "{}")
    except ValueError:
        return set()
    scripts = raw.get("scripts") if isinstance(raw, dict) else None
    return set(scripts) if isinstance(scripts, dict) else set()


def _infer_tooling(project_dir: Path) -> dict[str, str]:
    """Build the task → command map from on-disk conventions."""
    tooling: dict[str, str] = {}
    for manifest, _language, commands in _ECOSYSTEMS:
        if (project_dir / manifest).is_file():
            for task, command in commands.items():
                tooling.setdefault(task, command)
    if (project_dir / "package.json").is_file():
        for task in _INFERRED_TASKS:
            if task in _npm_scripts(project_dir):
                tooling.setdefault(task, f"npm run {task}")
    # A justfile is the project's own declared interface — it wins outright.
    recipes = _just_recipes(project_dir)
    for task in _INFERRED_TASKS:
        if task in recipes:
            tooling[task] = f"just {task}"
    return tooling


def _infer_language(project_dir: Path) -> str:
    """Pick the language from the first matching manifest file."""
    for manifest, language, _commands in _ECOSYSTEMS:
        if (project_dir / manifest).is_file():
            return language
    return "unknown"


def infer_descriptor(project_dir: Path) -> ProjectDescriptor | None:
    """Infer a minimal descriptor for a plain git repo; never raises.

    Args:
        project_dir: Candidate directory (already known to lack the
            project-init descriptor).

    Returns:
        A conservative inferred descriptor, or ``None`` when the directory
        is not a git repository — a bare folder is not a project.
    """
    project_dir = project_dir.resolve()
    if not (project_dir / ".git").exists():
        return None
    return ProjectDescriptor(
        name=project_dir.name,
        path=project_dir,
        language=_infer_language(project_dir),
        tooling=_infer_tooling(project_dir),
        warnings=("no project-init descriptor — inferred from repo conventions",),
    )
