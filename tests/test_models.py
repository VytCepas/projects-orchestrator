"""The model table: one place that says which tier and effort each task class runs on (#324)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from projects_orchestrator import models
from projects_orchestrator.models import ModelChoice, ModelTableError, parse_table
from projects_orchestrator.registry import load_fleet_config


def _entry(model: str = "sonnet", effort: str = "high", fallback: str = "opus") -> dict[str, str]:
    return {"model": model, "effort": effort, "fallback": fallback}


# --- The shipped defaults ------------------------------------------------------


def test_the_defaults_cover_every_task_class() -> None:
    assert set(models.DEFAULT_TABLE) == {models.HEAL, models.WORK, models.ATTACH}


def test_the_defaults_follow_the_tier_decision() -> None:
    # #324: scoped fixes on Sonnet; a human taking over a FAILED run escalates.
    assert models.DEFAULT_TABLE[models.HEAL].model == "sonnet"
    assert models.DEFAULT_TABLE[models.WORK].model == "sonnet"
    assert models.DEFAULT_TABLE[models.ATTACH].model == "opus"


def test_the_defaults_pass_their_own_validation() -> None:
    raw = {
        task: {"model": c.model, "effort": c.effort, "fallback": c.fallback}
        for task, c in models.DEFAULT_TABLE.items()
    }
    assert parse_table(raw) == dict(models.DEFAULT_TABLE)


# --- Argv ----------------------------------------------------------------------


def test_a_choice_renders_all_three_flags() -> None:
    assert ModelChoice("haiku", "low", "sonnet").cli_args() == [
        "--model",
        "haiku",
        "--effort",
        "low",
        "--fallback-model",
        "sonnet",
    ]


# --- Validation: a bad value is rejected at load --------------------------------


def test_an_override_replaces_only_the_named_class() -> None:
    table = parse_table({"heal": _entry("haiku", "low", "sonnet")})
    assert table[models.HEAL] == ModelChoice("haiku", "low", "sonnet")
    assert table[models.WORK] == models.DEFAULT_TABLE[models.WORK]


def test_no_override_is_the_defaults() -> None:
    assert parse_table(None) == dict(models.DEFAULT_TABLE)


@pytest.mark.parametrize(
    "bad",
    [
        _entry(model="gpt-5"),  # not a tier at all
        _entry(model="claude-sonnet-5-5"),  # a pinned id: goes stale, ticket forbids it
        _entry(model="Sonnet"),  # aliases are exact
        _entry(fallback="claude-opus-5-5"),  # pinned ids are refused in the fallback too
        _entry(effort="turbo"),
        _entry(model="sonnet", fallback="sonnet"),  # a fallback to itself falls back to nothing
        {"model": "sonnet", "effort": "high"},  # incomplete: fallback missing
        {**_entry(), "extra": "x"},  # unknown key — a typo must not pass silently
        "sonnet",  # not a mapping
    ],
)
def test_a_bad_entry_is_rejected(bad: object) -> None:
    with pytest.raises(ModelTableError):
        parse_table({"work": bad})


def test_an_unknown_task_class_is_rejected() -> None:
    with pytest.raises(ModelTableError, match="scan"):
        parse_table({"scan": _entry("haiku", "low", "sonnet")})


def test_a_non_mapping_table_is_rejected() -> None:
    with pytest.raises(ModelTableError):
        parse_table(["heal"])


def test_the_error_names_the_offending_value() -> None:
    with pytest.raises(ModelTableError, match="claude-sonnet-5-5"):
        parse_table({"work": _entry(model="claude-sonnet-5-5")})


# --- Records: the launcher's choice survives a process boundary ------------------


def test_a_valid_record_becomes_a_choice() -> None:
    assert models.from_record("haiku", "low", "opus") == ModelChoice("haiku", "low", "opus")


@pytest.mark.parametrize(
    "fields",
    [("", "", ""), ("claude-sonnet-5-5", "high", "opus"), ("sonnet", "turbo", "opus")],
)
def test_an_unusable_record_is_no_choice(fields: tuple[str, str, str]) -> None:
    assert models.from_record(*fields) is None


# --- The active table ------------------------------------------------------------


def test_choice_for_reads_the_configured_table(monkeypatch: pytest.MonkeyPatch) -> None:
    table = {**models.DEFAULT_TABLE, models.HEAL: ModelChoice("haiku", "low", "sonnet")}
    monkeypatch.setattr(models, "_active", table)
    assert models.choice_for(models.HEAL) == ModelChoice("haiku", "low", "sonnet")


def test_configure_replaces_the_active_table(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(models, "_active", models.DEFAULT_TABLE)  # restored on teardown
    table = {**models.DEFAULT_TABLE, models.WORK: ModelChoice("opus", "max", "sonnet")}
    models.configure(table)
    assert models.choice_for(models.WORK).model == "opus"


# --- fleet.yaml is where the table is configured ---------------------------------


def _fleet(tmp_path: Path, body: dict[str, object]) -> Path:
    path = tmp_path / "fleet.yaml"
    path.write_text(yaml.safe_dump(body), encoding="utf-8")
    return path


def test_fleet_yaml_overrides_a_class(tmp_path: Path) -> None:
    config = load_fleet_config(
        _fleet(tmp_path, {"models": {"heal": _entry("haiku", "low", "opus")}})
    )
    assert config.models[models.HEAL] == ModelChoice("haiku", "low", "opus")
    assert config.warnings == ()


def test_fleet_yaml_without_models_carries_the_defaults(tmp_path: Path) -> None:
    config = load_fleet_config(_fleet(tmp_path, {"roots": []}))
    assert config.models == models.DEFAULT_TABLE


def test_fleet_yaml_with_a_pinned_id_is_rejected_with_a_warning(tmp_path: Path) -> None:
    # load_fleet_config never raises (ADR-003), so "rejected at load" is: the
    # whole block is refused, the defaults stand, and the operator is told why.
    body = {"models": {"heal": _entry("haiku", "low", "opus"), "work": _entry("claude-sonnet-5-5")}}
    config = load_fleet_config(_fleet(tmp_path, body))
    assert config.models == models.DEFAULT_TABLE
    assert any("claude-sonnet-5-5" in w for w in config.warnings)


def test_the_cli_activates_the_fleet_files_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from projects_orchestrator.__main__ import main

    monkeypatch.setattr(models, "_active", models.DEFAULT_TABLE)  # restored on teardown
    fleet = _fleet(tmp_path, {"roots": [], "models": {"work": _entry("opus", "max", "sonnet")}})
    main(["projects", "--fleet", str(fleet)])
    assert models.choice_for(models.WORK) == ModelChoice("opus", "max", "sonnet")
