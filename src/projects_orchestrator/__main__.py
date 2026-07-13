"""CLI entry point for `projects-orchestrator`.

One command surface over the fleet engine: discover projects, show git
health, run their declared gates, search their memories, or drive it all
interactively (``controller`` REPL / ``tui``). Every data command takes
``--json`` so external monitors can consume the same truth the tables show.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from dataclasses import asdict, replace
from pathlib import Path

from projects_orchestrator import __version__, cache
from projects_orchestrator.adapters.cloud import as_check_results as cloud_check_results
from projects_orchestrator.adapters.cloud import collect_cloud
from projects_orchestrator.adapters.github import as_check_results, collect_github
from projects_orchestrator.adapters.gitlab import as_check_results as gitlab_check_results
from projects_orchestrator.adapters.gitlab import collect_gitlab, provider_is_gitlab
from projects_orchestrator.adapters.project_init import (
    latest_upstream_version,
    parse_scaffold_result,
    trigger_upgrade,
)
from projects_orchestrator.adapters.status_url import probe_status_url, status_check_results
from projects_orchestrator.audit import audit_project, render_markdown
from projects_orchestrator.capabilities import (
    HOOK,
    MCP,
    SKILL,
    load_capabilities,
)
from projects_orchestrator.capabilities import (
    aggregate as aggregate_capabilities,
)
from projects_orchestrator.checks import DEFAULT_TASKS, CheckResult, collect_checks
from projects_orchestrator.controller import ControllerContext, dispatch, parse_command
from projects_orchestrator.descriptor import ProjectDescriptor
from projects_orchestrator.digest import (
    compute_digest,
    digest_payload,
    load_prior,
    render_digest,
    save_current,
)
from projects_orchestrator.doctor import diagnose
from projects_orchestrator.drift import compute_drift
from projects_orchestrator.fleet import fleet_rows, fleet_snapshots, render_table
from projects_orchestrator.hardening import checklist as hardening_checklist
from projects_orchestrator.hardening import render_text as render_hardening
from projects_orchestrator.history import DEFAULT_TREND_WIDTH as HISTORY_TREND_WIDTH
from projects_orchestrator.history import load_history, project_history, sparkline, transitions
from projects_orchestrator.history import record as history_record
from projects_orchestrator.html import render_html
from projects_orchestrator.memory import load_memory, retrieval_mode, search_memory
from projects_orchestrator.notify import (
    alerts_payload,
    fleet_alerts,
    post_webhook,
    render_alerts,
)
from projects_orchestrator.observability import filter_since, load_events
from projects_orchestrator.pool import map_ordered
from projects_orchestrator.registry import (
    FLEET_FILENAME,
    Fleet,
    FleetConfig,
    default_fleet_config,
    discover,
    load_fleet_config,
    register_project,
)
from projects_orchestrator.server import DEFAULT_HOST, DEFAULT_PORT, serve
from projects_orchestrator.status import clean_worktree_head, collect_status
from projects_orchestrator.supervisor import logs as run_logs
from projects_orchestrator.supervisor import start as run_start
from projects_orchestrator.supervisor import stop as run_stop
from projects_orchestrator.upgrade import upgrade_plan


def _fleet_config(args: argparse.Namespace) -> FleetConfig:
    """Resolve discovery config from --fleet / --root / defaults."""
    if args.fleet:
        return load_fleet_config(Path(args.fleet))
    if args.root:
        return FleetConfig(roots=(Path(args.root).expanduser().resolve(),))
    return default_fleet_config()


def _discover(args: argparse.Namespace) -> Fleet:
    """Discover the fleet, surfacing warnings on stderr."""
    fleet = discover(_fleet_config(args))
    for warning in fleet.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    return fleet


def _emit_json(payload: object) -> int:
    """Print a JSON document (paths become strings)."""
    print(json.dumps(payload, indent=2, default=str))
    return 0


def _cmd_projects(args: argparse.Namespace) -> int:
    """List discovered projects."""
    fleet = _discover(args)
    if args.json:
        return _emit_json([asdict(d) for d in fleet.descriptors])
    for descriptor in fleet.descriptors:
        print(f"{descriptor.name}  ({descriptor.language}, {descriptor.path})")
    if not fleet.descriptors:
        print("no projects discovered")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    """Show the fleet table, or one project's git health."""
    fleet = _discover(args)
    if args.project:
        descriptor = fleet.get(args.project)
        if descriptor is None:
            print(f"unknown project: {args.project}", file=sys.stderr)
            return 2
        status = collect_status(descriptor)
        if args.json:
            return _emit_json(asdict(status))
        print(f"{status.project}: {status.health} on {status.branch or '?'}")
        return 0
    snapshots = fleet_snapshots(fleet)
    if args.json:
        return _emit_json([asdict(s.status) for s in snapshots])
    print(render_table(fleet_rows(snapshots)))
    return 0


def _reusable_pass(
    cached: dict[str, CheckResult] | None, task: str, head: str
) -> CheckResult | None:
    """Return the cached result that can stand in for a fresh run, if any.

    A cached result is trusted only when it is a ``pass`` recorded at the
    same clean-worktree HEAD the project is at now. Fails, skips, dirty
    trees, and unknown identities never match — they always re-run.
    """
    if not head or cached is None:
        return None
    result = cached.get(task)
    if result is not None and result.status == "pass" and result.head == head:
        return result
    return None


def _project_checks(
    descriptor: ProjectDescriptor,
    tasks: tuple[str, ...],
    cached: dict[str, CheckResult] | None,
    changed_only: bool,
) -> list[tuple[CheckResult, bool]]:
    """Run one project's gates, reusing cached passes when allowed.

    Returns:
        ``(result, reused)`` pairs in task order; ``reused`` marks results
        served from the cache instead of a fresh run.
    """
    head = clean_worktree_head(descriptor)
    reusable: dict[str, CheckResult] = {}
    if changed_only:
        reusable = {
            task: result
            for task in tasks
            if (result := _reusable_pass(cached, task, head)) is not None
        }
    to_run = tuple(task for task in tasks if task not in reusable)
    fresh_results = collect_checks(descriptor, to_run, head=head)
    # Re-verify the worktree did not change while the gates ran: a tracked file
    # edited mid-run and reverted would otherwise stamp a pass under `head` that
    # a fresh run at that same HEAD would not reproduce. On any change, drop the
    # recorded identity so --changed-only can never reuse these results.
    if head and clean_worktree_head(descriptor) != head:
        fresh_results = [replace(result, head="") for result in fresh_results]
    fresh = dict(zip(to_run, fresh_results, strict=True))
    return [(reusable[task], True) if task in reusable else (fresh[task], False) for task in tasks]


def _cmd_checks(args: argparse.Namespace) -> int:
    """Run declared gates; exit 1 when any project fails one."""
    fleet = _discover(args)
    if args.project:
        descriptor = fleet.get(args.project)
        if descriptor is None:
            print(f"unknown project: {args.project}", file=sys.stderr)
            return 2
        selected = [descriptor]
    else:
        selected = list(fleet.descriptors)

    tasks = tuple(args.task) if args.task else DEFAULT_TASKS
    cached = cache.load_results() if args.changed_only else {}
    per_project = map_ordered(
        lambda d: _project_checks(d, tasks, cached.get(d.name), args.changed_only),
        selected,
        jobs=args.jobs,
    )
    pairs = [pair for project_pairs in per_project for pair in project_pairs]
    fresh = [result for result, reused in pairs if not reused]
    cache.save_results(fresh)
    history_record(fresh)
    if args.json:
        return _emit_json([{**asdict(r), "cached": reused} for r, reused in pairs])
    for result, reused in pairs:
        suffix = f" — {result.detail}" if result.detail else ""
        cached_mark = " (cached)" if reused else ""
        print(f"{result.project} {result.task}: {result.status}{cached_mark}{suffix}")
    return 1 if any(result.status == "fail" for result, _ in pairs) else 0


def _cmd_memory(args: argparse.Namespace) -> int:
    """Search every project's memory, using each project's tier retrieval surface."""
    fleet = _discover(args)
    # Degrade-by-tier (ADR-025 §4): grep the memory_path baseline, and add the
    # graph's facts for tier>=2 children. RAG-tier children are noted so an
    # operator knows a surface exists that this local-only search does not query.
    for descriptor in fleet.descriptors:
        if retrieval_mode(descriptor) == "rag":
            print(
                f"note: {descriptor.name} exposes a tier-3 RAG endpoint "
                f"({descriptor.rag_endpoint}) not queried by local search",
                file=sys.stderr,
            )
    memories = [load_memory(d) for d in fleet.descriptors]
    hits = search_memory(memories, " ".join(args.query))
    if args.json:
        return _emit_json([asdict(h) for h in hits])
    for hit in hits:
        location = f"{hit.file.project}/{hit.file.path.name}:{hit.line_number}"
        print(f"{location} [{hit.file.type}] {hit.file.name} — {hit.line}")
    if not hits:
        print("no matches")
    return 0


def _cmd_capabilities(args: argparse.Namespace) -> int:
    """Aggregate each project's CAPABILITIES.md — who exposes which skill/MCP."""
    fleet = _discover(args)
    selected = list(fleet.descriptors)
    if args.project:
        descriptor = fleet.get(args.project)
        if descriptor is None:
            print(f"unknown project: {args.project}", file=sys.stderr)
            return 2
        selected = [descriptor]
    inventories = [load_capabilities(d) for d in selected]
    if args.json:
        return _emit_json([asdict(inv) for inv in inventories])
    if args.kind:
        index = aggregate_capabilities(inventories, args.kind)
        for name, projects in index.items():
            print(f"{name}: {', '.join(projects)}")
        if not index:
            print(f"no {args.kind} capabilities across the fleet")
        return 0
    for inventory in inventories:
        if not inventory.present:
            print(f"{inventory.project}: no CAPABILITIES.md")
            continue
        print(
            f"{inventory.project}: {len(inventory.skills)} skill(s), "
            f"{len(inventory.mcp_servers)} MCP server(s), {len(inventory.hooks)} hook(s)"
        )
    return 0


def _cmd_drift(args: argparse.Namespace) -> int:
    """Report scaffold drift; exit 1 when any project drifted."""
    fleet = _discover(args)
    selected = list(fleet.descriptors)
    if args.project:
        descriptor = fleet.get(args.project)
        if descriptor is None:
            print(f"unknown project: {args.project}", file=sys.stderr)
            return 2
        selected = [descriptor]
    reports = [compute_drift(d) for d in selected]
    if args.json:
        return _emit_json([asdict(r) for r in reports])
    for report in reports:
        print(f"{report.project}: {report.summary}")
        for relpath in report.modified:
            print(f"  modified: {relpath}")
        for relpath in report.missing:
            print(f"  missing:  {relpath}")
    return 1 if any(r.status == "drift" for r in reports) else 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    """Diagnose contract-v1 conformance; exit 1 when any project fails."""
    fleet = _discover(args)
    selected = list(fleet.descriptors)
    if args.project:
        descriptor = fleet.get(args.project)
        if descriptor is None:
            print(f"unknown project: {args.project}", file=sys.stderr)
            return 2
        selected = [descriptor]
    reports = [diagnose(d) for d in selected]
    if args.json:
        return _emit_json([asdict(r) for r in reports])
    for report in reports:
        print(f"{report.project}: {report.status}")
        for finding in report.findings:
            print(f"  [{finding.status}] {finding.check}: {finding.detail}")
    return 1 if any(r.status == "fail" for r in reports) else 0


def _cmd_audit(args: argparse.Namespace) -> int:
    """Run the composed governance audit; exit 1 when anything needs attention."""
    fleet = _discover(args)
    selected = list(fleet.descriptors)
    if args.project:
        descriptor = fleet.get(args.project)
        if descriptor is None:
            print(f"unknown project: {args.project}", file=sys.stderr)
            return 2
        selected = [descriptor]
    cached = cache.load_results()
    reports = [audit_project(d, cached.get(d.name)) for d in selected]
    if args.digest:
        digest = compute_digest(reports, load_prior())
        save_current(reports)
        if args.json:
            return _emit_json(digest_payload(digest))
        print(render_digest(digest))
        return 1 if digest.new else 0
    if args.json:
        return _emit_json([asdict(r) for r in reports])
    if args.markdown:
        print(render_markdown(reports))
    else:
        for report in reports:
            print(f"{report.project}: {report.status}")
            for finding in report.findings:
                print(f"  [{finding.severity}] {finding.category}: {finding.message}")
    return 1 if any(r.needs_attention for r in reports) else 0


def _cmd_hardening(args: argparse.Namespace) -> int:
    """Show setup-readiness gaps with concrete next actions."""
    fleet = _discover(args)
    selected = list(fleet.descriptors)
    if args.project:
        descriptor = fleet.get(args.project)
        if descriptor is None:
            print(f"unknown project: {args.project}", file=sys.stderr)
            return 2
        selected = [descriptor]
    reports = hardening_checklist(selected, cache.load_results())
    if args.json:
        return _emit_json([asdict(report) for report in reports])
    print(render_hardening(reports))
    return 1 if any(report.needs_attention for report in reports) else 0


def _cmd_ci(args: argparse.Namespace) -> int:
    """Probe each project's CI conclusion + open-PR count via gh; cache them.

    Exits 1 when any project's CI has failed. Results are written to the
    checks cache so the ``status`` table shows last-known CI state offline.
    """
    fleet = _discover(args)
    selected = list(fleet.descriptors)
    if args.project:
        descriptor = fleet.get(args.project)
        if descriptor is None:
            print(f"unknown project: {args.project}", file=sys.stderr)
            return 2
        selected = [descriptor]
    probes = map_ordered(_probe_ci, selected)
    cache.save_results([r for _, results, _ in probes for r in results])
    if args.json:
        return _emit_json([payload for payload, _, _ in probes])
    for payload, _results, _failed in probes:
        count = "?" if payload["count"] is None else str(payload["count"])
        print(f"{payload['project']}: CI {payload['ci']}, {count} open {payload['unit']}(s)")
    return 1 if any(failed for _, _, failed in probes) else 0


def _probe_ci(
    descriptor: ProjectDescriptor,
) -> tuple[dict[str, object], list[CheckResult], bool]:
    """Probe one project's CI via the forge its host names; never raises.

    Returns a ``(display payload, cacheable results, ci-failed)`` triple so the
    ``ci`` command can render, cache, and set its exit code uniformly across
    GitHub and GitLab.
    """
    checked_at = _dt.datetime.now(tz=_dt.UTC).isoformat(timespec="seconds")
    if descriptor.ci is not None:
        # An explicitly declared status URL wins over the forge: the project is
        # telling us its CI is somewhere else (Jenkins, Buildkite, a self-hosted
        # runner), and `gh`/`glab` would report `unknown` for it forever.
        ci = probe_status_url(descriptor)
        payload: dict[str, object] = {
            "project": descriptor.name,
            "ci": ci,
            # A build endpoint reports builds, not code review — there is no
            # PR/MR count to show, and `?` is the honest cell.
            "count": None,
            "unit": "PR",
        }
        return payload, status_check_results(descriptor.name, ci, checked_at), ci == "fail"
    if provider_is_gitlab(descriptor):
        gl = collect_gitlab(descriptor)
        payload = {
            "project": gl.project,
            "ci": gl.ci,
            "count": gl.open_mrs,
            "unit": "MR",
        }
        return payload, gitlab_check_results(gl, checked_at), gl.ci == "fail"
    gh = collect_github(descriptor)
    payload = {"project": gh.project, "ci": gh.ci, "count": gh.open_prs, "unit": "PR"}
    return payload, as_check_results(gh, checked_at), gh.ci == "fail"


def _cmd_cloud_status(args: argparse.Namespace) -> int:
    """Probe deploy/runtime status per project (descriptor-driven); cache it.

    Exits 1 when any probed service is stopped or unhealthy. ``deploy: none``
    projects cost nothing (no subprocess, no network).
    """
    fleet = _discover(args)
    selected = list(fleet.descriptors)
    if args.project:
        descriptor = fleet.get(args.project)
        if descriptor is None:
            print(f"unknown project: {args.project}", file=sys.stderr)
            return 2
        selected = [descriptor]
    checked_at = _dt.datetime.now(tz=_dt.UTC).isoformat(timespec="seconds")
    statuses = map_ordered(collect_cloud, selected)
    cache.save_results([r for s in statuses for r in cloud_check_results(s, checked_at)])
    if args.json:
        return _emit_json([asdict(s) for s in statuses])
    for status in statuses:
        parts = [status.state]
        if status.revision:
            parts.append(status.revision)
        if status.health:
            parts.append(status.health)
        print(f"{status.project}: {status.target} — {' '.join(parts)}")
    return 1 if any(s.health == "unhealthy" or s.state == "stopped" for s in statuses) else 0


def _cmd_events(args: argparse.Namespace) -> int:
    """Show guard/usage events across the fleet's observability logs."""
    fleet = _discover(args)
    selected = list(fleet.descriptors)
    if args.project:
        descriptor = fleet.get(args.project)
        if descriptor is None:
            print(f"unknown project: {args.project}", file=sys.stderr)
            return 2
        selected = [descriptor]
    reports = [load_events(d) for d in selected]
    since = args.since or ""
    if args.json:
        payload = [
            {
                "project": r.project,
                "events": [asdict(e) for e in filter_since(r.events, since)],
                "warnings": list(r.warnings),
            }
            for r in reports
        ]
        return _emit_json(payload)
    empty = True
    for report in reports:
        for warning in report.warnings:
            print(f"warning: {report.project}: {warning}", file=sys.stderr)
        for event in filter_since(report.events, since):
            empty = False
            command = f" — {event.command}" if event.command else ""
            session = f" ({event.session})" if event.session else ""
            print(
                f"{event.project} {event.timestamp} [{event.hook}] {event.action}{session}{command}"
            )
    if empty:
        print("no events recorded")
    return 0


def _cmd_upgrade_plan(args: argparse.Namespace) -> int:
    """Compare each project's scaffold version against upstream project-init.

    Exits 1 when any project is outdated. ``--apply`` dispatches each outdated
    child's own ``project-init-upgrade.yml`` workflow (never edits its tree).
    """
    fleet = _discover(args)
    selected = list(fleet.descriptors)
    if args.project:
        descriptor = fleet.get(args.project)
        if descriptor is None:
            print(f"unknown project: {args.project}", file=sys.stderr)
            return 2
        selected = [descriptor]
    latest = latest_upstream_version(Path.cwd())
    rows = upgrade_plan(selected, latest, cache.load_results())
    applied: dict[str, str] = {}
    if args.apply:
        by_name = {d.name: d for d in selected}
        applied = {
            row.project: trigger_upgrade(by_name[row.project])
            for row in rows
            if row.status == "outdated"
        }
    if args.json:
        return _emit_json([{**asdict(r), "applied": applied.get(r.project)} for r in rows])
    for row in rows:
        line = (
            f"{row.project}: {row.status} "
            f"(scaffold {row.scaffold_version}, drift {row.drift}, PRs {row.open_prs})"
        )
        if row.project in applied:
            line += f" — upgrade {applied[row.project]}"
        print(line)
    return 1 if any(r.status == "outdated" for r in rows) else 0


def _cmd_register(args: argparse.Namespace) -> int:
    """Register a freshly-scaffolded project from `scaffold --json` output.

    Consumes the project-init ``--json`` seam (#510): reads a scaffold-result
    document (a file path, or ``-`` for stdin) and adds the new project to the
    fleet file so the next command governs it — no manual edit, no second read.
    """
    raw = sys.stdin.read() if args.result == "-" else _read_text_or_none(args.result)
    if raw is None:
        print(f"cannot read scaffold result: {args.result}", file=sys.stderr)
        return 2
    result = parse_scaffold_result(raw)
    if result is None:
        print("not a project-init scaffold result (no 'target')", file=sys.stderr)
        return 1
    fleet_file = Path(args.fleet) if args.fleet else Path.cwd() / FLEET_FILENAME
    outcome = register_project(fleet_file, result.target)
    for warning in outcome.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    if args.json:
        return _emit_json(
            {
                "target": str(outcome.project),
                "fleet_file": str(outcome.fleet_file),
                "added": outcome.added,
                "contract_version": result.contract_version,
                "files_created": result.files_created,
                "conflicts": list(result.conflicts),
            }
        )
    verb = "registered" if outcome.added else "already registered"
    print(
        f"{verb} {outcome.project} in {outcome.fleet_file} "
        f"(contract v{result.contract_version}, {result.files_created} files)"
    )
    for conflict in result.conflicts:
        print(f"  scaffold conflict (left unwritten): {conflict}", file=sys.stderr)
    # A write failure surfaces as a warning with added=False; treat as an error.
    return 0 if outcome.added or not outcome.warnings else 1


def _read_text_or_none(path: str) -> str | None:
    """Read a file's text, or ``None`` when it is unreadable."""
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return None


def _resolve_project(args: argparse.Namespace) -> ProjectDescriptor | None:
    """Resolve the required project argument, printing the error itself."""
    fleet = _discover(args)
    descriptor = fleet.get(args.project)
    if descriptor is None:
        print(f"unknown project: {args.project}", file=sys.stderr)
    return descriptor


def _cmd_start(args: argparse.Namespace) -> int:
    """Launch a project's declared run_command, detached and logged."""
    descriptor = _resolve_project(args)
    if descriptor is None:
        return 2
    message = run_start(descriptor)
    print(message)
    # Match the outcome on the "(pid " anchor the success lines carry, so a
    # project named e.g. "restarted-service" whose failure line contains
    # "started" as a substring is not misread as success.
    return 0 if "(pid " in message else 1


def _cmd_stop(args: argparse.Namespace) -> int:
    """Terminate a project's supervised process."""
    descriptor = _resolve_project(args)
    if descriptor is None:
        return 2
    print(run_stop(descriptor))
    return 0


def _cmd_logs(args: argparse.Namespace) -> int:
    """Show the tail of a project's captured run output."""
    descriptor = _resolve_project(args)
    if descriptor is None:
        return 2
    for line in run_logs(descriptor, lines=args.lines):
        print(line)
    return 0


def _cmd_snapshot(args: argparse.Namespace) -> int:
    """Dump the full joined fleet view (text, JSON, or standalone HTML)."""
    fleet = _discover(args)
    snapshots = fleet_snapshots(fleet)
    if args.json:
        return _emit_json([asdict(s) for s in snapshots])
    # -o implies --html: writing the text table to a .html file the operator
    # named would silently produce a non-page, so treat an output path as a
    # request for the HTML document.
    if args.html or args.output:
        generated_at = _dt.datetime.now(tz=_dt.UTC).isoformat(timespec="seconds")
        document = render_html(fleet_rows(snapshots), generated_at)
        if args.output:
            try:
                Path(args.output).write_text(document, encoding="utf-8")
            except OSError as exc:
                print(f"cannot write {args.output}: {exc}", file=sys.stderr)
                return 1
            print(f"wrote {args.output}")
        else:
            print(document, end="")
        return 0
    print(render_table(fleet_rows(snapshots)))
    return 0


def _cmd_history(args: argparse.Namespace) -> int:
    """Show per-task check-history trends and pass/fail transitions for a project."""
    fleet = _discover(args)
    descriptor = fleet.get(args.project)
    if descriptor is None:
        print(f"unknown project: {args.project}", file=sys.stderr)
        return 2
    by_task = project_history(load_history(), descriptor.name)
    if args.json:
        return _emit_json(
            {
                task: {
                    "trend": sparkline(entries, args.width),
                    "transitions": [asdict(e) for e in transitions(entries)],
                }
                for task, entries in by_task.items()
            }
        )
    if not by_task:
        print(f"{descriptor.name}: no check history yet")
        return 0
    for task in sorted(by_task):
        entries = by_task[task]
        print(f"{task}: {sparkline(entries, args.width)}  ({len(entries)} run(s))")
        last = transitions(entries)[-1] if transitions(entries) else None
        if last is not None:
            print(f"  last change: {last.status} at {last.checked_at}")
    return 0


def _cmd_notify(args: argparse.Namespace) -> int:
    """Compute threshold alerts and optionally push them to a webhook."""
    fleet = _discover(args)
    selected = list(fleet.descriptors)
    if args.project:
        descriptor = fleet.get(args.project)
        if descriptor is None:
            print(f"unknown project: {args.project}", file=sys.stderr)
            return 2
        selected = [descriptor]
    snapshots = [s for s in fleet_snapshots(fleet) if s.descriptor in selected]
    alerts = fleet_alerts(snapshots)
    if args.json:
        _emit_json(alerts_payload(alerts))
    else:
        print(render_alerts(alerts))
    if args.webhook and alerts:
        ok = post_webhook(args.webhook, alerts)
        print(f"webhook: {'delivered' if ok else 'delivery failed'}", file=sys.stderr)
    return 1 if alerts else 0


def _cmd_serve(args: argparse.Namespace) -> int:
    """Serve the live fleet dashboard over HTTP until interrupted."""
    serve(_fleet_config(args), host=args.host, port=args.port)
    return 0


def _cmd_controller(args: argparse.Namespace) -> int:
    """Run the deterministic command REPL."""
    ctx = ControllerContext(config=_fleet_config(args))
    print(f"fleet: {len(ctx.fleet.descriptors)} project(s) — type 'help' for commands")
    while True:
        try:
            line = input("orchestrator> ")
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        intent = parse_command(line)
        if intent.verb == "quit":
            return 0
        for output in dispatch(intent, ctx):
            print(output)


def _cmd_tui(args: argparse.Namespace) -> int:
    """Launch the Textual TUI (requires the ``tui`` extra)."""
    try:
        from projects_orchestrator.tui import OrchestratorApp
    except ModuleNotFoundError:
        print(
            "the TUI needs the optional dependency: uv sync --extra tui "
            "(or: pip install 'projects-orchestrator[tui]')",
            file=sys.stderr,
        )
        return 2
    OrchestratorApp(config=_fleet_config(args)).run()
    return 0


def _add_common(parser: argparse.ArgumentParser, json_flag: bool = True) -> None:
    """Attach the shared --fleet/--root (and usually --json) options."""
    parser.add_argument("--fleet", help="path to a fleet.yaml describing the fleet")
    parser.add_argument("--root", help="directory scanned one level deep for projects")
    if json_flag:
        parser.add_argument("--json", action="store_true", help="emit JSON instead of text")


def _build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser with all subcommands."""
    parser = argparse.ArgumentParser(
        prog="projects-orchestrator",
        description="Cross-project orchestration layer for agentic development.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")

    specs: list[tuple[str, str, object, bool]] = [
        ("projects", "list discovered projects", _cmd_projects, True),
        ("status", "fleet git health (table) or one project", _cmd_status, True),
        ("checks", "run each project's declared gates", _cmd_checks, True),
        ("memory", "search all project memories", _cmd_memory, True),
        (
            "capabilities",
            "aggregate CAPABILITIES.md — who exposes which skill/MCP/hook",
            _cmd_capabilities,
            True,
        ),
        ("drift", "scaffold drift vs the recorded manifest", _cmd_drift, True),
        ("doctor", "diagnose contract-v1 conformance", _cmd_doctor, True),
        (
            "audit",
            "composed governance report (conformance + drift + memory + freshness)",
            _cmd_audit,
            True,
        ),
        (
            "hardening",
            "setup-readiness checklist with concrete next actions",
            _cmd_hardening,
            True,
        ),
        ("ci", "latest CI conclusion + open-PR count per project (via gh)", _cmd_ci, True),
        (
            "cloud-status",
            "deploy/runtime status per project (descriptor deploy block)",
            _cmd_cloud_status,
            True,
        ),
        ("events", "guard/usage events from the fleet's observability logs", _cmd_events, True),
        ("history", "per-task check-history trend and pass/fail transitions", _cmd_history, True),
        (
            "notify",
            "threshold alerts (CI red, drift, hooks, cloud) — optionally to a webhook",
            _cmd_notify,
            True,
        ),
        ("start", "launch a project's run_command (detached, logged)", _cmd_start, False),
        ("stop", "terminate a project's supervised process", _cmd_stop, False),
        ("logs", "tail a project's captured run output", _cmd_logs, False),
        (
            "upgrade-plan",
            "scaffold version vs upstream project-init (--apply triggers upgrades)",
            _cmd_upgrade_plan,
            True,
        ),
        (
            "register",
            "register a scaffolded project from `project-init scaffold --json` output",
            _cmd_register,
            True,
        ),
        ("snapshot", "full joined fleet view", _cmd_snapshot, True),
        ("serve", "serve the live fleet dashboard over HTTP", _cmd_serve, False),
        ("controller", "interactive deterministic command REPL", _cmd_controller, False),
        ("tui", "terminal UI (requires the tui extra)", _cmd_tui, False),
    ]
    for name, help_text, handler, json_flag in specs:
        sp = sub.add_parser(name, help=help_text)
        _add_common(sp, json_flag=json_flag)
        sp.set_defaults(handler=handler)

    sub.choices["status"].add_argument("project", nargs="?", help="limit to one project")
    sub.choices["checks"].add_argument("project", nargs="?", help="limit to one project")
    sub.choices["capabilities"].add_argument("project", nargs="?", help="limit to one project")
    sub.choices["capabilities"].add_argument(
        "--kind",
        choices=(SKILL, MCP, HOOK),
        help="invert the fleet: list each capability of this kind and who exposes it",
    )
    sub.choices["drift"].add_argument("project", nargs="?", help="limit to one project")
    sub.choices["doctor"].add_argument("project", nargs="?", help="limit to one project")
    sub.choices["audit"].add_argument("project", nargs="?", help="limit to one project")
    sub.choices["hardening"].add_argument("project", nargs="?", help="limit to one project")
    sub.choices["audit"].add_argument(
        "--markdown", action="store_true", help="render the report as Markdown"
    )
    sub.choices["audit"].add_argument(
        "--digest",
        action="store_true",
        help="show only what changed since the last audit run (exit 1 on new issues)",
    )
    sub.choices["ci"].add_argument("project", nargs="?", help="limit to one project")
    sub.choices["cloud-status"].add_argument("project", nargs="?", help="limit to one project")
    sub.choices["events"].add_argument("project", nargs="?", help="limit to one project")
    sub.choices["events"].add_argument("--since", help="only events at/after this ISO-8601 instant")
    sub.choices["history"].add_argument("project", help="project to show history for")
    sub.choices["history"].add_argument(
        "-n", "--width", type=int, default=HISTORY_TREND_WIDTH, help="trend width (default 10)"
    )
    sub.choices["notify"].add_argument("project", nargs="?", help="limit to one project")
    sub.choices["notify"].add_argument(
        "--webhook", help="POST alerts as JSON to this URL (Slack-compatible)"
    )
    for name in ("start", "stop", "logs"):
        sub.choices[name].add_argument("project", help="project to act on")
    sub.choices["logs"].add_argument(
        "-n", "--lines", type=int, default=40, help="trailing lines to show (default 40)"
    )
    sub.choices["register"].add_argument(
        "result", help="path to `scaffold --json` output, or '-' for stdin"
    )
    sub.choices["upgrade-plan"].add_argument("project", nargs="?", help="limit to one project")
    sub.choices["upgrade-plan"].add_argument(
        "--apply", action="store_true", help="dispatch the upgrade workflow for outdated projects"
    )
    sub.choices["snapshot"].add_argument(
        "--html", action="store_true", help="render a self-contained HTML dashboard"
    )
    sub.choices["snapshot"].add_argument(
        "-o", "--output", help="write the HTML to this file instead of stdout"
    )
    sub.choices["checks"].add_argument(
        "--task", action="append", help="gate to run (repeatable; default: lint, test)"
    )
    sub.choices["checks"].add_argument(
        "--jobs", type=int, help="parallel projects (default: min(8, cpu count))"
    )
    sub.choices["checks"].add_argument(
        "--changed-only",
        action="store_true",
        help="skip gates whose last cached pass is at the current clean HEAD",
    )
    sub.choices["memory"].add_argument("query", nargs="+", help="text to search for")
    sub.choices["serve"].add_argument(
        "--host", default=DEFAULT_HOST, help=f"bind host (default {DEFAULT_HOST})"
    )
    sub.choices["serve"].add_argument(
        "--port", type=int, default=DEFAULT_PORT, help=f"bind port (default {DEFAULT_PORT})"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the projects-orchestrator CLI.

    Args:
        argv: Optional argument vector; defaults to ``sys.argv[1:]``.

    Returns:
        Process exit code.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    exit_code: int = args.handler(args)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
