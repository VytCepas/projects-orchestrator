"""Pin every ``gcloud`` subprocess to one named credential directory.

:func:`projects_orchestrator.runner.run_command` executes a shell string with no
``env=``, so a ``gcloud`` child inherits whatever the ambient CLI identity
happens to be. That is a silent *correctness* bug, not a crash, and the
pessimistic contract in :mod:`projects_orchestrator.adapters.gcp` (ADR-003)
cannot catch it. ADR-003 separates "I found nothing" from "I could not look" — a
scan run under a **narrower** identity is neither. It exits 0 and returns a
SHORTER inventory, so the orphan hunt reports a clean estate from a partial scan:
exactly the failure ADR-003 exists to prevent, reached by a route it never
watched. This is not hypothetical — the operator's ambient identity changed to a
narrower account on 2026-07-29 and nothing failed.

So the identity is **named, not inherited**. A misconfigured pin needs no special
case here: ``gcloud`` under a credential-less directory exits non-zero, and the
existing pessimistic contract already reads a non-zero exit as *unknown*. An
``is_dir()`` gate was tried and removed — it duplicated that contract, made two
adapters depend on the filesystem, and would have short-circuited before the
subprocess on any machine without the directory (every CI runner), silently
turning four existing behavioural tests green for the wrong reason.

Pure and testable, like :func:`projects_orchestrator.sandbox.agent_env`:
:func:`gcloud_env` is a function of an input mapping and :func:`gcloud_config_dir`
a function of one variable, so neither needs a subprocess to prove.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

#: Operator-facing override. Point it at another identity directory to inventory
#: as a different account, or at the ambient ``~/.config/gcloud`` to deliberately
#: opt back into whatever the CLI's current account is. Naming ambient explicitly
#: is fine; *defaulting* to it is the bug.
CONFIG_DIR_ENV = "PROJECTS_ORCHESTRATOR_GCLOUD_CONFIG"

#: Default identity, relative to ``$HOME``. Asset inventory needs org-wide
#: visibility, which the operator's day-to-day account does not have; a narrower
#: account is precisely the failure this module exists to prevent, so the default
#: names the broad-authority directory rather than leaving it to chance.
_DEFAULT_CONFIG_DIR = Path(".config") / "gcloud" / "identities" / "tools"

#: Variable families that outrank ``CLOUDSDK_CONFIG`` and must therefore be
#: stripped, not merged. Every gcloud property has a
#: ``CLOUDSDK_<SECTION>_<NAME>`` env override, so an inherited
#: ``CLOUDSDK_CORE_ACCOUNT`` silently beats the directory we just pinned; and
#: ``GOOGLE_APPLICATION_CREDENTIALS`` / ``GOOGLE_CLOUD_PROJECT`` do the same for
#: the credential and the project. Pinning the directory without this scrub
#: yields a pin that any inherited variable can quietly defeat.
_OVERRIDE_PREFIXES = ("CLOUDSDK_", "GOOGLE_")


def gcloud_config_dir(base: Mapping[str, str] | None = None) -> Path:
    """The credential directory every gcloud subprocess must use (pure).

    Args:
        base: The environment to read the override from. Defaults to the real
            ``os.environ``.

    Returns:
        :data:`CONFIG_DIR_ENV`'s value when set and non-empty, else
        ``$HOME`` / :data:`_DEFAULT_CONFIG_DIR`. Existence is deliberately NOT
        checked: a directory holding no credential makes ``gcloud`` exit
        non-zero, which every caller already degrades to *unknown*.
    """
    source = os.environ if base is None else base
    override = source.get(CONFIG_DIR_ENV, "")
    if override:
        return Path(override)
    return Path.home() / _DEFAULT_CONFIG_DIR


def gcloud_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Build the environment a gcloud subprocess must see (pure).

    Copies ``base``, drops every :data:`_OVERRIDE_PREFIXES` variable, then pins
    ``CLOUDSDK_CONFIG``. The drop is the load-bearing half: a pin that an
    inherited ``CLOUDSDK_CORE_ACCOUNT`` can override is not a pin.

    Args:
        base: The environment to derive from. Defaults to the real
            ``os.environ``.

    Returns:
        A complete, fresh environment dict — complete because
        :class:`subprocess.Popen` *replaces* rather than merges ``env``.
    """
    source = os.environ if base is None else base
    env = {name: value for name, value in source.items() if not name.startswith(_OVERRIDE_PREFIXES)}
    env["CLOUDSDK_CONFIG"] = str(gcloud_config_dir(source))
    return env
