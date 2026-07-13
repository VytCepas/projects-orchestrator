"""Audit digest: delta vs the last run, persisted and never-raising."""

from __future__ import annotations

from pathlib import Path

from projects_orchestrator.audit import AuditFinding, AuditReport
from projects_orchestrator.digest import (
    AuditDigest,
    compute_digest,
    digest_path,
    digest_payload,
    load_prior,
    render_digest,
    save_current,
)


def _report(*findings: AuditFinding, project: str = "alpha") -> AuditReport:
    return AuditReport(project=project, findings=findings)


def _warn(message: str, category: str = "drift", project: str = "alpha") -> AuditFinding:
    return AuditFinding(project, category, "warn", message)


def test_first_run_marks_everything_new() -> None:
    digest = compute_digest([_report(_warn("2 file(s) diverged from scaffold"))], prior=[])
    assert len(digest.new) == 1
    assert digest.resolved == ()


def test_unchanged_run_reports_no_delta() -> None:
    finding = _warn("2 file(s) diverged from scaffold")
    digest = compute_digest([_report(finding)], prior=[finding])
    assert not digest.changed


def test_new_finding_appears_in_new() -> None:
    prior = [_warn("old issue", category="hooks")]
    current = [
        _report(_warn("old issue", category="hooks"), _warn("fresh issue", category="drift"))
    ]
    digest = compute_digest(current, prior)
    assert [f.message for f in digest.new] == ["fresh issue"]


def test_cleared_finding_appears_in_resolved() -> None:
    prior = [_warn("gone now", category="hooks")]
    digest = compute_digest([_report()], prior)
    assert [f.message for f in digest.resolved] == ["gone now"]


def test_ok_findings_are_not_tracked() -> None:
    ok = AuditFinding("alpha", "freshness", "ok", "2 cached result(s)")
    digest = compute_digest([_report(ok)], prior=[])
    assert not digest.changed


def test_render_digest_all_clear_when_unchanged() -> None:
    assert "no change" in render_digest(AuditDigest())


def test_render_digest_lists_new_and_resolved() -> None:
    digest = AuditDigest(new=(_warn("a new one"),), resolved=(_warn("an old one"),))
    text = render_digest(digest)
    assert "1 new, 1 resolved" in text
    assert "a new one" in text
    assert "an old one" in text


def test_save_then_load_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "digest.json"
    save_current([_report(_warn("2 file(s) diverged"))], path)
    prior = load_prior(path)
    assert [f.message for f in prior] == ["2 file(s) diverged"]


def test_save_persists_only_attention_worthy_findings(tmp_path: Path) -> None:
    path = tmp_path / "digest.json"
    ok = AuditFinding("alpha", "config", "ok", "config.yaml parsed")
    save_current([_report(ok, _warn("real issue"))], path)
    assert [f.message for f in load_prior(path)] == ["real issue"]


def test_load_missing_state_is_empty(tmp_path: Path) -> None:
    assert load_prior(tmp_path / "absent.json") == []


def test_load_corrupt_state_is_empty(tmp_path: Path) -> None:
    path = tmp_path / "digest.json"
    path.write_text("{not json", encoding="utf-8")
    assert load_prior(path) == []


def test_digest_path_honors_xdg_state_home(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert digest_path() == tmp_path / "projects-orchestrator" / "audit-digest.json"


# --- The webhook payload: a scheduled run posts this to Slack (#98) ---
# Slack renders the top-level `text` key, so the payload has to carry a
# human-readable summary alongside the machine-readable delta.


def test_digest_payload_carries_a_slack_text_summary() -> None:
    digest = compute_digest([_report(_warn("2 file(s) diverged from scaffold"))], prior=[])
    assert digest_payload(digest)["text"] == render_digest(digest)


def test_digest_payload_text_says_no_change_when_unchanged() -> None:
    assert "no change since last run" in str(digest_payload(AuditDigest())["text"])


def test_digest_payload_carries_the_machine_readable_delta() -> None:
    digest = compute_digest([_report(_warn("drifted"))], prior=[])
    payload = digest_payload(digest)
    assert payload["new"][0]["message"] == "drifted"  # type: ignore[index]
