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

**Bounded, and it cleans up after itself.** The dashboard draws the tile on
every poll, so a broken reporter must not cost memory or leave processes
behind. At most :data:`_MAX_OUTPUT_BYTES` of its stdout is ever held (stderr is
discarded), and it runs in its own session: on a timeout the whole process
group is killed, so a reporter script's stalled children die with it.

**An argv, never a shell.** The command is split with :func:`shlex.split` and
run without a shell, like every other subprocess this package launches with
declared input. With no shell to expand it, a leading ``~`` in the program's
path is expanded here, so ``~/bin/host-health`` works as it reads.

Never raises.
"""

from __future__ import annotations

import logging
import os
import selectors
import shlex
import signal
import subprocess
import time
from pathlib import Path
from typing import IO, cast

_log = logging.getLogger(__name__)

#: What the tile says whenever there is no verdict to show.
HOST_UNKNOWN = "host: unknown"

#: Seconds the command gets. The tile is drawn on every ``status``, so a slow
#: reporter must not hold the fleet view hostage.
HOST_TIMEOUT = 5.0

#: The tile is one line; a reporter that prints a paragraph is cut here.
_MAX_CHARS = 200

#: The most stdout ever held from one run. A reporter that keeps printing past
#: it without exiting runs into the timeout and reads unknown.
_MAX_OUTPUT_BYTES = 65_536


def _kill_session(proc: subprocess.Popen[bytes]) -> None:
    """Kill the reporter's whole process group; never raises.

    Called only before the leader is reaped, so its pid (the group id) cannot
    have been reused by an unrelated process.
    """
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError as exc:
        _log.debug("cannot kill process group %d: %r", proc.pid, exc)
        proc.kill()


def _read_capped(stdout: IO[bytes], deadline: float) -> bytes | None:
    """Read stdout until EOF or :data:`_MAX_OUTPUT_BYTES`; ``None`` past the deadline.

    One thread and a selector, not a blocking reader thread: a thread blocked in
    ``read`` holds the buffer's lock, so closing the pipe afterwards waited for
    whatever still held its other end, such as a reporter's orphaned child,
    and the timeout stopped bounding the call.
    """
    buf = bytearray()
    with selectors.DefaultSelector() as selector:
        selector.register(stdout, selectors.EVENT_READ)
        while len(buf) < _MAX_OUTPUT_BYTES:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                return None
            chunk = os.read(stdout.fileno(), _MAX_OUTPUT_BYTES - len(buf))
            if not chunk:
                break
            buf += chunk
    return bytes(buf)


def _run_capped(argv: list[str], timeout: float) -> tuple[int | None, bytes]:
    """Run ``argv`` in its own session, holding at most :data:`_MAX_OUTPUT_BYTES` of stdout.

    Returns:
        ``(exit code, stdout)``, or ``(None, b"")`` when it did not finish in time.

    Raises:
        OSError: The program could not be started.
    """
    deadline = time.monotonic() + timeout
    proc = subprocess.Popen(  # noqa: S603 — argv from shlex, no shell
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    stdout = cast("IO[bytes]", proc.stdout)  # stdout=PIPE always sets it
    try:
        raw = _read_capped(stdout, deadline)
        if raw is None:
            raise subprocess.TimeoutExpired(argv, timeout)
        code = proc.wait(timeout=max(0.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        _kill_session(proc)
        proc.wait()
        return None, b""
    finally:
        stdout.close()
    return code, raw


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
        code, raw = _run_capped(argv, timeout)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        _log.warning("host_health_command did not run: %r", exc)
        return HOST_UNKNOWN
    if code is None:
        _log.warning("host_health_command timed out after %ss", timeout)
        return HOST_UNKNOWN
    if code != 0:
        _log.warning("host_health_command exited %d", code)
        return HOST_UNKNOWN
    text = raw.decode("utf-8", errors="replace")
    line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    if not line:
        _log.warning("host_health_command printed nothing")
        return HOST_UNKNOWN
    return f"host: {line[:_MAX_CHARS]}"
