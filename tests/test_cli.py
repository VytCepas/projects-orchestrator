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


def test_snapshot_json_has_descriptor(fleet_dir: Path, capsys) -> None:
    make_project(fleet_dir, "alpha")
    main(["snapshot", "--root", str(fleet_dir), "--json"])
    assert json.loads(capsys.readouterr().out)[0]["descriptor"]["name"] == "alpha"


def test_fleet_file_drives_discovery(fleet_dir: Path, tmp_path: Path, capsys) -> None:
    make_project(fleet_dir, "alpha")
    fleet_file = tmp_path / "fleet.yaml"
    fleet_file.write_text(f'roots: ["{fleet_dir}"]\n', encoding="utf-8")
    main(["projects", "--fleet", str(fleet_file)])
    assert "alpha" in capsys.readouterr().out
