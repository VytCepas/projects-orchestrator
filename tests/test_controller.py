"""Deterministic controller: parsing table and dispatch flows."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import add_memory, make_project, make_project_v2

from projects_orchestrator.adapters import cloud
from projects_orchestrator.controller import ControllerContext, dispatch, parse_command
from projects_orchestrator.registry import FleetConfig


@pytest.mark.parametrize(
    ("text", "verb"),
    [
        ("help", "help"),
        ("", "help"),
        ("status", "status"),
        ("lint", "check"),
        ("test alpha", "check"),
        ("checks all", "check"),
        ("run build alpha", "run"),
        ("memory postgres", "memory"),
        ("drift", "drift"),
        ("doctor", "doctor"),
        ("doctor alpha", "doctor"),
        ("audit", "audit"),
        ("audit alpha", "audit"),
        ("ci", "ci"),
        ("ci alpha", "ci"),
        ("upgrade", "upgrade"),
        ("upgrade alpha", "upgrade"),
        ("projects", "projects"),
        ("refresh", "refresh"),
        ("quit", "quit"),
        ("exit", "quit"),
        ("/ask what is broken", "ask"),
        ("cloud", "cloud"),
        ("events alpha", "events"),
        ("detail alpha", "detail"),
        ("start alpha", "start"),
        ("stop alpha", "stop"),
        ("logs alpha", "logs"),
        ("deploy alpha", "deploy"),
        ("deploy alpha rollback", "deploy"),
        ("work alpha fix the ci", "work"),
        ("heal alpha", "heal"),
        ("frobnicate", "unknown"),
    ],
)
def test_parse_command_maps_verb(text: str, verb: str) -> None:
    assert parse_command(text).verb == verb


def test_parse_command_ask_requires_exact_token() -> None:
    # "/asked why is ci red" must not be parsed as an /ask with question
    # "ed why is ci red"; only the exact /ask token opens ask mode.
    assert parse_command("/ask why is ci red").verb == "ask"
    assert parse_command("/asked why is ci red").verb != "ask"


def test_parse_command_checks_expands_to_both_tasks() -> None:
    assert parse_command("checks").args == ("lint", "test")


def test_parse_command_defaults_target_to_all() -> None:
    assert parse_command("lint").target == "all"


def test_parse_command_run_extracts_task() -> None:
    assert parse_command("run build alpha").args == ("build",)


def test_parse_command_memory_joins_query_words() -> None:
    assert parse_command("memory deploy target").args == ("deploy target",)


def _ctx(fleet_dir: Path, cache_file: Path | None = None) -> ControllerContext:
    return ControllerContext(
        config=FleetConfig(roots=(fleet_dir,)),
        cache_file=cache_file or fleet_dir / "checks.json",
    )


def test_dispatch_lint_reports_pass(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha", tooling={"lint": "true"})
    lines = list(dispatch(parse_command("lint alpha"), _ctx(fleet_dir)))
    assert lines == ["alpha lint: PASS"]


def test_dispatch_lint_reports_fail_with_detail(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha", tooling={"lint": "echo boom >&2; false"})
    lines = list(dispatch(parse_command("lint alpha"), _ctx(fleet_dir)))
    assert "FAIL" in lines[0]


def test_dispatch_check_unknown_project_lists_known(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    lines = list(dispatch(parse_command("lint nope"), _ctx(fleet_dir)))
    assert "unknown project: nope" in lines[0]


def test_dispatch_check_all_covers_every_project(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha", tooling={"lint": "true"})
    make_project(fleet_dir, "beta", tooling={"lint": "true"})
    lines = list(dispatch(parse_command("lint all"), _ctx(fleet_dir)))
    assert len(lines) == 2


def test_dispatch_check_persists_results_to_cache(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha", tooling={"lint": "true"})
    cache_file = fleet_dir / "checks.json"
    list(dispatch(parse_command("lint alpha"), _ctx(fleet_dir, cache_file)))
    assert cache_file.is_file()


def _deployable(fleet_dir: Path, name: str = "alpha") -> Path:
    """A service project that ships the workflow a dispatch would target."""
    project = make_project_v2(fleet_dir, name, deploy_target="fly")
    workflow = project / ".github/workflows/deploy.yml"
    workflow.parent.mkdir(parents=True, exist_ok=True)
    workflow.write_text("on: workflow_dispatch\n", encoding="utf-8")
    return project


def test_dispatch_deploy_from_repl_is_plan_only(fleet_dir: Path) -> None:
    # The cockpit never dispatches (ADR-005): a REPL `deploy` reports the plan
    # and points at the CLI --apply, so an agent can't fire a prod deploy.
    _deployable(fleet_dir)
    lines = list(dispatch(parse_command("deploy alpha"), _ctx(fleet_dir)))
    assert any("planned" in line and "--apply" in line for line in lines)


def test_repl_deploy_never_shells_out(fleet_dir: Path, monkeypatch) -> None:
    # The REPL is the surface an agent actually drives, so assert the guardrail
    # on BEHAVIOUR: no subprocess may be launched, whatever the output says.
    def explode(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("the REPL shelled out — the plan-only cockpit is broken")

    monkeypatch.setattr(cloud, "run_command", explode)
    _deployable(fleet_dir)
    for action in ("deploy", "rollback", "restart"):
        list(dispatch(parse_command(f"deploy alpha --action {action}"), _ctx(fleet_dir)))


def test_dispatch_deploy_carries_action(fleet_dir: Path) -> None:
    _deployable(fleet_dir)
    lines = list(dispatch(parse_command("deploy alpha rollback"), _ctx(fleet_dir)))
    assert any("rollback" in line for line in lines)


def test_dispatch_deploy_requires_project(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    lines = list(dispatch(parse_command("deploy"), _ctx(fleet_dir)))
    assert "usage: deploy" in lines[0]


def test_dispatch_status_table_has_header(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    lines = list(dispatch(parse_command("status"), _ctx(fleet_dir)))
    assert lines[0].startswith("Project")


def test_dispatch_status_single_project(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    lines = list(dispatch(parse_command("status alpha"), _ctx(fleet_dir)))
    assert lines[0].startswith("alpha:")


def test_dispatch_status_all_renders_table(fleet_dir: Path) -> None:
    # "status all" must render the fleet table, not crash or show one project.
    make_project(fleet_dir, "alpha")
    make_project(fleet_dir, "beta")
    lines = list(dispatch(parse_command("status all"), _ctx(fleet_dir)))
    assert lines[0].startswith("Project")
    assert any("alpha" in line for line in lines)
    assert any("beta" in line for line in lines)


def test_dispatch_status_all_on_empty_fleet_does_not_crash(tmp_path: Path) -> None:
    # Previously raised IndexError on selected[0]; now renders the (empty) table.
    ctx = ControllerContext(config=FleetConfig(roots=(tmp_path,)))
    lines = list(dispatch(parse_command("status all"), ctx))
    assert lines  # a line was produced, not an exception


def test_dispatch_memory_empty_query_shows_usage(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    # An empty args tuple (reachable via the /ask verb path) must not crash.
    from projects_orchestrator.controller import Intent, _dispatch_memory

    lines = list(_dispatch_memory(_ctx(fleet_dir), Intent(verb="memory")))
    assert lines == ["usage: memory <query>"]


def test_dispatch_memory_finds_fact(fleet_dir: Path) -> None:
    add_memory(make_project(fleet_dir, "alpha"), "project_context.md", body="deploys to fly.io")
    lines = list(dispatch(parse_command("memory fly.io"), _ctx(fleet_dir)))
    assert "fly.io" in lines[0]


def test_dispatch_memory_no_match_is_friendly(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    lines = list(dispatch(parse_command("memory unicorns"), _ctx(fleet_dir)))
    assert lines == ["no memory matches for: unicorns"]


def test_dispatch_projects_lists_names(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    assert list(dispatch(parse_command("projects"), _ctx(fleet_dir))) == ["alpha"]


def test_dispatch_refresh_picks_up_new_project(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    ctx = _ctx(fleet_dir)
    make_project(fleet_dir, "beta")
    lines = list(dispatch(parse_command("refresh"), ctx))
    assert "2 project(s)" in lines[0]


def test_dispatch_drift_reports_per_project(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    lines = list(dispatch(parse_command("drift"), _ctx(fleet_dir)))
    assert lines == ["alpha: -"]


def test_dispatch_doctor_reports_project_status(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    lines = list(dispatch(parse_command("doctor"), _ctx(fleet_dir)))
    assert lines[0] == "alpha: warn"


def test_dispatch_audit_reports_project_status(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    lines = list(dispatch(parse_command("audit"), _ctx(fleet_dir)))
    assert lines[0] == "alpha: warn"


def test_dispatch_ci_reports_per_project(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    lines = list(dispatch(parse_command("ci"), _ctx(fleet_dir)))
    assert lines[0].startswith("alpha: CI ")


def test_dispatch_upgrade_reports_per_project(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    lines = list(dispatch(parse_command("upgrade"), _ctx(fleet_dir)))
    assert lines[0].startswith("alpha: ")


def test_dispatch_ask_is_disabled(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    lines = list(dispatch(parse_command("/ask anything"), _ctx(fleet_dir)))
    assert "not enabled" in lines[0]


def test_dispatch_unknown_points_to_help(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    lines = list(dispatch(parse_command("frobnicate"), _ctx(fleet_dir)))
    assert "try: help" in lines[0]


def test_dispatch_cloud_reports_per_project(fleet_dir: Path, tmp_path: Path) -> None:
    make_project(fleet_dir, "alpha")
    lines = list(dispatch(parse_command("cloud"), _ctx(fleet_dir, tmp_path / "checks.json")))
    assert lines[0] == "alpha: none — none"


def test_dispatch_events_empty_fleet_says_none(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    lines = list(dispatch(parse_command("events"), _ctx(fleet_dir)))
    assert lines == ["no events recorded"]


def test_dispatch_detail_requires_project(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    lines = list(dispatch(parse_command("detail"), _ctx(fleet_dir)))
    assert lines == ["usage: detail <project>"]


def test_dispatch_detail_renders_project_heading(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    lines = list(dispatch(parse_command("detail alpha"), _ctx(fleet_dir)))
    assert lines[0] == "# alpha"


def test_dispatch_start_requires_project(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    lines = list(dispatch(parse_command("start"), _ctx(fleet_dir)))
    assert lines == ["usage: start <project>"]


def test_dispatch_stop_not_running_is_friendly(fleet_dir: Path, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    make_project(fleet_dir, "alpha")
    lines = list(dispatch(parse_command("stop alpha"), _ctx(fleet_dir)))
    assert lines == ["alpha: not running"]


def test_repl_hint_carries_the_planned_action(fleet_dir: Path) -> None:
    # `--action` defaults to `deploy`. A bare `deploy alpha --apply` copied out
    # of a ROLLBACK plan would dispatch a DEPLOY — the cockpit handing the
    # operator the wrong production mutation, in their own words.
    _deployable(fleet_dir)
    lines = list(dispatch(parse_command("deploy alpha rollback"), _ctx(fleet_dir)))
    hint = next(line for line in lines if "--apply" in line)
    assert "--action rollback" in hint


def test_repl_hint_is_copy_pasteable_for_every_action(fleet_dir: Path) -> None:
    _deployable(fleet_dir)
    for action in ("deploy", "rollback", "restart"):
        lines = list(dispatch(parse_command(f"deploy alpha {action}"), _ctx(fleet_dir)))
        hint = next(line for line in lines if "--apply" in line)
        assert f"--action {action}" in hint


def test_dispatch_heal_requires_project(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    lines = list(dispatch(parse_command("heal"), _ctx(fleet_dir)))
    assert lines == ["usage: heal <project>"]


def test_dispatch_heal_no_cached_failure_is_a_no_op(fleet_dir: Path) -> None:
    from conftest import git_init

    project = make_project(fleet_dir, "alpha")
    git_init(project)
    lines = list(dispatch(parse_command("heal alpha"), _ctx(fleet_dir)))
    assert lines == ["alpha: no_action — no failing lint/test gate cached"]


# --- #124: the controller may PROPOSE a work run, but never launch one ----------


def test_parse_work_keeps_the_whole_tail_as_the_task() -> None:
    intent = parse_command("work alpha fix the flaky ci test")
    assert intent.verb == "work"
    assert intent.target == "alpha"
    assert intent.args == ("fix", "the", "flaky", "ci", "test")


def test_dispatch_work_proposes_the_exact_cli_command(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha", tooling={"lint": "true"})
    lines = list(dispatch(parse_command("work alpha fix the ci"), _ctx(fleet_dir)))
    joined = "\n".join(lines)
    assert 'work alpha "fix the ci"' in joined  # the exact command to run
    assert "Nothing was dispatched" in joined


def test_dispatch_work_on_unknown_project_errors(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    lines = list(dispatch(parse_command("work nope do a thing"), _ctx(fleet_dir)))
    assert any("nope" in line for line in lines)
    assert not any('work nope "' in line for line in lines)  # no proposal for a phantom


def test_dispatch_work_without_a_task_shows_usage(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    lines = list(dispatch(parse_command("work alpha"), _ctx(fleet_dir)))
    assert any("usage: work" in line for line in lines)


def test_dispatch_work_launches_no_agent(fleet_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # THE guard (criterion 2): asserted on BEHAVIOUR, not a returned status. Even
    # if a future edit wired a launcher in, this spy would catch it — the shortest
    # path from a typo (or the /ask model) to a running agent must end at a string.
    from projects_orchestrator import work

    launches: list[str] = []
    monkeypatch.setattr(work, "launch", lambda *_a, **_k: launches.append("launch") or None)
    monkeypatch.setattr(
        work.subprocess, "Popen", lambda *_a, **_k: pytest.fail("a subprocess was spawned")
    )

    make_project(fleet_dir, "alpha", tooling={"lint": "true"})
    list(dispatch(parse_command("work alpha fix the ci"), _ctx(fleet_dir)))
    assert launches == []  # nothing launched


def test_ask_resolving_to_work_proposes_but_launches_nothing(
    fleet_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The /ask model may route "have an agent fix alpha" to a work intent — and
    # the controller must still only PROPOSE it. End to end: ask -> work -> string.
    from projects_orchestrator import work

    monkeypatch.setenv("ORCHESTRATOR_ASK_MODEL", "claude-test")
    launches: list[str] = []
    monkeypatch.setattr(work, "launch", lambda *_a, **_k: launches.append("launch") or None)

    make_project(fleet_dir, "alpha", tooling={"lint": "true"})
    ctx = ControllerContext(
        config=FleetConfig(roots=(fleet_dir,)),
        cache_file=fleet_dir / "checks.json",
        ask_complete=lambda _m, _p: '{"verb": "work", "target": "alpha", "args": ["fix the ci"]}',
    )
    lines = list(dispatch(parse_command("/ask have an agent fix alpha's ci"), ctx))
    joined = "\n".join(lines)
    assert 'work alpha "fix the ci"' in joined  # proposed
    assert launches == []  # but never launched
