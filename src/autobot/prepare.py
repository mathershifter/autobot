"""`prepare`: source the local script in a shell, and read the environment that shell leaves."""

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

from . import log
from .types import RunError

# Stripped before looking for a shebang: whitespace, a BOM, and the zero-width characters of copy and paste.
_JUNK = " \t\r\n﻿​⁠"
SHELLS = ("sh", "bash", "dash", "ksh", "zsh")
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


def shell(script: str) -> list[str]:
    """The command of the shell a `prepare` script is for: the words of its shebang, `/bin/sh` without one.
    A shebang that names anything but a shell, behind `env` as its first word that is no option, is a ValueError."""
    script = script.lstrip(_JUNK)
    if not script.startswith("#!"):
        return ["/bin/sh"]
    words = script.partition("\n")[0][2:].split()
    rest = words[1:] if words and os.path.basename(words[0]) == "env" else words[:1]
    name = next((w for w in rest if not w.startswith("-")), "")
    if os.path.basename(name) not in SHELLS:
        raise ValueError(f"prepare is a shell script: its shebang names {name!r}")
    if {"-", "--"}.intersection(words[words.index(name) + 1 :]):
        raise ValueError("prepare is a shell script: '--' or '-' in its shebang ends the shell's options, and -c follows them")
    return words


class _Terminated(BaseException):
    """SIGTERM arrived while the script ran."""


@contextlib.contextmanager
def _terminable() -> Iterator[None]:
    """While `prepare` runs, SIGTERM unwinds like an interrupt, so the script's process is killed and its
    temp file removed; then autobot ends by the signal, as it would have without this.

    Only where SIGTERM has its default action and a handler can be set: in the main thread.
    """

    def unwind(signum: int, frame: object) -> None:
        raise _Terminated

    try:
        default = signal.getsignal(signal.SIGTERM) is signal.SIG_DFL
        if default:
            signal.signal(signal.SIGTERM, unwind)
    except ValueError:  # not the main thread
        default = False
    try:
        yield
    except _Terminated:
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        os.kill(os.getpid(), signal.SIGTERM)
        raise
    finally:
        if default:
            signal.signal(signal.SIGTERM, signal.SIG_DFL)


def run(script: str, environ: dict[str, str]) -> dict[str, str]:
    """Source the `prepare` script in its shell, in `environ`, and give the environment that shell leaves;
    `environ` itself when it left none to read."""
    command = shell(script)
    script = script.lstrip(_JUNK)
    log.say("prepare: running local script" + ("" if script.startswith("#!") else " (no shebang, using /bin/sh)"))
    # a file, not a pipe: a process the script leaves running would keep a pipe open; the shell's stdout is stderr
    with _terminable(), tempfile.TemporaryFile() as tmp, open(_dump(tmp.fileno()), "rb") as out:
        fd = out.fileno()
        shown = subprocess.DEVNULL if sys.stderr is None else 2  # no descriptor 2: it is closed, or some other file by now
        f = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix="_autobot_", suffix=".sh", delete=False)
        try:  # the temp file is removed, whatever fails
            with f:
                f.write(script)  # may fail: a lone surrogate can't be encoded
            try:
                result = subprocess.run([*command, "-c", _WRAPPER % fd, f.name], check=False, env=environ, stdout=shown, stderr=shown, pass_fds=[fd])
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
    if data.endswith(b"\0"):
        # decoded as `os.environ` is; a name is the text before the first `=`
        environ = dict(entry.partition("=")[::2] for entry in os.fsdecode(data).split("\0")[:-1])
    else:  # the script replaced the shell or its EXIT trap, or left no `env` to run
        log.say("prepare: its environment could not be read", "warn")
    log.say("prepare: done", "ok")
    return environ
