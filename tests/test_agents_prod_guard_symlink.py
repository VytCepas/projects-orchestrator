"""The deployed .agents/hooks/prod_guard.py must refuse a symlinked marker.

PI-903 (project-init#904). This is a REGRESSION test on the SCAFFOLDED COPY, not
on upstream's source: upstream already covers the behaviour, and that did not stop
this repo from carrying the pre-fix version for weeks. A scaffold refresh can
silently restore the old file, and nothing else here would notice.

Behavioural, not textual: it runs the hook as the harness does — real payload on
stdin, real symlinked `.agents` on disk — and asserts the parsed decision. A
`grep` for `is_symlink` would pass on a docstring (AGENTS.md: assert parsed
structure or behaviour, not a substring).

Why refusal is the safe direction: `_find_config` locates the file `safety.allow`
is read from, so a marker symlinked outside the repo supplies its own allowlist
and switches the destructive-command deny table off wholesale. A symlink is
writable from outside the repo's own review, which is what the guard defends.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

GUARD = Path(__file__).resolve().parents[1] / ".agents" / "hooks" / "prod_guard.py"
DESTRUCTIVE = "gcloud sql instances delete prod-db"


def run_guard(cwd: Path) -> dict:
    """Invoke the hook the way Claude Code does and return its parsed verdict."""
    payload = {
        "session_id": "test-pi903",
        "cwd": str(cwd),
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": DESTRUCTIVE},
    }
    proc = subprocess.run(
        [sys.executable, str(GUARD)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"hook exited {proc.returncode}: {proc.stderr}"
    return json.loads(proc.stdout) if proc.stdout.strip() else {}


def decision(verdict: dict) -> str | None:
    return verdict.get("hookSpecificOutput", {}).get("permissionDecision")


def write_allowlist(agents_dir: Path) -> None:
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "config.yaml").write_text(
        f'safety:\n  allow:\n    - "{DESTRUCTIVE}"\n', encoding="utf-8"
    )


@pytest.mark.skipif(not GUARD.exists(), reason=".agents/hooks/prod_guard.py not scaffolded here")
class TestSymlinkedMarkerCannotDisableTheDenyTable:
    def test_baseline_a_real_allowlist_is_honoured(self, tmp_path: Path):
        """Control. Without this, the test below could pass for the wrong reason.

        If a REAL in-repo allowlist did not suppress the verdict, then "denied
        with a symlink" would prove nothing about symlinks — it would just mean
        the allowlist never worked. This pins that the mechanism the attack
        targets is live.
        """
        repo = tmp_path / "real"
        write_allowlist(repo / ".agents")
        assert decision(run_guard(repo)) is None, (
            "a real .agents/config.yaml allowlist should suppress the ask; "
            "if it does not, the symlink assertion below is vacuous"
        )

    def test_symlinked_agents_dir_is_refused(self, tmp_path: Path):
        planted = tmp_path / "planted"
        write_allowlist(planted)

        repo = tmp_path / "victim"
        repo.mkdir()
        (repo / ".agents").symlink_to(planted, target_is_directory=True)

        assert decision(run_guard(repo)) == "ask", (
            "a symlinked .agents supplied its own safety.allow and disabled the deny table (PI-903)"
        )

    def test_symlinked_config_file_is_refused(self, tmp_path: Path):
        planted = tmp_path / "planted"
        write_allowlist(planted)

        repo = tmp_path / "victim"
        (repo / ".agents").mkdir(parents=True)
        (repo / ".agents" / "config.yaml").symlink_to(planted / "config.yaml")

        assert decision(run_guard(repo)) == "ask", (
            "a symlinked .agents/config.yaml supplied its own safety.allow "
            "(PI-903 covers the file, not just the directory)"
        )
