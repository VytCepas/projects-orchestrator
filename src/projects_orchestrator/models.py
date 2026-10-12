"""Which model tier and effort each kind of agent launch runs on (#324).

Every ``claude`` this program starts runs under a **fresh HOME** (:mod:`sandbox`),
so no user-level model setting reaches it. Without explicit flags an unattended
run takes whatever the CLI defaults to — a tier chosen
by nobody. The fleet's decision is to route work to the *smallest tier that
meets the bar*: Haiku for scans, polling and gate-checked heals, Sonnet for ticket
work, Opus for coordination, guard design and final verification.

This module is the ONE place that decision lives. A launch names its **task
class** and asks :func:`choice_for`; no launcher spells an alias. The table is
:data:`DEFAULT_TABLE`, overridable per class in ``fleet.yaml`` (``models:``), which
:func:`registry.load_fleet_config` parses through :func:`parse_table`.

Only tier **aliases** (``haiku``/``sonnet``/``opus``) are accepted, never pinned
model ids: an alias tracks the current model of its tier, a pinned id goes stale
and silently freezes a fleet on a retired model. Validation therefore rejects an
id at load rather than passing it to a CLI that would accept it.

The primary and the fallback must differ: a fallback to the model that just
failed falls back to nothing. (The CLI itself does not refuse the pair — probed
on 2.1.285 — so this is enforced here, not delegated.)
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass

_log = logging.getLogger(__name__)

#: Task classes — one per place this program starts a ``claude`` process.
HEAL = "heal"
WORK = "work"
#: The interactive session a human takes over a blocked run in.
ATTACH = "attach"

TASK_CLASSES = (HEAL, WORK, ATTACH)
#: The tier aliases the CLI resolves to each tier's current model.
ALIASES = ("haiku", "sonnet", "opus")
#: Effort levels the CLI accepts for ``--effort``.
EFFORTS = ("low", "medium", "high", "xhigh", "max")

_ENTRY_KEYS = frozenset({"model", "effort", "fallback"})


class ModelTableError(ValueError):
    """A model table value the launchers must never be handed."""


@dataclass(frozen=True)
class ModelChoice:
    """The tier, effort and fallback tier one task class runs on.

    Attributes:
        model: Tier alias for ``--model``.
        effort: Level for ``--effort``.
        fallback: Tier alias for ``--fallback-model``; differs from ``model``.
    """

    model: str
    effort: str
    fallback: str

    def cli_args(self) -> list[str]:
        """The argv fragment that pins this choice on a ``claude`` launch."""
        return [
            "--model",
            self.model,
            "--effort",
            self.effort,
            "--fallback-model",
            self.fallback,
        ]


#: The shipped table. heal and work are scoped fixes with their own verification
#: (heal re-runs the failing gate; work lands a draft PR a human reviews). A heal is
#: narrow and gate-checked, which a 141-run trial found Haiku at high effort passes
#: as reliably as Sonnet and Opus; work stays on Sonnet until real tickets say
#: otherwise. A failed run's human takeover is the escalation, so Opus.
#: Fallbacks go the other way in cost only when the primary is overloaded or
#: unavailable: a stronger tier for the fixes, the next tier down for attach.
DEFAULT_TABLE: Mapping[str, ModelChoice] = {
    HEAL: ModelChoice("haiku", "high", "sonnet"),
    WORK: ModelChoice("sonnet", "high", "opus"),
    ATTACH: ModelChoice("opus", "high", "sonnet"),
}


def _pick(value: object, allowed: tuple[str, ...], what: str, where: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        message = f"{where}: {what} {value!r} is not one of {', '.join(allowed)}"
        if what != "effort":
            message += " (tier aliases only — a pinned model id is refused)"
        raise ModelTableError(message)
    return value


def _parse_entry(raw: object, where: str) -> ModelChoice:
    if not isinstance(raw, dict):
        message = f"{where}: must be a mapping of model, effort and fallback"
        raise ModelTableError(message)
    unknown = sorted(str(key) for key in raw if key not in _ENTRY_KEYS)
    if unknown:
        message = f"{where}: unknown key(s) {', '.join(unknown)}"
        raise ModelTableError(message)
    missing = sorted(_ENTRY_KEYS - raw.keys())
    if missing:
        message = f"{where}: missing {', '.join(missing)}"
        raise ModelTableError(message)
    model = _pick(raw["model"], ALIASES, "model", where)
    effort = _pick(raw["effort"], EFFORTS, "effort", where)
    fallback = _pick(raw["fallback"], ALIASES, "fallback", where)
    if fallback == model:
        message = f"{where}: fallback must differ from model ({model!r})"
        raise ModelTableError(message)
    return ModelChoice(model, effort, fallback)


def parse_table(raw: object) -> dict[str, ModelChoice]:
    """Merge a ``models:`` override onto :data:`DEFAULT_TABLE`, or raise.

    ``None`` (no override) is the defaults. Each entry must give all three of
    ``model``, ``effort`` and ``fallback`` — a partial entry would silently mix an
    override with a default it may collide with (a new ``model`` equal to the
    default ``fallback``).

    Raises:
        ModelTableError: a non-mapping table, an unknown task class, or an entry
            with an unknown key, a missing key, a non-alias model or fallback, an
            unknown effort, or a fallback equal to its model.
    """
    table = dict(DEFAULT_TABLE)
    if raw is None:
        return table
    if not isinstance(raw, dict):
        message = "models: must be a mapping of task class to {model, effort, fallback}"
        raise ModelTableError(message)
    for task, entry in raw.items():
        if task not in TASK_CLASSES:
            message = f"models: unknown task class {task!r} (known: {', '.join(TASK_CLASSES)})"
            raise ModelTableError(message)
        table[task] = _parse_entry(entry, f"models.{task}")
    return table


def from_record(model: str, effort: str, fallback: str) -> ModelChoice | None:
    """A launcher's recorded choice, or ``None`` when it is absent or unusable.

    A detached wrapper reads these from the run record, which is a file on disk:
    the values pass the same validation as the fleet file, so a stale or edited
    record can never hand the CLI a pinned id.
    """
    try:
        return _parse_entry({"model": model, "effort": effort, "fallback": fallback}, "record")
    except ModelTableError as exc:
        _log.debug("unusable recorded model choice: %r", exc)
        return None


#: The table launchers read. Set once by the CLI from the fleet file
#: (:func:`configure`); a library caller that never configures gets the defaults.
_active: Mapping[str, ModelChoice] = DEFAULT_TABLE


def configure(table: Mapping[str, ModelChoice]) -> None:
    """Make ``table`` the one launchers consult (the CLI does this per invocation)."""
    global _active
    _active = table


def choice_for(task_class: str) -> ModelChoice:
    """The choice for ``task_class`` under the active table."""
    return _active[task_class]
