"""What a run actually cost — and the honest admission when we do not know.

ADR-007 put a leash on agent spend (a concurrency cap, a wall-clock timeout, a
canary before every fan-out, ``--max-budget-usd`` on the CLI itself) and then
conceded the gap those controls leave open: *"cost is bounded but not metered.
None of them report what a run actually cost."* This module closes it.

Nothing new is instrumented. The ``claude`` CLI is already invoked with
``--output-format json`` (:mod:`work`), so every run that reaches its own end
**writes its cost into its log** — a terminal JSON object carrying
``total_cost_usd``, a token breakdown, and a turn count. The number has been
sitting on disk all along; no one read it. All this module does is read it.

**Unknown is not zero, and that distinction is the whole point.** A run that is
killed, times out, or dies mid-flight never emits that terminal object. Its cost
is *unknown* — but it is emphatically not free: it burned tokens right up until
the moment it died, and a timeout burns the most of all. So an unmetered run
parses to ``None`` and renders as an em-dash, never ``$0.00``.

This is :mod:`runs`' own rule about state ("silence is not success") applied to
money. A fleet that quietly totals unmetered runs as zero would under-report
spend precisely when a run misbehaved, and would report a fleet of thirty
timed-out runs as costing nothing at all. That is the one lie a cost feature
must never tell, so :func:`total` counts the unmetered rather than summing them.

Reads are never-raise (ADR-003): a truncated, malformed, absent, or unreadable
log degrades to ``None``.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

#: What an unknown cost looks like in any rendered surface. Deliberately not
#: ``$0.00`` — see the module docstring.
UNKNOWN = "—"

#: The fast path: a chatty agent's log is mostly tool output and the record we
#: want is the last thing written, so try the tail before reading the whole file.
_TAIL_BYTES = 65_536

#: The fallback cap. The result object embeds the agent's entire final answer in
#: its ``result`` field, so it can itself be far larger than the tail — a long
#: diff easily clears 64 KiB. A tail-only read would start *inside* that object,
#: decode only its nested ``usage`` (which carries no ``total_cost_usd``), and
#: report a perfectly well-metered run as unmetered. That is a false unknown, and
#: it under-reports spend on exactly the biggest runs — the opposite of this
#: module's whole purpose. So when the tail yields nothing, widen the read.
_MAX_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class RunCost:
    """The metered cost of one agent run, as the CLI reported it.

    Attributes:
        usd: What the run cost, per the CLI's own ``total_cost_usd``.
        input_tokens: Uncached input tokens.
        output_tokens: Generated tokens.
        cache_read_tokens: Input served from the prompt cache (cheap).
        cache_creation_tokens: Input written into the prompt cache.
        turns: Agent turns taken.
    """

    usd: float
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    turns: int = 0

    @property
    def total_tokens(self) -> int:
        """Every token the run was billed for, cached or not."""
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_creation_tokens
        )


@dataclass(frozen=True)
class CostTotal:
    """Aggregate spend across many runs, keeping the unmetered ones visible.

    ``unmetered`` is not a rounding detail to be tidied away — it is the
    confidence interval on ``usd``. A total of ``$0.00`` over twelve unmetered
    runs means "we know nothing", not "it was free", and a caller that renders
    the former as the latter has reintroduced the exact lie this module exists
    to prevent.

    Attributes:
        usd: Summed cost of the runs we could meter.
        input_tokens: Summed uncached input tokens.
        output_tokens: Summed generated tokens.
        turns: Summed agent turns.
        metered: How many runs contributed to the sums.
        unmetered: How many runs could not be metered, and so contributed nothing.
    """

    usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    turns: int = 0
    metered: int = 0
    unmetered: int = 0

    @property
    def is_complete(self) -> bool:
        """Whether every run in the total was actually metered."""
        return self.unmetered == 0


def _as_int(raw: object) -> int:
    """Coerce a JSON number to a non-negative int; 0 if it is not one."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return 0
    return max(0, int(raw))


def from_payload(raw: object) -> RunCost | None:
    """Build a :class:`RunCost` from a decoded CLI result object.

    ``None`` unless the payload actually carries a cost. A result object with no
    ``total_cost_usd`` is not a zero-cost run, it is an *unmetered* one — the
    field's absence is the signal, so it must not be defaulted to ``0.0``.
    """
    if not isinstance(raw, dict):
        return None
    usd = raw.get("total_cost_usd")
    if isinstance(usd, bool) or not isinstance(usd, (int, float)):
        return None

    usage = raw.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    return RunCost(
        usd=float(usd),
        input_tokens=_as_int(usage.get("input_tokens")),
        output_tokens=_as_int(usage.get("output_tokens")),
        cache_read_tokens=_as_int(usage.get("cache_read_input_tokens")),
        cache_creation_tokens=_as_int(usage.get("cache_creation_input_tokens")),
        turns=_as_int(raw.get("num_turns")),
    )


def from_record(raw: object) -> RunCost | None:
    """Rebuild a :class:`RunCost` from its own persisted form; ``None`` if absent.

    The mirror of ``dataclasses.asdict``, and deliberately *not* :func:`from_payload`
    — that one reads the CLI's wire shape (``total_cost_usd``, a nested ``usage``),
    this one reads ours (a flat ``usd``). A run record written before this module
    existed simply has no ``cost`` key, and correctly reloads as unmetered.
    """
    if not isinstance(raw, dict):
        return None
    usd = raw.get("usd")
    if isinstance(usd, bool) or not isinstance(usd, (int, float)):
        return None
    return RunCost(
        usd=float(usd),
        input_tokens=_as_int(raw.get("input_tokens")),
        output_tokens=_as_int(raw.get("output_tokens")),
        cache_read_tokens=_as_int(raw.get("cache_read_tokens")),
        cache_creation_tokens=_as_int(raw.get("cache_creation_tokens")),
        turns=_as_int(raw.get("turns")),
    )


def _scan(text: str) -> RunCost | None:
    """Find the last result object in a log tail, ignoring everything around it.

    The log is stdout *and* stderr merged, so the result object is routinely
    fenced in by warnings, tool chatter, progress bars, and — when a previous
    write was cut short — raw partial bytes, any of which can share a line with
    it. Line-splitting is therefore not enough.

    So we scan for ``{`` from the end backwards and let ``raw_decode`` parse from
    each one, which stops cleanly at the end of a well-formed object and simply
    ignores whatever trails it. Going backwards means the *newest* result wins,
    which is what a re-run appends. Inner objects (``usage``) decode fine but
    carry no ``total_cost_usd``, so they are skipped by :func:`from_payload` and
    the scan walks out to the enclosing result.
    """
    decoder = json.JSONDecoder()
    index = text.rfind("{")
    while index != -1:
        try:
            payload, _ = decoder.raw_decode(text, index)
        except ValueError:
            payload = None
        found = from_payload(payload)
        if found is not None:
            return found
        index = text.rfind("{", 0, index)
    return None


def _tail(path: Path, limit: int) -> tuple[str, int]:
    """Read the last ``limit`` bytes of a file, with the file's full size."""
    with path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        handle.seek(max(0, size - limit))
        return handle.read().decode("utf-8", errors="replace"), size


def parse_log(path: Path | str) -> RunCost | None:
    """Recover a run's cost from its log; ``None`` when it cannot be known.

    ``None`` is the answer for every way this can go wrong — the log is missing,
    unreadable, truncated mid-write, or holds no result object because the run
    was killed before it could emit one. Every one of those is *unknown cost*,
    and none of them is *no cost*.

    Read in two stages. The tail almost always holds the result object, so try
    that first and pay nothing for the common case. But the object embeds the
    agent's whole final answer, so it can be bigger than the tail window — and a
    tail-only read would then land inside it and find only its nested ``usage``,
    reporting a metered run as unmetered. So a miss escalates to a wider read
    before we are willing to say "unknown", because a *false* unknown here would
    silently under-report the spend of the largest runs.
    """
    target = Path(path)
    try:
        text, size = _tail(target, _TAIL_BYTES)
        found = _scan(text)
        if found is None and size > _TAIL_BYTES:
            text, _ = _tail(target, _MAX_BYTES)
            found = _scan(text)
    except (OSError, ValueError):
        return None
    return found


def total(costs: Iterable[RunCost | None]) -> CostTotal:
    """Aggregate run costs, counting the unmetered instead of summing them as 0."""
    agg = CostTotal()
    for item in costs:
        if item is None:
            agg = CostTotal(
                usd=agg.usd,
                input_tokens=agg.input_tokens,
                output_tokens=agg.output_tokens,
                turns=agg.turns,
                metered=agg.metered,
                unmetered=agg.unmetered + 1,
            )
            continue
        agg = CostTotal(
            usd=agg.usd + item.usd,
            input_tokens=agg.input_tokens + item.input_tokens,
            output_tokens=agg.output_tokens + item.output_tokens,
            turns=agg.turns + item.turns,
            metered=agg.metered + 1,
            unmetered=agg.unmetered,
        )
    return agg


def format_usd(item: RunCost | None) -> str:
    """Render one run's cost for a table cell; :data:`UNKNOWN` when unmetered.

    Sub-cent runs render as ``<$0.01`` rather than ``$0.00``: a run that cost a
    tenth of a cent is cheap, but it is not free, and only an *unmetered* run is
    entitled to look like nothing happened.
    """
    if item is None:
        return UNKNOWN
    if 0 < item.usd < 0.01:
        return "<$0.01"
    return f"${item.usd:.2f}"


def format_total(agg: CostTotal) -> str:
    """Render an aggregate, naming the unmetered runs rather than hiding them."""
    if agg.metered == 0 and agg.unmetered == 0:
        return "no runs"
    body = f"${agg.usd:.2f} across {agg.metered} run{'s' if agg.metered != 1 else ''}"
    if agg.unmetered:
        body += f" (+{agg.unmetered} unmetered — true spend is higher)"
    return body
