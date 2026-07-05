"""Infer how to run and test a project from the tooling it declares.

Ownership boundaries (AGENTS.md) say ``just`` owns commands, so a ``justfile``
recipe wins when present; otherwise fall back to the language tool the project
already uses (``bun`` for Node, ``uv`` for Python, compose for containers).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

_JUST_RECIPE = re.compile(r"^([a-zA-Z][\w-]*)\s*:", re.MULTILINE)
_RUN_NAMES = ("dev", "serve", "start", "run", "watch", "up")
_TEST_NAMES = ("test", "ci", "check")


@dataclass(frozen=True)
class RunPlan:
    """The commands the cockpit will use to operate a project.

    Attributes:
        run: Shell command that starts the project, or ``None`` if unknown.
        test: Shell command that runs the test/lint gate, or ``None``.
        source: Which tool the plan was inferred from (for display).
    """

    run: str | None
    test: str | None
    source: str


def _pick(names: tuple[str, ...], available: set[str]) -> str | None:
    """Return the first preferred name present in ``available``."""
    return next((n for n in names if n in available), None)


def _from_justfile(path: Path) -> RunPlan | None:
    """Infer commands from a project ``justfile``."""
    justfile = path / "justfile"
    if not justfile.is_file():
        return None
    recipes = set(_JUST_RECIPE.findall(justfile.read_text(encoding="utf-8")))
    run = _pick(_RUN_NAMES, recipes)
    test = _pick(_TEST_NAMES, recipes)
    return RunPlan(
        run=f"just {run}" if run else None,
        test=f"just {test}" if test else None,
        source="justfile",
    )


def _from_package_json(path: Path) -> RunPlan | None:
    """Infer commands from Node ``package.json`` scripts (bun, per node.md)."""
    manifest = path / "package.json"
    if not manifest.is_file():
        return None
    try:
        scripts = set(json.loads(manifest.read_text(encoding="utf-8")).get("scripts", {}))
    except json.JSONDecodeError:
        return None
    run = _pick(_RUN_NAMES, scripts)
    test = _pick(_TEST_NAMES, scripts)
    return RunPlan(
        run=f"bun run {run}" if run else None,
        test=f"bun run {test}" if test else None,
        source="package.json",
    )


def _from_compose(path: Path) -> RunPlan | None:
    """Infer a run command from a docker compose file."""
    for name in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"):
        if (path / name).is_file():
            return RunPlan(run="docker compose up", test=None, source="compose")
    return None


def _from_python(path: Path) -> RunPlan | None:
    """Infer a test command for a uv/Python project."""
    if (path / "pyproject.toml").is_file():
        return RunPlan(run=None, test="uv run pytest", source="python")
    return None


def plan_for(project_path: Path) -> RunPlan:
    """Build the best run/test plan for a project directory.

    Args:
        project_path: The project root to inspect.

    Returns:
        A :class:`RunPlan`; ``run``/``test`` are ``None`` when nothing matched.
    """
    plans = [
        _from_justfile(project_path),
        _from_package_json(project_path),
        _from_compose(project_path),
        _from_python(project_path),
    ]
    run = next((p.run for p in plans if p and p.run), None)
    test = next((p.test for p in plans if p and p.test), None)
    source = next((p.source for p in plans if p and (p.run or p.test)), "none")
    return RunPlan(run=run, test=test, source=source)
