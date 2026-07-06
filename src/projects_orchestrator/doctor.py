"""Diagnose each child's conformance to the descriptor contract.

``doctor`` answers one question: *can the orchestrator fully manage this
project?* It reads only the contract surfaces
(see ``docs/reference/descriptor-contract-v1.md``) and reports one finding per
requirement. Versions between v1 and the highest understood contract
(``CONTRACT_VERSION_MAX``) pass; a newer version warns, since the orchestrator
may misread surfaces it does not yet know. Like the rest of the engine it never
raises — a broken or half-scaffolded child yields ``fail``/``warn`` findings,
not an exception.

Severity is manageability, not correctness: ``fail`` means the orchestrator
cannot treat the project as a contract-v1 child; ``warn`` means a capability
(drift detection, gates, hook enforcement) is degraded but the project is
still readable; ``ok`` means the surface is present and usable.
"""

from __future__ import annotations

from dataclasses import dataclass

from projects_orchestrator.adapters.project_init import (
    has_upgrade_workflow,
    upgrade_workflow_relpath,
)
from projects_orchestrator.descriptor import (
    CONTRACT_V2,
    DEPLOY_NONE,
    ProjectDescriptor,
    parse_scaffold_version,
)
from projects_orchestrator.drift import compute_drift, hook_health

OK = "ok"
WARN = "warn"
FAIL = "fail"

# Lowest and highest contract versions this orchestrator actually understands.
CONTRACT_VERSION = 1
CONTRACT_VERSION_MAX = CONTRACT_V2


@dataclass(frozen=True)
class Finding:
    """One conformance check outcome for one project.

    Attributes:
        check: Requirement name (``config``, ``contract``, ``scaffold``,
            ``manifest``, ``hooks``, ``tooling``).
        status: ``ok`` | ``warn`` | ``fail``.
        detail: Short human-readable explanation.
    """

    check: str
    status: str
    detail: str = ""


@dataclass(frozen=True)
class DoctorReport:
    """Every conformance finding for one project.

    Attributes:
        project: Project name.
        findings: One finding per requirement, in check order.
    """

    project: str
    findings: tuple[Finding, ...] = ()

    @property
    def status(self) -> str:
        """Worst finding severity across the report (``fail`` > ``warn`` > ``ok``)."""
        statuses = {finding.status for finding in self.findings}
        if FAIL in statuses:
            return FAIL
        if WARN in statuses:
            return WARN
        return OK


def _check_config(descriptor: ProjectDescriptor) -> Finding:
    """The descriptor parsed without warnings."""
    if descriptor.warnings:
        return Finding("config", FAIL, descriptor.warnings[0])
    return Finding("config", OK, "config.yaml parsed")


def _check_contract(descriptor: ProjectDescriptor) -> Finding:
    """A contract version is declared and understood."""
    version = descriptor.contract_version
    if version < CONTRACT_VERSION:
        return Finding(
            "contract", FAIL, "no project_init_contract_version — predates the contract"
        )
    if version > CONTRACT_VERSION_MAX:
        # A newer child may use surfaces this orchestrator misreads — flag it
        # rather than silently claiming full conformance.
        return Finding(
            "contract",
            WARN,
            f"contract v{version} is newer than understood (v{CONTRACT_VERSION_MAX}) — upgrade the orchestrator",
        )
    return Finding("contract", OK, f"contract v{version}")


def _check_scaffold(descriptor: ProjectDescriptor) -> Finding:
    """A comparable scaffold version is recorded."""
    if parse_scaffold_version(descriptor.project_init_version) is None:
        return Finding("scaffold", WARN, "project_init_version missing or not comparable")
    return Finding("scaffold", OK, descriptor.project_init_version)


def _check_manifest(descriptor: ProjectDescriptor) -> Finding:
    """A scaffold manifest is present so drift detection can run."""
    report = compute_drift(descriptor)
    if report.status == "no-manifest":
        return Finding("manifest", WARN, "no scaffold.manifest — drift detection unavailable")
    return Finding("manifest", OK, f"{report.total} files tracked")


def _check_hooks(descriptor: ProjectDescriptor) -> Finding:
    """Git hooks the project ships are installed in its clone."""
    health = hook_health(descriptor)
    if health == "ok":
        return Finding("hooks", OK, "installed")
    if health == "-":
        return Finding("hooks", OK, "no hooks shipped")
    return Finding("hooks", WARN, f"git hooks {health} — run install_hooks.sh")


def _check_tooling(descriptor: ProjectDescriptor) -> Finding:
    """At least one gate command is declared to run."""
    if descriptor.tooling:
        return Finding("tooling", OK, ", ".join(sorted(descriptor.tooling)))
    return Finding("tooling", WARN, "no *_command declared — nothing to check")


def _check_upgrade(descriptor: ProjectDescriptor) -> Finding:
    """The child ships an upgrade workflow ``upgrade-plan --apply`` can dispatch.

    Absence is a real capability gap (a GitLab-hosted or ``--lifecycle none``
    child), so warn — otherwise ``--apply`` would report ``no upgrade workflow``
    with no prior diagnosis.
    """
    relpath = upgrade_workflow_relpath(descriptor)
    if has_upgrade_workflow(descriptor):
        return Finding("upgrade", OK, f"{relpath} present")
    return Finding("upgrade", WARN, f"no {relpath} — upgrade-plan --apply cannot dispatch")


def _check_cloud(descriptor: ProjectDescriptor) -> Finding:
    """Service projects declare enough deploy metadata for cloud-status to probe."""
    if descriptor.delivery != "service":
        return Finding("cloud", OK, f"{descriptor.delivery} delivery — no runtime probe expected")
    deploy = descriptor.deploy
    if deploy is None:
        return Finding(
            "cloud",
            WARN,
            "service project has no deploy metadata — add a contract-v2 deploy block",
        )
    if deploy.target == DEPLOY_NONE:
        return Finding(
            "cloud",
            WARN,
            "service project deploy target is none — cloud-status cannot probe it",
        )
    if deploy.target == "cloud-run" and (not deploy.app or not deploy.region):
        return Finding(
            "cloud",
            WARN,
            "cloud-run deploy metadata needs app and region for cloud-status",
        )
    return Finding("cloud", OK, f"{deploy.target} deploy metadata present")


_CHECKS = (
    _check_config,
    _check_contract,
    _check_scaffold,
    _check_manifest,
    _check_hooks,
    _check_tooling,
    _check_upgrade,
    _check_cloud,
)


def diagnose(descriptor: ProjectDescriptor) -> DoctorReport:
    """Run every contract-conformance check for one project (never raises).

    Args:
        descriptor: The project to diagnose.

    Returns:
        A report whose :attr:`DoctorReport.status` is the worst finding.
    """
    return DoctorReport(
        project=descriptor.name,
        findings=tuple(check(descriptor) for check in _CHECKS),
    )
