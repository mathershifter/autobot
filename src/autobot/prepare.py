"""`prepare`: source the local script in a shell, and read the environment that shell leaves.

Nothing is said here: the runner reports what `run` gives back (`PrepareResult`).
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import resource
import signal
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass

from .errors import RunError

# Stripped before looking for a shebang: whitespace, a BOM, and the zero-width characters of copy and paste.
_JUNK = " \t\r\n﻿​⁠"
# What the script assigns is exported; the environment is written when the shell exits, wherever the script
# ends, to the file of a descriptor that is the shell's for this alone, opened by its path when the trap runs:
# the script has its own descriptors (`exec 9>lock`), and zsh, and bash for an `exit` in a function, run the
# trap with the redirections of the command that was running. `>|`: the script may set noclobber (`set -C`).
_WRAPPER = 'set -a; trap "env -0 >|/dev/fd/%d || :" EXIT; . "$0"'
_FD = 173  # that descriptor is this one or the next that is free: a number no script picks for its own


def _dump(fd: int) -> int:
    """A descriptor of the file of `fd` for the shell's environment, `_FD` or above: a lower one is a script's own."""
    try:
        return fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, _FD)
    except OSError as e:
        limit = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
        raise RunError(f"prepare needs a file descriptor above {_FD - 1}, and the limit on open files is {limit}") from e


class _Terminated(BaseException):
    """SIGTERM or SIGHUP arrived while the script ran."""


@contextlib.contextmanager
def _terminable() -> Iterator[None]:
    """While `prepare` runs, SIGTERM and SIGHUP unwind like an interrupt, so the script's process is killed
    and its temp file removed; then autobot ends by the signal, as it would have without this.

    Only a signal that has its default action, and where a handler can be set: in the main thread.
    """
    got: list[int] = []

    def unwind(signum: int, frame: object) -> None:
        if not got:  # the first one ends the run: a second would unwind the cleanup
            got.append(signum)
            raise _Terminated

    try:
        with contextlib.suppress(ValueError):  # not the main thread
            for sig in (signal.SIGTERM, signal.SIGHUP):
                if signal.getsignal(sig) is signal.SIG_DFL:
                    signal.signal(sig, unwind)
        yield
    except _Terminated:
        signal.signal(got[0], signal.SIG_DFL)
        os.kill(os.getpid(), got[0])
        sys.exit(128 + got[0])  # the signal didn't end the process: it is PID 1
    finally:
        for sig in (signal.SIGTERM, signal.SIGHUP):
            if signal.getsignal(sig) is unwind:
                signal.signal(sig, signal.SIG_DFL)


@dataclass(frozen=True)
class PrepareResult:
    """What `run` gives back: the environment the shell left (`environ` as given when it left none to read),
    whether it could be read, and the shell that ran the script."""

    environ: dict[str, str]
    read: bool
    shell: list[str]


def shebang(script: str) -> list[str]:
    """The words of the script's shebang, whatever program they name; empty when it has none."""
    script = script.lstrip(_JUNK)
    return script.partition("\n")[0][2:].split() if script.startswith("#!") else []


def run(script: str, environ: dict[str, str]) -> PrepareResult:
    """Source the `prepare` script in its shell, in `environ`, and give the environment that shell leaves.
    The shell is the words of the shebang, whatever program they name, and `/bin/sh` without any."""
    script = script.lstrip(_JUNK)
    command = shebang(script)
    # a file, not a pipe: a process the script leaves running would keep a pipe open; the shell's stdout is stderr
    with _terminable(), tempfile.TemporaryFile() as tmp, open(_dump(tmp.fileno()), "rb") as out:
        fd = out.fileno()
        shown = subprocess.DEVNULL if sys.stderr is None else 2  # no descriptor 2: it is closed, or some other file by now
        f = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix="_autobot_", suffix=".sh", delete=False)
        try:  # the temp file is removed, whatever fails
            with f:
                f.write(script)  # may fail: a lone surrogate can't be encoded
            try:
                result = subprocess.run([*(command or ["/bin/sh"]), "-c", _WRAPPER % fd, f.name], check=False, env=environ, stdout=shown, stderr=shown, pass_fds=[fd])
            except OSError as e:
                first = script.partition("\n")[0].rstrip()
                raise RunError(f"prepare script could not run ({first!r}): [Errno {e.errno}] {e.strerror}") from e
        finally:
            with contextlib.suppress(FileNotFoundError):  # the script may remove itself
                os.unlink(f.name)
        # read at its own offset, and emptied: a process the script left running has the descriptor too
        data = os.pread(fd, os.fstat(fd).st_size, 0)
        os.ftruncate(fd, 0)
    if result.returncode != 0:
        raise RunError(f"prepare script failed with exit code {result.returncode}")
    read = data.endswith(b"\0")
    if read:
        # decoded as `os.environ` is; a name is the text before the first `=`
        environ = dict(entry.partition("=")[::2] for entry in os.fsdecode(data).split("\0")[:-1])
    # else the script replaced the shell or its EXIT trap, or left no `env` to run
    return PrepareResult(environ, read, command or ["/bin/sh"])
