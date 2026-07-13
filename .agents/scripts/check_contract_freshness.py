#!/usr/bin/env python3
"""Report whether the vendored project-init contract still matches upstream.

The contract tests are a tripwire against a producer change — but only against
the copy vendored *here*. If project-init changes the contract and nobody
re-vendors, those tests keep passing on a stale copy and the drift ships
silently. This closes that loop (epic #68 / #106).

Exit codes are chosen for a scheduled job:
  0  fresh, or unknown (upstream unreachable — a flaky network is not a
     contract change, and a job that cried wolf on every blip gets muted)
  1  stale — the vendored schema or the golden fixture's pinned project-init
     version has diverged; re-vendor per tests/fixtures/project_init/README.md

Run it locally with `just contract-freshness`.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from projects_orchestrator.freshness import (  # noqa: E402
    STALE,
    compare,
    fetch_upstream_schema,
    load_vendored_schema,
    render,
)

_VENDORED_SCHEMA = REPO / "tests/fixtures/project_init/schemas/descriptor.schema.json"
_GOLDEN_FIXTURE = REPO / "tests/fixtures/project_init/config.v2.yaml"
_VERSION_RE = re.compile(r"^\s*project_init_version:\s*(\S+)", re.MULTILINE)


def pinned_version(fixture: Path) -> str:
    """The project-init version that generated the golden fixture; '' if unreadable."""
    try:
        match = _VERSION_RE.search(fixture.read_text(encoding="utf-8"))
    except OSError:
        return ""
    return match.group(1).strip().strip('"') if match else ""


# `gh` is resolved from PATH, as everywhere else in this repo (the lifecycle
# scripts, the github/gitlab adapters). Fixed argv, no shell.
_GH_LATEST_RELEASE = [
    "gh",
    "release",
    "view",
    "--repo",
    "VytCepas/project-init",
    "--json",
    "tagName",
    "-q",
    ".tagName",
]


def upstream_version() -> str:
    """The newest project-init release tag, without the leading 'v'; '' on any problem."""
    try:
        result = subprocess.run(  # noqa: S603
            _GH_LATEST_RELEASE, capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip().lstrip("v") if result.returncode == 0 else ""


def main() -> int:
    """Compare the vendored contract against upstream and report."""
    report = compare(
        vendored_schema=load_vendored_schema(_VENDORED_SCHEMA),
        upstream_schema=fetch_upstream_schema(),
        pinned_version=pinned_version(_GOLDEN_FIXTURE),
        upstream_version=upstream_version(),
    )
    print(render(report))
    return 1 if report.status == STALE else 0


if __name__ == "__main__":
    raise SystemExit(main())
