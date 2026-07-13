"""CLI surface: subcommands drive the engine end-to-end."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import (
    add_capabilities,
    add_graph,
    add_memory,
    git_init,
    make_memory_project,
    make_project,
)

import projects_orchestrator.__main__ as cli
from projects_orchestrator import __version__
from projects_orchestrator.__main__ import main


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    # `checks` now records history under XDG_STATE_HOME — isolate it too so no
    # test writes to the real ~/.local/state.
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


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


def test_capabilities_summarizes_each_project(fleet_dir: Path, capsys) -> None:
    add_capabilities(make_project(fleet_dir, "alpha"), skills=["plan", "status"])
    main(["capabilities", "--root", str(fleet_dir)])
    assert "2 skill(s)" in capsys.readouterr().out


def test_capabilities_kind_inverts_to_projects(fleet_dir: Path, capsys) -> None:
    add_capabilities(make_project(fleet_dir, "alpha"), mcp_servers=["context7"])
    add_capabilities(make_project(fleet_dir, "beta"), mcp_servers=["context7"])
    main(["capabilities", "--root", str(fleet_dir), "--kind", "mcp"])
    assert "context7: alpha, beta" in capsys.readouterr().out


def test_capabilities_missing_inventory_is_reported(fleet_dir: Path, capsys) -> None:
    make_project(fleet_dir, "alpha")
    main(["capabilities", "--root", str(fleet_dir)])
    assert "no CAPABILITIES.md" in capsys.readouterr().out


def test_capabilities_json_is_parseable(fleet_dir: Path, capsys) -> None:
    add_capabilities(make_project(fleet_dir, "alpha"), skills=["plan"])
    main(["capabilities", "--root", str(fleet_dir), "--json"])
    assert json.loads(capsys.readouterr().out)[0]["skills"][0]["name"] == "plan"


def test_memory_search_reads_graph_surface(fleet_dir: Path, capsys) -> None:
    project = make_memory_project(fleet_dir, "alpha", tier=2, graph_path="graphify-out/graph.json")
    add_graph(project, [{"name": "AuthService", "description": "handles oauth login"}])
    main(["memory", "oauth", "--root", str(fleet_dir)])
    assert "oauth" in capsys.readouterr().out


def test_memory_notes_unqueried_rag_endpoint(fleet_dir: Path, capsys) -> None:
    make_memory_project(
        fleet_dir,
        "alpha",
        tier=3,
        graph_path="graphify-out/graph.json",
        rag_endpoint="http://127.0.0.1:8099",
    )
    main(["memory", "anything", "--root", str(fleet_dir)])
    assert "RAG endpoint" in capsys.readouterr().err


def test_register_adds_scaffolded_project(tmp_path: Path, capsys) -> None:
    project = make_project(tmp_path, "alpha")
    result = tmp_path / "scaffold.json"
    result.write_text(
        json.dumps({"target": str(project), "contract_version": "1", "files_created": 42}),
        encoding="utf-8",
    )
    fleet_file = tmp_path / "fleet.yaml"
    assert main(["register", str(result), "--fleet", str(fleet_file)]) == 0
    assert "registered" in capsys.readouterr().out


def test_register_makes_project_discoverable(tmp_path: Path, capsys) -> None:
    project = make_project(tmp_path, "alpha")
    result = tmp_path / "scaffold.json"
    result.write_text(json.dumps({"target": str(project)}), encoding="utf-8")
    fleet_file = tmp_path / "fleet.yaml"
    main(["register", str(result), "--fleet", str(fleet_file)])
    capsys.readouterr()
    main(["projects", "--fleet", str(fleet_file)])
    assert "alpha" in capsys.readouterr().out


def test_register_invalid_result_exits_one(tmp_path: Path) -> None:
    result = tmp_path / "scaffold.json"
    result.write_text(json.dumps({"preset": "auto"}), encoding="utf-8")
    assert main(["register", str(result), "--fleet", str(tmp_path / "fleet.yaml")]) == 1


def test_register_missing_file_exits_two(tmp_path: Path) -> None:
    assert (
        main(["register", str(tmp_path / "absent.json"), "--fleet", str(tmp_path / "f.yaml")]) == 2
    )


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


def test_hardening_with_gaps_exits_one_and_groups_actions(fleet_dir: Path, capsys) -> None:
    make_project(fleet_dir, "alpha")
    assert main(["hardening", "--root", str(fleet_dir)]) == 1
    out = capsys.readouterr().out
    assert "alpha:" in out
    assert "checks:" in out
    assert "memory:" in out


def test_hardening_json_is_parseable(fleet_dir: Path, capsys) -> None:
    make_project(fleet_dir, "alpha")
    main(["hardening", "--root", str(fleet_dir), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["project"] == "alpha"
    assert {"checks", "memory"} <= {item["category"] for item in payload[0]["items"]}


def test_audit_markdown_renders_heading(fleet_dir: Path, capsys) -> None:
    make_project(fleet_dir, "alpha")
    main(["audit", "--root", str(fleet_dir), "--markdown"])
    assert "## alpha" in capsys.readouterr().out


def test_audit_digest_first_run_exits_one_then_zero(fleet_dir: Path, tmp_path, monkeypatch) -> None:
    # First --digest run surfaces new issues (exit 1); an unchanged re-run has
    # no new issues (exit 0).
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    make_project(fleet_dir, "alpha")
    assert main(["audit", "--root", str(fleet_dir), "--digest"]) == 1
    assert main(["audit", "--root", str(fleet_dir), "--digest"]) == 0


def test_audit_digest_reports_no_change_on_second_run(
    fleet_dir: Path, tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    make_project(fleet_dir, "alpha")
    main(["audit", "--root", str(fleet_dir), "--digest"])
    capsys.readouterr()
    main(["audit", "--root", str(fleet_dir), "--digest"])
    assert "no change since last run" in capsys.readouterr().out


def test_audit_digest_webhook_posts_the_delta(fleet_dir: Path, monkeypatch) -> None:
    # The scheduled job's whole point: a changed digest reaches the sink (#98).
    posted: dict[str, object] = {}
    monkeypatch.setattr(cli, "post_payload", lambda url, payload: posted.update(url=url, **payload))
    make_project(fleet_dir, "alpha")
    main(["audit", "--root", str(fleet_dir), "--digest", "--webhook", "http://hook"])
    assert posted["url"] == "http://hook"
    assert posted["changed"] is True


def test_audit_digest_webhook_stays_silent_when_nothing_changed(
    fleet_dir: Path, monkeypatch
) -> None:
    # A daily cron must not post "no change" every day — only deltas.
    calls: list[str] = []
    monkeypatch.setattr(cli, "post_payload", lambda url, _payload: calls.append(url))
    make_project(fleet_dir, "alpha")
    main(["audit", "--root", str(fleet_dir), "--digest", "--webhook", "http://hook"])
    calls.clear()
    main(["audit", "--root", str(fleet_dir), "--digest", "--webhook", "http://hook"])
    assert calls == []


def test_audit_digest_survives_a_failing_webhook(fleet_dir: Path, monkeypatch) -> None:
    # Delivery is best-effort: a dead sink must not break the audit's exit code.
    monkeypatch.setattr(cli, "post_payload", lambda _url, _payload: False)
    make_project(fleet_dir, "alpha")
    assert main(["audit", "--root", str(fleet_dir), "--digest", "--webhook", "http://hook"]) == 1


def test_audit_webhook_without_digest_is_rejected(fleet_dir: Path) -> None:
    # --webhook only means something for a digest; a full report has no delta.
    make_project(fleet_dir, "alpha")
    assert main(["audit", "--root", str(fleet_dir), "--webhook", "http://hook"]) == 2


def test_ci_offline_exits_zero(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    assert main(["ci", "--root", str(fleet_dir)]) == 0


def test_ci_json_reports_status(fleet_dir: Path, capsys) -> None:
    make_project(fleet_dir, "alpha")
    main(["ci", "--root", str(fleet_dir), "--json"])
    assert json.loads(capsys.readouterr().out)[0]["ci"] == "unknown"


def test_ci_gitlab_host_reports_merge_requests(fleet_dir: Path, capsys) -> None:
    # A gitlab.com host routes through the glab adapter — the count unit is MR.
    config = (
        "project:\n  name: alpha\n  project_init_contract_version: 1\n"
        "  project_init_host: gitlab.com\nlanguage: python\n"
    )
    make_project(fleet_dir, "alpha", config_text=config)
    main(["ci", "--root", str(fleet_dir)])
    assert "open MR(s)" in capsys.readouterr().out


def test_upgrade_plan_offline_renders(fleet_dir: Path, capsys, monkeypatch) -> None:
    make_project(fleet_dir, "alpha")
    monkeypatch.setattr(cli, "latest_upstream_version", lambda _cwd: None)
    main(["upgrade-plan", "--root", str(fleet_dir)])
    assert "alpha: unknown" in capsys.readouterr().out


def test_upgrade_plan_json_has_status(fleet_dir: Path, capsys, monkeypatch) -> None:
    make_project(fleet_dir, "alpha")
    monkeypatch.setattr(cli, "latest_upstream_version", lambda _cwd: None)
    main(["upgrade-plan", "--root", str(fleet_dir), "--json"])
    assert json.loads(capsys.readouterr().out)[0]["status"] == "unknown"


def test_upgrade_plan_renders_outdated_when_upstream_available(
    fleet_dir: Path, capsys, monkeypatch
) -> None:
    make_project(fleet_dir, "alpha")
    monkeypatch.setattr(cli, "latest_upstream_version", lambda _cwd: (0, 6, 0))
    main(["upgrade-plan", "--root", str(fleet_dir)])
    assert "alpha: outdated" in capsys.readouterr().out


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


def test_checks_records_history_read_back_by_history_command(
    fleet_dir: Path, tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    make_project(fleet_dir, "alpha", tooling={"test": "true"})
    main(["checks", "--root", str(fleet_dir), "--task", "test"])
    capsys.readouterr()
    assert main(["history", "alpha", "--root", str(fleet_dir)]) == 0
    out = capsys.readouterr().out
    assert "test:" in out and "+" in out


def test_history_unknown_project_exits_2(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    assert main(["history", "ghost", "--root", str(fleet_dir)]) == 2


def test_history_no_runs_is_friendly(fleet_dir: Path, tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    make_project(fleet_dir, "alpha")
    main(["history", "alpha", "--root", str(fleet_dir)])
    assert "no check history yet" in capsys.readouterr().out


def test_notify_clean_fleet_exits_zero(fleet_dir: Path, capsys) -> None:
    make_project(fleet_dir, "alpha", tooling={"test": "true"})
    assert main(["notify", "--root", str(fleet_dir)]) == 0
    assert "no alerts" in capsys.readouterr().out


def test_notify_with_alerts_exits_one(fleet_dir: Path) -> None:
    # A project shipping git hooks that aren't installed trips a hooks alert.
    project = make_project(fleet_dir, "alpha")
    source = project / ".github" / "hooks"
    source.mkdir(parents=True)
    (source / "pre-commit").write_text("#!/bin/sh\n", encoding="utf-8")
    assert main(["notify", "--root", str(fleet_dir)]) == 1


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
