"""The project name is not ours — sanitise it before it becomes a path.

``descriptor.name`` is read from a **child repo's own** ``config.yaml``. ADR-003
extends a "trusted shell string" trust level to the project's declared *tooling
commands* and to nothing else; the name never earned it. ``heal`` already treats
it as untrusted when building argv (``_run_argv``, never a shell string).

The same caution is needed the moment it becomes part of a **path**, and it is
easy to miss, because the failure is silent rather than loud::

    >>> Path("/home/me/.local/state/po/worktrees") / "/tmp/owned"
    PosixPath('/tmp/owned')

An absolute component **discards everything to its left**. A child repo naming
itself ``/tmp/owned`` — or ``../../../../tmp/owned`` — does not get a rejected
path, it gets its checkout written exactly where it asked. So a name is reduced
to a single, inert path component before it is ever joined.
"""

from __future__ import annotations

import hashlib
import re

#: Everything outside this set becomes a dash. Note that ``/`` and ``.`` are the
#: interesting ones — the first escapes downward, the second escapes upward.
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")

#: A name that reduces to nothing (``"..."``, ``"///"``) must still produce a
#: usable directory, and must not produce ``""`` (which joins to the parent) or
#: ``"."``/``".."`` (which traverse).
FALLBACK = "unnamed"


def safe_component(name: str) -> str:
    """Reduce ``name`` to one inert path component (pure).

    Guarantees, in order of how badly you want them: the result contains no path
    separator, is not ``.`` or ``..``, is not empty, does not begin with a dash
    (which would read as a flag were it ever passed to a CLI) — and is
    **injective for distinct inputs that needed cleaning**: a name the
    sanitiser had to alter carries a short hash of the original, so ``api/foo``
    and ``api.foo`` cannot both reduce to the component that a sibling
    literally named ``api-foo`` already owns. Two projects sharing one state
    file means ``stop`` can kill the other project's PID — collision is not a
    cosmetic problem here. Names that were already safe pass through unchanged,
    so existing state files keep their paths.

    Args:
        name: An untrusted name, typically ``descriptor.name``.

    Returns:
        A string safe to use as exactly one path segment.
    """
    cleaned = _UNSAFE.sub("-", name).strip(".-")
    if cleaned == name and name:
        return name
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:8]
    return f"{cleaned or FALLBACK}-{digest}"
