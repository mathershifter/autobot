"""`prepare`: source the local script in a shell, and read the environment that shell leaves."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import tempfile
from collections.abc import Iterator

from . import log
from .types import RunError

# Stripped before looking for a shebang: whitespace, a BOM, and the zero-width characters of copy and paste.
_JUNK = " \t\r\n﻿​⁠"
SHELLS = ("sh", "bash", "dash", "ksh", "zsh")
# The script's stdout is the shell's stderr; what it assigns is exported; the environment is written when the
# shell exits, wherever the script ends, to the shell's own stdout, kept as descriptor 9: zsh, and bash for an
# `exit` in a function, run the trap with the redirections of the command that was running.
_WRAPPER = 'exec 9>&1 >&2; set -a; trap "env -0 >&9 || :" EXIT; . "$0"'


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
    # stdout is a file, not a pipe: a process the script leaves running would keep a pipe open
    with _terminable(), tempfile.TemporaryFile() as out:
        f = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix="_autobot_", suffix=".sh", delete=False)
        try:  # the temp file is removed, whatever fails
            with f:
                f.write(script)  # may fail: a lone surrogate can't be encoded
            try:
                result = subprocess.run([*command, "-c", _WRAPPER, f.name], check=False, env=environ, stdout=out)
            except OSError as e:
                first = script.partition("\n")[0].rstrip()
                raise RunError(f"prepare script could not run ({first!r}): [Errno {e.errno}] {e.strerror}") from e
        finally:
            with contextlib.suppress(FileNotFoundError):  # the script may remove itself
                os.unlink(f.name)
        # read at its own offset: the file's is shared with a process the script left running
        data = os.pread(out.fileno(), os.fstat(out.fileno()).st_size, 0)
    if result.returncode != 0:
        raise RunError(f"prepare script failed with exit code {result.returncode}")
    if data.endswith(b"\0"):
        # decoded as `os.environ` is; a name is the text before the first `=`
        environ = dict(entry.partition("=")[::2] for entry in os.fsdecode(data).split("\0")[:-1])
    else:  # the script replaced the shell or its EXIT trap, or left no `env` to run
        log.say("prepare: its environment could not be read", "warn")
    log.say("prepare: done", "ok")
    return environ
