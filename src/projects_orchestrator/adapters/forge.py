"""Route a project to the CI adapter its own descriptor names.

Which forge can answer for a project is a property of the *project*, not of the
caller doing the asking. There are three answers — an explicitly declared status
URL (Jenkins, Buildkite, a self-hosted runner), GitLab via ``glab``, or GitHub
via ``gh`` — and every caller that probes CI needs all three.

They did not get all three. The ``ci`` CLI routed properly while the
controller's ``/ci`` called ``collect_github`` unconditionally, so probing a
GitLab-hosted project from the REPL asked ``gh`` about a repo it does not host,
got ``unknown`` back — and then **cached** it, over whatever the CLI had
correctly recorded. A probe that silently degrades is bad; one that writes its
degradation over a good answer is worse, because the next reader cannot tell the
difference. So the routing lives here, once, and both callers import it.

It lives under ``adapters/`` rather than in ``__main__`` for an ordinary reason:
``__main__`` imports the controller, so the controller cannot import back.
"""

from __future__ import annotations

import datetime as _dt

from projects_orchestrator.adapters.github import as_check_results, collect_github
from projects_orchestrator.adapters.gitlab import as_check_results as gitlab_check_results
from projects_orchestrator.adapters.gitlab import collect_gitlab, provider_is_gitlab
from projects_orchestrator.adapters.status_url import probe_status_url, status_check_results
from projects_orchestrator.checks import CheckResult
from projects_orchestrator.descriptor import ProjectDescriptor


def probe_ci(
    descriptor: ProjectDescriptor,
) -> tuple[dict[str, object], list[CheckResult], bool]:
    """Probe one project's CI via the forge its host names; never raises.

    Returns a ``(display payload, cacheable results, ci-failed)`` triple so every
    caller can render, cache, and set an exit code uniformly across GitHub,
    GitLab, and a declared status URL.
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
