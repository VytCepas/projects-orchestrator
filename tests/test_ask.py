"""Optional /ask mode: disabled by default, intent-selection only when enabled."""

from __future__ import annotations

import json
from pathlib import Path

from conftest import make_project

from projects_orchestrator.ask import (
    ALLOWED_VERBS,
    DISABLED_MESSAGE,
    NO_KEY_MESSAGE,
    build_prompt,
    parse_intent_reply,
    resolve_ask,
)
from projects_orchestrator.controller import ControllerContext, Intent, dispatch, parse_command
from projects_orchestrator.registry import FleetConfig


def test_resolve_ask_disabled_by_default() -> None:
    assert resolve_ask("run tests", ("alpha",), {}) == DISABLED_MESSAGE


def test_resolve_ask_without_api_key_is_actionable() -> None:
    env = {"ORCHESTRATOR_ASK_MODEL": "claude-haiku-4-5"}
    assert resolve_ask("run tests", ("alpha",), env) == NO_KEY_MESSAGE


def test_resolve_ask_empty_question_gives_usage() -> None:
    env = {"ORCHESTRATOR_ASK_MODEL": "claude-haiku-4-5"}
    result = resolve_ask("  ", ("alpha",), env, complete=lambda _m, _p: "{}")
    assert result == "usage: /ask <question>"


def test_resolve_ask_maps_reply_to_intent() -> None:
    env = {"ORCHESTRATOR_ASK_MODEL": "claude-haiku-4-5"}
    reply = json.dumps({"verb": "check", "target": "alpha", "args": ["test"]})
    result = resolve_ask("run tests on alpha", ("alpha",), env, complete=lambda _m, _p: reply)
    assert result == Intent(verb="check", target="alpha", args=("test",))


def test_resolve_ask_unmappable_reply_degrades() -> None:
    env = {"ORCHESTRATOR_ASK_MODEL": "claude-haiku-4-5"}
    result = resolve_ask("hi", ("alpha",), env, complete=lambda _m, _p: "no json here")
    assert isinstance(result, str)


def test_parse_intent_reply_rejects_unknown_verb() -> None:
    assert parse_intent_reply(json.dumps({"verb": "rm -rf"})) is None


def test_parse_intent_reply_never_selects_ask() -> None:
    assert "ask" not in ALLOWED_VERBS


def test_parse_intent_reply_extracts_json_from_prose() -> None:
    text = 'Sure! Here you go: {"verb": "status"} — hope that helps.'
    assert parse_intent_reply(text) == Intent(verb="status")


def test_parse_intent_reply_ignores_blank_target() -> None:
    assert parse_intent_reply(json.dumps({"verb": "status", "target": " "})) == Intent(
        verb="status"
    )


def test_build_prompt_lists_projects() -> None:
    assert "alpha, beta" in build_prompt("q", ("alpha", "beta"))


def test_dispatch_ask_disabled_by_default(fleet_dir: Path, monkeypatch) -> None:
    monkeypatch.delenv("ORCHESTRATOR_ASK_MODEL", raising=False)
    make_project(fleet_dir, "alpha")
    ctx = ControllerContext(config=FleetConfig(roots=(fleet_dir,)))
    lines = list(dispatch(parse_command("/ask anything"), ctx))
    assert "not enabled" in lines[0]


def test_dispatch_ask_enabled_runs_selected_intent(
    fleet_dir: Path, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("ORCHESTRATOR_ASK_MODEL", "claude-haiku-4-5")
    make_project(fleet_dir, "alpha", tooling={"lint": "true"})
    reply = json.dumps({"verb": "check", "target": "alpha", "args": ["lint"]})
    ctx = ControllerContext(
        config=FleetConfig(roots=(fleet_dir,)),
        cache_file=tmp_path / "checks.json",
        ask_complete=lambda _m, _p: reply,
    )
    lines = list(dispatch(parse_command("/ask lint alpha please"), ctx))
    assert any("alpha lint: PASS" in line for line in lines)
