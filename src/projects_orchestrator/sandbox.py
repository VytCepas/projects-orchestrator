"""The environment an agent run is allowed to see — the data plane, scrubbed out.

ADR-007 §4: an agent run must not be able to reach production. The tool
allow-list (heal's ``--allowedTools``) stops the *``claude`` CLI* from running
``gcloud`` or ``gsutil`` — but it is a property of that one CLI. It says nothing
about the process **environment**, and the environment is where the keys live.

``subprocess.run(..., cwd=X)`` with no ``env=`` inherits the *entire* parent
environment. So the operator's ``GOOGLE_APPLICATION_CREDENTIALS``,
``AWS_SECRET_ACCESS_KEY``, ``FLY_API_TOKEN``, ``GH_TOKEN`` and every other secret
in their shell is handed straight to the agent. If any injected tool, hook, or
MCP server shells out — past the CLI's allow-list or around it — the credentials
to wreck production are already sitting in ``os.environ``. A key you never handed
over cannot be leaked, misused, or logged.

**Allowlist, not denylist — and this is the whole design.** The tempting version
is "drop everything matching ``*TOKEN*``, ``*SECRET*``, ``*KEY*``". That is the
same mistake as blocklisting branch names: it is always one variable short.
``SNOWFLAKE_PASSWORD`` has none of those substrings; ``GOOGLE_APPLICATION_CREDENTIALS``
has none of those substrings. So instead the agent's environment is built from
**nothing**, and only a small, explicit, audited set of harmless variables is
copied in — the ones a coding agent genuinely needs to function (a ``PATH`` to
find ``git``, a ``HOME`` for tool config, locale). Anything not on that list does
not exist, whether or not anyone predicted it.

**Scrubbing the variables is not enough — the FILES they point at leak too.**
This is the subtle half. Google Application Default Credentials do not only read
``GOOGLE_APPLICATION_CREDENTIALS``; they fall back to
``$HOME/.config/gcloud/application_default_credentials.json``. The AWS SDKs read
``~/.aws/credentials``. ``flyctl`` reads ``~/.config/fly``. So preserving the
operator's ``HOME`` re-opens every file-backed credential the env scrub just
closed — and copying ``XDG_CONFIG_HOME`` (``~/.config``) points *straight back*
at the gcloud directory even if ``HOME`` were redirected. The scrub therefore
gives the agent a **fresh, empty ``HOME``** with every XDG base dir redirected
underneath it: the credential *files* are as absent as the credential *vars*.
A clean HOME also means the agent does not inherit the operator's globally
configured MCP servers — isolation we want anyway.

Pure and testable: :func:`agent_env` is a function of an input mapping and an
explicit home path, so the scrub can be asserted without spawning anything or
touching the real filesystem.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

#: The ONLY variables an agent subprocess inherits. Each is here because a coding
#: agent cannot function without it, and none of them is a credential:
#:
#: - PATH: find `git`, the language toolchain, the agent's own helpers.
#: - HOME / USER / LOGNAME: tool config discovery (`~/.gitconfig`), git identity.
#: - locale (LANG/LC_*): correct text decoding; wrong locale corrupts diffs.
#: - TERM: some tools misbehave with no terminal type at all.
#: - TMPDIR: a place to write scratch files that is not the repo.
#: - the agent's OWN key: it must be able to call the model it IS. This is the
#:   agent's credential, not the operator's cloud/production credentials — the
#:   distinction ADR-012 draws — and without it the agent cannot run at all.
#: NOTE what is NOT here: HOME. It is set fresh under the sandbox home rather than
#: copied, because the operator's HOME is the search path for every file-backed
#: credential (see the module docstring). USER/LOGNAME are copied for git identity
#: discovery, which reads them but not HOME.
_ALLOWED = frozenset(
    {
        "PATH",
        "USER",
        "LOGNAME",
        "SHELL",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TERM",
        "TMPDIR",
        "TZ",
        "ANTHROPIC_API_KEY",
    }
)

#: Locale prefix, copied wholesale — LC_TIME, LC_NUMERIC, … carry no credentials
#: and a wrong locale corrupts text. XDG_ is deliberately NOT here: XDG_CONFIG_HOME
#: (``~/.config``) points straight back at the gcloud/aws credential directories,
#: so the XDG base dirs are set fresh under the sandbox home instead of inherited.
_ALLOWED_PREFIXES = ("LC_",)


def agent_env(base: Mapping[str, str] | None = None, *, home: str) -> dict[str, str]:
    """Build the environment an agent subprocess may see (pure).

    Starts from **nothing**, copies in only :data:`_ALLOWED` and the
    :data:`_ALLOWED_PREFIXES` families, then points ``HOME`` and every XDG base
    dir at ``home`` — a fresh, empty directory. Everything else — every cloud
    credential and API token in the operator's shell, and every credential FILE
    their real HOME points at — is simply absent.

    Args:
        base: The environment to scrub. Defaults to the real ``os.environ``.
        home: The agent's ``HOME`` — a fresh, empty, per-run directory. Required,
            because "reuse the operator's HOME" is precisely the mistake: it is
            the search path for ``~/.config/gcloud``, ``~/.aws``, ``~/.config/fly``
            and friends, and re-opens every file-backed credential the variable
            scrub just closed.

    Returns:
        A fresh dict: the audited, credential-free subset, with HOME/XDG isolated.
    """
    source = os.environ if base is None else base
    env = {
        name: value
        for name, value in source.items()
        if name in _ALLOWED or name.startswith(_ALLOWED_PREFIXES)
    }
    env["HOME"] = home
    env["XDG_CONFIG_HOME"] = str(Path(home) / ".config")
    env["XDG_CACHE_HOME"] = str(Path(home) / ".cache")
    env["XDG_DATA_HOME"] = str(Path(home) / ".local" / "share")
    env["XDG_STATE_HOME"] = str(Path(home) / ".local" / "state")
    return env
