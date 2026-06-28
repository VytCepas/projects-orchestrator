"""Prod-safety guard (PI-168, ADR-012): deny destructive infra/DB commands.

PreToolUse hook on Bash. Deterministic deny-table — no LLM, no network.
Destructive operations that bypass the git/CI boundary (cloud deletes,
DROP DATABASE, terraform destroy, …) get:

- ``ask``   in interactive sessions — a human confirms or rejects;
- ``block`` in fully autonomous sessions (``bypassPermissions``) — there is
  no human to ask, so the command is blocked outright.

Escape hatch: ``safety.allow`` in ``.claude/config.yaml`` holds a JSON list
of regex patterns; a command matching any of them is never flagged. Use it
for known-safe contexts (e.g. a dev-cluster kubectl context).

This is a guardrail, not the security boundary (ADR-007/ADR-012): a
sufficiently creative command can evade a deny-list. The guarantee comes
from credential separation — agent sessions must never hold production
credentials (see .claude/docs/guides/secrets.md).

Fail-open by design: any internal error lets the command proceed.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

# (pattern, label) — matched against the full command string. ``_SEG``
# tolerates global flags between the CLI name and the destructive verb
# (e.g. `kubectl --context prod delete …`) while stopping at pipeline and
# command separators; the cost is rare false positives on odd resource
# names, which the ask/allowlist paths absorb cheaply.
_SEG = r"[^|;&]*?"
DENY_RULES: list[tuple[re.Pattern[str], str]] = [
    # OpenTofu (`tofu`) is a CLI-identical Terraform fork — guard both the same
    # way (PI-488). `(?:-\S+\s+)*` tolerates global options before the verb
    # (e.g. `tofu -chdir=infra destroy`) like _SEG does for the other rules, but
    # stays flag-specific (only skips leading `-tokens`) so a read-only
    # `plan -destroy` is NOT flagged. Routine `apply -auto-approve` is
    # intentionally not flagged; only destroy / apply-with-destroy is.
    (
        re.compile(r"\b(?:terraform|tofu)\s+(?:-\S+\s+)*(destroy|apply\s+.*-destroy)\b"),
        "terraform/tofu destroy/apply -destroy",
    ),
    (re.compile(rf"\bkubectl\b{_SEG}\bdelete\b"), "kubectl delete"),
    (re.compile(rf"\bhelm\b{_SEG}\b(uninstall|delete)\b"), "helm uninstall"),
    (re.compile(rf"\baws\b{_SEG}\b(delete|terminate|remove)\S*\b"), "aws delete/terminate"),
    (
        re.compile(rf"\baws\b{_SEG}\bs3\s+(rb\b|rm\b{_SEG}--recursive)"),
        "aws s3 bucket/recursive removal",
    ),
    (re.compile(rf"\bgcloud\b{_SEG}\bdelete\b"), "gcloud delete"),
    (re.compile(rf"\baz\b{_SEG}\bdelete\b"), "az delete"),
    (re.compile(r"\bdrop\s+(table|database|schema)\b", re.IGNORECASE), "SQL DROP"),
    (re.compile(r"\btruncate\s+table\b", re.IGNORECASE), "SQL TRUNCATE"),
    (
        re.compile(
            r"\brm\s+(-[a-zA-Z]*r[a-zA-Z]*f|-[a-zA-Z]*f[a-zA-Z]*r)[a-zA-Z]*\s+(/(?!tmp\b)|~)"
        ),
        "recursive force-remove outside the project",
    ),
    (re.compile(r"\bgh\s+repo\s+delete\b"), "gh repo delete"),
    (re.compile(r"\bdocker\s+(volume\s+prune|system\s+prune)\b"), "docker prune"),
]

# Fully autonomous mode: no human is watching the prompt, so "ask" is
# meaningless — block outright. Other modes (default, plan, acceptEdits)
# still surface an interactive permission prompt for Bash.
_AUTONOMOUS_MODES = {"bypassPermissions", "dangerouslySkipPermissions"}


def _find_config(start: Path) -> Path | None:
    """Walk up from *start* to the project's .claude/config.yaml, if any."""
    for candidate in (start, *start.parents):
        config = candidate / ".claude" / "config.yaml"
        if config.is_file():
            return config
    return None


def _unquote(value: str) -> str:
    """Strip one pair of matching surrounding quotes, leaving mismatched or
    single quotes intact so ``'foo"`` is not silently corrupted (PI-187 review).
    """
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _allow_patterns(root: Path) -> list[re.Pattern[str]]:
    """Read the safety.allow list from .claude/config.yaml (fail-open).

    Accepts both an inline JSON list (``allow: ["a", "b"]``) and a multi-line
    YAML list (``allow:`` on its own line followed by ``- "a"`` items). The
    inline-only parser silently dropped the natural YAML form to ``[]`` (PI-187).

    *root* is the Bash tool's cwd, which may be a subdirectory after `cd` —
    the config is located by walking up the tree.
    """
    config = _find_config(root)
    if config is None:
        return []
    patterns: list[str] = []
    try:
        in_safety = False
        in_allow = False
        for line in config.read_text(encoding="utf-8").splitlines():
            if line.startswith("safety:"):
                in_safety = True
                continue
            if not in_safety:
                continue
            if line.strip() and not line.startswith((" ", "\t")):
                break  # a column-0 key ends the safety block
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if in_allow and stripped.startswith("- "):
                patterns.append(_unquote(stripped[2:].strip()))
                continue
            in_allow = False
            if stripped.startswith("allow:"):
                raw = stripped.split(":", 1)[1].strip()
                if raw:
                    parsed = json.loads(raw)  # inline JSON list
                    # A non-list allow (JSON string/object/number) must not be
                    # iterated character-by-character into an over-permissive
                    # allowlist or crash the guard — ignore it and keep
                    # guarding (PI-187 review).
                    if isinstance(parsed, list):
                        patterns.extend(p for p in parsed if isinstance(p, str))
                else:
                    in_allow = True  # multi-line YAML list follows
        return [re.compile(p) for p in patterns if p]
    except (OSError, json.JSONDecodeError, re.error):
        return []


def _find_obs_dir(start: Path) -> Path | None:
    """Locate the overlay marker dir (.claude/observability/), or None.

    Prefers ``$CLAUDE_PROJECT_DIR``; otherwise walks up from *start* (the Bash
    cwd, which may be a subdirectory after ``cd``), mirroring ``_find_config``.
    """
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env:
        obs = Path(env) / ".claude" / "observability"
        return obs if obs.is_dir() else None
    for candidate in (start, *start.parents):
        obs = candidate / ".claude" / "observability"
        if obs.is_dir():
            return obs
    return None


def usage_log(payload: dict, root: Path) -> None:
    """Append a self-log line iff the observability overlay is installed (#406).

    Shipped-always-dormant: no-ops unless ``.claude/observability/`` exists.
    Uses the *already-parsed* ``payload`` (no second stdin read) and is fully
    fail-open — it must never raise or block the guard.
    """
    try:
        obs = _find_obs_dir(root)
        if obs is None:
            return
        line = {
            # time.gmtime keeps this portable across every Python 3 (no
            # datetime.UTC, which is 3.11+) — scaffolded projects may run older.
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "hook": "prod_guard",
            "event": "PreToolUse",
            "project": str(obs.parent.parent),
        }
        session = payload.get("session_id") or os.environ.get("CLAUDE_SESSION_ID")
        if session:
            line["session"] = session
        with (obs / "usage.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line) + "\n")
    except Exception:  # noqa: BLE001 — logging must never break the guard
        return


def evaluate(command: str, permission_mode: str, allow: list[re.Pattern[str]]) -> dict | None:
    """Return the hook verdict for *command*, or None to let it through."""
    if any(p.search(command) for p in allow):
        return None
    for pattern, label in DENY_RULES:
        if pattern.search(command):
            reason = (
                f"prod_guard: '{label}' is a destructive operation. "
                "If this is intentional and safe, add a matching regex to "
                "safety.allow in .claude/config.yaml, or run it yourself. "
                "(Guardrail only — real protection is credential separation, "
                "see .claude/docs/guides/secrets.md.)"
            )
            decision = "deny" if permission_mode in _AUTONOMOUS_MODES else "ask"
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": decision,
                    "permissionDecisionReason": reason,
                }
            }
    return None


def main() -> int:
    """Read the PreToolUse payload from stdin; print a verdict if any."""
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return 0
    if not isinstance(payload, dict):
        return 0  # non-dict JSON (e.g. a list) → fail open, never raise
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}  # tool_input present but non-dict → fail open, never raise
    command = (tool_input.get("command") or "").strip()
    if not command:
        return 0
    mode = payload.get("permission_mode") or payload.get("permissionMode") or ""
    root = Path(payload.get("cwd") or ".")
    # Self-log this firing from the same parsed payload (no second stdin read,
    # #406). Dormant unless the observability overlay is installed; fail-open.
    usage_log(payload, root)
    try:
        verdict = evaluate(command, mode, _allow_patterns(root))
    except Exception:  # noqa: BLE001 — guardrail must never break the session
        return 0
    if verdict is not None:
        sys.stdout.write(json.dumps(verdict))
    return 0


if __name__ == "__main__":
    sys.exit(main())
