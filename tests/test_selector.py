"""Selectors: filter the fleet on what it already knows — and never lie about it.

The load-bearing property is not "the filter works". It is that a filter which
CANNOT work says so. A mistyped `--where` that silently matches nothing looks
exactly like a healthy fleet; one that silently matches everything is worse.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import git_init, make_project

from projects_orchestrator.fleet import fleet_snapshots
from projects_orchestrator.registry import FleetConfig, discover
from projects_orchestrator.selector import (
    FIELDS,
    SelectorError,
    Term,
    parse_term,
    select,
)


def _snapshots(fleet_dir: Path, cache_file: Path | None = None) -> list:
    return fleet_snapshots(discover(FleetConfig(roots=(fleet_dir,))), cache_file)


def _names(snapshots: list) -> list[str]:
    return sorted(s.descriptor.name for s in snapshots)


# --- Parsing: nonsense is refused, loudly -------------------------------------


def test_an_unknown_field_is_refused() -> None:
    # THE guard. `--where cli=fail` (a typo) must not quietly select nothing.
    with pytest.raises(SelectorError, match="unknown field 'cli'"):
        parse_term("cli=fail")


def test_an_unknown_field_names_the_real_ones() -> None:
    # An error that does not tell you the valid fields just makes you guess again.
    with pytest.raises(SelectorError, match="scaffold"):
        parse_term("scafold=none")  # codespell:ignore


@pytest.mark.parametrize("nonsense", ["", "  ", "nofield", "=value", "ci", "ci~fail"])
def test_unparseable_expressions_are_refused(nonsense: str) -> None:
    with pytest.raises(SelectorError):
        parse_term(nonsense)


def test_select_refuses_rather_than_returning_everything() -> None:
    # The worst possible failure: a bad filter that silently selects the whole
    # fleet, which an operator would then act on.
    with pytest.raises(SelectorError):
        select([], ["cli=fail"])


@pytest.mark.parametrize(
    ("expression", "field", "op", "value"),
    [
        ("ci=fail", "ci", "=", "fail"),
        ("scaffold=none", "scaffold", "=", "none"),
        ("drift>0", "drift", ">", "0"),
        ("lint!=pass", "lint", "!=", "pass"),
        ("drift>=2", "drift", ">=", "2"),
        ("drift<=1", "drift", "<=", "1"),
        ("ci = fail", "ci", "=", "fail"),  # whitespace tolerated
    ],
)
def test_valid_expressions_parse(expression: str, field: str, op: str, value: str) -> None:
    term = parse_term(expression)
    assert (term.field, term.op, term.value) == (field, op, value)


def test_two_character_operators_are_matched_before_one() -> None:
    # `drift>=1` must not match on the bare `>` and yield the value `=1`, which
    # would parse as no number and silently match nothing.
    assert parse_term("drift>=1").op == ">="
    assert parse_term("lint!=pass").op == "!="


# --- Filtering ----------------------------------------------------------------


def test_no_selector_selects_everything(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    make_project(fleet_dir, "beta")
    assert len(select(_snapshots(fleet_dir), [])) == 2


def test_selecting_by_name(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    make_project(fleet_dir, "beta")
    assert _names(select(_snapshots(fleet_dir), ["name=beta"])) == ["beta"]


def test_selecting_by_language(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    assert _names(select(_snapshots(fleet_dir), ["language=python"])) in ([], ["alpha"])


def test_a_matching_nothing_selector_returns_empty_not_everything(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    assert select(_snapshots(fleet_dir), ["name=nonexistent"]) == []


def test_terms_combine_with_and(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    make_project(fleet_dir, "beta")
    selected = select(_snapshots(fleet_dir), ["name=alpha", "name=beta"])
    assert selected == []  # AND, not OR


def test_not_equals_inverts(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")
    make_project(fleet_dir, "beta")
    assert _names(select(_snapshots(fleet_dir), ["name!=alpha"])) == ["beta"]


# --- The gate fields, and the honesty of `unknown` ------------------------------


def test_a_gate_nobody_has_run_is_unknown_not_pass(fleet_dir: Path) -> None:
    # "We have never run this project's tests" and "its tests pass" are different
    # facts. A fleet tool that conflates them silently skips the projects it knows
    # LEAST about — exactly the ones that need attention.
    #
    # NOTE the gate chosen: `test` is genuinely ABSENT from a fresh snapshot's
    # checks. `ci` is not — the snapshot pre-populates it with an explicit
    # `unknown` — so an assertion on `ci` cannot tell a real fallback from the
    # pre-populated value, and would pass even if the fallback said "pass".
    make_project(fleet_dir, "alpha")
    snapshots = _snapshots(fleet_dir)
    assert "test" not in snapshots[0].checks  # the precondition this test relies on

    assert _names(select(snapshots, ["test=unknown"])) == ["alpha"]
    assert select(snapshots, ["test=pass"]) == []


def test_selecting_a_failing_gate_from_the_cache(fleet_dir: Path, tmp_path: Path) -> None:
    import json

    make_project(fleet_dir, "alpha")
    make_project(fleet_dir, "beta")
    cache_file = tmp_path / "checks.json"
    cache_file.write_text(
        json.dumps(
            {
                "alpha": {
                    "lint": {"project": "alpha", "task": "lint", "status": "fail", "detail": "E501"}
                },
                "beta": {
                    "lint": {"project": "beta", "task": "lint", "status": "pass", "detail": ""}
                },
            }
        ),
        encoding="utf-8",
    )
    selected = select(_snapshots(fleet_dir, cache_file), ["lint=fail"])
    assert _names(selected) == ["alpha"]


# --- Numeric comparison --------------------------------------------------------


def test_a_non_numeric_value_never_matches_a_numeric_comparison(fleet_dir: Path) -> None:
    # `drift>abc` is nonsense; it must match nothing rather than raise or match all.
    make_project(fleet_dir, "alpha")
    assert select(_snapshots(fleet_dir), ["drift>abc"]) == []


def test_drift_zero_is_not_greater_than_zero(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha")
    git_init(project)
    assert select(_snapshots(fleet_dir), ["drift>0"]) == []


# --- The field registry --------------------------------------------------------


def test_the_advertised_fields_all_actually_work(fleet_dir: Path) -> None:
    # Guards the registry against drifting from its own advertisement: every field
    # named in the help text must be filterable, or the error message lies.
    make_project(fleet_dir, "alpha")
    snapshots = _snapshots(fleet_dir)
    for field in FIELDS:
        term = Term(field=field, op="=", value="__nothing_matches_this__")
        assert term.matches(snapshots[0]) is False  # it evaluates without raising


def _plain_repo(fleet_dir: Path, name: str) -> Path:
    """A git repo with NO project-init scaffold — what the rollout targets."""
    import subprocess

    repo = fleet_dir / name
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    return repo


def test_scaffold_none_finds_an_unscaffolded_repo(fleet_dir: Path) -> None:
    # The target list for the project-init rollout (#122): repos with no scaffold.
    _plain_repo(fleet_dir, "legacy-app")
    make_project(fleet_dir, "alpha")  # already scaffolded (the fixture writes a version)
    fleet = discover(FleetConfig(roots=(fleet_dir,), include_plain_repos=True))
    snapshots = fleet_snapshots(fleet)
    assert _names(select(snapshots, ["scaffold=none"])) == ["legacy-app"]


def test_an_unscaffolded_repo_is_invisible_without_include_plain_repos(fleet_dir: Path) -> None:
    # A sharp edge worth pinning: `include_plain_repos` defaults to FALSE, so by
    # default the orchestrator cannot SEE the very repos that need project-init.
    # `scaffold=none` is only meaningful once discovery is told to include them —
    # otherwise the rollout's target list is silently empty, which reads exactly
    # like "there is nothing to roll out".
    _plain_repo(fleet_dir, "legacy-app")
    default_fleet = discover(FleetConfig(roots=(fleet_dir,)))
    assert select(fleet_snapshots(default_fleet), ["scaffold=none"]) == []

    opted_in = discover(FleetConfig(roots=(fleet_dir,), include_plain_repos=True))
    assert _names(select(fleet_snapshots(opted_in), ["scaffold=none"])) == ["legacy-app"]


# --- The CLI surface -----------------------------------------------------------


def test_cli_status_filters_with_where(fleet_dir: Path, capsys) -> None:
    from projects_orchestrator.__main__ import main

    make_project(fleet_dir, "alpha")
    make_project(fleet_dir, "beta")
    assert main(["status", "--root", str(fleet_dir), "--where", "name=beta"]) == 0
    out = capsys.readouterr().out
    assert "beta" in out
    assert "alpha" not in out


def test_cli_an_unknown_field_exits_two(fleet_dir: Path, capsys) -> None:
    # It must NOT exit 0 with an empty table, which reads as "all healthy".
    from projects_orchestrator.__main__ import main

    make_project(fleet_dir, "alpha")
    assert main(["status", "--root", str(fleet_dir), "--where", "cli=fail"]) == 2
    assert "unknown field" in capsys.readouterr().err


def test_cli_a_selector_matching_nothing_says_so(fleet_dir: Path, capsys) -> None:
    from projects_orchestrator.__main__ import main

    make_project(fleet_dir, "alpha")
    assert main(["status", "--root", str(fleet_dir), "--where", "name=nope"]) == 0
    assert "no projects match" in capsys.readouterr().out


def test_cli_work_where_is_dry_run_by_default(
    fleet_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    # THE guard. A --where that matches forty projects would launch forty agents,
    # and the most likely reason it matches forty is that the filter is wrong.
    # Assert on BEHAVIOUR: no run is launched without --apply.
    from projects_orchestrator import work
    from projects_orchestrator.__main__ import main

    make_project(fleet_dir, "alpha")
    launched: list[str] = []
    monkeypatch.setattr(work, "launch", lambda d, _t, **_k: launched.append(d.name))

    code = main(["work", "--where", "name=alpha", "fix it", "--root", str(fleet_dir)])
    assert code == 0
    assert launched == []  # nothing launched
    out = capsys.readouterr().out
    assert "would launch" in out
    assert "--apply" in out


def test_cli_work_where_launches_with_apply(
    fleet_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from projects_orchestrator import runs, work
    from projects_orchestrator.__main__ import main

    make_project(fleet_dir, "alpha")
    make_project(fleet_dir, "beta")
    launched: list[str] = []

    def fake_launch(descriptor, task, **_kwargs):
        launched.append(descriptor.name)
        return runs.AgentRun(id="x", project=descriptor.name, task=task, state=runs.RUNNING)

    monkeypatch.setattr(work, "launch", fake_launch)
    code = main(["work", "--where", "name=alpha", "fix it", "--apply", "--root", str(fleet_dir)])
    assert code == 0
    assert launched == ["alpha"]  # only the matching project


def test_cli_work_where_with_a_bad_field_launches_nothing(
    fleet_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Even with --apply: a typo must not fan out across the whole fleet.
    from projects_orchestrator import work
    from projects_orchestrator.__main__ import main

    make_project(fleet_dir, "alpha")
    launched: list[str] = []
    monkeypatch.setattr(work, "launch", lambda d, _t, **_k: launched.append(d.name))

    assert main(["work", "--where", "cli=fail", "t", "--apply", "--root", str(fleet_dir)]) == 2
    assert launched == []
