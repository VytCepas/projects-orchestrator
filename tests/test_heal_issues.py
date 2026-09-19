"""Notify-mode heal files one issue per failing gate, and closes it when the gate passes (#164).

Every ``gh`` call goes to :class:`_FakeGitHub`, which stands in for one
repository's issue tracker. No test here reaches the real ``gh``: a real issue
notifies a real person.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import git_init, make_project

from projects_orchestrator import landing, status
from projects_orchestrator.__main__ import main
from projects_orchestrator.checks import CheckResult
from projects_orchestrator.descriptor import ProjectDescriptor, load_descriptor
from projects_orchestrator.heal import (
    FIXED,
    MODE_FIX,
    MODE_NOTIFY,
    AgentOutcome,
    FleetHealReport,
    HealResult,
    heal_fleet,
)
from projects_orchestrator.heal_issues import heal_issue_sink
from projects_orchestrator.runner import RunResult


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    def _explode(*_args: object, **_kwargs: object) -> AgentOutcome:
        message = "a test reached the REAL coding agent — notify mode must never start one"
        raise AssertionError(message)

    monkeypatch.setattr("projects_orchestrator.heal._default_agent_run", _explode)


_FAKE_TIP = "0123456789abcdef"
_REAL_TIP = status.published_default_head


@pytest.fixture(autouse=True)
def _published_tip(monkeypatch: pytest.MonkeyPatch) -> None:
    """A project that is not a git checkout reads as published at the fixtures' HEAD.

    The sink files only at origin's default-branch tip. The unit tests below build
    plain directories and hand the sink results stamped ``_FAKE_TIP``; real git
    checkouts (the CLI tests) keep the real lookup, so the published-tip rule is
    exercised end to end there.
    """

    def tip(path: Path) -> str:
        return _REAL_TIP(path) if (path / ".git").exists() else _FAKE_TIP

    monkeypatch.setattr(status, "published_default_head", tip)


class _FakeGitHub:
    """One repository's issues, answering the ``gh`` argv the boundary sends."""

    def __init__(self) -> None:
        self.issues: dict[int, dict[str, str]] = {}
        self.calls: list[list[str]] = []
        self.broken: set[str] = set()  # gh subcommands that fail ("list", "create", …)

    def file_by_hand(self, title: str, body: str) -> int:
        number = len(self.issues) + 1
        self.issues[number] = {"title": title, "body": body, "state": "OPEN", "comment": ""}
        return number

    @property
    def writes(self) -> list[list[str]]:
        return [args for args in self.calls if args[2] in ("create", "close", "edit", "reopen")]

    def open_issues(self) -> dict[int, dict[str, str]]:
        return {n: issue for n, issue in self.issues.items() if issue["state"] == "OPEN"}

    def __call__(self, args: list[str], cwd: Path, timeout: float = 30.0) -> RunResult:  # noqa: ARG002
        assert args[:2] == ["gh", "issue"], f"the boundary launched something else: {args}"
        self.calls.append(args)
        sub = args[2]
        if sub in self.broken:
            return RunResult(command=" ".join(args), returncode=1, stderr=f"gh: {sub} broke")
        if sub == "list":
            limit = int(args[args.index("--limit") + 1])
            rows = [
                {"number": n, "url": f"https://example.test/issues/{n}", "body": issue["body"]}
                for n, issue in self.open_issues().items()
            ][:limit]
            return RunResult(command=" ".join(args), returncode=0, stdout=json.dumps(rows))
        if sub == "create":
            title = args[args.index("--title") + 1]
            body = args[args.index("--body") + 1]
            number = self.file_by_hand(title, body)
            return RunResult(
                command=" ".join(args),
                returncode=0,
                stdout=f"https://example.test/issues/{number}\n",
            )
        if sub == "view":
            issue = self.issues[int(args[3])]
            payload = {"state": issue["state"], "body": issue["body"]}
            return RunResult(command=" ".join(args), returncode=0, stdout=json.dumps(payload))
        if sub == "close":
            issue = self.issues[int(args[3])]
            issue["state"] = "CLOSED"
            issue["comment"] = args[args.index("--comment") + 1]
            return RunResult(command=" ".join(args), returncode=0)
        raise AssertionError(f"unexpected gh subcommand: {args}")


@pytest.fixture
def github(monkeypatch: pytest.MonkeyPatch) -> _FakeGitHub:
    fake = _FakeGitHub()
    monkeypatch.setattr("projects_orchestrator.landing._run_argv", fake)
    return fake


def _alpha(fleet_dir: Path) -> ProjectDescriptor:
    return load_descriptor(
        make_project(fleet_dir, "alpha", tooling={"lint": "ruff check .", "test": "pytest -q"})
    )


def _check(task: str, state: str, detail: str = "", head: str = _FAKE_TIP) -> CheckResult:
    """A result taken at the published tip by default, as `heal` stamps it (#164 review)."""
    return CheckResult(
        project="alpha",
        task=task,
        status=state,
        detail=detail,
        checked_at="2026-09-19T03:00:00+00:00",
        head=head,
    )


def _git(project: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(project), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _publish(project: Path) -> None:
    """Give ``project`` a bare ``origin`` holding its ``main``, with ``origin/HEAD`` set."""
    bare = project.parent.parent / "remotes" / f"{project.name}.git"
    bare.parent.mkdir(exist_ok=True)
    subprocess.run(["git", "clone", "-q", "--bare", str(project), str(bare)], check=True)
    _git(project, "remote", "add", "origin", str(bare))
    _git(project, "fetch", "-q", "origin")
    _git(project, "remote", "set-head", "origin", "main")


def _committed_project(fleet_dir: Path, lint: str = "false") -> Path:
    """A real, clean git checkout on the published ``main``, whose lint gate runs ``lint``."""
    project = make_project(fleet_dir, "alpha", tooling={"lint": lint})
    git_init(project)
    _publish(project)
    return project


def _pass(descriptor: ProjectDescriptor, cached: dict[str, CheckResult]) -> bool | None:
    """One notify-mode heal pass over one project, delivered to the issue sink."""
    targets = [(descriptor, cached)]
    return heal_issue_sink(targets, MODE_NOTIFY)(heal_fleet(targets, limit=1, mode=MODE_NOTIFY))


# --- filing -----------------------------------------------------------------------------


def test_a_failing_gate_files_one_issue_with_the_evidence_and_the_next_step(
    fleet_dir: Path, github: _FakeGitHub
) -> None:
    alpha = _alpha(fleet_dir)
    assert _pass(alpha, {"lint": _check("lint", "fail", "E501 line too long (120 > 100)")}) is True
    [issue] = github.open_issues().values()
    assert issue["title"] == "heal: lint is failing in alpha"
    assert "E501 line too long (120 > 100)" in issue["body"]
    assert "command `ruff check .`" in issue["body"]
    assert "projects-orchestrator heal alpha" in issue["body"]
    assert "at `0123456789ab`" in issue["body"]
    assert landing.marker_key(issue["body"]) == "alpha/lint"


def test_a_gate_still_failing_on_the_next_pass_files_nothing_new(
    fleet_dir: Path, github: _FakeGitHub
) -> None:
    alpha = _alpha(fleet_dir)
    failing = {"lint": _check("lint", "fail", "boom")}
    assert _pass(alpha, failing) is True
    assert _pass(alpha, {"lint": _check("lint", "fail", "a different line, same gate")}) is None
    assert len(github.issues) == 1
    assert len(github.writes) == 1


def test_a_second_failing_gate_on_the_same_project_gets_its_own_issue(
    fleet_dir: Path, github: _FakeGitHub
) -> None:
    # Control: deduplication that folded distinct findings into one issue would
    # hide the second failure behind the first, which is worse than a duplicate.
    alpha = _alpha(fleet_dir)
    _pass(alpha, {"lint": _check("lint", "fail", "boom")})
    _pass(
        alpha, {"lint": _check("lint", "fail", "boom"), "test": _check("test", "fail", "1 failed")}
    )
    keys = sorted(landing.marker_key(i["body"]) for i in github.open_issues().values())
    assert keys == ["alpha/lint", "alpha/test"]


def test_the_home_directory_never_reaches_the_issue(fleet_dir: Path, github: _FakeGitHub) -> None:
    home = str(Path.home())
    _pass(_alpha(fleet_dir), {"lint": _check("lint", "fail", f"{home}/code/alpha/x.py:3: E1")})
    [issue] = github.open_issues().values()
    assert home not in issue["body"]
    assert "~/code/alpha/x.py:3: E1" in issue["body"]


def test_evidence_containing_a_code_fence_cannot_close_the_quote(
    fleet_dir: Path, github: _FakeGitHub
) -> None:
    _pass(_alpha(fleet_dir), {"lint": _check("lint", "fail", "```\nnot the end\n```")})
    [issue] = github.open_issues().values()
    assert "````text\n```\nnot the end\n```\n````" in issue["body"]


# --- closing ----------------------------------------------------------------------------


def test_a_gate_that_passes_again_closes_its_issue_with_a_comment(
    fleet_dir: Path, github: _FakeGitHub
) -> None:
    alpha = _alpha(fleet_dir)
    _pass(alpha, {"lint": _check("lint", "fail", "boom")})
    assert _pass(alpha, {"lint": _check("lint", "pass")}) is True
    [issue] = github.issues.values()
    assert issue["state"] == "CLOSED"
    assert (
        issue["comment"] == "`lint` passed on 2026-09-19T03:00:00+00:00 at `0123456789ab`. Closing."
    )


def test_only_the_gate_that_passed_is_closed(fleet_dir: Path, github: _FakeGitHub) -> None:
    alpha = _alpha(fleet_dir)
    _pass(alpha, {"lint": _check("lint", "fail", "x"), "test": _check("test", "fail", "y")})
    _pass(alpha, {"lint": _check("lint", "pass"), "test": _check("test", "fail", "y")})
    still_open = [landing.marker_key(i["body"]) for i in github.open_issues().values()]
    assert still_open == ["alpha/test"]


def test_a_skipped_or_unrun_gate_leaves_its_issue_open(
    fleet_dir: Path, github: _FakeGitHub
) -> None:
    # Skip is not evidence of a pass, and neither is a gate this pass did not run.
    alpha = _alpha(fleet_dir)
    _pass(alpha, {"lint": _check("lint", "fail", "x")})
    assert _pass(alpha, {"lint": _check("lint", "skip")}) is None
    assert _pass(alpha, {}) is None
    assert len(github.open_issues()) == 1


def test_an_issue_a_person_filed_is_never_closed(fleet_dir: Path, github: _FakeGitHub) -> None:
    github.file_by_hand("lint is broken", "I noticed lint fails on main.")
    assert _pass(_alpha(fleet_dir), {"lint": _check("lint", "pass")}) is None
    assert github.issues[1]["state"] == "OPEN"
    assert github.writes == []


def test_an_issue_edited_to_drop_its_marker_is_not_closed(
    fleet_dir: Path, github: _FakeGitHub
) -> None:
    # The body is re-read at close time: between the list and the close, a person
    # may have rewritten the issue into their own report.
    alpha = _alpha(fleet_dir)
    _pass(alpha, {"lint": _check("lint", "fail", "x")})
    issues = landing.own_open_issues(alpha.path)
    assert issues is not None
    github.issues[1]["body"] = "Rewritten by hand; tracking the real cause here."
    closed = landing.close_own_issue(alpha.path, issues[0].number, "alpha/lint", "done")
    assert closed.status == landing.REFUSED
    assert github.issues[1]["state"] == "OPEN"


def test_an_issue_a_person_already_closed_is_not_closed_again(
    fleet_dir: Path, github: _FakeGitHub
) -> None:
    alpha = _alpha(fleet_dir)
    _pass(alpha, {"lint": _check("lint", "fail", "x")})
    issues = landing.own_open_issues(alpha.path)
    assert issues is not None
    github.issues[1]["state"] = "CLOSED"
    closed = landing.close_own_issue(alpha.path, issues[0].number, "alpha/lint", "done")
    assert closed.status == landing.REFUSED
    assert [args for args in github.calls if args[2] == "close"] == []


def test_only_issues_the_signed_in_account_opened_are_read(
    fleet_dir: Path, github: _FakeGitHub
) -> None:
    # Anyone can open an issue on a public repository. One that pasted a marker
    # must not suppress the real report, nor be closed as if it were ours.
    _pass(_alpha(fleet_dir), {"lint": _check("lint", "fail", "x")})
    [listed] = [args for args in github.calls if args[2] == "list"]
    assert listed[listed.index("--author") + 1] == "@me"


# --- what reaches nobody ----------------------------------------------------------------


def test_a_clean_pass_with_nothing_open_writes_nothing(
    fleet_dir: Path, github: _FakeGitHub
) -> None:
    alpha = _alpha(fleet_dir)
    assert _pass(alpha, {"lint": _check("lint", "pass"), "test": _check("test", "pass")}) is None
    assert github.writes == []


def test_a_project_with_no_healable_result_is_not_even_read(
    fleet_dir: Path, github: _FakeGitHub
) -> None:
    assert _pass(_alpha(fleet_dir), {}) is None
    assert github.calls == []


def test_a_fix_mode_project_never_gets_an_issue(fleet_dir: Path, github: _FakeGitHub) -> None:
    # Its report is the draft PR. Not even a read: a passing gate on a fix-mode
    # project must not send the sink looking for issues to close.
    alpha = replace(_alpha(fleet_dir), heal_mode=MODE_FIX)
    cached = {"lint": _check("lint", "fail", "x"), "test": _check("test", "pass")}
    report = FleetHealReport(results=(HealResult("alpha", FIXED, tasks=("lint",)),), limit=1)
    assert heal_issue_sink([(alpha, cached)], MODE_NOTIFY)(report) is None
    assert github.calls == []


def test_a_declared_notify_project_is_reported_in_a_fix_mode_pass(
    fleet_dir: Path, github: _FakeGitHub
) -> None:
    alpha = replace(_alpha(fleet_dir), heal_mode=MODE_NOTIFY)
    targets = [(alpha, {"lint": _check("lint", "fail", "x")})]
    report = heal_fleet(targets, limit=1, mode=MODE_FIX)
    assert heal_issue_sink(targets, MODE_FIX)(report) is True
    assert len(github.open_issues()) == 1


# --- degrading --------------------------------------------------------------------------


def test_an_unreadable_issue_list_files_nothing_and_says_so(
    fleet_dir: Path, github: _FakeGitHub
) -> None:
    github.broken.add("list")
    assert _pass(_alpha(fleet_dir), {"lint": _check("lint", "fail", "x")}) is False
    assert github.writes == []


def test_a_refused_create_is_a_failed_delivery_not_a_crash(
    fleet_dir: Path, github: _FakeGitHub
) -> None:
    github.broken.add("create")
    assert _pass(_alpha(fleet_dir), {"lint": _check("lint", "fail", "x")}) is False


def test_a_full_page_of_issues_is_unknown_not_none(fleet_dir: Path, github: _FakeGitHub) -> None:
    # An issue past the page could be the one that makes this a duplicate.
    repo = _alpha(fleet_dir).path
    github.file_by_hand("one", "a")
    github.file_by_hand("two", "b")
    assert landing.own_open_issues(repo, limit=2) is None
    assert landing.own_open_issues(repo, limit=3) == ()


def test_an_issue_without_a_marker_is_refused_at_the_boundary(
    fleet_dir: Path, github: _FakeGitHub
) -> None:
    filed = landing.open_issue(_alpha(fleet_dir).path, "t", "no marker here")
    assert filed.status == landing.REFUSED
    assert github.calls == []


@pytest.mark.parametrize("key", ["alpha/lint -->", "al pha/lint", "alpha", "a/b/c", "alpha/"])
def test_an_unsafe_key_has_no_marker(key: str) -> None:
    assert landing.issue_marker(key) == ""


# --- the CLI ----------------------------------------------------------------------------


def test_the_cli_files_on_a_notify_pass_and_reports_the_delivery(
    fleet_dir: Path, github: _FakeGitHub, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _committed_project(fleet_dir)
    argv = ["heal", "--all", "--mode", "notify", "--root", str(fleet_dir), "--issues", "--json"]
    assert main(argv) == 1
    out = capsys.readouterr()
    assert json.loads(out.out)["issues"] == "delivered"
    assert "issues: delivered" in out.err
    [issue] = github.open_issues().values()
    assert issue["title"] == "heal: lint is failing in alpha"
    assert f"at `{_git(project, 'rev-parse', 'HEAD')[:12]}`" in issue["body"]


def test_the_cli_without_the_flag_files_nothing(fleet_dir: Path, github: _FakeGitHub) -> None:
    make_project(fleet_dir, "alpha", tooling={"lint": "false"})
    assert main(["heal", "--all", "--mode", "notify", "--root", str(fleet_dir)]) == 1
    assert github.calls == []


def test_a_broken_gh_does_not_change_the_exit_code(
    fleet_dir: Path, github: _FakeGitHub, capsys: pytest.CaptureFixture[str]
) -> None:
    github.broken.add("list")
    _committed_project(fleet_dir)
    argv = ["heal", "--all", "--mode", "notify", "--root", str(fleet_dir), "--issues"]
    assert main(argv) == 1
    assert "issues: delivery failed" in capsys.readouterr().err


# --- the #164 review -------------------------------------------------------------------


def test_a_nul_byte_in_the_evidence_is_quoted_not_a_crash(
    fleet_dir: Path, github: _FakeGitHub
) -> None:
    assert _pass(_alpha(fleet_dir), {"lint": _check("lint", "fail", "E1 bad\x00byte")}) is True
    [issue] = github.open_issues().values()
    assert "\x00" not in issue["body"]
    assert "E1 bad\\0byte" in issue["body"]


def test_an_argv_holding_a_nul_degrades_instead_of_raising(tmp_path: Path) -> None:
    # Refused by subprocess before exec, so nothing runs.
    result = landing._run_argv(["printf", "a\x00b"], cwd=tmp_path)
    assert result.returncode is None
    assert "null" in result.error


def test_a_gate_printing_a_nul_still_prints_the_heal_report(
    fleet_dir: Path, github: _FakeGitHub, capsys: pytest.CaptureFixture[str]
) -> None:
    project = make_project(fleet_dir, "alpha", tooling={"lint": "sh lint.sh"})
    (project / "lint.sh").write_text("printf 'E1 bad\\000byte\\n' >&2\nexit 1\n", encoding="utf-8")
    git_init(project)
    _publish(project)
    argv = ["heal", "--all", "--mode", "notify", "--root", str(fleet_dir), "--issues"]
    assert main(argv) == 1
    out = capsys.readouterr()
    assert "lint failing" in out.out
    assert "issues: delivered" in out.err
    [issue] = github.open_issues().values()
    assert "\x00" not in issue["body"]


def test_uncommitted_changes_file_nothing(fleet_dir: Path, github: _FakeGitHub) -> None:
    alpha = _alpha(fleet_dir)
    assert _pass(alpha, {"lint": _check("lint", "fail", "wip", head="")}) is None
    assert github.calls == []


def test_uncommitted_changes_close_nothing(fleet_dir: Path, github: _FakeGitHub) -> None:
    alpha = _alpha(fleet_dir)
    _pass(alpha, {"lint": _check("lint", "fail", "boom")})
    assert _pass(alpha, {"lint": _check("lint", "pass", head="")}) is None
    assert len(github.open_issues()) == 1


def test_the_cli_files_nothing_for_a_dirty_tree(
    fleet_dir: Path, github: _FakeGitHub, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _committed_project(fleet_dir)
    (project / "wip.txt").write_text("uncommitted\n", encoding="utf-8")
    argv = ["heal", "--all", "--mode", "notify", "--root", str(fleet_dir), "--issues"]
    assert main(argv) == 1
    assert "lint failing" in capsys.readouterr().out
    assert github.calls == []


def test_an_unreadable_list_on_a_clean_pass_is_not_a_failed_delivery(
    fleet_dir: Path, github: _FakeGitHub
) -> None:
    # A clean pass reaches nobody (the HealSink contract): with nothing failing
    # there was nothing to deliver, so an unreadable list is not "delivery failed".
    github.broken.add("list")
    assert _pass(_alpha(fleet_dir), {"lint": _check("lint", "pass")}) is None


def test_a_refused_close_is_a_failed_delivery(fleet_dir: Path, github: _FakeGitHub) -> None:
    alpha = _alpha(fleet_dir)
    _pass(alpha, {"lint": _check("lint", "fail", "boom")})
    github.broken.add("close")
    assert _pass(alpha, {"lint": _check("lint", "pass")}) is False


_UNIT = Path(__file__).resolve().parents[1] / "contrib/systemd/projects-orchestrator-heal.service"


@pytest.mark.parametrize(
    ("value", "enabled"), [("1", True), ("0", False), ("false", False), ("", False), (None, False)]
)
def test_only_po_heal_issues_1_enables_filing_on_the_timer(
    tmp_path: Path, value: str | None, enabled: bool
) -> None:
    if not _UNIT.is_file():
        pytest.skip("no contrib/ beside the tests (e.g. mutmut's mutants/ copy)")
    exec_start = next(
        line
        for line in _UNIT.read_text(encoding="utf-8").splitlines()
        if line.startswith("ExecStart=")
    )
    script = re.search(r"-c '(.*)'$", exec_start).group(1).replace("%h", str(tmp_path))
    stub = tmp_path / ".local/bin/projects-orchestrator"
    stub.parent.mkdir(parents=True)
    stub.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n', encoding="utf-8")
    stub.chmod(0o755)
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    if value is not None:
        env["PO_HEAL_ISSUES"] = value
    argv = subprocess.run(
        ["/bin/sh", "-c", script], env=env, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    assert ("--issues" in argv) is enabled


# --- the published default branch (review of #287) ------------------------------------


def test_a_result_not_at_the_published_tip_files_and_closes_nothing(
    fleet_dir: Path, github: _FakeGitHub
) -> None:
    alpha = _alpha(fleet_dir)
    assert _pass(alpha, {"lint": _check("lint", "fail", "x", head="feedface")}) is None
    assert github.calls == []
    _pass(alpha, {"lint": _check("lint", "fail", "boom")})
    assert _pass(alpha, {"lint": _check("lint", "pass", head="feedface")}) is None
    assert len(github.open_issues()) == 1


def test_a_fix_on_an_unpushed_branch_does_not_close_the_issue(
    fleet_dir: Path, github: _FakeGitHub
) -> None:
    # The review's reproduction: the issue is filed from main, then a fix is
    # committed on a local branch that was never pushed. main is still red.
    project = make_project(fleet_dir, "alpha", tooling={"lint": "sh lint.sh"})
    (project / "lint.sh").write_text("echo E1 >&2\nexit 1\n", encoding="utf-8")
    git_init(project)
    _publish(project)
    argv = ["heal", "--all", "--mode", "notify", "--root", str(fleet_dir), "--issues"]
    assert main(argv) == 1
    assert len(github.open_issues()) == 1

    _git(project, "checkout", "-q", "-b", "local-fix")
    (project / "lint.sh").write_text("exit 0\n", encoding="utf-8")
    _git(project, "commit", "-qam", "fix lint")
    main(argv)
    assert len(github.open_issues()) == 1, "an unpushed fix closed the issue"

    # Control: the same fix published on main does close it.
    _git(project, "checkout", "-q", "main")
    _git(project, "merge", "-q", "--ff-only", "local-fix")
    _git(project, "push", "-q", "origin", "main")
    _git(project, "fetch", "-q", "origin")
    main(argv)
    assert github.open_issues() == {}


def test_a_checkout_with_no_origin_files_nothing(fleet_dir: Path, github: _FakeGitHub) -> None:
    project = make_project(fleet_dir, "alpha", tooling={"lint": "false"})
    git_init(project)
    argv = ["heal", "--all", "--mode", "notify", "--root", str(fleet_dir), "--issues"]
    assert main(argv) == 1
    assert github.calls == []


def test_a_stale_clone_is_not_the_published_tip(
    fleet_dir: Path, tmp_path: Path, github: _FakeGitHub
) -> None:
    # Codex on #293: after another checkout pushes, this clone's HEAD and its
    # remote-tracking ref still agree with each other. Only the remote knows.
    project = _committed_project(fleet_dir)
    other = tmp_path / "other"
    subprocess.run(
        ["git", "clone", "-q", _git(project, "remote", "get-url", "origin"), str(other)], check=True
    )
    _git(other, "config", "user.email", "test@example.com")
    _git(other, "config", "user.name", "Test")
    (other / "later.txt").write_text("pushed from another checkout\n", encoding="utf-8")
    _git(other, "add", "later.txt")
    _git(other, "commit", "-qm", "later")
    _git(other, "push", "-q", "origin", "main")
    argv = ["heal", "--all", "--mode", "notify", "--root", str(fleet_dir), "--issues"]
    assert main(argv) == 1
    assert github.calls == []


def test_only_the_remote_head_line_names_the_tip(monkeypatch: pytest.MonkeyPatch) -> None:
    # `ls-remote origin HEAD` also matches any ref ENDING in HEAD, e.g. a branch
    # called feature/HEAD. Only the bare HEAD line is the default branch.
    listing = f"{'a' * 40}\trefs/heads/feature/HEAD\n{'b' * 40}\tHEAD"
    monkeypatch.setattr(status, "_git", lambda _path, *_args: listing)
    assert _REAL_TIP(Path()) == "b" * 40
    monkeypatch.setattr(status, "_git", lambda _path, *_args: None)
    assert _REAL_TIP(Path()) == ""
