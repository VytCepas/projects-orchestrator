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

Pure and testable: :func:`agent_env` is a function of an input mapping, so the
scrub can be asserted without spawning anything.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

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
_ALLOWED = frozenset(
    {
        "PATH",
        "HOME",
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

#: Prefixes copied wholesale — locale (LC_TIME, LC_NUMERIC, …) and XDG base dirs
#: (XDG_CONFIG_HOME, XDG_CACHE_HOME) that tools read for config/cache locations.
#: Deliberately NOT a general escape hatch: neither family carries credentials,
#: and both are needed for a tool to behave the way it does in the operator's
#: own shell.
_ALLOWED_PREFIXES = ("LC_", "XDG_")


def agent_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Build the environment an agent subprocess may see (pure).

    Starts from **nothing** and copies in only :data:`_ALLOWED` and the
    :data:`_ALLOWED_PREFIXES` families. Everything else — every cloud credential,
    API token, and CI secret in the operator's shell — is simply absent.

    Args:
        base: The environment to scrub. Defaults to the real ``os.environ``.

    Returns:
        A fresh dict containing only the audited, credential-free subset.
    """
    source = os.environ if base is None else base
    return {
        name: value
        for name, value in source.items()
        if name in _ALLOWED or name.startswith(_ALLOWED_PREFIXES)
    }
