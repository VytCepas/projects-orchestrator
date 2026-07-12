"""Audit digest — what changed since the last audit run.

``audit`` renders the full governance report every time; a scheduled job wants
only the delta. This persists the set of attention-worthy findings (``warn`` /
``fail``) from each run under ``$XDG_STATE_HOME`` and, on the next run, reports
which are **new** and which have **resolved** — so a cron/CI job can post a
short "what changed" note instead of the whole table.

Never raises: an unreadable or corrupt state file is treated as "no prior run"
(everything is new), and the write degrades silently like the checks cache.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from projects_orchestrator.audit import AuditFinding, AuditReport
from projects_orchestrator.doctor import OK

_STATE_DIRNAME = "projects-orchestrator"
_DIGEST_FILENAME = "audit-digest.json"

_FIELDS = ("project", "category", "severity", "message")


def digest_path() -> Path:
    """Return the audit-digest state file path, honoring ``$XDG_STATE_HOME``."""
    base = os.environ.get("XDG_STATE_HOME", "")
    root = Path(base).expanduser() if base else Path.home() / ".local" / "state"
    return root / _STATE_DIRNAME / _DIGEST_FILENAME


@dataclass(frozen=True)
class AuditDigest:
    """The delta between two audit runs.

    Attributes:
        new: Attention-worthy findings present now but not last run.
        resolved: Findings present last run but gone now.
    """

    new: tuple[AuditFinding, ...] = ()
    resolved: tuple[AuditFinding, ...] = ()

    @property
    def changed(self) -> bool:
        """Whether anything appeared or resolved since the last run."""
        return bool(self.new or self.resolved)


def _key(finding: AuditFinding) -> tuple[str, str, str]:
    """Identity of a finding for delta purposes (project, category, message)."""
    return (finding.project, finding.category, finding.message)


def _issues(reports: list[AuditReport]) -> dict[tuple[str, str, str], AuditFinding]:
    """The attention-worthy (non-``ok``) findings across all reports, keyed."""
    return {
        _key(finding): finding
        for report in reports
        for finding in report.findings
        if finding.severity != OK
    }


def compute_digest(reports: list[AuditReport], prior: list[AuditFinding]) -> AuditDigest:
    """Diff this run's issues against the prior run's (pure).

    Args:
        reports: The current audit reports.
        prior: The attention-worthy findings recorded on the last run.

    Returns:
        The new and resolved findings. With no prior run, everything is new.
    """
    prior_by_key = {_key(finding): finding for finding in prior}
    current = _issues(reports)
    new = tuple(finding for key, finding in current.items() if key not in prior_by_key)
    resolved = tuple(finding for key, finding in prior_by_key.items() if key not in current)
    return AuditDigest(new=new, resolved=resolved)


def render_digest(digest: AuditDigest) -> str:
    """Render a digest as text lines (pure)."""
    if not digest.changed:
        return "audit digest: no change since last run"
    lines = [f"audit digest: {len(digest.new)} new, {len(digest.resolved)} resolved"]
    lines.extend(f"  + [{f.severity}] {f.project}: {f.message} ({f.category})" for f in digest.new)
    lines.extend(f"  - resolved {f.project}: {f.message} ({f.category})" for f in digest.resolved)
    return "\n".join(lines)


def digest_payload(digest: AuditDigest) -> dict[str, object]:
    """Build the JSON payload for a digest."""
    return {
        "changed": digest.changed,
        "new": [asdict(f) for f in digest.new],
        "resolved": [asdict(f) for f in digest.resolved],
    }


def load_prior(path: Path | None = None) -> list[AuditFinding]:
    """Load the previous run's attention-worthy findings; ``[]`` on any problem."""
    path = path or digest_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(raw, dict) or not isinstance(raw.get("issues"), list):
        return []
    return [
        AuditFinding(**{field: item[field] for field in _FIELDS})
        for item in raw["issues"]
        if isinstance(item, dict) and all(isinstance(item.get(f), str) for f in _FIELDS)
    ]


def save_current(reports: list[AuditReport], path: Path | None = None) -> None:
    """Persist this run's attention-worthy findings for the next diff; never raises."""
    path = path or digest_path()
    issues = list(_issues(reports).values())
    body = json.dumps({"issues": [asdict(f) for f in issues]}, indent=2)
    with contextlib.suppress(OSError, ValueError):
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
        tmp_path = Path(tmp)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(body)
            tmp_path.replace(path)
        except OSError:
            with contextlib.suppress(OSError):
                tmp_path.unlink()
            raise
