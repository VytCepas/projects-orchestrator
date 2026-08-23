"""Prod-safety guard (PI-168, ADR-012): destructive commands, and secret reads.

PreToolUse hook on Bash. Deterministic deny-table — no LLM, no network.

TWO CLASSES, one hook. Destruction is the original one. Reading a secret file
is the second (PI-893): it destroys nothing, but the contents land in the model
transcript, where they are re-sent on every subsequent turn and outlive the
session. It lives here rather than in a sibling hook because it needs the
identical machinery — the config walk with its symlink refusal, ``safety.allow``,
the ask/deny-by-mode posture, fail-open — and a second copy of security-critical
code is the drift this repo keeps finding. Extending this hook also reaches the
non-Claude surfaces through ``agent_guard_adapter.py`` for free.
Destructive operations that bypass the git/CI boundary (cloud deletes,
DROP DATABASE, terraform destroy, …) get:

- ``ask``   in interactive sessions — a human confirms or rejects;
- ``block`` in fully autonomous sessions (``bypassPermissions``) — there is
  no human to ask, so the command is blocked outright.

Escape hatch: ``safety.allow`` in ``.agents/config.yaml`` holds a JSON list
of regex patterns; a command matching any of them is never flagged. Use it
for known-safe contexts (e.g. a dev-cluster kubectl context).

This is a guardrail, not the security boundary (ADR-007/ADR-012): a
sufficiently creative command can evade a deny-list. The guarantee comes
from credential separation — agent sessions must never hold production
credentials (see .agents/docs/guides/secrets.md).

Fail-open by design: any internal error lets the command proceed.
"""

from __future__ import annotations

import contextlib
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
# `--replace` / `--replace=true` but never `--replace=false` (PI-906): the
# false spelling is the default written out, and flagging it would nag on a
# command that is explicitly asking NOT to overwrite.
_REPLACE = r"--replace(?:=true)?(?![\w=-])"
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
    # GCS recursive removal, both spellings (PI-906). The `gcloud delete` rule
    # above LOOKS like it covers this and does not: it keys on the token
    # `delete`, which neither `gsutil rm -r` nor the modern `gcloud storage rm
    # -r` carries — so the highest-blast-radius GCS operation walked past a
    # gcloud rule. (`gcloud storage buckets delete` does carry the token and is
    # already caught above.) Shaped like the `aws s3 rm --recursive` rule: a
    # single-object `rm` is not flagged, only the recursive form, which is what
    # empties a bucket. `-\w*[rR]\w*` covers gsutil's `-r` and `-R`; the flag may
    # sit before or after the URL, hence _SEG on both sides.
    (
        re.compile(rf"\bgsutil\b{_SEG}\brm\b{_SEG}\s(?:-\w*[rR]\w*|--recursive)\b"),
        "gsutil recursive bucket removal",
    ),
    (
        re.compile(rf"\bgcloud\b{_SEG}\bstorage\s+rm\b{_SEG}\s(?:-\w*[rR]\w*|--recursive)\b"),
        "gcloud storage recursive bucket removal",
    ),
    # BigQuery dataset/table destruction (PI-906). Flag-specific rather than
    # _SEG for `plan -destroy`'s reason: `bq query` takes SQL as an argument, so
    # an _SEG reaching into the statement would fire on any query whose text
    # happens to contain `rm`. Only global flags (`--project_id=…`,
    # `--location=…`) may sit between `bq` and the verb. `--help`/`-h` is a
    # read-only lookup of the very command being guarded — never flag it.
    (
        re.compile(r"\bbq\s+(?:-\S+\s+)*rm\b(?!\s+(?:--help|-h)\b)"),
        "bq rm (BigQuery dataset/table removal)",
    ),
    # `bq truncate` is a bq SUBCOMMAND, not SQL, so the `truncate table` rule
    # below never sees it — there is no `table` token in `bq truncate ds.t`
    # (PI-906). Same shape and same help exemption as `bq rm`.
    (
        re.compile(r"\bbq\s+(?:-\S+\s+)*truncate\b(?!\s+(?:--help|-h)\b)"),
        "bq truncate (BigQuery table truncation)",
    ),
    # `--replace` overwrites the destination: the prior contents are gone as
    # surely as after a DROP, while the verb reads like an ordinary write
    # (PI-906). `--replace=false` is the DEFAULT spelled out and must not be
    # flagged, which a bare `--replace\b` would do — `\b` matches before the
    # `=`. Hence `_REPLACE`: the bare flag or `=true`, never `=false`.
    (
        re.compile(rf"\bbq\s+(?:-\S+\s+)*load\b{_SEG}\s{_REPLACE}"),
        "bq load --replace (destination overwrite)",
    ),
    # A destination_table WITHOUT --replace appends, which is not destruction;
    # both must be present, and the flags appear in either order.
    (
        re.compile(
            rf"\bbq\s+(?:-\S+\s+)*query\b"
            rf"(?={_SEG}\s--destination_table[=\s])"
            rf"(?={_SEG}\s{_REPLACE})"
        ),
        "bq query --replace (destination table overwrite)",
    ),
    (re.compile(rf"\baz\b{_SEG}\bdelete\b"), "az delete"),
    # dbt `--full-refresh` drops and rebuilds incremental models: data
    # destruction wearing a build verb (PI-906). Two lookaheads because the
    # flags appear in either order, the same idiom the `rm -r -f` rule below
    # uses. DELIBERATE LIMIT: a production TARGET must be named explicitly, so
    # routine `dbt run --full-refresh --target dev` stays unflagged — a guard
    # that nags on ordinary dev work gets switched off. The cost is that a
    # bare `--full-refresh` against a profile whose DEFAULT target is prod is
    # not reached; naming the target is the supported way to be protected.
    #
    # THE TARGET VALUE IS QUOTED AS OFTEN AS NOT, and the first cut required
    # `prod` immediately after whitespace — so `--target "prod"` and
    # `--target="prod"` ran the destructive refresh straight through a rule
    # written to stop it (PR #915 review, P1). The shell strips the quotes
    # before dbt sees them; this guard reads the RAW command string, so it has
    # to tolerate what the shell would remove.
    #
    # AND THE VALUE MUST END. `\s*prod` also matched `--target prod-dev`,
    # flagging a dev target because its name starts with the same four letters
    # (PR #915 review). `(?![\w-])` is the boundary — a plain `\b` does NOT
    # help here, because `-` is a non-word character and `prod\b` matches
    # happily inside `prod-dev`.
    (
        re.compile(
            r"\bdbt\b"
            rf"(?={_SEG}\s--full-refresh\b)"
            rf"(?={_SEG}\s(?:--target[=\s]|-t\s)\s*[\"']?(?:prod|production)(?![\w-]))"
        ),
        "dbt --full-refresh against a production target",
    ),
    # A dbt run/build/seed/snapshot against a PRODUCTION target rewrites prod
    # relations whether or not `--full-refresh` is passed: a `table`
    # materialisation is a drop-and-recreate every time, and `run-operation`
    # executes an arbitrary macro (PI-906). The `--full-refresh` rule above
    # stays first so its more specific label keeps firing for that case.
    #
    # JUDGMENT CALL, stated so it can be overruled: this flags the ordinary
    # production deploy command, `dbt build --target prod`. It is `ask`, not
    # deny, and the posture is that an agent shell reaching prod should confirm
    # once — a deploy pipeline does not run through this hook, and a human who
    # deploys by hand all day has `safety.allow`. Read-only verbs (test,
    # compile, parse, docs, ls, debug, deps, show, source) are deliberately
    # absent from the verb list.
    (
        re.compile(
            r"\bdbt\b"
            rf"(?={_SEG}\s(?:build|run|run-operation|seed|snapshot)\b)"
            rf"(?={_SEG}\s(?:--target[=\s]|-t\s)\s*[\"']?(?:prod|production)(?![\w-]))"
        ),
        "dbt write against a production target",
    ),
    # IAM mutation on shared identities (PI-906). Granting access is not
    # obviously "destructive" and so was never modelled, but where an identity
    # is shared it changes other people's reach without their knowledge, and it
    # is the least reversible thing in this table. Grants are narrowed to the
    # roles that hand over the estate (owner/editor/any *Admin) so that routine
    # `roles/bigquery.dataViewer` grants stay unflagged; removals are flagged
    # unconditionally, because taking access away is destruction by another
    # name. `get-iam-policy` is read-only and matches neither.
    #
    # Same quoting problem as the dbt rule above, and the same severity (PR #915
    # review, P1): `--role="roles/owner"` passes gcloud exactly the argument
    # `roles/owner`, but the raw command carries a quote between `--role=` and
    # `roles/`, so an unquoted-only pattern let the estate handover through in
    # autonomous mode — where the verdict is a hard deny, not a prompt.
    (
        re.compile(
            rf"\bgcloud\b{_SEG}\badd-iam-policy-binding\b"
            rf"{_SEG}--role[=\s]\s*[\"']?roles/(?:owner|editor|\S*[Aa]dmin)"
        ),
        "gcloud IAM grant of owner/editor/admin",
    ),
    (
        re.compile(rf"\bgcloud\b{_SEG}\bremove-iam-policy-binding\b"),
        "gcloud IAM binding removal",
    ),
    # `set-iam-policy` REPLACES the whole policy from a file, so it revokes
    # every binding the file omits — the widest access change in the set, and
    # the one that looks least like one (PI-906). Flagged unconditionally: the
    # roles are in the file, which this guard does not read.
    (
        re.compile(rf"\bgcloud\b{_SEG}\bset-iam-policy\b"),
        "gcloud IAM policy replacement",
    ),
    # A service-account key is a long-lived credential that outlives the
    # session, cannot be rotated by revoking a login, and is exactly what the
    # credential-separation boundary (ADR-012) exists to keep out of an agent
    # shell. `keys list` / `keys describe` are reads and match neither.
    (
        re.compile(rf"\bgcloud\b{_SEG}\biam\s+service-accounts\s+keys\s+create\b"),
        "gcloud service-account key creation",
    ),
    # Bucket-level access. `iam ch` edits bindings, `iam set` replaces the
    # policy wholesale; `iam get` is a read and is not flagged.
    (
        re.compile(rf"\bgsutil\b{_SEG}\biam\s+(?:ch|set)\b"),
        "gsutil bucket IAM mutation",
    ),
    # The same three verbs exist on `bq` for datasets and tables, and none of
    # the gcloud rules above reach them — different CLI, identical effect.
    (
        re.compile(
            r"\bbq\s+(?:-\S+\s+)*"
            r"(?:set-iam-policy|add-iam-policy-binding|remove-iam-policy-binding)\b"
        ),
        "bq IAM policy mutation",
    ),
    # `bq update --source <file>` replaces a dataset's ACL (or a table's
    # schema) from a file — the pre-IAM spelling of set-iam-policy, still
    # supported and still a wholesale replacement.
    (
        re.compile(rf"\bbq\s+(?:-\S+\s+)*update\b{_SEG}\s--source[=\s]"),
        "bq update --source (dataset ACL/schema replacement)",
    ),
    (re.compile(r"\bdrop\s+(table|database|schema)\b", re.IGNORECASE), "SQL DROP"),
    (re.compile(r"\btruncate\s+table\b", re.IGNORECASE), "SQL TRUNCATE"),
    # A full-table DELETE empties it as surely as a TRUNCATE, and neither rule
    # above reaches it (PI-906). `\s+` after the verb keeps identifiers that
    # merely start with the word — `deleted_at`, `delete_log` — out, and the
    # `\b` before it keeps `is_deleted FROM …` out: the word must stand alone
    # and be immediately followed by FROM, which a SELECT never does.
    #
    # A TABLE MUST FOLLOW, AND THE CLAUSE MUST END. "delete from" is ordinary
    # English, unlike "drop table" and "truncate table", so the bare form this
    # rule first shipped with flagged six perfectly normal commands — measured
    # against the live deny table, not supposed:
    #     git commit -m "chore: delete from the stale cache"
    #     git log --grep "delete from"
    #     grep -rn "DELETE FROM" src/
    #     echo 'how to delete from a list in python'
    #     # TODO: delete from the queue once drained
    #     echo "we should delete from that table eventually"
    # Note the second-order problem: writing a commit message ABOUT this rule
    # tripped it.
    #
    # Requiring an identifier and then a WHERE, a statement terminator or a
    # closing quote separates the statement from the sentence — prose continues
    # with more words, SQL does not. Measured after: 0 false positives on those
    # six, 0 missed true positives on the destructive corpus.
    (
        re.compile(
            r"\bdelete\s+from\s+[A-Za-z_`\"\[][\w.`\"\[\]$-]*\s*(?:\bwhere\b|;|\"|'|$)",
            re.IGNORECASE,
        ),
        "SQL DELETE FROM",
    ),
    # MERGE rewrites and can DELETE rows in the target; only DROP/TRUNCATE and
    # DELETE FROM were modelled (PI-906). "merge" alone is hopeless as a
    # signal — `git merge`, `gh pr merge` and every commit message about a
    # merge would match — so the rule keys on the SHAPE of the statement:
    # target identifier, USING, ON, then WHEN [NOT] MATCHED.
    #
    # USING ... ON alone was NOT enough, measured rather than supposed: the
    # sentence `echo "we should merge into that table using the new source on
    # monday"` matched it. Every clause in that shape is ordinary English. The
    # WHEN [NOT] MATCHED clause is not, and MERGE is invalid without at least
    # one of them, so requiring it costs no true positive.
    (
        re.compile(
            r"\bmerge\s+(?:into\s+)?[A-Za-z_`\"\[][\w.`\"\[\]$-]*"
            r"\s+(?:(?:as\s+)?[A-Za-z_]\w*\s+)?using\b"
            rf"{_SEG}\bon\b{_SEG}\bwhen\s+(?:not\s+)?matched\b",
            re.IGNORECASE,
        ),
        "SQL MERGE",
    ),
    (
        # Recursive + force can be bundled (-rf/-fr) OR split across separate
        # args in any order (rm -r -f /, rm --force --recursive /) — the old
        # single-token pattern missed the split forms (2026-07 review). Two
        # lookaheads assert both a recursive and a force flag appear somewhere in
        # the option run before a dangerous target (an absolute path other than
        # /tmp, or ~).
        re.compile(
            r"\brm\b"
            r"(?=(?:\s+-{1,2}[\w-]+)*\s+(?:-\w*r\w*|--recursive)\b)"
            r"(?=(?:\s+-{1,2}[\w-]+)*\s+(?:-\w*f\w*|--force)\b)"
            r"(?:\s+-{1,2}[\w-]+)+\s+(/(?!tmp\b)|~)"
        ),
        "recursive force-remove outside the project",
    ),
    # Publishing a GTM container version pushes tags to every live page the
    # container is on — instant, global, and not a git-mediated change
    # (PI-906). THE HONEST LIMIT: this matcher sees a `curl` in a Bash tool
    # call. The same publish through a non-Bash tool, an MCP server or the GTM
    # UI bypasses it entirely. The durable control is a GTM-side permission;
    # this only stops the spelling an agent shell reaches for.
    (
        re.compile(r"tagmanager\.googleapis\.com[^\s'\"]*:publish"),
        "GTM container version publish",
    ),
    (re.compile(r"\bgh\s+repo\s+delete\b"), "gh repo delete"),
    (re.compile(r"\bdocker\s+(volume\s+prune|system\s+prune)\b"), "docker prune"),
]

# ── Secret-file exposure (PI-893) ───────────────────────────────────────────
# The scaffold's secret machinery is write/commit-oriented: gitleaks and the
# pre-commit gate stop you COMMITTING a secret, .gitignore stops you tracking
# one, and this table stopped you destroying things. Nothing stopped `cat .env`,
# so the values land in the transcript — which is re-sent on every following
# turn and outlives the session that read them.
#
# `permissions.deny` in the scaffolded settings.json closes the Read TOOL. It
# cannot close Bash, because a permission rule matches a tool's arguments and
# Bash's argument is one opaque string. That is this check's job.
_SECRET_PATH = re.compile(
    r"""
    (?:^|[\s=:'"(<@])                 # a token boundary, never mid-word
                                       # `@` because `curl -d @.env` is exfil
    (?:[\w.@~${}-]*/)*                 # optional directory prefix, incl. an
                                       # expansion: `$PWD/.env`, `${HOME}/.netrc`
                                       # and `"$HOME/.ssh/id_rsa"` all reach a
                                       # real file and all missed without this
    (?:
        # `.env`, `<stem>.env` and `.env.<anything>` EXCEPT the four
        # documented example spellings. Those are committed, value-free, and
        # the file an agent reads to learn which variables exist — denying
        # them would be a false positive on the safe half of the convention.
        #
        # THE STEM IS NOT DECORATION. Without `[\w-]*` the check missed
        # `prod.env`, `staging.env` and `my.env.local` — three ordinary
        # spellings of the file it exists to guard. A mutation run found it:
        # deleting the leading token boundary changed no test result, which
        # meant nothing pinned that part of the pattern, which meant nobody
        # had checked what it excluded. direnv's `.envrc` is here for the
        # same reason — it routinely holds `export AWS_SECRET_...`.
        [\w-]*\.env(?:\.(?!example|sample|template|dist)[\w-]+)*(?![\w.-])
      | \.envrc(?![\w.-])
      | id_(?:rsa|dsa|ecdsa|ed25519)(?![\w.-])
      | \.(?:netrc|pgpass|npmrc)(?![\w.-])
        # `key` covers the `server.key` / `tls.key` convention. It was absent
        # while `.gitignore` in every scaffolded repo already lists `*.key`,
        # so the tree classified the file as a secret and the guard read it
        # out loud: measured, `cat server.key` and `cat tls.key` were ALLOWED
        # while `cat id_rsa` and `cat secrets.pem` asked. A false positive
        # here costs one confirmation, which is the cheap side of the trade.
      | [\w.-]*\.(?:pem|p12|pfx|jks|keystore|key)(?![\w.-])
      | [\w.-]*(?:service[-_]?account|credentials|client[-_]secret)[\w.-]*\.json(?![\w.-])
      | secrets?/[\w./-]+
    )
    """,
    re.VERBOSE,
)

# Commands that cannot put a file's CONTENTS anywhere: they act on the name,
# the metadata or the directory entry. `rm .env` is not exposure — it may be
# unwise, but the values do not reach the transcript, and flagging it would nag
# on the cleanup that follows every scaffold demo.
#
# `echo`/`printf` are here for one specific daily command: `echo ".env" >>
# .gitignore`. WRITING a secret file is also not exposure — the values came
# from the session, they did not enter it.
_EXPOSURE_SAFE_VERBS = frozenset(
    {
        "ls",
        "ll",
        "stat",
        "file",
        "test",
        "[",
        "[[",
        "touch",
        "mkdir",
        "rmdir",
        "rm",
        "chmod",
        "chown",
        "ln",
        "echo",
        "printf",
        "basename",
        "dirname",
        "find",
        "which",
        "type",
    }
)

# Tools whose FIRST non-flag argument is a pattern or a program, not a path.
# `grep -rn ".env" src/` searches for the string and opens nothing named by it;
# scanning that argument as a path made a routine search prompt. `sed`/`awk`
# take a script first and their paths after, so only the first is skipped.
_PATTERN_FIRST_ARG = frozenset(
    {"grep", "egrep", "fgrep", "rg", "ag", "ack", "sed", "awk", "gawk", "nawk"}
)

# A commit message is prose, not an access. `git commit -m "docs: describe .env
# handling"` is the same shape of false positive that the SQL DELETE rule hit —
# writing about the guarded thing tripping the guard.
_MESSAGE_ARG = re.compile(r"""(?:-m|-am|--message)[=\s]+(?P<q>['"]).*?(?P=q)""", re.DOTALL)


# A command substitution hides a whole command inside another one, and the
# outer verb is the one the exemption looks at. `echo "$(cat .env)"` prints the
# secret while presenting `echo` as its head (PR #942 review, P1). Both
# spellings, one nesting level — a deeper nest is unusual enough to leave to
# the ask/deny posture rather than pretend to a parser.
_SUBSTITUTION = re.compile(r"\$\(([^()]*)\)|`([^`]*)`")

# `find` is exempt only when it is doing what makes it exempt. With an action,
# or piped into something that reads, it becomes the READER'S argument list:
# `find . -name .env -exec cat {} +` and `find . -name .env | xargs cat` both
# print the file, and neither stage looks dangerous on its own — the path is in
# the find, the verb is downstream (PR #942 review, P1).
#
# NOT "any pipe": `find . -name .env | wc -l` counts matches and reads nothing,
# and a guard that prompts on it is the false positive that gets guards turned
# off. The downstream verb has to actually consume contents.
_FIND_ACTS = re.compile(r"\s-(?:exec|execdir|ok|okdir)\b")
_READER_VERBS = frozenset(
    {
        "xargs",
        "cat",
        "bat",
        "less",
        "more",
        "head",
        "tail",
        "nl",
        "tee",
        "strings",
        "xxd",
        "od",
        "grep",
        "egrep",
        "rg",
        "sed",
        "awk",
        "cut",
        "cp",
        "curl",
        "wget",
        "scp",
        "base64",
        "source",
    }
)


def _leaf_commands(command: str) -> list[str]:
    """Every simple command in *command*, including ones inside substitutions.

    The outer text keeps its shape with substitutions blanked out, so an
    exempt verb wrapping a read is judged on the read, not on the wrapper.
    """
    out: list[str] = []
    pending = [command]
    while pending and len(out) < 100:
        chunk = pending.pop()
        inner = [g for match in _SUBSTITUTION.finditer(chunk) for g in match.groups() if g]
        if inner:
            pending.extend(inner)
            chunk = _SUBSTITUTION.sub(" ", chunk)
        out.extend(re.split(r"[;&|\n]+", chunk))
    return out


def _statements(command: str) -> list[str]:
    """Split *command* into statements, keeping each pipeline intact.

    `_leaf_commands` collapses `;`, `&` and `|` into one separator, which loses
    the distinction that matters for the reader check: `a | b` shares a data path
    and `a && b` does not. Substitutions are inlined the same way, so a read
    hidden inside one is still judged.
    """
    out: list[str] = []
    pending = [command]
    while pending and len(out) < 100:
        chunk = pending.pop()
        inner = [g for match in _SUBSTITUTION.finditer(chunk) for g in match.groups() if g]
        if inner:
            pending.extend(inner)
            chunk = _SUBSTITUTION.sub(" ", chunk)
        # `|&` IS A PIPE. Bash's shorthand for `2>&1 |` survives the statement
        # split intact (the `&` is excluded below), and then the per-stage split
        # on `|` leaves the downstream segment headed by `&` instead of the real
        # verb — so no reader is found, the producer is exempted, and the read
        # goes through. Measured: `ls <dotenv> |& xargs cat` allowed, including
        # the documented `find … | xargs cat` shape (PR #952 review, second
        # round). Normalising here fixes both the statement split and the stage
        # split, because both read this output.
        chunk = chunk.replace("|&", "|")
        # `&&` and `||` FIRST, or they split wrongly. And a bare `&` is only a
        # separator when it is not part of a redirection: splitting on any `&`
        # broke `2>&1` and `&>`, which tore a producer away from its downstream
        # reader and REINTRODUCED the bypass — measured, `ls <dotenv> 2>&1 |
        # xargs cat` went back to allow.
        out.extend(re.split(r"(?:&&|\|\||;|\n|(?<![>&|])&(?![>&]))+", chunk))
    return out


def _exposes_secret(command: str) -> str | None:
    """Return a label if *command* could read a secret-bearing file, else None.

    Segment-wise, because `ls .env && cat .env` is two commands and only the
    second one reads: a whole-string match would be decided by the harmless
    verb that happens to come first.
    """
    # PER STATEMENT, NOT PER COMMAND. Computing the reader set over the whole
    # string made any reader anywhere taint every safe segment, so
    # `cat README.md && echo ".env" >> .gitignore` was flagged as a secret read
    # — `cat` is a reader, `echo` was therefore not exempt, and the `.env` being
    # written INTO .gitignore matched. Prompting on that is the false positive
    # this guard cannot afford: it is ordinary work, and a guard that blocks
    # ordinary work gets switched off. `|` shares a data path, `&&` and `;` do
    # not, so the reader question is only meaningful inside one pipeline.
    for statement in _statements(command):
        found = _statement_exposes(statement)
        if found:
            return found
    return None


def _statement_exposes(statement: str) -> str | None:
    """The original per-segment check, scoped to one statement's pipeline."""
    leaves = [seg for seg in statement.split("|") if seg.strip()]
    heads = {seg.split()[0].rsplit("/", 1)[-1] for seg in leaves if seg.split()}
    # A PRODUCER IS ONLY SAFE WHILE NOTHING DOWNSTREAM CAN READ WHAT IT NAMES.
    # This gate existed for `find` alone, so every other producer in
    # _EXPOSURE_SAFE_VERBS was exempted unconditionally and the pipeline that
    # actually reads the file was skipped along with it. Measured before the fix:
    #   find . -name .env | xargs cat   -> ask      (gated, correct)
    #   printf '.env\n'   | xargs cat   -> ALLOWED  (exempt, wrong)
    #   echo .env         | xargs cat   -> ALLOWED  (exempt, wrong)
    #   ls .env           | xargs cat   -> ALLOWED  (exempt, wrong)
    # The reader segment carries no path of its own, so once the naming segment
    # is skipped nothing is left to match and the contents reach the transcript.
    producers_are_safe = not _FIND_ACTS.search(statement) and not (heads & _READER_VERBS)
    for segment in leaves:
        seg = _MESSAGE_ARG.sub(" ", segment).strip()
        if not seg:
            continue
        tokens = seg.split()
        head = tokens[0].rsplit("/", 1)[-1]  # /bin/cat and cat are one verb
        if head == "find" and not producers_are_safe:
            pass  # an action or a pipe turns it into a reader's argument list
        elif head in _EXPOSURE_SAFE_VERBS and producers_are_safe:
            continue
        if head in _PATTERN_FIRST_ARG:
            rest = [t for t in tokens[1:] if not t.startswith("-")]
            if rest:
                seg = seg.replace(rest[0], " ", 1)
        if _SECRET_PATH.search(" " + seg):
            return "read of a secret-bearing file"
    return None


# Fully autonomous mode: no human is watching the prompt, so "ask" is
# meaningless — block outright. Other modes (default, plan, acceptEdits)
# still surface an interactive permission prompt for Bash.
_AUTONOMOUS_MODES = {"bypassPermissions", "dangerouslySkipPermissions"}


# THE MARKER CONTRACT (PI-901) — `context: ambient` is the owner opting a
# repo back in to the ambient (global) agent layer. Anchored at column 0: a
# top-level YAML key cannot be indented, and matching an indented one would let
# a `context: ambient` nested under some unrelated block opt the whole repo out.
# Quoted keys/values and a space before the colon ARE valid top-level YAML, and
# project-init's own `_CONTEXT_KEY_RE` preserves exactly those spellings on
# upgrade — a spelling the writer keeps but the reader misses is an opt-out that
# survives in the file and is then ignored. KEEP IN STEP with the ambient
# layer's own marker reader, which reads the same key with the same
# tolerances — two readers of one contract that disagree is the failure the
# contract exists to prevent.
#
# A COMMENT NEEDS WHITESPACE BEFORE IT (PR #927 review). `#` only begins a YAML
# comment when preceded by whitespace; otherwise it is part of the plain scalar.
# So `context: ambient#typo` is the value `ambient#typo` — NOT `ambient` — and
# the first cut read it as an opt-out, silently discarding that repo's allowlist.
# The direction is safe for this guard (no allowlist ⇒ keep guarding) and it is
# still wrong: it disables a control the owner declared, on a typo, and the
# orchestrator's real YAML parser resolves the same line differently, which is
# precisely the three-readers divergence the shared fixtures exist to prevent.
_CONTEXT_AMBIENT_RE = re.compile(
    r"""^["']?context["']?[^\S\n]*:[^\S\n]*["']?ambient["']?(?:[^\S\n]+\#.*)?[^\S\n]*$""",
    re.MULTILINE,
)


def _declares_ambient(config: Path) -> bool:
    """True iff *config* carries a top-level ``context: ambient`` declaration.

    Unreadable is not ambient: an unreadable config supplies no allowlist
    either, so the guard already keeps guarding, and inventing a verdict from a
    failed read would be a guess.
    """
    try:
        text = config.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return bool(_CONTEXT_AMBIENT_RE.search(text))


def _find_config(start: Path) -> Path | None:
    """Walk up from *start* to the project's .agents/config.yaml, if any.

    A SYMLINKED marker is refused and the walk continues (PI-903; the marker
    contract). ``is_file()`` follows symlinks, so this used to
    accept an ``.agents/`` — or an ``.agents/config.yaml`` — pointing anywhere on
    disk. That matters more here than it does for a boundary verdict: this
    function locates the file ``safety.allow`` is read from, so a link planted
    outside the repo supplies its own allowlist and switches the destructive-
    command deny table off wholesale. A symlink is writable from outside the
    repo's own review, which is exactly what the guard is defending.

    Refusing is the safe direction (no allowlist ⇒ keep guarding), and it makes
    this walk agree with the ambient layer's own marker reader, which has
    refused symlinked markers since the 2026-07-24 marker-forgery finding. Nothing surfaced the
    disagreement while it existed.

    Two further rules from the same frozen contract:

    ``context: ambient`` (the marker contract) — the owner declaring that this
    repo does NOT govern itself and the ambient layer keeps acting here. A repo
    that has opted out of governed status does not get to relax the deny table
    with its own ``safety.allow``, so the declaration returns None (no
    allowlist) rather than continuing the walk. Deciding AT the marker is the
    contract's rule and the reason it is not a ``continue``: the innermost
    marker wins, so an explicit inner opt-out must not fall through and be
    overruled by an outer repo's config. A symlink is refused because it is
    forged; an ``ambient`` value is honoured because it is the owner speaking.

    ``$HOME`` (the marker contract) — the walk stops before it. A marker sitting in
    the home directory itself otherwise supplies an allowlist to every command
    run anywhere beneath it, and it is written by accident rather than by
    attack: project-init run once in the wrong cwd scaffolds one there. Paths
    outside $HOME are untouched and still walk to ``/``. Resolved first, because
    the stop is an equality test and ``~/.`` names the same directory as ``~``.
    """
    # RuntimeError as well as OSError, and the difference is measurable rather
    # than defensive (PR #927 review): `Path.resolve()` raises RuntimeError on a
    # SYMLINK LOOP under Python 3.11 and 3.12 and stopped doing so in 3.13 —
    # checked on all three. Both older versions are in this repo's CI matrix and
    # this template ships to projects running whichever Python they have. An
    # escaping exception here does not crash the session (the guard's outer
    # handler is fail-open by design) — it makes the guard STAND DOWN, so a
    # planted loop anywhere in the walk path switches the deny table off for
    # that command. A path that cannot be resolved is used as spelled instead.
    with contextlib.suppress(OSError, RuntimeError):
        start = start.resolve()
    try:
        home: Path | None = Path.home().resolve()
    except (RuntimeError, OSError):
        home = None  # no home to stop before; inventing one would be a guess
    for candidate in (start, *start.parents):
        if candidate == home:
            break
        agents = candidate / ".agents"
        config = agents / "config.yaml"
        if agents.is_symlink() or config.is_symlink():
            continue
        if config.is_file():
            return None if _declares_ambient(config) else config
    return None


def _unquote(value: str) -> str:
    """Strip one pair of matching surrounding quotes, leaving mismatched or
    single quotes intact so ``'foo"`` is not silently corrupted (PI-187 review).
    """
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _parse_inline_allow(raw: str, problems: list[str]) -> list[str]:
    """Parse the inline form, ``allow: ["a", "b"]``.

    A REGEX IS NOT JSON (PI-943). ``["^cat \\.env$"]`` is the natural way to
    write an escaped dot, and it is not valid JSON — ``\\.`` is not a legal
    escape — so ``json.loads`` raised, the caller fell open, and the operator's
    allowlist silently did not exist. They saw a prompt for the very command
    they had just allowlisted, with nothing anywhere saying why.

    Strict parse first, so an operator who correctly wrote ``\\\\.`` keeps the
    literal backslash they asked for. Only on failure are backslashes escaped
    and the parse retried — which is what someone writing a raw regex meant.
    Doing it in that order is what keeps the lenient path from changing the
    meaning of input that was already valid.
    """
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        try:
            parsed = json.loads(raw.replace("\\", "\\\\"))
        except json.JSONDecodeError as exc:
            problems.append(f"inline `allow:` could not be parsed ({exc.msg})")
            return []
    # A non-list allow (JSON string/object/number) must not be iterated
    # character-by-character into an over-permissive allowlist or crash the
    # guard — ignore it and keep guarding (PI-187 review).
    if not isinstance(parsed, list):
        problems.append("`allow:` is not a list — ignored")
        return []
    return [p for p in parsed if isinstance(p, str)]


def _allow_patterns(root: Path) -> tuple[list[re.Pattern[str]], list[str]]:
    """Read safety.allow from .agents/config.yaml → (patterns, problems).

    Accepts both an inline JSON list (``allow: ["a", "b"]``) and a multi-line
    YAML list (``allow:`` on its own line followed by ``- "a"`` items). The
    inline-only parser silently dropped the natural YAML form to ``[]`` (PI-187).

    FAIL-OPEN IS RIGHT FOR A MISSING CONFIG AND WRONG FOR A MALFORMED ONE
    (PI-943). The two are indistinguishable to the operator, and the malformed
    case means a rule they wrote is not in force. So problems are collected and
    returned rather than swallowed, and the caller puts them in front of the
    person who is about to wonder why their allowlist did nothing.

    *root* is the Bash tool's cwd, which may be a subdirectory after `cd` —
    the config is located by walking up the tree.
    """
    config = _find_config(root)
    problems: list[str] = []
    if config is None:
        return [], problems
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
                    patterns.extend(_parse_inline_allow(raw, problems))
                else:
                    in_allow = True  # multi-line YAML list follows
    except OSError as exc:
        problems.append(f"could not read {config}: {exc.strerror or exc}")
        return [], problems

    # Compiled ONE AT A TIME. The old `[re.compile(p) for p in ...]` inside the
    # try meant a single malformed pattern discarded every other rule in the
    # file, including the ones that had parsed perfectly (PI-943).
    compiled: list[re.Pattern[str]] = []
    for pattern in patterns:
        if not pattern:
            continue
        try:
            compiled.append(re.compile(pattern))
        except re.error as exc:
            problems.append(f"safety.allow pattern {pattern!r} is not a valid regex ({exc.msg})")
    return compiled, problems


def _find_obs_dir(start: Path) -> Path | None:
    """Locate the overlay marker dir (.agents/observability/), or None.

    Prefers ``$CLAUDE_PROJECT_DIR``; otherwise walks up from *start* (the Bash
    cwd, which may be a subdirectory after ``cd``), mirroring ``_find_config``.
    """
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env:
        obs = Path(env) / ".agents" / "observability"
        return obs if obs.is_dir() else None
    for candidate in (start, *start.parents):
        obs = candidate / ".agents" / "observability"
        if obs.is_dir():
            return obs
    return None


def _redact_command(command: str) -> str:
    """Truncate to 500 chars and redact common secret patterns."""
    cmd = command[:500]
    cmd = re.sub(r"(?i)(token|key|secret|password|auth|api_key)=[\w-]+", r"\1=***", cmd)
    return re.sub(r"://[^@]+@", r"://***@", cmd)


def usage_log(payload: dict, root: Path, decision: str, command: str) -> None:
    """Append a self-log line iff the observability overlay is installed (#406).

    Shipped-always-dormant: no-ops unless ``.agents/observability/`` exists.
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
            "decision": decision,
            "command": _redact_command(command),
        }
        session = payload.get("session_id") or os.environ.get("CLAUDE_SESSION_ID")
        if session:
            line["session"] = session
        with (obs / "usage.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line) + "\n")
    except Exception:  # noqa: BLE001 — logging must never break the guard
        return


def _verdict(reason: str, permission_mode: str, problems: list[str] | None = None) -> dict:
    """Build the hook verdict. Autonomous modes have no human to ask (ADR-012).

    *problems* is appended to the reason. This is the one place the operator is
    guaranteed to read: they are staring at a prompt for a command they believe
    they allowlisted, which is exactly the moment to tell them the allowlist
    did not load (PI-943). stderr from a PreToolUse hook that exits 0 is not
    reliably surfaced, so it cannot be the only channel.
    """
    if problems:
        reason += " NOTE: safety.allow was not fully applied — " + "; ".join(problems) + "."
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny" if permission_mode in _AUTONOMOUS_MODES else "ask",
            "permissionDecisionReason": reason,
        }
    }


def evaluate(
    command: str,
    permission_mode: str,
    allow: list[re.Pattern[str]],
    problems: list[str] | None = None,
) -> dict | None:
    """Return the hook verdict for *command*, or None to let it through."""
    if any(p.search(command) for p in allow):
        return None
    for pattern, label in DENY_RULES:
        if pattern.search(command):
            return _verdict(
                f"prod_guard: '{label}' is a destructive operation. "
                "If this is intentional and safe, add a matching regex to "
                "safety.allow in .agents/config.yaml, or run it yourself. "
                "(Guardrail only — real protection is credential separation, "
                "see .agents/docs/guides/secrets.md.)",
                permission_mode,
                problems,
            )
    exposure = _exposes_secret(command)
    if exposure is not None:
        return _verdict(
            f"prod_guard: '{exposure}' — its contents would enter the transcript "
            "and be re-sent on every following turn. Read the .example file, or "
            "have the value injected as an environment variable. If the read is "
            "genuinely needed, add a matching regex to safety.allow in "
            ".agents/config.yaml, or run the command yourself. "
            "(Guardrail only — real protection is credential separation, "
            "see .agents/docs/guides/secrets.md.)",
            permission_mode,
            problems,
        )
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
    try:
        allow, problems = _allow_patterns(root)
        for problem in problems:
            # Best-effort second channel. Not the primary one — see _verdict.
            print(f"prod_guard: {problem}", file=sys.stderr)
        verdict = evaluate(command, mode, allow, problems)
    except Exception:  # noqa: BLE001 — guardrail must never break the session
        verdict = None

    decision = "allow"
    if verdict is not None:
        raw_decision = verdict.get("hookSpecificOutput", {}).get("permissionDecision", "allow")
        decision = "block" if raw_decision == "deny" else raw_decision

    # Self-log this firing from the same parsed payload (no second stdin read,
    # #406). Dormant unless the observability overlay is installed; fail-open.
    usage_log(payload, root, decision, command)

    if verdict is not None:
        sys.stdout.write(json.dumps(verdict))
    return 0


if __name__ == "__main__":
    sys.exit(main())
