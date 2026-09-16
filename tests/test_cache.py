"""Checks cache: last-known results survive across runs, corruption is empty."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from projects_orchestrator import cache
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


# --- Version skew is not corruption (#183) -----------------------------------
#
# The cache carried no format version, so a renamed or removed CheckResult field
# made every entry fail coercion and the whole file read as empty — a silent
# full-cache wipe indistinguishable from "never probed" AND from corruption.


def test_a_saved_cache_records_the_schema_version(tmp_path: Path) -> None:
    path = tmp_path / "checks.json"
    save_results([_result()], path)
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document[cache._VERSION_KEY] == cache.SCHEMA_VERSION


def test_a_versioned_cache_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "checks.json"
    save_results([_result()], path)
    assert load_results(path)["alpha"]["lint"].status == "pass"


def test_a_pre_envelope_cache_is_still_read(tmp_path: Path) -> None:
    """The migration path: a cache written before the envelope existed is a bare
    results map, and dropping those results on upgrade would be the very wipe
    this change exists to prevent."""
    path = tmp_path / "checks.json"
    path.write_text(
        json.dumps({"alpha": {"lint": asdict(_result())}}),
        encoding="utf-8",
    )
    state = cache.read_cache(path)
    assert state.status == cache.LEGACY
    assert state.results["alpha"]["lint"].status == "pass"


def test_a_newer_cache_reads_as_skew_not_corruption(tmp_path: Path) -> None:
    """The distinction the whole change is for: both used to read as empty."""
    path = tmp_path / "checks.json"
    path.write_text(
        json.dumps({cache._VERSION_KEY: cache.SCHEMA_VERSION + 1}),
        encoding="utf-8",
    )
    assert cache.read_cache(path).status == cache.FUTURE


def test_a_corrupt_cache_is_distinguishable_from_a_newer_one(tmp_path: Path) -> None:
    path = tmp_path / "checks.json"
    path.write_text("{not json", encoding="utf-8")
    assert cache.read_cache(path).status == cache.UNREADABLE


def test_saving_does_not_clobber_a_cache_written_by_a_newer_build(tmp_path: Path) -> None:
    """THE DATA-LOSS PATH, and the reason this is a bug and not a nicety.

    `save_results` merges into what it loaded and writes the result back. A
    newer cache read as empty, so the merge wrote this build's handful of
    results OVER a file it could not understand — version skew turning into
    permanent loss on the next probe."""
    path = tmp_path / "checks.json"
    newer = {cache._VERSION_KEY: cache.SCHEMA_VERSION + 1, "kept": {"data": {}}}
    path.write_text(json.dumps(newer), encoding="utf-8")
    save_results([_result()], path)
    assert json.loads(path.read_text(encoding="utf-8")) == newer


def test_the_caller_still_gets_its_results_when_the_cache_is_skewed(tmp_path: Path) -> None:
    """Refusing to write must not mean refusing to answer — the run that just
    happened is still valid, it simply is not persisted."""
    path = tmp_path / "checks.json"
    path.write_text(
        json.dumps({cache._VERSION_KEY: cache.SCHEMA_VERSION + 1}),
        encoding="utf-8",
    )
    merged = save_results([_result()], path)
    assert merged["alpha"]["lint"].status == "pass"


def _pre_envelope_read(path: Path) -> dict[str, dict[str, object]]:
    """The reader EVERY build before the envelope shipped with.

    Walks the top level and treats each dict value as a project's task map. It
    is reproduced here rather than imported because the point is what an OLD
    installation does with a file this build wrote.
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    results: dict[str, dict[str, object]] = {}
    for project, tasks in raw.items():
        if not isinstance(tasks, dict):
            continue
        results[project] = tasks
    return results


def test_a_pre_envelope_reader_still_sees_every_project(tmp_path: Path) -> None:
    """THE ROLLBACK PATH (raised in review on #242).

    A wrapper envelope is invisible to an older build: it walks the top level,
    finds only `results`, coerces nothing, and reads the cache as EMPTY — then
    its next save writes a bare map over the file, recreating the very
    skew-into-data-loss this change exists to prevent. As a sibling key the old
    reader skips the version and merges instead of clobbering.
    """
    path = tmp_path / "checks.json"
    save_results([_result(project="alpha"), _result(project="beta")], path)
    assert set(_pre_envelope_read(path)) == {"alpha", "beta"}


def test_dropping_a_result_keeps_the_envelope(tmp_path: Path) -> None:
    """`drop_result` built its own payload and omitted the version key, silently
    demoting the file to the pre-envelope shape between a drop and the next
    save (raised in review on #242)."""
    path = tmp_path / "checks.json"
    save_results([_result(task="lint"), _result(task="test")], path)
    drop_result("alpha", "lint", path)
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document[cache._VERSION_KEY] == cache.SCHEMA_VERSION


def test_dropping_a_result_refuses_a_cache_from_a_newer_build(tmp_path: Path) -> None:
    path = tmp_path / "checks.json"
    newer = {cache._VERSION_KEY: cache.SCHEMA_VERSION + 1, "alpha": {"lint": {}}}
    path.write_text(json.dumps(newer), encoding="utf-8")
    drop_result("alpha", "lint", path)
    assert json.loads(path.read_text(encoding="utf-8")) == newer
