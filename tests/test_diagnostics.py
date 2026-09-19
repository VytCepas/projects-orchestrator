"""The diagnostic trail (#185): degraded paths leave a reason, the CLI boundary never
leaks a traceback, and neither changes what a default run prints.
"""

from __future__ import annotations

import ast
import io
import json
import logging
import tokenize
from pathlib import Path

import pytest
from conftest import git_init, make_project

import projects_orchestrator.__main__ as cli
from projects_orchestrator.__main__ import EXIT_INTERNAL_ERROR, VERBOSE_ENV, main
from projects_orchestrator.adapters.generic import infer_descriptor
from projects_orchestrator.descriptor import load_descriptor, parse_scaffold_version
from projects_orchestrator.upgrade import build_row, unknown_reason

SRC = Path(__file__).resolve().parent.parent / "src" / "projects_orchestrator"
MARKER = "expected:"


@pytest.fixture(autouse=True)
def _no_ambient_verbose(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's own shell toggle must not decide what these tests see."""
    monkeypatch.delenv(VERBOSE_ENV, raising=False)


# --- every swallowed exception leaves a trail ------------------------------------------


def _comment_lines(source: str) -> dict[int, str]:
    """Line number -> comment text. Tokens, not a grep, so a string never counts."""
    tokens = tokenize.generate_tokens(io.StringIO(source).readline)
    return {t.start[0]: t.string for t in tokens if t.type == tokenize.COMMENT}


def _is_suppress(node: ast.expr) -> bool:
    func = node.func if isinstance(node, ast.Call) else None
    if isinstance(func, ast.Attribute):
        return func.attr == "suppress"
    return isinstance(func, ast.Name) and func.id == "suppress"


def _marked(comments: dict[int, str], first: int, last: int) -> bool:
    return any(MARKER in comments.get(line, "") for line in range(first, last + 1))


# A generator expression runs lazily, so like a nested def it may never run at all
# (Codex on #269). A list/set/dict comprehension runs NOW and stays in scope.
_NEW_SCOPE = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef, ast.GeneratorExp)


def _same_scope(statements: list[ast.stmt], stop: tuple[type, ...] = ()) -> list[ast.AST]:
    """Every node under ``statements`` that runs in the handler's own scope.

    A nested def, lambda, class or generator expression is code that may never
    run, so a ``raise`` or a read inside one says nothing about what the handler
    does with the exception it caught (Codex on #269). ``stop`` names further
    node types not to descend into.
    """
    fence = _NEW_SCOPE + stop
    found: list[ast.AST] = []
    stack: list[ast.AST] = [s for s in statements if not isinstance(s, fence)]
    while stack:
        node = stack.pop()
        found.append(node)
        stack.extend(c for c in ast.iter_child_nodes(node) if not isinstance(c, fence))
    return found


# A raise inside a nested `try` either re-raises the INNER exception (a bare
# `raise` in an inner handler) or may be caught before it escapes, so it is no
# evidence about the outer one (Codex on #269). `raise exc` / `raise X from exc`
# still count, as reads of the outer name.
_NESTED_TRY = (ast.Try, ast.TryStar)


def silent_sites(source: str) -> list[int]:
    """Lines where an exception is discarded with no trail and no stated reason.

    A handler leaves a trail when, in its own scope, it re-raises or READS the
    exception it bound — logging it, or carrying it into a warning or a message.
    Rebinding the name (``exc = None``) is not a read, and neither is anything in
    a nested function. Otherwise the handler must say, in an ``# expected:``
    comment between its header and its first statement, why the exception IS the
    answer rather than a fault. ``contextlib.suppress`` is the same swallow in
    another spelling, so it answers to the same rule.
    """
    tree = ast.parse(source)
    comments = _comment_lines(source)
    silent: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            raises = any(isinstance(n, ast.Raise) for n in _same_scope(node.body, _NESTED_TRY))
            reads = node.name is not None and any(
                isinstance(n, ast.Name) and n.id == node.name and isinstance(n.ctx, ast.Load)
                for n in _same_scope(node.body)
            )
            if not (raises or reads or _marked(comments, node.lineno, node.body[0].lineno)):
                silent.append(node.lineno)
        elif isinstance(node, ast.With) and any(_is_suppress(i.context_expr) for i in node.items):
            if not _marked(comments, node.lineno - 1, node.body[0].lineno):
                silent.append(node.lineno)
    return silent


def test_every_swallowed_exception_in_the_package_leaves_a_trail() -> None:
    files = sorted(SRC.rglob("*.py"))
    assert len(files) > 40, "the walk found too few modules to mean anything"
    offenders = [
        f"{path.relative_to(SRC)}:{line}"
        for path in files
        for line in silent_sites(path.read_text(encoding="utf-8"))
    ]
    assert offenders == [], (
        "an exception is swallowed with no trail: log it with `_log.debug(..., exc)`, "
        f"or state why it is the answer in an `# {MARKER}` comment — {offenders}"
    )


SILENT = "try:\n    f()\nexcept OSError:\n    return None\n"


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (SILENT, [3]),
        ("try:\n    f()\nexcept OSError as exc:\n    return None\n", [3]),
        ("try:\n    f()\nexcept OSError as exc:\n    _log.debug('x: %r', exc)\n", []),
        ("try:\n    f()\nexcept OSError:\n    raise\n", []),
        ("try:\n    f()\nexcept OSError:\n    # expected: absent is the answer\n    pass\n", []),
        ("try:\n    f()\nexcept OSError:  # expected: absent is the answer\n    pass\n", []),
        # A marker anywhere else is not a marker for THIS handler.
        ("# expected: nope\n\ntry:\n    f()\nexcept OSError:\n    pass\n", [5]),
        # A string that happens to contain the marker is not a comment.
        ("try:\n    f()\nexcept OSError:\n    x = '# expected: no'\n", [3]),
        # Rebinding the name is not reading it.
        ("try:\n    f()\nexcept OSError as exc:\n    exc = None\n", [3]),
        # A raise or a read inside a nested scope may never run.
        ("try:\n    f()\nexcept OSError:\n    def later():\n        raise\n", [3]),
        ("try:\n    f()\nexcept OSError as exc:\n    cb = lambda: log(exc)\n", [3]),
        ("try:\n    f()\nexcept OSError as exc:\n    g = (str(exc) for _ in r)\n", [3]),
        # An inner handler's bare raise re-raises the INNER exception, not this one.
        (
            "try:\n    f()\nexcept OSError:\n    try:\n        g()\n"
            "    except ValueError:\n        raise\n    x = 1\n",
            [3],
        ),
        # Re-raising the outer exception by name from inside an inner handler counts.
        (
            "try:\n    f()\nexcept OSError as exc:\n    try:\n        g()\n"
            "    except ValueError:\n        raise exc from None\n",
            [],
        ),
        # A comprehension is the handler's own code, run now.
        ("try:\n    f()\nexcept OSError as exc:\n    w = [str(exc) for _ in r]\n", []),
        ("with contextlib.suppress(OSError):\n    f()\n", [1]),
        ("with suppress(OSError):  # expected: already gone\n    f()\n", []),
        ("# expected: already gone\nwith contextlib.suppress(OSError):\n    f()\n", []),
    ],
)
def test_the_trail_rule_tells_a_silent_swallow_from_an_explained_one(
    source: str, expected: list[int]
) -> None:
    assert silent_sites(source) == expected


# --- the CLI boundary ------------------------------------------------------------------


def _explode(_args: object) -> int:
    raise KeyError("boom")


def test_an_escaped_exception_exits_nonzero_with_one_clean_line(
    fleet_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "_cmd_projects", _explode)
    code = main(["projects", "--root", str(fleet_dir)])
    err = capsys.readouterr().err
    assert code == EXIT_INTERNAL_ERROR
    assert "Traceback" not in err
    assert err.strip().splitlines() == [
        "projects-orchestrator: internal error in 'projects': KeyError: 'boom' — this is a bug; "
        "rerun with --verbose for the traceback"
    ]


@pytest.mark.parametrize(
    "argv",
    [["--verbose", "projects"], ["projects", "--verbose"]],
    ids=["flag-before-command", "flag-after-command"],
)
def test_verbose_adds_the_traceback_and_keeps_the_exit_code(
    argv: list[str],
    fleet_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "_cmd_projects", _explode)
    code = main([*argv, "--root", str(fleet_dir)])
    err = capsys.readouterr().err
    assert code == EXIT_INTERNAL_ERROR
    assert "Traceback (most recent call last)" in err
    assert "_explode" in err
    assert err.strip().splitlines()[-1].endswith("KeyError: 'boom' — this is a bug")


def test_the_env_toggle_is_the_flag(
    fleet_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "_cmd_projects", _explode)
    monkeypatch.setenv(VERBOSE_ENV, "1")
    main(["projects", "--root", str(fleet_dir)])
    assert "Traceback (most recent call last)" in capsys.readouterr().err


class _BrokenStdout(io.StringIO):
    """Stdout whose reader went away: every flush raises, as a real closed pipe does."""

    def flush(self) -> None:
        raise BrokenPipeError(32, "Broken pipe")


def _reader_went_away(_args: object) -> int:
    raise BrokenPipeError(32, "Broken pipe")


def test_a_closed_stdout_exits_quietly_and_redirects_only_at_exit(
    fleet_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    registered: list[object] = []
    monkeypatch.setattr(cli, "_cmd_projects", _reader_went_away)
    monkeypatch.setattr(cli.sys, "stdout", _BrokenStdout())
    monkeypatch.setattr(cli.atexit, "register", registered.append)
    monkeypatch.setattr(cli, "_silence_stdout", _not_before_exit)
    assert main(["projects", "--root", str(fleet_dir)]) == 1
    assert (capsys.readouterr().err, registered) == ("", [_not_before_exit])


def _not_before_exit() -> None:
    raise AssertionError("fd 1 was redirected while the caller's process still runs")


def test_a_reader_that_leaves_after_the_last_write_is_caught_in_main_too(
    fleet_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The usual `… | head` case: every write fits the buffer and the pipe breaks
    # only at the flush. Left to the interpreter's exit flush, that is
    # "Exception ignored" and exit 120, after main() can classify anything.
    make_project(fleet_dir, "alpha")
    registered: list[object] = []
    monkeypatch.setattr(cli.sys, "stdout", _BrokenStdout())
    monkeypatch.setattr(cli.atexit, "register", registered.append)
    assert main(["projects", "--root", str(fleet_dir)]) == 1
    assert (capsys.readouterr().err, len(registered)) == ("", 1)


def test_another_pipe_breaking_is_a_real_fault(
    fleet_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # stdout flushes fine, so the broken pipe was a subprocess's or a socket's:
    # waving it through as `… | head` would hide a bug behind a quiet exit 1.
    monkeypatch.setattr(cli, "_cmd_projects", _reader_went_away)
    assert main(["projects", "--root", str(fleet_dir)]) == EXIT_INTERNAL_ERROR
    assert "internal error in 'projects': BrokenPipeError" in capsys.readouterr().err


def test_a_usage_error_still_exits_2_through_argparse() -> None:
    with pytest.raises(SystemExit) as raised:
        main(["no-such-command"])
    assert raised.value.code == 2


# --- the trail itself ------------------------------------------------------------------


def _plain_repo_fleet(fleet_dir: Path) -> Path:
    repo = fleet_dir / "plain"
    repo.mkdir(parents=True)
    git_init(repo)
    fleet = fleet_dir.parent / "fleet.yaml"
    fleet.write_text(f"roots: [{fleet_dir}]\ninclude_plain_repos: true\n", encoding="utf-8")
    return fleet


def test_a_degraded_cell_names_its_cause_only_when_asked(
    fleet_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fleet = _plain_repo_fleet(fleet_dir)
    main(["snapshot", "--fleet", str(fleet)])
    quiet = capsys.readouterr()
    main(["snapshot", "--fleet", str(fleet), "--verbose"])
    loud = capsys.readouterr()
    assert "debug:" not in quiet.err
    assert loud.out == quiet.out, "the trail must never change what the command prints"
    assert "debug: projects_orchestrator.drift: cannot read the scaffold manifest" in loud.err
    assert "FileNotFoundError" in loud.err


def test_a_verbose_run_does_not_leave_the_trail_on_for_the_next_one(
    fleet_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fleet = _plain_repo_fleet(fleet_dir)
    main(["snapshot", "--fleet", str(fleet), "--verbose"])
    main(["snapshot", "--fleet", str(fleet), "--verbose"])
    twice = capsys.readouterr().err
    main(["snapshot", "--fleet", str(fleet)])
    after = capsys.readouterr().err
    once = twice[: len(twice) // 2]
    assert twice == once * 2, "a repeated main() must not stack a second handler"
    assert "debug:" not in after


class _Collect(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def test_a_host_root_logger_at_debug_neither_leaks_nor_doubles_the_trail(
    fleet_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fleet = _plain_repo_fleet(fleet_dir)
    root, collect = logging.getLogger(), _Collect()
    saved_level = root.level
    root.addHandler(collect)
    root.setLevel(logging.DEBUG)
    try:
        main(["snapshot", "--fleet", str(fleet)])
        quiet = capsys.readouterr().err
        main(["snapshot", "--fleet", str(fleet), "--verbose"])
        loud = capsys.readouterr().err
    finally:
        root.removeHandler(collect)
        root.setLevel(saved_level)
    ours = [r for r in collect.records if r.name.startswith("projects_orchestrator")]
    assert (ours, "debug:" in quiet, "debug:" in loud) == ([], False, True)


def test_a_handler_the_host_attached_to_the_package_logger_is_set_aside(
    fleet_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fleet = _plain_repo_fleet(fleet_dir)
    logger, collect = logging.getLogger("projects_orchestrator"), _Collect()
    saved_level = logger.level
    logger.addHandler(collect)
    logger.setLevel(logging.DEBUG)
    try:
        main(["snapshot", "--fleet", str(fleet)])
        main(["snapshot", "--fleet", str(fleet), "--verbose"])
        attached_after = collect in logger.handlers
    finally:
        logger.removeHandler(collect)
        logger.setLevel(saved_level)
    loud = capsys.readouterr().err
    assert (collect.records, attached_after, "debug:" in loud) == ([], True, True)


def test_a_handler_the_host_attached_to_a_module_logger_is_set_aside_too(
    fleet_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A module logger's handlers run BEFORE the record reaches the package
    # logger, so isolating only the package would still leak (Codex on #269).
    fleet = _plain_repo_fleet(fleet_dir)
    drift_log, collect = logging.getLogger("projects_orchestrator.drift"), _Collect()
    before = (drift_log.level, drift_log.propagate)
    drift_log.addHandler(collect)
    drift_log.setLevel(logging.WARNING)
    drift_log.propagate = False
    try:
        main(["snapshot", "--fleet", str(fleet)])
        main(["snapshot", "--fleet", str(fleet), "--verbose"])
        after = (collect in drift_log.handlers, drift_log.level, drift_log.propagate)
    finally:
        drift_log.removeHandler(collect)
        drift_log.setLevel(before[0])
        drift_log.propagate = before[1]
    loud = capsys.readouterr().err
    # The host's WARNING level and propagate=False must not hide drift's trail
    # from --verbose either: during the run the package owns the whole tree.
    assert "debug: projects_orchestrator.drift:" in loud
    assert (collect.records, after) == ([], (True, logging.WARNING, False))


def test_main_hands_the_package_logger_back_as_it_found_it(
    fleet_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A state main() would never set itself, so a missing restore cannot pass by
    # coincidence with whatever an earlier test left behind.
    logger = logging.getLogger("projects_orchestrator")
    monkeypatch.setattr(logger, "level", logging.INFO)
    monkeypatch.setattr(logger, "propagate", True)
    main(["snapshot", "--fleet", str(_plain_repo_fleet(fleet_dir)), "--verbose"])
    assert (logger.level, logger.propagate, logger.handlers) == (logging.INFO, True, [])


# --- upgrade-plan carries the reason into the row --------------------------------------


def test_a_plain_repo_is_unknown_because_it_has_no_descriptor(fleet_dir: Path) -> None:
    repo = fleet_dir / "plain"
    repo.mkdir(parents=True)
    git_init(repo)
    descriptor = infer_descriptor(repo)
    assert descriptor is not None
    row = build_row(descriptor, (1, 2, 2))
    assert (row.status, row.reason) == ("unknown", "no project-init descriptor")


def test_a_descriptor_without_a_version_says_so(fleet_dir: Path) -> None:
    config = "project:\n  name: bare\nlanguage: python\n"
    descriptor = load_descriptor(make_project(fleet_dir, "bare", config_text=config))
    assert descriptor is not None
    assert build_row(descriptor, (1, 2, 2)).reason == "descriptor records no project_init_version"


def test_an_unparseable_version_is_quoted(fleet_dir: Path) -> None:
    descriptor = load_descriptor(make_project(fleet_dir, "alpha"))
    assert descriptor is not None
    odd = descriptor.__class__(**{**descriptor.__dict__, "project_init_version": "1.2"})
    assert unknown_reason(odd, parse_scaffold_version("1.2"), (1, 2, 2)) == (
        "project_init_version '1.2' is not a comparable version"
    )


def test_an_offline_upstream_is_the_reason_when_the_row_is_otherwise_fine(
    fleet_dir: Path,
) -> None:
    descriptor = load_descriptor(make_project(fleet_dir, "alpha"))
    assert descriptor is not None
    assert build_row(descriptor, None).reason.startswith("latest project-init release unknown")


@pytest.mark.parametrize("latest", [(0, 5, 2), (0, 6, 0)], ids=["ok", "outdated"])
def test_a_classified_row_carries_no_reason(fleet_dir: Path, latest: tuple[int, ...]) -> None:
    descriptor = load_descriptor(make_project(fleet_dir, "alpha"))
    assert descriptor is not None
    assert build_row(descriptor, latest).reason == ""


def test_upgrade_plan_prints_the_reason_after_the_unchanged_row(
    fleet_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    make_project(fleet_dir, "alpha")
    monkeypatch.setattr(cli, "latest_upstream_version", lambda _cwd: None)
    monkeypatch.setattr(cli, "latest_plugin_version", lambda _cwd: None)
    main(["upgrade-plan", "--root", str(fleet_dir)])
    line = capsys.readouterr().out.strip()
    assert line.startswith("alpha: unknown (scaffold 0.5.2, drift ")
    assert line.endswith(
        " — latest project-init release unknown (gh unavailable, unauthenticated or offline)"
    )
    main(["upgrade-plan", "--root", str(fleet_dir), "--json"])
    assert json.loads(capsys.readouterr().out)[0]["reason"].startswith("latest project-init")
