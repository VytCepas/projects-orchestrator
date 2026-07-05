"""Append-only check history: bounded persistence, trend, and transitions."""

from __future__ import annotations

from pathlib import Path

from projects_orchestrator.checks import CheckResult
from projects_orchestrator.history import (
    MAX_ENTRIES,
    HistoryEntry,
    history_path,
    load_history,
    primary_trend,
    project_history,
    record,
    sparkline,
    transitions,
)


def _result(status: str, task: str = "test", project: str = "alpha", at: str = "t") -> CheckResult:
    return CheckResult(project=project, task=task, status=status, checked_at=at)


def _entry(status: str, task: str = "test", project: str = "alpha", at: str = "t") -> HistoryEntry:
    return HistoryEntry(project=project, task=task, status=status, checked_at=at)


def test_record_then_load_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "history.jsonl"
    record([_result("pass")], path)
    assert [e.status for e in load_history(path)] == ["pass"]


def test_record_appends_across_runs(tmp_path: Path) -> None:
    path = tmp_path / "history.jsonl"
    record([_result("pass")], path)
    record([_result("fail")], path)
    assert [e.status for e in load_history(path)] == ["pass", "fail"]


def test_record_ignores_skips(tmp_path: Path) -> None:
    path = tmp_path / "history.jsonl"
    record([_result("skip")], path)
    assert load_history(path) == []


def test_record_bounds_the_log(tmp_path: Path) -> None:
    path = tmp_path / "history.jsonl"
    record([_result("pass", at=str(i)) for i in range(MAX_ENTRIES + 50)], path)
    assert len(load_history(path)) == MAX_ENTRIES


def test_load_missing_log_is_empty(tmp_path: Path) -> None:
    assert load_history(tmp_path / "absent.jsonl") == []


def test_load_skips_malformed_lines(tmp_path: Path) -> None:
    path = tmp_path / "history.jsonl"
    path.write_text('{"broken\n{"project":"a","task":"t","status":"pass","checked_at":"x"}\n',
                    encoding="utf-8")
    assert [e.status for e in load_history(path)] == ["pass"]


def test_project_history_groups_by_task() -> None:
    entries = [_entry("pass", task="lint"), _entry("fail", task="test")]
    grouped = project_history(entries, "alpha")
    assert set(grouped) == {"lint", "test"}


def test_project_history_filters_by_project() -> None:
    entries = [_entry("pass", project="alpha"), _entry("fail", project="beta")]
    assert list(project_history(entries, "alpha")) == ["test"]


def test_sparkline_renders_newest_last() -> None:
    entries = [_entry("pass"), _entry("pass"), _entry("fail")]
    assert sparkline(entries) == "++x"


def test_sparkline_truncates_to_width() -> None:
    entries = [_entry("pass") for _ in range(20)]
    assert len(sparkline(entries, width=5)) == 5


def test_transitions_only_reports_status_changes() -> None:
    entries = [_entry("pass"), _entry("pass"), _entry("fail"), _entry("fail"), _entry("pass")]
    assert [e.status for e in transitions(entries)] == ["pass", "fail", "pass"]


def test_primary_trend_prefers_test_gate() -> None:
    entries = [_entry("pass", task="lint"), _entry("fail", task="test"), _entry("pass", task="test")]
    assert primary_trend(entries, "alpha") == "x+"


def test_primary_trend_falls_back_to_lint() -> None:
    entries = [_entry("pass", task="lint"), _entry("fail", task="lint")]
    assert primary_trend(entries, "alpha") == "+x"


def test_primary_trend_empty_without_history() -> None:
    assert primary_trend([], "alpha") == ""


def test_history_path_honors_xdg_state_home(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert history_path() == tmp_path / "projects-orchestrator" / "history.jsonl"
