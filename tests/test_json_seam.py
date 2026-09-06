"""The `--json` seam (#219): schemas are frozen, and the producer is held to them.

Three verbs are consumed by harbor (`CONTRACTS/orchestrator-json.md`), and until
this file existed the seam had no schema, no fixture and no producer-side check:
a field rename broke a different repo at runtime with nothing here going red.

TWO DISTINCT GUARANTEES, deliberately separate:

* **Producer conformance** — the LIVE output of `main([...])` validates against
  the schema. Catches the producer drifting away from the contract.
* **Golden fixtures** — the committed payloads under `fixtures/json_seam/`
  validate too. These are what a consumer vendors, so they must keep meaning
  what they meant; this catches the *schema* drifting away from the contract.

The negative cases are what make the positives worth anything. `test_*_rejects_*`
mutates a real payload into something plausible-but-wrong and asserts the schema
refuses it — including the null-padded array from #218, which is legal to
harbor's array-ness writer gate and renders a phantom healthy project.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from conftest import add_memory, git_init, make_project, make_project_v2
from jsonschema import Draft202012Validator

from projects_orchestrator.__main__ import main

SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schemas"
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "json_seam"

# verb -> (schema file, golden fixture). The three harbor consumes; nothing else
# on the CLI is frozen, and this mapping is the list of what is.
CONSUMED = {
    "snapshot": ("snapshot.v1.schema.json", "snapshot.v1.json"),
    "events": ("events.v1.schema.json", "events.v1.json"),
    "audit-digest": ("audit-digest.v1.schema.json", "audit-digest.v1.json"),
}


_MALFORMED_CONFIG = """\
project:
  name: "bad"
  description: "test project"
  project_init_version: 0.5.2
  project_init_contract_version: "two"

language: python
delivery: library

memory:
  tier: 0

tooling:
  lint_command: "true"
"""


def _validator(schema_file: str) -> Draft202012Validator:
    schema = json.loads((SCHEMA_DIR / schema_file).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)  # the schema itself must be legal
    return Draft202012Validator(schema)


def _fixture(name: str) -> Any:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _fleet(fleet_dir: Path) -> Path:
    """A fleet rich enough that the emitted payloads exercise real shapes."""
    alpha = make_project(fleet_dir, "alpha")
    add_memory(alpha, "project_context.md", name="Deploy target")
    git_init(alpha)
    return fleet_dir


def _emit(argv: list[str], capsys) -> Any:
    assert main(argv) in (0, 1)  # never-raise: a nonzero verdict is still valid JSON
    return json.loads(capsys.readouterr().out)


# --- The schemas are legal, and every consumed verb has one plus a fixture ---


@pytest.mark.parametrize(("schema_file", "fixture_file"), list(CONSUMED.values()))
def test_every_consumed_verb_has_a_schema_and_a_fixture(
    schema_file: str, fixture_file: str
) -> None:
    assert (SCHEMA_DIR / schema_file).is_file() and (FIXTURE_DIR / fixture_file).is_file()


@pytest.mark.parametrize("schema_file", [s for s, _ in CONSUMED.values()])
def test_each_schema_is_a_legal_json_schema(schema_file: str) -> None:
    _validator(schema_file)  # raises SchemaError if not


@pytest.mark.parametrize("schema_file", [s for s, _ in CONSUMED.values()])
def test_each_schema_declares_its_version(schema_file: str) -> None:
    # The version has to be readable without running anything: it is what a
    # consumer freshness-checks its vendored copy against.
    schema = json.loads((SCHEMA_DIR / schema_file).read_text(encoding="utf-8"))
    assert schema["x-schema-version"] == 1


# --- Golden fixtures validate (catches the SCHEMA drifting) ---


@pytest.mark.parametrize(("schema_file", "fixture_file"), list(CONSUMED.values()))
def test_the_golden_fixture_validates(schema_file: str, fixture_file: str) -> None:
    assert _validator(schema_file).validate(_fixture(fixture_file)) is None


# --- Live producer output validates (catches the PRODUCER drifting) ---


def test_snapshot_output_validates(fleet_dir: Path, capsys) -> None:
    payload = _emit(["snapshot", "--root", str(_fleet(fleet_dir)), "--json"], capsys)
    assert _validator("snapshot.v1.schema.json").validate(payload) is None


def test_snapshot_output_validates_for_a_contract_v2_project(fleet_dir: Path, capsys) -> None:
    """The minimal fleet above leaves `deploy`, `ci` and `observability_path` null.

    A schema only checked against sparse output is a schema that never sees half
    its own fields, so a v2 project with a deploy block is validated too.
    """
    git_init(
        make_project_v2(fleet_dir, "v2proj", deploy_target="cloud-run", health_url="https://x/hz")
    )
    payload = _emit(["snapshot", "--root", str(fleet_dir), "--json"], capsys)
    assert _validator("snapshot.v1.schema.json").validate(payload) is None


def test_snapshot_output_validates_a_project_with_a_malformed_field(
    fleet_dir: Path, capsys
) -> None:
    # Exercises descriptor.malformed as a NON-empty array of strings (#216).
    project = make_project(fleet_dir, "bad", config_text=_MALFORMED_CONFIG)
    git_init(project)
    payload = _emit(["snapshot", "--root", str(fleet_dir), "--json"], capsys)
    assert payload[0]["descriptor"]["malformed"] == ["project_init_contract_version"]


def test_events_output_validates(fleet_dir: Path, capsys) -> None:
    payload = _emit(["events", "--root", str(_fleet(fleet_dir)), "--json"], capsys)
    assert _validator("events.v1.schema.json").validate(payload) is None


def test_audit_digest_output_validates(fleet_dir: Path, capsys) -> None:
    payload = _emit(["audit", "--root", str(_fleet(fleet_dir)), "--json", "--digest"], capsys)
    assert _validator("audit-digest.v1.schema.json").validate(payload) is None


def test_audit_digest_output_carries_the_schema_version(fleet_dir: Path, capsys) -> None:
    # The one consumed verb whose payload is an object, so the only one that can
    # carry an in-band version additively. See digest.DIGEST_SCHEMA_VERSION.
    payload = _emit(["audit", "--root", str(_fleet(fleet_dir)), "--json", "--digest"], capsys)
    assert payload["schema_version"] == 1


# --- The negative cases: plausible-but-wrong payloads must be REFUSED ---


def _snapshot_is_valid(payload: Any) -> bool:
    return _validator("snapshot.v1.schema.json").is_valid(payload)


def test_a_null_padded_array_is_refused(fleet_dir: Path, capsys) -> None:
    """#218: legal to harbor's array-ness writer gate, phantom project downstream.

    harbor's writer checks only that the top level is an array, so a null
    element is admitted and `fleet-glyph.sh` renders an inflated anchor count
    with no error. Rejecting it needs a schema, which is why #218 waits on this
    ticket rather than standing alone.
    """
    payload = _emit(["snapshot", "--root", str(_fleet(fleet_dir)), "--json"], capsys)
    assert not _snapshot_is_valid([*payload, None])


def test_an_all_null_array_is_refused(fleet_dir: Path, capsys) -> None:
    _emit(["snapshot", "--root", str(_fleet(fleet_dir)), "--json"], capsys)
    assert not _snapshot_is_valid([None, None])


def test_an_object_where_the_array_belongs_is_refused() -> None:
    assert not _snapshot_is_valid({"projects": []})


def test_a_dropped_required_field_is_refused() -> None:
    payload = copy.deepcopy(_fixture("snapshot.v1.json"))
    del payload[0]["descriptor"]["contract_version"]
    assert not _snapshot_is_valid(payload)


def test_a_renamed_field_is_refused() -> None:
    # The exact failure this seam exists to prevent: a producer-side rename that
    # breaks harbor at runtime, in another repo, with nothing here going red.
    payload = copy.deepcopy(_fixture("snapshot.v1.json"))
    payload[0]["descriptor"]["contract"] = payload[0]["descriptor"].pop("contract_version")
    assert not _snapshot_is_valid(payload)


def test_a_retyped_field_is_refused() -> None:
    payload = copy.deepcopy(_fixture("snapshot.v1.json"))
    payload[0]["descriptor"]["contract_version"] = "2"  # string, not integer
    assert not _snapshot_is_valid(payload)


def test_a_bad_enum_value_is_refused() -> None:
    payload = copy.deepcopy(_fixture("snapshot.v1.json"))
    payload[0]["drift"]["status"] = "probably-fine"
    assert not _snapshot_is_valid(payload)


def test_a_digest_missing_its_schema_version_is_refused() -> None:
    payload = copy.deepcopy(_fixture("audit-digest.v1.json"))
    del payload["schema_version"]
    assert not _validator("audit-digest.v1.schema.json").is_valid(payload)


def test_a_digest_declaring_another_version_is_refused() -> None:
    payload = copy.deepcopy(_fixture("audit-digest.v1.json"))
    payload["schema_version"] = 2
    assert not _validator("audit-digest.v1.schema.json").is_valid(payload)


# --- And the one thing that must NOT be refused ---


def test_an_added_field_is_accepted() -> None:
    """Additive-only is the seam's rule, so the schema must not be closed.

    `additionalProperties` is deliberately open. Closing it would make every
    additive producer change fail validation, which inverts the contract and is
    the §2.11 false positive that gets a control switched off.
    """
    payload = copy.deepcopy(_fixture("snapshot.v1.json"))
    payload[0]["descriptor"]["some_future_field"] = "added in a later version"
    assert _snapshot_is_valid(payload)
