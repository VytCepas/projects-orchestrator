"""Campaigns — a declarative fan-out that canaries first and never lies.

The load-bearing properties are the safety ones, and each has a test whose whole
job is to fail if that safety is removed:

- a broken campaign file launches NOTHING (it raises at load, in the shell);
- the default is a CANARY — one project, not the fleet;
- progress is DERIVED — a project already handled is not launched again;
- an empty selector is DONE, not an error;
- the policy is ENFORCED — concurrency is bounded, and a runaway run is killed.
"""

from __future__ import annotations

import textwrap
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import make_project

from projects_orchestrator import campaign, runs
from projects_orchestrator.descriptor import ProjectDescriptor
from projects_orchestrator.fleet import fleet_snapshots
from projects_orchestrator.registry import FleetConfig, discover


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Runs are recorded under $XDG_STATE_HOME; keep every test's store its own.
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


def _write(path: Path, body: str) -> Path:
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def _valid_body(select: str = "scaffold=none", **policy: object) -> str:
    policy_lines = "".join(f"  {k}: {v}\n" for k, v in policy.items())
    policy_block = f"policy:\n{policy_lines}" if policy_lines else ""
    return f"version: 1\nname: rollout\nselect: {select}\ntask: apply project-init\n{policy_block}"


def _plain_repo(fleet_dir: Path, name: str) -> Path:
    """A git repo with NO project-init scaffold — what a rollout targets."""
    import subprocess

    repo = fleet_dir / name
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    return repo


def _descriptor(name: str) -> ProjectDescriptor:
    return ProjectDescriptor(name=name, path=Path(f"/tmp/{name}"), language="python")


# --- Loading: a broken campaign file launches nothing --------------------------


def test_a_valid_campaign_parses_with_all_fields(tmp_path: Path) -> None:
    camp = campaign.load_campaign(
        _write(
            tmp_path / "c.yml",
            """
            version: 1
            name: rollout
            select:
              - scaffold=none
              - language=python
            task: apply the scaffold
            policy:
              max_concurrent: 4
              timeout: 600
              include_plain_repos: true
              output: json
            """,
        )
    )
    assert camp.name == "rollout"
    assert camp.select == ("scaffold=none", "language=python")
    assert camp.task == "apply the scaffold"
    assert camp.policy == campaign.Policy(
        max_concurrent=4, timeout=600.0, include_plain_repos=True, output="json"
    )


def test_policy_defaults_when_omitted(tmp_path: Path) -> None:
    camp = campaign.load_campaign(_write(tmp_path / "c.yml", _valid_body()))
    assert camp.policy == campaign.Policy()  # max_concurrent=1: canary-safe default


@pytest.mark.parametrize("missing", ["name", "select", "task"])
def test_a_missing_required_field_is_refused(tmp_path: Path, missing: str) -> None:
    lines = [line for line in _valid_body().splitlines() if not line.startswith(f"{missing}:")]
    with pytest.raises(campaign.CampaignError, match=missing):
        campaign.load_campaign(_write(tmp_path / "c.yml", "\n".join(lines)))


def test_an_unknown_top_level_key_is_refused(tmp_path: Path) -> None:
    # `tasks:` (a typo for `task:`) must not be silently ignored, leaving the real
    # task empty and the campaign a no-op that reports success.
    body = _valid_body() + "\ntasks: do the thing\n"
    with pytest.raises(campaign.CampaignError, match="unknown campaign field"):
        campaign.load_campaign(_write(tmp_path / "c.yml", body))


def test_an_unknown_policy_key_is_refused(tmp_path: Path) -> None:
    body = _valid_body(max_concurent=3)  # codespell:ignore
    with pytest.raises(campaign.CampaignError, match="unknown policy field"):
        campaign.load_campaign(_write(tmp_path / "c.yml", body))


def test_a_bad_selector_is_refused_at_load(tmp_path: Path) -> None:
    # A typo'd field must fail in the operator's shell, NOT deep inside a fan-out
    # that has already launched real agents.
    with pytest.raises(campaign.CampaignError, match="unknown field 'scafold'"):
        campaign.load_campaign(
            _write(tmp_path / "c.yml", _valid_body(select="scafold=none"))  # codespell:ignore
        )


def test_an_empty_selector_list_is_refused(tmp_path: Path) -> None:
    body = "version: 1\nname: r\ntask: t\nselect: []\n"
    with pytest.raises(campaign.CampaignError, match="select"):
        campaign.load_campaign(_write(tmp_path / "c.yml", body))


def test_an_unsupported_version_is_refused(tmp_path: Path) -> None:
    with pytest.raises(campaign.CampaignError, match="version"):
        campaign.load_campaign(
            _write(tmp_path / "c.yml", _valid_body().replace("version: 1", "version: 2"))
        )


@pytest.mark.parametrize(
    ("policy", "match"),
    [
        ({"max_concurrent": 0}, "at least 1"),
        ({"max_concurrent": "true"}, "positive integer"),
        ({"timeout": 0}, "greater than 0"),
        ({"timeout": "soon"}, "positive number"),
        ({"output": "yaml"}, "output"),
    ],
)
def test_bad_policy_values_are_refused(
    tmp_path: Path, policy: dict[str, object], match: str
) -> None:
    with pytest.raises(campaign.CampaignError, match=match):
        campaign.load_campaign(_write(tmp_path / "c.yml", _valid_body(**policy)))


def test_non_yaml_is_refused(tmp_path: Path) -> None:
    with pytest.raises(campaign.CampaignError, match="not valid YAML"):
        campaign.load_campaign(_write(tmp_path / "c.yml", "name: [unclosed\n"))


def test_a_missing_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(campaign.CampaignError, match="cannot read"):
        campaign.load_campaign(tmp_path / "nope.yml")


# --- Deriving what is outstanding (progress is not tracked) ---------------------


def _snap_fleet(fleet_dir: Path) -> list:
    fleet = discover(FleetConfig(roots=(fleet_dir,), include_plain_repos=True))
    return fleet_snapshots(fleet)


def test_outstanding_is_the_selector_minus_handled_runs(fleet_dir: Path) -> None:
    _plain_repo(fleet_dir, "alpha")
    _plain_repo(fleet_dir, "beta")
    camp = campaign.Campaign(name="r", select=("scaffold=none",), task="apply project-init")
    snapshots = _snap_fleet(fleet_dir)

    # No runs yet: both are outstanding.
    assert _names(campaign.outstanding(camp, snapshots, {})) == ["alpha", "beta"]

    # A PR-opened run for THIS task on alpha removes it from the outstanding set —
    # nothing was written to a campaign file to remember that; it is derived.
    handled = runs.AgentRun(
        id="x", project="alpha", task="apply project-init", state=runs.PR_OPENED
    )
    by_project = {"alpha": [handled]}
    assert _names(campaign.outstanding(camp, snapshots, by_project)) == ["beta"]


@pytest.mark.parametrize(
    ("state", "still_outstanding"),
    [
        (runs.PR_OPENED, False),
        (runs.RUNNING, False),
        (runs.QUEUED, False),
        (runs.NEEDS_HUMAN, False),
        (runs.FAILED, True),  # a failure is retried on re-run
        (runs.ABANDONED, True),  # so is an abandoned run
    ],
)
def test_which_run_states_count_as_handled(
    fleet_dir: Path, state: str, still_outstanding: bool
) -> None:
    _plain_repo(fleet_dir, "alpha")
    camp = campaign.Campaign(name="r", select=("scaffold=none",), task="apply project-init")
    run = runs.AgentRun(id="x", project="alpha", task="apply project-init", state=state)
    result = campaign.outstanding(camp, _snap_fleet(fleet_dir), {"alpha": [run]})
    assert (_names(result) == ["alpha"]) is still_outstanding


def test_a_run_for_a_different_task_does_not_count_as_handled(fleet_dir: Path) -> None:
    # Two campaigns can touch the same project; one's PR must not make the other
    # think its own work is done.
    _plain_repo(fleet_dir, "alpha")
    camp = campaign.Campaign(name="r", select=("scaffold=none",), task="apply project-init")
    other = runs.AgentRun(id="x", project="alpha", task="something else", state=runs.PR_OPENED)
    assert _names(campaign.outstanding(camp, _snap_fleet(fleet_dir), {"alpha": [other]})) == [
        "alpha"
    ]


def test_a_selector_matching_nothing_is_empty_not_error(fleet_dir: Path) -> None:
    make_project(fleet_dir, "alpha")  # scaffolded — does NOT match scaffold=none
    camp = campaign.Campaign(name="r", select=("scaffold=none",), task="t")
    assert campaign.outstanding(camp, _snap_fleet(fleet_dir), {}) == []


def _names(snapshots: list) -> list[str]:
    return sorted(s.descriptor.name for s in snapshots)


# --- Executing under the policy ------------------------------------------------


class _FakeFleet:
    """A controllable stand-in for launch/poll/stop/clock/sleep.

    Each launched run needs ``done_after`` polls to reach ``pr-opened`` (so runs
    stay in flight long enough to observe concurrency), and ``clock`` advances
    only when ``sleep`` is called (so deadlines are deterministic).
    """

    def __init__(self, *, done_after: int = 1, launch_terminal: bool = False) -> None:
        self.done_after = done_after
        self.launch_terminal = launch_terminal
        self.launched: list[str] = []
        self.stopped: list[str] = []
        self.inflight: set[str] = set()
        self.max_inflight = 0
        self._runs: dict[str, runs.AgentRun] = {}
        self._polls: dict[str, int] = {}
        self.t = 0.0

    def launch(self, descriptor: ProjectDescriptor, task: str) -> runs.AgentRun:
        self.launched.append(descriptor.name)
        state = runs.FAILED if self.launch_terminal else runs.RUNNING
        run = runs.AgentRun(
            id=f"r-{descriptor.name}", project=descriptor.name, task=task, state=state, pid=1
        )
        self._runs[run.id] = run
        if not self.launch_terminal:
            self.inflight.add(run.id)
            self.max_inflight = max(self.max_inflight, len(self.inflight))
        return run

    def poll(self, run_id: str) -> runs.AgentRun | None:
        self._polls[run_id] = self._polls.get(run_id, 0) + 1
        run = self._runs[run_id]
        if self._polls[run_id] >= self.done_after:
            run = replace(run, state=runs.PR_OPENED, pr_url=f"http://pr/{run_id}")
            self._runs[run_id] = run
            self.inflight.discard(run_id)
        return run

    def stop(self, run_id: str) -> runs.AgentRun | None:
        self.stopped.append(run_id)
        run = replace(self._runs[run_id], state=runs.ABANDONED)
        self._runs[run_id] = run
        self.inflight.discard(run_id)
        return run

    def clock(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds

    def seams(self, poll_interval: float = 2.0) -> campaign.Seams:
        return campaign.Seams(
            launch=self.launch,
            poll=self.poll,
            stop=self.stop,
            clock=self.clock,
            sleep=self.sleep,
            poll_interval=poll_interval,
        )


def test_execute_runs_every_descriptor_to_a_terminal_outcome() -> None:
    fake = _FakeFleet(done_after=1)
    camp = campaign.Campaign(name="r", select=("scaffold=none",), task="t")
    descriptors = [_descriptor(n) for n in ("a", "b", "c")]
    outcomes = campaign.execute(camp, descriptors, fake.seams())
    assert sorted(o.run.project for o in outcomes) == ["a", "b", "c"]
    assert all(o.run.state == runs.PR_OPENED for o in outcomes)


def test_execute_never_exceeds_max_concurrent() -> None:
    # THE guard. Three targets, ceiling of two: at no instant may three be live.
    fake = _FakeFleet(done_after=2)  # each stays in flight across one reap cycle
    camp = campaign.Campaign(
        name="r", select=("x=y",), task="t", policy=campaign.Policy(max_concurrent=2)
    )
    outcomes = campaign.execute(camp, [_descriptor(n) for n in ("a", "b", "c")], fake.seams())
    assert fake.max_inflight == 2  # never 3
    assert len(fake.launched) == 3  # yet all three ran
    assert all(o.run.state == runs.PR_OPENED for o in outcomes)


def test_execute_kills_a_run_that_outlives_its_timeout() -> None:
    # THE guard. A run that never finishes is stopped, not left burning tokens.
    fake = _FakeFleet(done_after=999)  # never completes on its own
    camp = campaign.Campaign(
        name="r", select=("x=y",), task="t", policy=campaign.Policy(timeout=1.0)
    )
    outcomes = campaign.execute(camp, [_descriptor("a")], fake.seams(poll_interval=2.0))
    assert fake.stopped == ["r-a"]
    assert len(outcomes) == 1
    assert outcomes[0].timed_out is True


def test_execute_records_a_launch_that_failed_to_start() -> None:
    fake = _FakeFleet(launch_terminal=True)
    camp = campaign.Campaign(name="r", select=("x=y",), task="t")
    outcomes = campaign.execute(camp, [_descriptor("a")], fake.seams())
    assert [o.run.state for o in outcomes] == [runs.FAILED]
    assert fake.stopped == []  # nothing to supervise, nothing to kill


def test_execute_treats_a_vanished_record_as_failed() -> None:
    # If the run record disappears mid-flight we cannot confirm success, and must
    # not poll a ghost forever. Pessimistic: failed.
    fake = _FakeFleet(done_after=999)
    fake.poll = lambda _run_id: None  # type: ignore[method-assign, assignment]
    camp = campaign.Campaign(name="r", select=("x=y",), task="t")
    outcomes = campaign.execute(camp, [_descriptor("a")], fake.seams())
    assert [o.run.state for o in outcomes] == [runs.FAILED]


# --- The CLI: canary by default, --apply to fan out ----------------------------


def _campaign_file(fleet_dir: Path, **policy: object) -> Path:
    body = _valid_body(**policy) if policy else _valid_body(include_plain_repos="true")
    return _write(fleet_dir / "rollout.yml", body)


def test_cli_default_is_a_canary_of_one(
    fleet_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    # THE guard. Three projects match, but the default launches exactly ONE.
    from projects_orchestrator.__main__ import main

    for name in ("alpha", "beta", "gamma"):
        _plain_repo(fleet_dir, name)
    seen: list[list[str]] = []

    def fake_execute(camp, descriptors, _seams):
        seen.append([d.name for d in descriptors])
        return [
            campaign.Outcome(
                run=runs.AgentRun(
                    id=f"r-{d.name}",
                    project=d.name,
                    task=camp.task,
                    state=runs.PR_OPENED,
                    pr_url="http://pr",
                )
            )
            for d in descriptors
        ]

    monkeypatch.setattr(campaign, "execute", fake_execute)
    monkeypatch.setattr(campaign, "default_seams", lambda: None)
    cfile = _campaign_file(fleet_dir, include_plain_repos="true")

    assert main(["campaign", str(cfile), "--root", str(fleet_dir)]) == 0
    assert len(seen) == 1
    assert len(seen[0]) == 1  # one project, not three
    out = capsys.readouterr().out
    assert "canary complete" in out
    assert "2 project(s) still outstanding" in out


def test_cli_apply_fans_out_to_all_outstanding(
    fleet_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from projects_orchestrator.__main__ import main

    for name in ("alpha", "beta", "gamma"):
        _plain_repo(fleet_dir, name)
    seen: list[list[str]] = []
    monkeypatch.setattr(
        campaign,
        "execute",
        lambda camp, descriptors, _s: (
            seen.append([d.name for d in descriptors])
            or [
                campaign.Outcome(
                    run=runs.AgentRun(id="r", project=d.name, task=camp.task, state=runs.PR_OPENED)
                )
                for d in descriptors
            ]
        ),
    )
    monkeypatch.setattr(campaign, "default_seams", lambda: None)
    cfile = _campaign_file(fleet_dir, include_plain_repos="true")

    assert main(["campaign", str(cfile), "--apply", "--root", str(fleet_dir)]) == 0
    assert sorted(seen[0]) == ["alpha", "beta", "gamma"]


def test_cli_dry_run_launches_nothing(
    fleet_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    from projects_orchestrator.__main__ import main

    _plain_repo(fleet_dir, "alpha")
    launched = []
    monkeypatch.setattr(campaign, "execute", lambda *a: launched.append(a) or [])
    cfile = _campaign_file(fleet_dir, include_plain_repos="true")

    assert main(["campaign", str(cfile), "--dry-run", "--root", str(fleet_dir)]) == 0
    assert launched == []
    out = capsys.readouterr().out
    assert "outstanding" in out
    assert "alpha" in out


def test_cli_zero_targets_reports_done_not_error(
    fleet_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    # THE criterion. A selector matching nothing is success, exit 0.
    from projects_orchestrator.__main__ import main

    make_project(fleet_dir, "alpha")  # scaffolded — nothing matches scaffold=none
    called = []
    monkeypatch.setattr(campaign, "execute", lambda *a: called.append(a) or [])
    cfile = _campaign_file(fleet_dir, include_plain_repos="true")

    assert main(["campaign", str(cfile), "--apply", "--root", str(fleet_dir)]) == 0
    assert called == []  # nothing launched
    assert "is done" in capsys.readouterr().out


def test_cli_a_broken_file_exits_two_and_launches_nothing(
    fleet_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    from projects_orchestrator.__main__ import main

    called = []
    monkeypatch.setattr(campaign, "execute", lambda *a: called.append(a) or [])
    bad = _write(
        tmp_path / "bad.yml", "version: 1\nname: r\nselect: scafold=none\ntask: t\n"
    )  # codespell:ignore

    assert main(["campaign", str(bad), "--root", str(fleet_dir)]) == 2
    assert called == []
    assert "campaign:" in capsys.readouterr().err


def test_cli_a_handled_project_is_skipped_on_rerun(
    fleet_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Derived progress, end to end: a project with a PR-opened run for this task
    # drops out, so --apply only launches the untouched one.
    from projects_orchestrator.__main__ import main

    _plain_repo(fleet_dir, "alpha")
    _plain_repo(fleet_dir, "beta")
    runs.save(
        runs.AgentRun(
            id="done",
            project="alpha",
            task="apply project-init",
            state=runs.PR_OPENED,
            started_at="2026-01-01T00:00:00+00:00",
        )
    )
    seen: list[list[str]] = []
    monkeypatch.setattr(
        campaign,
        "execute",
        lambda camp, descriptors, _s: (
            seen.append([d.name for d in descriptors])
            or [
                campaign.Outcome(
                    run=runs.AgentRun(id="r", project=d.name, task=camp.task, state=runs.PR_OPENED)
                )
                for d in descriptors
            ]
        ),
    )
    monkeypatch.setattr(campaign, "default_seams", lambda: None)
    cfile = _campaign_file(fleet_dir, include_plain_repos="true")

    assert main(["campaign", str(cfile), "--apply", "--root", str(fleet_dir)]) == 0
    assert seen[0] == ["beta"]  # alpha already handled


def test_cli_json_output(fleet_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    import json

    from projects_orchestrator.__main__ import main

    _plain_repo(fleet_dir, "alpha")
    monkeypatch.setattr(
        campaign,
        "execute",
        lambda camp, descriptors, _s: [
            campaign.Outcome(
                run=runs.AgentRun(
                    id="r",
                    project=d.name,
                    task=camp.task,
                    state=runs.PR_OPENED,
                    pr_url="http://pr/1",
                )
            )
            for d in descriptors
        ],
    )
    monkeypatch.setattr(campaign, "default_seams", lambda: None)
    cfile = _campaign_file(fleet_dir, include_plain_repos="true")

    assert main(["campaign", str(cfile), "--json", "--root", str(fleet_dir)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["name"] == "rollout"
    assert payload["outcomes"][0]["pr_url"] == "http://pr/1"


def test_cli_a_failing_canary_exits_one(fleet_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from projects_orchestrator.__main__ import main

    _plain_repo(fleet_dir, "alpha")
    monkeypatch.setattr(
        campaign,
        "execute",
        lambda camp, descriptors, _s: [
            campaign.Outcome(
                run=runs.AgentRun(id="r", project=d.name, task=camp.task, state=runs.FAILED)
            )
            for d in descriptors
        ],
    )
    monkeypatch.setattr(campaign, "default_seams", lambda: None)
    cfile = _campaign_file(fleet_dir, include_plain_repos="true")

    assert main(["campaign", str(cfile), "--root", str(fleet_dir)]) == 1
