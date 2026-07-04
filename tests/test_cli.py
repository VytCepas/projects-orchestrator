"""CLI surface: subcommands drive the engine end-to-end."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import add_memory, git_init, make_project

from projects_orchestrator import __version__
from projects_orchestrator.__main__ import main


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


def test_version_is_set() -> None:
    assert __version__


def test_main_no_command_exits_zero() -> None:
    assert main([]) == 0


def test_version_flag_exits_zero() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0


def test_projects_lists_discovered(fleet_dir: Path, capsys) -> None:
    make_project(fleet_dir, "alpha")
    main(["projects", "--root", str(fleet_dir)])
    assert "alpha" in capsys.readouterr().out


def test_projects_json_is_parseable(fleet_dir: Path, capsys) -> None:
    make_project(fleet_dir, "alpha")
    main(["projects", "--root", str(fleet_dir), "--json"])
    assert json.loads(capsys.readouterr().out)[0]["name"] == "alpha"


def test_status_renders_table(fleet_dir: Path, capsys) -> None:
    git_init(make_project(fleet_dir, "alpha"))
    main(["status", "--root", str(fleet_dir)])
    assert "clean" in capsys.readouterr().out


def test_status_single_project(fleet_dir: Path, capsys) -> None:
    git_init(make_project(fleet_dir, "alpha"))
    main(["status", "alpha", "--root", str(fleet_dir)])
    assert "alpha: clean on main" in capsys.readouterr().out


def test_status_unknown_project_exits_2(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    assert main(["status", "nope", "--root", str(fleet_dir)]) == 2


def test_checks_pass_exits_zero(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha", tooling={"lint": "true", "test": "true"})
    assert main(["checks", "--root", str(fleet_dir)]) == 0


def test_checks_failure_exits_one(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha", tooling={"lint": "false"})
    assert main(["checks", "--root", str(fleet_dir)]) == 1


def test_checks_task_filter(fleet_dir: Path, capsys) -> None:
    make_project(fleet_dir, "alpha", tooling={"lint": "true", "test": "false"})
    main(["checks", "--root", str(fleet_dir), "--task", "lint"])
    assert "test" not in capsys.readouterr().out


def test_checks_updates_status_table(fleet_dir: Path, capsys) -> None:
    make_project(fleet_dir, "alpha", tooling={"lint": "true"})
    main(["checks", "--root", str(fleet_dir), "--task", "lint"])
    capsys.readouterr()
    main(["status", "--root", str(fleet_dir)])
    assert "pass" in capsys.readouterr().out


def test_memory_search_finds_fact(fleet_dir: Path, capsys) -> None:
    add_memory(make_project(fleet_dir, "alpha"), "project_context.md", body="uses postgres 16")
    main(["memory", "postgres", "--root", str(fleet_dir)])
    assert "postgres" in capsys.readouterr().out


def test_memory_search_json(fleet_dir: Path, capsys) -> None:
    add_memory(make_project(fleet_dir, "alpha"), "project_context.md", body="uses postgres 16")
    main(["memory", "postgres", "--root", str(fleet_dir), "--json"])
    assert json.loads(capsys.readouterr().out)[0]["file"]["project"] == "alpha"


def test_drift_no_manifest_exits_zero(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    assert main(["drift", "--root", str(fleet_dir)]) == 0


def test_drift_json_reports_status(fleet_dir: Path, capsys) -> None:
    make_project(fleet_dir, "alpha")
    main(["drift", "--root", str(fleet_dir), "--json"])
    assert json.loads(capsys.readouterr().out)[0]["status"] == "no-manifest"


def test_doctor_conformant_project_exits_zero(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    assert main(["doctor", "--root", str(fleet_dir)]) == 0


def test_doctor_nonconformant_project_exits_one(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha", config_text="project:\n  name: alpha\n")
    assert main(["doctor", "--root", str(fleet_dir)]) == 1


def test_doctor_json_reports_findings(fleet_dir: Path, capsys) -> None:
    make_project(fleet_dir, "alpha")
    main(["doctor", "--root", str(fleet_dir), "--json"])
    assert json.loads(capsys.readouterr().out)[0]["findings"][0]["check"] == "config"


def test_audit_warn_exits_one(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    assert main(["audit", "--root", str(fleet_dir)]) == 1


def test_audit_json_reports_findings(fleet_dir: Path, capsys) -> None:
    make_project(fleet_dir, "alpha")
    main(["audit", "--root", str(fleet_dir), "--json"])
    categories = {f["category"] for f in json.loads(capsys.readouterr().out)[0]["findings"]}
    assert "freshness" in categories


def test_audit_markdown_renders_heading(fleet_dir: Path, capsys) -> None:
    make_project(fleet_dir, "alpha")
    main(["audit", "--root", str(fleet_dir), "--markdown"])
    assert "## alpha" in capsys.readouterr().out


def test_ci_offline_exits_zero(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    assert main(["ci", "--root", str(fleet_dir)]) == 0


def test_ci_json_reports_status(fleet_dir: Path, capsys) -> None:
    make_project(fleet_dir, "alpha")
    main(["ci", "--root", str(fleet_dir), "--json"])
    assert json.loads(capsys.readouterr().out)[0]["ci"] == "unknown"


def test_upgrade_plan_offline_renders(fleet_dir: Path, capsys) -> None:
    make_project(fleet_dir, "alpha")
    main(["upgrade-plan", "--root", str(fleet_dir)])
    assert "alpha: unknown" in capsys.readouterr().out


def test_upgrade_plan_json_has_status(fleet_dir: Path, capsys) -> None:
    make_project(fleet_dir, "alpha")
    main(["upgrade-plan", "--root", str(fleet_dir), "--json"])
    assert json.loads(capsys.readouterr().out)[0]["status"] == "unknown"


def test_snapshot_json_has_descriptor(fleet_dir: Path, capsys) -> None:
    make_project(fleet_dir, "alpha")
    main(["snapshot", "--root", str(fleet_dir), "--json"])
    assert json.loads(capsys.readouterr().out)[0]["descriptor"]["name"] == "alpha"


def test_snapshot_output_path_writes_html_without_html_flag(
    fleet_dir: Path, tmp_path: Path
) -> None:
    # -o implies --html: an operator naming a .html file must get the page,
    # not a silently-discarded text table.
    make_project(fleet_dir, "alpha")
    out = tmp_path / "fleet.html"
    assert main(["snapshot", "--root", str(fleet_dir), "-o", str(out)]) == 0
    assert out.read_text(encoding="utf-8").lstrip().startswith("<")


def test_start_no_run_command_exits_nonzero(fleet_dir: Path) -> None:
    # A project whose name contains "started" as a substring (e.g. "restarted")
    # must still report failure when it has no run_command.
    make_project(fleet_dir, "restarted")
    assert main(["start", "restarted", "--root", str(fleet_dir)]) == 1


def test_serve_command_dispatches_to_server(fleet_dir: Path, monkeypatch) -> None:
    # serve() blocks, so verify wiring by capturing the call instead of running it.
    import projects_orchestrator.__main__ as cli

    captured = {}
    monkeypatch.setattr(
        cli, "serve", lambda _config, host, port: captured.update(host=host, port=port)
    )
    assert main(["serve", "--root", str(fleet_dir), "--host", "0.0.0.0", "--port", "9999"]) == 0
    assert captured == {"host": "0.0.0.0", "port": 9999}


def test_project_checks_drops_head_when_worktree_changes_mid_run(
    fleet_dir: Path, monkeypatch
) -> None:
    # If the worktree changes while gates run (then reverts), the stamped HEAD
    # must be cleared so --changed-only can never reuse the result.
    import projects_orchestrator.__main__ as cli
    from projects_orchestrator.descriptor import load_descriptor

    descriptor = load_descriptor(make_project(fleet_dir, "alpha", tooling={"lint": "true"}))
    heads = iter(["a" * 40, "b" * 40])  # before-run != after-run
    monkeypatch.setattr(cli, "clean_worktree_head", lambda _d: next(heads))
    (pair,) = cli._project_checks(descriptor, ("lint",), None, changed_only=False)
    result, _reused = pair
    assert result.head == ""


def test_fleet_file_drives_discovery(fleet_dir: Path, tmp_path: Path, capsys) -> None:
    make_project(fleet_dir, "alpha")
    fleet_file = tmp_path / "fleet.yaml"
    fleet_file.write_text(f'roots: ["{fleet_dir}"]\n', encoding="utf-8")
    main(["projects", "--fleet", str(fleet_file)])
    assert "alpha" in capsys.readouterr().out


def test_events_no_logs_reports_none(fleet_dir: Path, capsys) -> None:
    make_project(fleet_dir, "alpha")
    assert main(["events", "--root", str(fleet_dir)]) == 0
    assert "no events recorded" in capsys.readouterr().out


def test_events_reads_usage_log(fleet_dir: Path, capsys) -> None:
    project = make_project(fleet_dir, "alpha")
    log_dir = project / ".claude" / "observability"
    log_dir.mkdir(parents=True)
    (log_dir / "usage.jsonl").write_text(
        '{"ts": "2026-07-01T10:00:00+00:00", "hook": "prod_guard", "action": "block"}\n',
        encoding="utf-8",
    )
    main(["events", "--root", str(fleet_dir)])
    assert "[prod_guard] block" in capsys.readouterr().out


def test_events_since_filters(fleet_dir: Path, capsys) -> None:
    project = make_project(fleet_dir, "alpha")
    log_dir = project / ".claude" / "observability"
    log_dir.mkdir(parents=True)
    (log_dir / "usage.jsonl").write_text(
        '{"ts": "2026-07-01T10:00:00+00:00", "hook": "prod_guard", "action": "block"}\n',
        encoding="utf-8",
    )
    main(["events", "--root", str(fleet_dir), "--since", "2026-07-02T00:00:00+00:00"])
    assert "no events recorded" in capsys.readouterr().out


def test_events_json_is_parseable(fleet_dir: Path, capsys) -> None:
    make_project(fleet_dir, "alpha")
    main(["events", "--root", str(fleet_dir), "--json"])
    assert json.loads(capsys.readouterr().out)[0]["project"] == "alpha"


def test_cloud_status_no_deploy_exits_zero(fleet_dir: Path, capsys) -> None:
    make_project(fleet_dir, "alpha")
    assert main(["cloud-status", "--root", str(fleet_dir)]) == 0
    assert "alpha: none — none" in capsys.readouterr().out


def test_cloud_status_json_is_parseable(fleet_dir: Path, capsys) -> None:
    make_project(fleet_dir, "alpha")
    main(["cloud-status", "--root", str(fleet_dir), "--json"])
    assert json.loads(capsys.readouterr().out)[0]["state"] == "none"


def test_cloud_status_caches_for_status_table(fleet_dir: Path, capsys) -> None:
    make_project(fleet_dir, "alpha")
    main(["cloud-status", "--root", str(fleet_dir)])
    capsys.readouterr()
    main(["status", "--root", str(fleet_dir)])
    assert "none" in capsys.readouterr().out
