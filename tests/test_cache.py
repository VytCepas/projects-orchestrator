"""Checks cache: last-known results survive across runs, corruption is empty."""

from __future__ import annotations

from pathlib import Path

from projects_orchestrator.cache import cache_path, load_results, save_results
from projects_orchestrator.checks import CheckResult


def _result(project: str = "alpha", task: str = "lint", status: str = "pass") -> CheckResult:
    return CheckResult(
        project=project, task=task, status=status, checked_at="2026-07-02T00:00:00+00:00"
    )


def test_save_then_load_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "checks.json"
    save_results([_result()], path)
    assert load_results(path)["alpha"]["lint"].status == "pass"


def test_save_merges_new_task_into_existing_project(tmp_path: Path) -> None:
    path = tmp_path / "checks.json"
    save_results([_result(task="lint")], path)
    merged = save_results([_result(task="test", status="fail")], path)
    assert set(merged["alpha"]) == {"lint", "test"}


def test_save_overwrites_same_task(tmp_path: Path) -> None:
    path = tmp_path / "checks.json"
    save_results([_result(status="fail")], path)
    save_results([_result(status="pass")], path)
    assert load_results(path)["alpha"]["lint"].status == "pass"


def test_load_missing_file_is_empty(tmp_path: Path) -> None:
    assert load_results(tmp_path / "absent.json") == {}


def test_load_corrupt_file_is_empty(tmp_path: Path) -> None:
    path = tmp_path / "checks.json"
    path.write_text("{not json", encoding="utf-8")
    assert load_results(path) == {}


def test_load_wrong_shape_is_empty(tmp_path: Path) -> None:
    path = tmp_path / "checks.json"
    path.write_text('["a list"]', encoding="utf-8")
    assert load_results(path) == {}


def test_save_to_unwritable_path_still_returns_merge(tmp_path: Path) -> None:
    merged = save_results([_result()], tmp_path / "no" / "\0bad" / "checks.json")
    assert merged["alpha"]["lint"].status == "pass"


def test_cache_path_honors_xdg_cache_home(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert cache_path() == tmp_path / "projects-orchestrator" / "checks.json"
