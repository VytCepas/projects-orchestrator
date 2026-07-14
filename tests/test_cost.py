"""Run cost: what it was, and — the part that matters — when we don't know.

The guard these tests exist to hold is a single sentence: **unknown is not zero.**
A run that was killed, timed out, or never emitted a result burned real tokens,
and every path here must refuse to describe that as ``$0.00``.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from projects_orchestrator.cost import (
    UNKNOWN,
    CostTotal,
    RunCost,
    format_total,
    format_usd,
    from_payload,
    from_record,
    parse_log,
    total,
)

#: The shape the `claude` CLI writes with `--output-format json`.
_RESULT = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "num_turns": 7,
    "session_id": "abc",
    "total_cost_usd": 1.2345,
    "usage": {
        "input_tokens": 900,
        "output_tokens": 300,
        "cache_read_input_tokens": 12_000,
        "cache_creation_input_tokens": 400,
    },
}


def _log(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "run.log"
    path.write_text(text, encoding="utf-8")
    return path


# --- parse_log: the happy path and the noise around it -------------------------


def test_parse_log_reads_the_cost_the_cli_reported(tmp_path: Path) -> None:
    assert parse_log(_log(tmp_path, json.dumps(_RESULT))).usd == pytest.approx(1.2345)


def test_parse_log_reads_the_token_breakdown(tmp_path: Path) -> None:
    assert parse_log(_log(tmp_path, json.dumps(_RESULT))).output_tokens == 300


def test_parse_log_reads_cache_reads(tmp_path: Path) -> None:
    assert parse_log(_log(tmp_path, json.dumps(_RESULT))).cache_read_tokens == 12_000


def test_parse_log_reads_the_turn_count(tmp_path: Path) -> None:
    assert parse_log(_log(tmp_path, json.dumps(_RESULT))).turns == 7


def test_parse_log_finds_the_result_after_stderr_noise(tmp_path: Path) -> None:
    # stderr is merged into the log, so warnings routinely precede the result.
    noisy = f"warning: something\nnode: deprecation\n{json.dumps(_RESULT)}\n"
    assert parse_log(_log(tmp_path, noisy)).usd == pytest.approx(1.2345)


def test_parse_log_finds_the_result_when_trailing_noise_follows(tmp_path: Path) -> None:
    trailing = f"{json.dumps(_RESULT)}\nwarning: written after the result\n"
    assert parse_log(_log(tmp_path, trailing)).usd == pytest.approx(1.2345)


def test_parse_log_prefers_the_last_result_when_several_are_present(tmp_path: Path) -> None:
    first = json.dumps({**_RESULT, "total_cost_usd": 0.10})
    last = json.dumps({**_RESULT, "total_cost_usd": 0.99})
    assert parse_log(_log(tmp_path, f"{first}\n{last}\n")).usd == pytest.approx(0.99)


def test_parse_log_reads_a_pretty_printed_result(tmp_path: Path) -> None:
    assert parse_log(_log(tmp_path, json.dumps(_RESULT, indent=2))).usd == pytest.approx(1.2345)


# --- parse_log: every way it can fail is "unknown", never "free" ---------------


def test_parse_log_of_a_missing_file_is_unknown(tmp_path: Path) -> None:
    assert parse_log(tmp_path / "nope.log") is None


def test_parse_log_of_an_empty_log_is_unknown(tmp_path: Path) -> None:
    # The run was killed before it wrote anything. It still cost money.
    assert parse_log(_log(tmp_path, "")) is None


def test_parse_log_of_a_truncated_result_is_unknown(tmp_path: Path) -> None:
    truncated = json.dumps(_RESULT)[:-20]
    assert parse_log(_log(tmp_path, truncated)) is None


def test_parse_log_of_a_timed_out_run_is_unknown_not_zero(tmp_path: Path) -> None:
    # The load-bearing case: a timeout kills the CLI mid-flight, so no result
    # object is ever written — and a timeout is the MOST expensive way to fail.
    killed = "reading files...\nrunning tests...\n"
    assert parse_log(_log(tmp_path, killed)) is None


def test_parse_log_of_a_result_without_a_cost_field_is_unknown(tmp_path: Path) -> None:
    # An absent total_cost_usd means unmetered. It must not default to 0.0.
    no_cost = {k: v for k, v in _RESULT.items() if k != "total_cost_usd"}
    assert parse_log(_log(tmp_path, json.dumps(no_cost))) is None


def test_parse_log_of_a_directory_is_unknown(tmp_path: Path) -> None:
    assert parse_log(tmp_path) is None


def test_parse_log_survives_undecodable_bytes(tmp_path: Path) -> None:
    path = tmp_path / "run.log"
    path.write_bytes(b"\xff\xfe garbage " + json.dumps(_RESULT).encode())
    assert parse_log(path).usd == pytest.approx(1.2345)


def test_parse_log_reads_the_result_from_a_very_chatty_log(tmp_path: Path) -> None:
    # Only the log's tail is read; the result must still be found past the cap.
    chatter = "x" * 200_000
    assert parse_log(_log(tmp_path, f"{chatter}\n{json.dumps(_RESULT)}")).usd == pytest.approx(
        1.2345
    )


# --- from_payload / from_record ------------------------------------------------


def test_from_payload_rejects_a_non_object() -> None:
    assert from_payload(["not", "an", "object"]) is None


def test_from_payload_rejects_a_boolean_cost() -> None:
    # json's `true` is an int subclass in Python; it is not a price.
    assert from_payload({"total_cost_usd": True}) is None


def test_from_payload_tolerates_a_missing_usage_block() -> None:
    assert from_payload({"total_cost_usd": 0.5}).input_tokens == 0


def test_from_payload_clamps_a_negative_token_count() -> None:
    payload = {"total_cost_usd": 0.5, "usage": {"output_tokens": -10}}
    assert from_payload(payload).output_tokens == 0


def test_from_record_round_trips_a_run_cost() -> None:
    original = RunCost(usd=0.25, input_tokens=10, output_tokens=20, turns=3)
    assert from_record(asdict(original)) == original


def test_from_record_of_an_absent_cost_is_unknown() -> None:
    # A run recorded before this feature existed has no `cost` key at all.
    assert from_record(None) is None


def test_from_record_does_not_read_the_wire_shape() -> None:
    # from_payload's key, not ours — it must not be silently accepted.
    assert from_record({"total_cost_usd": 1.0}) is None


def test_total_tokens_sums_every_billed_class() -> None:
    item = RunCost(usd=1.0, input_tokens=1, output_tokens=2, cache_read_tokens=4)
    assert item.total_tokens == 7


# --- total: the unmetered are counted, never summed as zero --------------------


def test_total_sums_the_metered_runs() -> None:
    costs = [RunCost(usd=1.0), RunCost(usd=2.50)]
    assert total(costs).usd == pytest.approx(3.50)


def test_total_counts_the_metered_runs() -> None:
    assert total([RunCost(usd=1.0), RunCost(usd=2.0)]).metered == 2


def test_total_counts_an_unmetered_run_rather_than_summing_it() -> None:
    assert total([RunCost(usd=1.0), None]).unmetered == 1


def test_total_does_not_let_an_unmetered_run_inflate_the_metered_count() -> None:
    assert total([RunCost(usd=1.0), None]).metered == 1


def test_total_of_only_unmetered_runs_is_not_reported_as_complete() -> None:
    # Thirty timed-out runs must never present as a $0.00 fleet with confidence.
    assert not total([None, None, None]).is_complete


def test_total_of_only_unmetered_runs_sums_to_zero_but_says_so() -> None:
    assert total([None, None]).unmetered == 2


def test_total_of_all_metered_runs_is_complete() -> None:
    assert total([RunCost(usd=1.0)]).is_complete


def test_total_of_no_runs_is_complete() -> None:
    assert total([]).is_complete


# --- rendering: an unmetered run never looks free ------------------------------


def test_format_usd_of_an_unmetered_run_is_the_em_dash() -> None:
    assert format_usd(None) == UNKNOWN


def test_format_usd_of_an_unmetered_run_is_never_zero_dollars() -> None:
    assert format_usd(None) != "$0.00"


def test_format_usd_renders_a_normal_cost() -> None:
    assert format_usd(RunCost(usd=1.5)) == "$1.50"


def test_format_usd_renders_a_sub_cent_cost_as_less_than_a_cent() -> None:
    # Cheap is not free, and only an UNMETERED run is entitled to look like zero.
    assert format_usd(RunCost(usd=0.001)) == "<$0.01"


def test_format_usd_renders_a_genuinely_free_run_as_zero() -> None:
    assert format_usd(RunCost(usd=0.0)) == "$0.00"


def test_format_total_names_the_unmetered_runs() -> None:
    rendered = format_total(CostTotal(usd=1.0, metered=1, unmetered=2))
    assert "2 unmetered" in rendered


def test_format_total_warns_that_true_spend_is_higher_when_runs_are_unmetered() -> None:
    rendered = format_total(CostTotal(usd=1.0, metered=1, unmetered=1))
    assert "true spend is higher" in rendered


def test_format_total_does_not_hedge_when_everything_was_metered() -> None:
    assert "unmetered" not in format_total(CostTotal(usd=1.0, metered=2))


def test_format_total_of_no_runs_says_so() -> None:
    assert format_total(CostTotal()) == "no runs"
