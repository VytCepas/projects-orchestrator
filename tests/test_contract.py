"""Producer→consumer contract tripwire (epic #68, WS1 / #69).

The orchestrator's other tests parse configs written by its *own* `conftest.py`
template — a private copy of the descriptor contract. If project-init changes
the emitted shape, those tests still pass and the drift ships silently.

This test instead parses a **real, generated** project-init `config.yaml`
(`tests/fixtures/project_init/`, refreshed from a pinned project-init version)
and asserts every field `descriptor.py` reads is present and correctly typed.
It fails when project-init drops, renames, or retypes a contract field the
orchestrator depends on — turning "sync" into a CI failure, not a fleet
surprise. It also documents the currently-*unemitted* v2 surfaces so the day
project-init starts emitting them is a deliberate, reviewed fixture change.
"""

from __future__ import annotations

import json
from pathlib import Path

from projects_orchestrator.adapters.project_init import parse_scaffold_result
from projects_orchestrator.capabilities import MCP, SKILL, parse_capabilities
from projects_orchestrator.descriptor import parse_config
from projects_orchestrator.drift import _load_manifest

_FIXTURES = Path(__file__).parent / "fixtures" / "project_init"
_CONFIG_V1 = _FIXTURES / "config.v1.yaml"
_SCAFFOLD_RESULT_V1 = _FIXTURES / "scaffold_result.v1.json"
_CAPABILITIES_V1 = _FIXTURES / "capabilities.v1.md"


def _descriptor(tmp_path: Path):
    # parse_config needs a project dir for path resolution; the config text is
    # the real generated fixture.
    (tmp_path / ".claude").mkdir(parents=True, exist_ok=True)
    text = _CONFIG_V1.read_text(encoding="utf-8")
    return parse_config(text, tmp_path)


def test_real_config_parses_without_warnings(tmp_path: Path) -> None:
    # A pristine project-init config must parse cleanly — any warning means the
    # orchestrator failed to understand a field the scaffolder emitted.
    assert _descriptor(tmp_path).warnings == ()


def test_real_config_exposes_every_field_the_orchestrator_reads(tmp_path: Path) -> None:
    d = _descriptor(tmp_path)
    assert d.name == "demo-service"
    assert d.language == "python"
    assert d.delivery == "service"
    assert d.contract_version == 1
    assert d.project_init_version == "1.0.0"
    assert d.host == "github.com"  # drives forge-adapter selection
    assert d.memory_tier == 0
    assert d.memory_path == tmp_path / ".claude/memory"
    # tooling gates every check/run the orchestrator can drive
    assert {"lint", "format", "test"} <= set(d.tooling)


def test_real_config_carries_a_hashable_scaffold_manifest(tmp_path: Path) -> None:
    # drift detection reads scaffold.manifest (file → sha256) from the same config.
    (tmp_path / ".claude").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".claude/config.yaml").write_text(
        _CONFIG_V1.read_text(encoding="utf-8"), encoding="utf-8"
    )
    manifest = _load_manifest(tmp_path)
    assert manifest, "scaffold.manifest is the whole contract for drift detection"
    assert all(len(sha) == 64 for sha in manifest.values())  # sha256 hex


def test_real_capabilities_md_exposes_the_skill_inventory() -> None:
    # CAPABILITIES.md is the ADR-025 §3 capability inventory the fleet aggregates.
    # A real github-lifecycle scaffold ships a non-empty skill set the parser reads.
    inventory = parse_capabilities(
        _CAPABILITIES_V1.read_text(encoding="utf-8"), "demo-service", _CAPABILITIES_V1
    )
    assert inventory.of_kind(SKILL), "the capability inventory must expose skills"


def test_real_capabilities_md_v1_ships_no_mcp_servers() -> None:
    # demo-service scaffolds with installed_mcps: none; when project-init emits a
    # scaffold that wires MCP servers this fixture refresh flips deliberately.
    inventory = parse_capabilities(
        _CAPABILITIES_V1.read_text(encoding="utf-8"), "demo-service", _CAPABILITIES_V1
    )
    assert inventory.of_kind(MCP) == ()


def test_scaffold_result_json_carries_the_registration_seam() -> None:
    # The --json seam (#510) is what an orchestrator reads to register a freshly
    # scaffolded project without a second config read. Pin its shape.
    result = json.loads(_SCAFFOLD_RESULT_V1.read_text(encoding="utf-8"))
    assert result["contract_version"] == "1"
    assert result["config"] == ".claude/config.yaml"
    assert set(result["memory"]) >= {"tier", "stack", "memory_path"}
    assert isinstance(result["files_created"], int)


def test_scaffold_result_parses_through_the_consumer() -> None:
    # The real --json fixture must parse through the seam the `register` command
    # uses, so an upstream shape change fails here rather than at register time.
    parsed = parse_scaffold_result(_SCAFFOLD_RESULT_V1.read_text(encoding="utf-8"))
    assert parsed is not None
    assert parsed.contract_version == 1  # string "1" upstream → int for the reader


# --- Known contract state: v2 surfaces are NOT yet emitted (epic #68) ---
# These assert the *current* truth so that when project-init emits contract v2
# (VytCepas/project-init#604) the fixture refresh flips them deliberately, under
# review — rather than the orchestrator's v2 read paths silently staying dead.


def test_v1_fixture_does_not_yet_declare_contract_v2(tmp_path: Path) -> None:
    assert _descriptor(tmp_path).contract_version < 2


def test_v1_fixture_has_no_deploy_block_or_run_command(tmp_path: Path) -> None:
    d = _descriptor(tmp_path)
    # deploy is still a scalar upstream (type collision tracked in #604), so the
    # v2 deploy block is absent; run_command is not yet emitted either.
    assert d.deploy is None
    assert "run" not in d.tooling
