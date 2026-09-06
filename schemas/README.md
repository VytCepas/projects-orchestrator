# `--json` output schemas — the L2 Fleet → L0 Helm seam

These freeze the three `--json` payloads that **harbor consumes**, per its
`CONTRACTS/orchestrator-json.md`. Before they existed the seam had no schema, no
fixture and no producer-side validation, while the consumer side ran **live on a
5-minute launchd timer** — so a field rename here broke a different repo at
runtime, with nothing in this repo going red (#219).

| Verb | Schema | Golden fixture | Top-level shape |
|---|---|---|---|
| `snapshot --json` | `snapshot.v1.schema.json` | `tests/fixtures/json_seam/snapshot.v1.json` | **bare array** |
| `events --json` | `events.v1.schema.json` | `tests/fixtures/json_seam/events.v1.json` | **bare array** |
| `audit --json --digest` | `audit-digest.v1.schema.json` | `tests/fixtures/json_seam/audit-digest.v1.json` | **object** |

Enforced by `tests/test_json_seam.py`, which holds two separate guarantees:
the **live** output of each verb validates (catches the producer drifting), and
the **committed fixture** validates (catches the schema drifting). The negative
cases are what give the positives meaning — a null-padded array, a dropped
field, a renamed field, a retyped field, a bad enum value and a wrong version
are each asserted to be *refused*.

## Why `additionalProperties` is open

Deliberately. The seam's rule is **additive-only within a version**, so a schema
closed to new keys would fail validation on exactly the kind of change the
contract calls safe. That is the CONSTITUTION §2.11 false positive that gets a
control switched off. A **missing** required key fails; an **added** key passes,
and a test pins that in both directions.

## Where the version lives, and why it is not in-band everywhere

`audit --json --digest` emits an **object**, so it carries `schema_version`
in-band — adding a key to an object is the textbook additive change
(`digest.DIGEST_SCHEMA_VERSION`).

`snapshot --json` and `events --json` emit **bare arrays**. There is nowhere in
an array to put a version key, and **wrapping them in an envelope is a breaking
change**: harbor's `fleet-glyph.sh` reads the cache as an array and
`statusline-test.sh` explicitly pins "an object where an array belongs" as a
degradation case, so the wrap would blank the fleet tile on the next tick. The
contract says a breaking change "bumps the version and lands lock-step" — and
lock-step needs a simultaneous harbor release, which cannot be done from this
repo alone.

So for those two, the version lives in the schema file's `x-schema-version`, and
a consumer freshness-checks its vendored copy the way the descriptor seam
already does. **The in-band wrap is deferred, not forgotten** — see "Migrating
to an in-band envelope" below.

`x-schema-version` is an `x-` extension key rather than a bare `version`,
because JSON Schema reserves no such keyword and a validator must ignore it.

## Provenance — what was observed vs. what was typed from the producer

Every shape here was **derived from real emitted output** against a synthetic
fleet (contract v2, git repos, a memory file, an observability log with a
malformed line, a scaffold manifest with both a modified and a missing entry,
a populated checks cache, and a project with a malformed `contract_version`).
It is not generated from the dataclasses — and that distinction caught a real
discrepancy: **`events --json` builds its item by hand in `_cmd_events` and
omits `ProjectEvents.path`**, so a dataclass-generated schema would have
required a key the producer never emits.

Two fields are typed loosely on purpose, and it is recorded here rather than
hidden:

| Field | Status |
|---|---|
| `snapshot[].run_state` | non-null shape **UNOBSERVED** — no supervised process was running in the fixture. Typed `["object","null"]` with no required keys rather than invented. |
| `snapshot[].agent_run` | non-null shape **UNOBSERVED** — no open agent run in the fixture. Same treatment. |

Tightening those needs a fixture with a live process and an in-flight run.
Until then the schema says what is known and no more.

## Changing a payload

1. **Additive** (a new key, a new enum member): edit the schema, regenerate the
   fixture, keep the version. `tests/test_json_seam.py` must stay green.
2. **Breaking** (rename, retype, remove, or wrapping an array in an envelope):
   bump to `v2` as a **new file** (`snapshot.v2.schema.json`), keep `v1` until
   every consumer has migrated, and land it lock-step with harbor. Do not edit a
   `v1` file into a `v2` meaning — a consumer's vendored copy is keyed on that
   name.

## Migrating to an in-band envelope (deferred, needs harbor)

The end state the contract describes is every payload carrying
`schema_version`. Getting the two arrays there:

1. Add the envelope behind an opt-in flag so both shapes are emittable at once.
2. Harbor migrates its readers to the envelope, keeping the bare-array fallback.
3. Flip the default here and bump to `v2`.
4. Harbor drops the fallback.

Steps 2 and 4 are harbor's; this repo cannot do them, which is why the work
stops at a versioned schema plus a validating producer rather than pretending
the envelope landed.

## What is NOT frozen

Only the three verbs above. The other `--json` outputs on this CLI have no
declared consumer and no schema; adding one is a decision to support it forever,
so it should follow a consumer, not precede one.
