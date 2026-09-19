"""The host-health tile: one line about the machine the fleet runs on (#247).

Every other surface reports per-project state. When the whole fleet goes red at
once, the likeliest cause is the host — a full disk, a dead network, a
swapping machine — and that is the one signal the fleet view could not show.
An external reporter usually measures it already, so the fleet file names a
command (``host_health_command``) and its first output line becomes the tile.

**Never blank, never "ok" by default.** The tile reads :data:`HOST_UNKNOWN` when
no command is declared, the command cannot start, exits non-zero, times out or
prints nothing. A tile that went blank, or defaulted to healthy, would look the
same as a host that was checked and is fine.

**An argv, never a shell.** The command is split with :func:`shlex.split` and
run without a shell, like every other subprocess this package launches with
declared input. With no shell to expand it, a leading ``~`` in the program's
path is expanded here, so ``~/bin/host-health`` works as it reads.

Never raises.
"""

from __future__ import annotations

import logging
import shlex
import subprocess
from pathlib import Path

_log = logging.getLogger(__name__)

#: What the tile says whenever there is no verdict to show.
HOST_UNKNOWN = "host: unknown"

#: Seconds the command gets. The tile is drawn on every ``status``, so a slow
#: reporter must not hold the fleet view hostage.
HOST_TIMEOUT = 5.0

#: The tile is one line; a reporter that prints a paragraph is cut here.
_MAX_CHARS = 200


def host_health(command: str, timeout: float = HOST_TIMEOUT) -> str:
    """Run the declared host-health command and return the tile's text; never raises.

    Args:
        command: The fleet file's ``host_health_command`` (``""`` when absent).
        timeout: Seconds before the command is abandoned.

    Returns:
        ``host: <first non-empty output line>``, or :data:`HOST_UNKNOWN`.
    """
    if not command.strip():
        return HOST_UNKNOWN
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        _log.warning("host_health_command cannot be parsed: %s", exc)
        return HOST_UNKNOWN
    if not argv:
        return HOST_UNKNOWN
    if argv[0].startswith("~"):
        # Only a tilde path: `Path` would also rewrite `./reporter` to
        # `reporter`, turning a relative path into a PATH lookup.
        argv[0] = str(Path(argv[0]).expanduser())
    try:
        proc = subprocess.run(  # noqa: S603 — argv from shlex, no shell
            argv, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        _log.warning("host_health_command did not run: %r", exc)
        return HOST_UNKNOWN
    if proc.returncode != 0:
        _log.warning("host_health_command exited %d", proc.returncode)
        return HOST_UNKNOWN
    line = next((line.strip() for line in proc.stdout.splitlines() if line.strip()), "")
    if not line:
        _log.warning("host_health_command printed nothing")
        return HOST_UNKNOWN
    return f"host: {line[:_MAX_CHARS]}"
