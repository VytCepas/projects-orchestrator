"""Notify-mode heal files one GitHub issue per failing gate, and closes it when the gate passes.

ADR-008 gives notify mode its channel: a project the operator wants *told about*
rather than *edited by an agent* gets a GitHub issue on its own repository,
carrying the evidence and the next step (#164). This module is that sink, the
second :data:`~projects_orchestrator.heal.HealSink` beside the webhook (#165):
heal produces the outcome, and a sink delivers it.

**One issue per finding.** A finding is one failing gate on one project, keyed
``<project>/<gate>``. The failing command's error text is evidence, not identity:
keyed on it, every changed line count in a test run would file a fresh issue.
A second gate failing on the same project is a second finding, and gets its own
issue, so the second failure is not hidden behind the first.

**Dedup state lives in the issues themselves.** Each issue body carries a hidden
marker with its key, and every pass reads the repository's open issues to find
them. A state file would lose track after a reinstall, a new machine or a
cleared ``$XDG_STATE_HOME``, and then file every issue again. GitHub, where the
issues live, cannot fall out of step with the issues. The cost is one ``gh``
read per notify-mode project per pass, and a read that fails leaves the answer
unknown: nothing is filed or closed for that project, and the pass reports a
failed delivery.

**Only the published default branch is reported.** A result is filed or closed
on only when it ran on a clean tree whose HEAD is the commit ``origin``'s
default branch points at. Anything else describes the operator's own work, not
the project: uncommitted changes, a local feature branch, or commits not yet
pushed or pulled. Filing on it would publish a failure the default branch may
not have, citing a commit GitHub may never have seen. Closing on it would close
an issue while the default branch is still red, which an independent review of
#287 reproduced with a fix committed on an unpushed branch. Those results are
logged and left alone, and the heal report still shows the failure.

**Closing needs evidence.** An issue closes only when this pass ran its gate and
the gate *passed*. A gate that was skipped, or not run this pass, says nothing
either way, so its issue stays open. An issue closed by hand while its gate
still fails is filed again on the next pass, once: it is the operator's policy
that this project reports its failures, and a closed issue on a red gate would
hide one.

Never raises, like the rest of the engine: every ``gh`` failure degrades to a
logged reason and a ``False`` delivery.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from pathlib import Path

from projects_orchestrator import landing, status
from projects_orchestrator.checks import CheckResult
from projects_orchestrator.descriptor import ProjectDescriptor
from projects_orchestrator.heal import (
    HEALABLE_TASKS,
    MODE_NOTIFY,
    NOTIFIED,
    FleetHealReport,
    HealResult,
    HealSink,
)

_log = logging.getLogger(__name__)

#: A cap on the quoted evidence, far below GitHub's body limit. Today the evidence
#: is a gate's last output line, already capped at 200 characters by ``checks``.
_EVIDENCE_LIMIT = 4000


def finding_key(project: str, task: str) -> str:
    """The identity of one failing gate, ``<project>/<gate>`` (pure)."""
    return f"{project}/{task}"


def _redact(text: str) -> str:
    """Make gate output safe to put in an issue body (pure).

    The home directory becomes ``~``, because the issue may land in a public
    repository. A NUL byte becomes a visible backslash-zero: the body travels as one
    argv element, and an argv element cannot hold a NUL, so a gate that printed
    one used to crash the whole heal pass.
    """
    home = str(Path.home())
    text = text.replace("\x00", "\\0")
    return text.replace(home, "~") if home not in ("", "/") else text


def _fenced(text: str) -> str:
    """Quote ``text`` in a code fence no backtick run inside it can close (pure)."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}text\n{text}\n{fence}"


def issue_title(project: str, task: str) -> str:
    """The issue title for one failing gate (pure)."""
    return f"heal: {task} is failing in {project}"


def issue_body(
    descriptor: ProjectDescriptor, check: CheckResult, next_step: str, marker: str
) -> str:
    """The issue body: what failed, the evidence, what to do next, and the marker (pure)."""
    task = check.task
    command = descriptor.tooling.get(task, "")
    evidence = _redact(check.detail.strip())[-_EVIDENCE_LIMIT:] or "(the gate reported no output)"
    when = check.checked_at or "the last heal pass"
    at = f" at `{check.head[:12]}`" if check.head else ""
    lines = [
        f"**`{task}` is failing in `{descriptor.name}`.** The orchestrator's heal pass runs "
        "this project in notify mode, so it reports the failure and changes nothing: no "
        "agent was started and no branch was pushed.",
        "",
        f"- Gate: `{task}`" + (f", command `{_redact(command)}`" if command else ""),
        f"- Checked: {when}{at}",
        "",
        "Evidence (the last line the gate printed):",
        "",
        _fenced(evidence),
        "",
        f"**Next:** {_redact(next_step)}",
        "",
        f"This issue closes itself once `{task}` passes on a later heal pass. Closing it "
        "by hand while the gate still fails files a new one on the next pass.",
        "",
        marker,
    ]
    return "\n".join(lines)


def _close_comment(check: CheckResult) -> str:
    """The closing comment: the passing run that cleared the finding (pure)."""
    when = check.checked_at or "this heal pass"
    at = f" at `{check.head[:12]}`" if check.head else ""
    return f"`{check.task}` passed on {when}{at}. Closing."


def _published(
    name: str, tasks: Sequence[str], cached: dict[str, CheckResult], tip: str
) -> list[str]:
    """The tasks whose result ran at ``tip``, the published default branch; log the rest."""
    kept = [task for task in tasks if tip and cached[task].head == tip]
    skipped = [task for task in tasks if task not in kept]
    if skipped:
        _log.warning(
            "%s: %s did not run at origin's default-branch tip (%s), so no issue is filed"
            " or closed for it: a dirty or untracked tree, a local branch, unpushed or"
            " unpulled commits, or no origin/HEAD (`git remote set-head origin --auto`)",
            name,
            ", ".join(skipped),
            tip[:12] or "unknown",
        )
    return kept


def _file_issues(
    descriptor: ProjectDescriptor,
    cached: dict[str, CheckResult],
    to_file: Sequence[str],
    open_keys: set[str],
    next_step: str,
) -> tuple[bool, bool]:
    """File one issue per new finding; returns ``(wrote, ok)``."""
    wrote = False
    ok = True
    for task in to_file:
        key = finding_key(descriptor.name, task)
        if key in open_keys:
            continue  # expected: already reported, and one issue per finding is the contract
        marker = landing.issue_marker(key)
        if not marker:
            _log.warning(
                "%s: %r cannot be written as an issue marker; not filed", descriptor.name, key
            )
            ok = False
            continue
        body = issue_body(descriptor, cached[task], next_step, marker)
        filed = landing.open_issue(descriptor.path, issue_title(descriptor.name, task), body)
        wrote = True
        if filed.ok:
            _log.info("%s: filed %s", key, filed.pr_url or "an issue")
        else:
            _log.warning("%s: issue not filed: %s", key, filed.detail)
            ok = False
    return wrote, ok


def _close_issues(
    descriptor: ProjectDescriptor,
    cached: dict[str, CheckResult],
    passing: Sequence[str],
    open_by_key: dict[str, landing.OwnIssue],
) -> tuple[bool, bool]:
    """Close the issue of every finding whose gate passed; returns ``(wrote, ok)``."""
    wrote = False
    ok = True
    for task in passing:
        key = finding_key(descriptor.name, task)
        issue = open_by_key.get(key)
        if issue is None:
            continue  # expected: a passing gate with nothing open has nothing to close
        closed = landing.close_own_issue(
            descriptor.path, issue.number, key, _close_comment(cached[task])
        )
        wrote = True
        if closed.ok:
            _log.info("%s: closed #%d", key, issue.number)
        else:
            _log.warning("%s: #%d not closed: %s", key, issue.number, closed.detail)
            ok = False
    return wrote, ok


def _deliver_project(
    descriptor: ProjectDescriptor,
    cached: dict[str, CheckResult],
    notified: HealResult | None,
) -> bool | None:
    """File and close one project's issues; ``None`` when nothing needed doing."""
    to_file = [task for task in (notified.tasks if notified else ()) if task in cached]
    passing = [
        task for task, check in cached.items() if task in HEALABLE_TASKS and check.status == "pass"
    ]
    if to_file or passing:
        tip = status.published_default_head(descriptor.path)
        to_file = _published(descriptor.name, to_file, cached, tip)
        passing = _published(descriptor.name, passing, cached, tip)
    if not to_file and not passing:
        return None
    issues = landing.own_open_issues(descriptor.path)
    if issues is None:
        if not to_file:
            # A clean pass reaches nobody (the HealSink contract). With nothing
            # failing there was nothing to deliver; only a possible close is lost,
            # and the next readable pass makes it.
            _log.warning(
                "%s: cannot read its open issues; nothing is failing, so nothing is closed"
                " this pass",
                descriptor.name,
            )
            return None
        _log.warning(
            "%s: cannot read its open issues, so it is neither filed nor closed", descriptor.name
        )
        return False
    open_by_key = {issue.key: issue for issue in issues}
    filed_any, filed_ok = _file_issues(
        descriptor, cached, to_file, set(open_by_key), notified.detail if notified else ""
    )
    closed_any, closed_ok = _close_issues(descriptor, cached, passing, open_by_key)
    ok = filed_ok and closed_ok
    return ok if (filed_any or closed_any or not ok) else None


def heal_issue_sink(
    targets: Sequence[tuple[ProjectDescriptor, dict[str, CheckResult]]], mode: str
) -> HealSink:
    """A :data:`~projects_orchestrator.heal.HealSink` that files and closes notify-mode issues.

    Args:
        targets: The ``(descriptor, {task: CheckResult})`` pairs the pass ran on.
            They carry the evidence a filed issue quotes and the passing results a
            close needs; the report alone names only what failed.
        mode: The pass's run-wide heal mode. A project's declared ``heal.mode``
            wins over it, as in :func:`~projects_orchestrator.heal.heal_project`.
            Only notify-mode projects are touched: a fix-mode project reports
            through its draft PR, never through an issue.

    Returns:
        A sink returning ``True`` when every write it attempted was accepted,
        ``False`` when any write failed or a project's issues could not be read,
        and ``None`` when there was nothing to file and nothing to close.
    """

    def sink(report: FleetHealReport) -> bool | None:
        notified = {
            result.project: result for result in report.results if result.status == NOTIFIED
        }
        outcomes = [
            _deliver_project(descriptor, cached, notified.get(descriptor.name))
            for descriptor, cached in targets
            if (descriptor.heal_mode or mode) == MODE_NOTIFY
        ]
        attempted = [outcome for outcome in outcomes if outcome is not None]
        return all(attempted) if attempted else None

    return sink
