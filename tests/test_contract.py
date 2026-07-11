"""Producer→consumer contract tripwire (epic #68, WS1 / #69).

The orchestrator's other tests parse configs written by its *own* `conftest.py`
template — a private copy of the descriptor contract. If project-init changes
the emitted shape, those tests still pass and the drift ships silently.

This test instead parses a **real, generated** project-init `config.yaml`
(`tests/fixtures/project_init/`, refreshed from a pinned project-init version)
and asserts every field `descriptor.py` reads is present and correctly typed.
It fails when project-init drops, renames, or retypes a contract field the
orchestrator depends on — turning "sync" into a CI failure, not a fleet
surprise. Two fixtures are pinned: a legacy `.claude/`-layout v1 scaffold and a
current `.agents/`-layout v2 scaffold (PI-627), so both the relocation and the
real contract-v2 shape (deploy / hooks.expected / observability.path) are
guarded against silent drift.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from projects_orchestrator.adapters.project_init import parse_scaffold_result
from projects_orchestrator.capabilities import MCP, SKILL, parse_capabilities
from projects_orchestrator.descriptor import load_descriptor, parse_config
from projects_orchestrator.drift import _load_manifest

_FIXTURES = Path(__file__).parent / "fixtures" / "project_init"
_CONFIG_V1 = _FIXTURES / "config.v1.yaml"
_SCAFFOLD_RESULT_V1 = _FIXTURES / "scaffold_result.v1.json"
_CAPABILITIES_V1 = _FIXTURES / "capabilities.v1.md"
_CONFIG_V2 = _FIXTURES / "config.v2.yaml"
_SCAFFOLD_RESULT_V2 = _FIXTURES / "scaffold_result.v2.json"
_CAPABILITIES_V2 = _FIXTURES / "capabilities.v2.md"


def _descriptor(tmp_path: Path):
    # The v1 fixture is a legacy .claude/-layout scaffold; parse it there to
    # prove the orchestrator still reads pre-PI-627 projects.
    (tmp_path / ".claude").mkdir(parents=True, exist_ok=True)
    text = _CONFIG_V1.read_text(encoding="utf-8")
    return parse_config(text, tmp_path, config_root=".claude")


def _descriptor_v2(tmp_path: Path):
    # The v2 fixture is a real current-project-init scaffold: config lives under
    # .agents/. Materialize it and go through load_descriptor so the *discovery*
    # path (resolve_config) is exercised, not just parsing.
    (tmp_path / ".agents").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".agents" / "config.yaml").write_text(
        _CONFIG_V2.read_text(encoding="utf-8"), encoding="utf-8"
    )
    descriptor = load_descriptor(tmp_path)
    assert descriptor is not None, "a real .agents/ scaffold must be discovered"
    return descriptor


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
    manifest = _load_manifest(tmp_path / ".claude" / "config.yaml")
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


# --- Contract v2: the real current-project-init shape (PI-627 + #603/#604) ---
# project-init now emits contract v2 at .agents/config.yaml with a structured
# deploy block, hooks.expected, observability.path, and run_command. These
# parse the *real* generated v2 fixture through the discovery + reader chain, so
# a producer that drops/renames/relocates any of them fails here, not on a fleet.


def test_v2_fixture_is_discovered_under_agents_layout(tmp_path: Path) -> None:
    d = _descriptor_v2(tmp_path)
    assert d.config_root == ".agents"
    assert d.warnings == ()


def test_v2_fixture_declares_contract_v2(tmp_path: Path) -> None:
    assert _descriptor_v2(tmp_path).contract_version == 2


def test_v2_fixture_carries_a_structured_deploy_block(tmp_path: Path) -> None:
    deploy = _descriptor_v2(tmp_path).deploy
    assert deploy is not None
    assert deploy.target == "cloud-run"  # drives the cloud-status probe
    assert deploy.app == "demo-service"


def test_v2_fixture_emits_run_command_and_hooks_and_observability(tmp_path: Path) -> None:
    d = _descriptor_v2(tmp_path)
    assert "run" in d.tooling  # the Runnable column / controller `run` verb
    assert tuple(d.hooks_expected) == ("pre-commit", "commit-msg", "pre-push")
    assert d.observability_path == tmp_path / ".agents" / "observability"


def test_v2_fixture_manifest_covers_the_descriptor(tmp_path: Path) -> None:
    # drift reads scaffold.manifest from .agents/config.yaml on a v2 scaffold.
    manifest = _load_manifest(_descriptor_v2(tmp_path).config_path)
    assert manifest, "scaffold.manifest is the whole contract for drift detection"
    assert all(len(sha) == 64 for sha in manifest.values())


def test_v2_capabilities_md_exposes_the_skill_inventory() -> None:
    inventory = parse_capabilities(
        _CAPABILITIES_V2.read_text(encoding="utf-8"), "demo-service", _CAPABILITIES_V2
    )
    assert inventory.of_kind(SKILL), "the v2 capability inventory must expose skills"


def test_v2_scaffold_result_seam_targets_agents_config() -> None:
    result = json.loads(_SCAFFOLD_RESULT_V2.read_text(encoding="utf-8"))
    assert result["contract_version"] == "2"
    assert result["config"] == ".agents/config.yaml"
    parsed = parse_scaffold_result(_SCAFFOLD_RESULT_V2.read_text(encoding="utf-8"))
    assert parsed is not None
    assert parsed.contract_version == 2


# --- Schema validation: the golden fixtures conform to the shipped JSON Schema ---
# project-init ships descriptor.schema.json + usage-event.schema.json as the
# machine source of truth (VytCepas/project-init#603, packaged via #786). The
# schemas are vendored under tests/fixtures/project_init/schemas/ (regenerate on
# a contract bump — see the fixtures README); validating the golden fixtures
# against them catches a schema-level drift the reader-based tripwire can miss.


_SCHEMAS = _FIXTURES / "schemas"
_DESCRIPTOR_SCHEMA = _SCHEMAS / "descriptor.schema.json"
_USAGE_EVENT_SCHEMA = _SCHEMAS / "usage-event.schema.json"


def _validate(instance, schema_path: Path) -> None:
    import jsonschema

    jsonschema.validate(
        instance=instance, schema=json.loads(schema_path.read_text(encoding="utf-8"))
    )


def test_v2_fixture_validates_against_descriptor_schema() -> None:
    # Only the v2 fixture is expected to conform: the schema IS the v2 contract,
    # and the v1 fixture is a legacy pre-schema shape (scalar deploy) kept solely
    # to prove the reader still parses `.claude/`-layout projects.
    _validate(yaml.safe_load(_CONFIG_V2.read_text(encoding="utf-8")), _DESCRIPTOR_SCHEMA)


def test_a_usage_event_validates_against_usage_event_schema() -> None:
    # The shape project-init's guards log (ts/hook/event/project required).
    event = {
        "ts": "2026-07-04T10:00:00Z",
        "hook": "github_command_guard",
        "event": "deny",
        "project": "demo-service",
        "command": "git push origin main",
        "session": "abc123",
    }
    _validate(event, _USAGE_EVENT_SCHEMA)


def test_vendored_descriptor_schema_is_the_v2_shape() -> None:
    # A stale vendored copy (e.g. a v1 schema) would silently weaken validation;
    # pin that the vendored schema still defines the v2 surfaces.
    schema = json.loads(_DESCRIPTOR_SCHEMA.read_text(encoding="utf-8"))
    assert "v2" in schema["title"]
    assert {"deploy", "observability", "hooks"} <= set(schema["properties"])
