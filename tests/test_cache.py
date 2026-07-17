"""Checks cache: last-known results survive across runs, corruption is empty."""

from __future__ import annotations

from pathlib import Path

from projects_orchestrator.cache import cache_path, drop_result, load_results, save_results
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


def test_load_type_corrupt_entry_is_dropped(tmp_path: Path) -> None:
    # Valid JSON, but status/checked_at have the wrong types (e.g. a hand edit
    # or bit flip). Loading must not surface a value that crashes renderers.
    path = tmp_path / "checks.json"
    path.write_text(
        '{"app":{"lint":{"project":"app","task":"lint","status":7,"checked_at":5}}}',
        encoding="utf-8",
    )
    assert load_results(path) == {}


def test_load_keeps_valid_entry_beside_corrupt_one(tmp_path: Path) -> None:
    path = tmp_path / "checks.json"
    save_results([_result(task="lint")], path)
    raw = path.read_text(encoding="utf-8").rstrip().removesuffix("}")
    path.write_text(raw + ', "bad": {"lint": {"status": 9}}}', encoding="utf-8")
    loaded = load_results(path)
    assert loaded["alpha"]["lint"].status == "pass"
    assert "bad" not in loaded


def test_load_integer_duration_is_coerced_to_float(tmp_path: Path) -> None:
    path = tmp_path / "checks.json"
    path.write_text(
        '{"app":{"lint":{"project":"app","task":"lint","status":"pass","duration":3}}}',
        encoding="utf-8",
    )
    assert load_results(path)["app"]["lint"].duration == 3.0


def test_drop_result_retires_one_task(tmp_path: Path) -> None:
    path = tmp_path / "checks.json"
    save_results([_result(task="lint"), _result(task="process")], path)
    drop_result("alpha", "process", path)
    assert set(load_results(path)["alpha"]) == {"lint"}


def test_drop_result_removes_an_emptied_project(tmp_path: Path) -> None:
    path = tmp_path / "checks.json"
    save_results([_result(task="process")], path)
    drop_result("alpha", "process", path)
    assert "alpha" not in load_results(path)


def test_drop_result_missing_entry_is_a_no_op(tmp_path: Path) -> None:
    path = tmp_path / "checks.json"
    save_results([_result(task="lint")], path)
    drop_result("alpha", "process", path)
    drop_result("ghost", "process", path)
    assert load_results(path)["alpha"]["lint"].status == "pass"
