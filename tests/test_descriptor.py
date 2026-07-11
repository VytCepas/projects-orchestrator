"""Descriptor parsing: contract v1 in, dataclass out, never raise."""

from __future__ import annotations

from pathlib import Path

from conftest import make_project, make_project_v2

from projects_orchestrator.capabilities import load_capabilities
from projects_orchestrator.descriptor import (
    load_descriptor,
    parse_config,
    parse_scaffold_version,
    resolve_config,
)
from projects_orchestrator.observability import observability_dir


def test_parse_scaffold_version_reads_dotted_integers() -> None:
    assert parse_scaffold_version("0.5.2") == (0, 5, 2)


def test_parse_scaffold_version_unknown_is_none() -> None:
    assert parse_scaffold_version("unknown") is None


def test_parse_scaffold_version_non_numeric_is_none() -> None:
    assert parse_scaffold_version("1.2.beta") is None


def test_parse_scaffold_version_two_components_is_none() -> None:
    assert parse_scaffold_version("0.6") is None


def test_parse_scaffold_version_bare_integer_is_none() -> None:
    assert parse_scaffold_version("999") is None


def test_parse_scaffold_version_orders_by_component() -> None:
    assert parse_scaffold_version("0.6.0") > parse_scaffold_version("0.5.9")


def test_load_descriptor_reads_name(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha")
    assert load_descriptor(project).name == "alpha"


def test_load_descriptor_reads_tooling_commands(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha", tooling={"lint": "true", "test": "false"})
    assert load_descriptor(project).tooling == {"lint": "true", "test": "false"}


def test_load_descriptor_reads_host(tmp_path: Path) -> None:
    text = "project:\n  name: alpha\n  project_init_host: gitlab.com\n"
    assert parse_config(text, tmp_path).host == "gitlab.com"


def test_load_descriptor_host_defaults_empty(tmp_path: Path) -> None:
    assert parse_config("project:\n  name: alpha\n", tmp_path).host == ""


def test_load_descriptor_reads_contract_version(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha")
    assert load_descriptor(project).contract_version == 1


def test_load_descriptor_resolves_memory_path(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha")
    assert load_descriptor(project).memory_path == project.resolve() / ".claude/memory"


def test_load_descriptor_returns_none_for_non_project(tmp_path: Path) -> None:
    assert load_descriptor(tmp_path) is None


def test_load_descriptor_non_utf8_config_does_not_raise(tmp_path: Path) -> None:
    # A config saved in a non-UTF-8 encoding must degrade, not crash discovery.
    project = tmp_path / "cafe"
    (project / ".claude").mkdir(parents=True)
    (project / ".claude" / "config.yaml").write_bytes(b"project:\n  name: caf\xe9\n")
    descriptor = load_descriptor(project)
    assert descriptor is not None


def test_parse_config_invalid_yaml_degrades_with_warning(tmp_path: Path) -> None:
    descriptor = parse_config("{unclosed: [", tmp_path)
    assert descriptor.warnings != ()


def test_parse_config_invalid_yaml_falls_back_to_dir_name(tmp_path: Path) -> None:
    descriptor = parse_config("{unclosed: [", tmp_path / "beta")
    assert descriptor.name == "beta"


def test_parse_config_empty_file_warns(tmp_path: Path) -> None:
    assert parse_config("", tmp_path).warnings == ("config.yaml is empty",)


def test_parse_config_ignores_blank_tooling_commands(tmp_path: Path) -> None:
    descriptor = parse_config('tooling:\n  lint_command: "  "\n', tmp_path)
    assert descriptor.has_task("lint") is False


def test_has_task_true_for_declared_command(fleet_dir: Path) -> None:
    project = make_project(fleet_dir, "alpha", tooling={"run": "echo hi"})
    assert load_descriptor(project).has_task("run") is True


def test_parse_config_tolerates_non_string_tooling_key(tmp_path: Path) -> None:
    # PyYAML coerces a bare `on:` key to the bool True; must not crash.
    descriptor = parse_config('tooling:\n  on: echo hi\n  lint_command: "ruff check"\n', tmp_path)
    assert descriptor.has_task("lint") is True


def test_v2_config_parses_deploy_block(fleet_dir: Path) -> None:
    descriptor = load_descriptor(make_project_v2(fleet_dir, "alpha", deploy_target="fly"))
    assert descriptor.deploy.target == "fly"


def test_v2_config_parses_health_url(fleet_dir: Path) -> None:
    project = make_project_v2(fleet_dir, "alpha", health_url="https://x.example/health")
    assert load_descriptor(project).deploy.health_url == "https://x.example/health"


def test_v2_config_resolves_observability_path(fleet_dir: Path) -> None:
    project = make_project_v2(fleet_dir, "alpha", observability_path="logs/agent")
    assert load_descriptor(project).observability_path == project.resolve() / "logs/agent"


def test_v2_config_parses_hooks_expected(fleet_dir: Path) -> None:
    descriptor = load_descriptor(make_project_v2(fleet_dir, "alpha"))
    assert descriptor.hooks_expected == ("pre-commit", "commit-msg")


def test_memory_path_escaping_project_root_is_clamped(tmp_path: Path) -> None:
    text = "project:\n  name: alpha\nmemory:\n  memory_path: ../../etc\n"
    descriptor = parse_config(text, tmp_path)
    assert descriptor.memory_path == tmp_path.resolve() / ".claude/memory"


def test_memory_path_escaping_project_root_warns(tmp_path: Path) -> None:
    text = "project:\n  name: alpha\nmemory:\n  memory_path: /etc\n"
    assert any("escapes the project root" in w for w in parse_config(text, tmp_path).warnings)


def test_observability_path_escaping_project_root_is_ignored(tmp_path: Path) -> None:
    text = (
        "project:\n  name: alpha\n  project_init_contract_version: 2\n"
        "observability:\n  path: ../../var/log\n"
    )
    descriptor = parse_config(text, tmp_path)
    assert descriptor.observability_path is None
    assert any("escapes the project root" in w for w in descriptor.warnings)


def test_v1_config_ignores_v2_fields(tmp_path: Path) -> None:
    text = (
        "project:\n  name: alpha\n  project_init_contract_version: 1\n"
        "deploy:\n  target: fly\n"
    )
    assert parse_config(text, tmp_path).deploy is None


def test_v1_config_has_no_hooks_expected(tmp_path: Path) -> None:
    text = (
        "project:\n  name: alpha\n  project_init_contract_version: 1\n"
        "hooks:\n  expected: [pre-commit]\n"
    )
    assert parse_config(text, tmp_path).hooks_expected == ()


def test_v2_config_without_deploy_block_is_none(tmp_path: Path) -> None:
    text = "project:\n  name: alpha\n  project_init_contract_version: 2\n"
    assert parse_config(text, tmp_path).deploy is None


def test_v2_deploy_defaults_to_none_target(tmp_path: Path) -> None:
    text = (
        "project:\n  name: alpha\n  project_init_contract_version: 2\n"
        "deploy:\n  app: svc\n"
    )
    assert parse_config(text, tmp_path).deploy.target == "none"


def _memory_config(tier: int, extra: str = "") -> str:
    return (
        "project:\n  name: alpha\n"
        f"memory:\n  tier: {tier}\n  stack: obsidian-graphify-rag\n"
        "  memory_path: .claude/memory\n" + extra
    )


def test_descriptor_reads_memory_stack(tmp_path: Path) -> None:
    assert parse_config(_memory_config(0), tmp_path).memory_stack == "obsidian-graphify-rag"


def test_memory_stack_defaults_unknown(tmp_path: Path) -> None:
    assert parse_config("project:\n  name: alpha\n", tmp_path).memory_stack == "unknown"


def test_tier1_config_exposes_vault_path(tmp_path: Path) -> None:
    descriptor = parse_config(_memory_config(1, "  vault_path: .claude/vault\n"), tmp_path)
    assert descriptor.vault_path == tmp_path.resolve() / ".claude/vault"


def test_tier0_config_ignores_vault_path(tmp_path: Path) -> None:
    # Below tier 1 the vault surface does not exist — a stray value is ignored.
    descriptor = parse_config(_memory_config(0, "  vault_path: .claude/vault\n"), tmp_path)
    assert descriptor.vault_path is None


def test_tier2_config_exposes_graph_path(tmp_path: Path) -> None:
    descriptor = parse_config(_memory_config(2, "  graph_path: graphify-out/graph.json\n"), tmp_path)
    assert descriptor.graph_path == tmp_path.resolve() / "graphify-out/graph.json"


def test_tier1_config_ignores_graph_path(tmp_path: Path) -> None:
    descriptor = parse_config(_memory_config(1, "  graph_path: graphify-out/graph.json\n"), tmp_path)
    assert descriptor.graph_path is None


def test_tier3_config_exposes_rag_endpoint(tmp_path: Path) -> None:
    descriptor = parse_config(_memory_config(3, "  rag_endpoint: http://127.0.0.1:8099\n"), tmp_path)
    assert descriptor.rag_endpoint == "http://127.0.0.1:8099"


def test_tier2_config_ignores_rag_endpoint(tmp_path: Path) -> None:
    descriptor = parse_config(_memory_config(2, "  rag_endpoint: http://127.0.0.1:8099\n"), tmp_path)
    assert descriptor.rag_endpoint == ""


def test_tier3_unset_rag_endpoint_is_empty(tmp_path: Path) -> None:
    # A tier-3 child that has not run its RAG setup yet omits the endpoint.
    assert parse_config(_memory_config(3), tmp_path).rag_endpoint == ""


def test_vault_path_escaping_project_root_is_ignored(tmp_path: Path) -> None:
    descriptor = parse_config(_memory_config(1, "  vault_path: ../../etc\n"), tmp_path)
    assert descriptor.vault_path is None


def test_vault_path_escaping_project_root_warns(tmp_path: Path) -> None:
    descriptor = parse_config(_memory_config(1, "  vault_path: /etc\n"), tmp_path)
    assert any("escapes the project root" in w for w in descriptor.warnings)


class TestScaffoldLayout:
    """PI-627: the descriptor lives under ``.agents/`` (current project-init)
    or ``.claude/`` (legacy). The reader must find both — a current scaffold
    has no ``.claude/config.yaml`` at all, so hardcoding it dropped every
    modern project from discovery."""

    def test_discovers_agents_layout(self, fleet_dir: Path) -> None:
        project = make_project(fleet_dir, "modern", layout=".agents")
        d = load_descriptor(project)
        assert d is not None
        assert d.config_root == ".agents"
        assert d.config_path == project / ".agents" / "config.yaml"

    def test_legacy_claude_layout_still_works(self, fleet_dir: Path) -> None:
        project = make_project(fleet_dir, "legacy", layout=".claude")
        d = load_descriptor(project)
        assert d is not None
        assert d.config_root == ".claude"

    def test_agents_preferred_when_both_present(self, fleet_dir: Path) -> None:
        # A stray legacy .claude/config.yaml must not shadow the current one.
        project = make_project(fleet_dir, "both", layout=".agents")
        (project / ".claude").mkdir(parents=True, exist_ok=True)
        (project / ".claude" / "config.yaml").write_text(
            'project:\n  name: "stale"\n', encoding="utf-8"
        )
        d = load_descriptor(project)
        assert d is not None
        assert d.config_root == ".agents"
        assert d.name == "both"

    def test_no_config_is_not_a_project(self, tmp_path: Path) -> None:
        assert load_descriptor(tmp_path) is None
        assert resolve_config(tmp_path) is None

    def test_memory_defaults_under_config_root(self, tmp_path: Path) -> None:
        # A config that omits memory_path anchors it beside the descriptor.
        d = parse_config('project:\n  name: "m"\n', tmp_path, config_root=".agents")
        assert d.memory_path == tmp_path / ".agents" / "memory"

    def test_capabilities_read_under_agents_layout(self, fleet_dir: Path) -> None:
        project = make_project(fleet_dir, "modern", layout=".agents")
        (project / ".agents" / "CAPABILITIES.md").write_text(
            "# Capabilities\n\n## Skills (1)\n\n| Skill | Description |\n|---|---|\n| plan | plan |\n",
            encoding="utf-8",
        )
        d = load_descriptor(project)
        assert d is not None
        cap = load_capabilities(d)
        assert cap.present

    def test_observability_dir_defaults_under_config_root(self, tmp_path: Path) -> None:
        d = parse_config('project:\n  name: "o"\n', tmp_path, config_root=".agents")
        assert observability_dir(d) == tmp_path / ".agents" / "observability"
