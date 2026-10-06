"""`attach.prepare`: run the local script, and read back the environment a shell script leaves."""

from __future__ import annotations

import contextlib
import fcntl
import os
import re
import shlex
import signal
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from typing import IO

from . import log
from .types import RunError

# Stripped before looking for a shebang: whitespace and line breaks, a BOM,
# and the zero-width characters that copy and paste leave behind.
_JUNK = " \t\r\n﻿​⁠"

# The interpreters that can source the script, so that it runs as an rc script
SHELLS = ("sh", "bash", "dash", "ksh", "zsh")
# A shebang argument the script can be sourced with: options that `set` turns on just before the script
OPTIONS_RE = re.compile(r"-[aefuxC]+")
# The shell's own bookkeeping, never taken from the script: a `cd` in it doesn't move autobot
IGNORED = ("_", "SHLVL", "PWD", "OLDPWD")

# Writes the environment it is started with to the file descriptor it is given: `NAME=value` entries, each
# ended by a NUL, and one more NUL to end the dump. Bytes in, bytes out: nothing in a value can break an
# entry. The descriptor must still be the file autobot opened (its device and inode are the other two
# arguments): a script may have reused the number for a file of its own, which must not get the dump.
_DUMP = """\
import os,sys
f,d,i=map(int,sys.argv[1:4])
try:s=os.fstat(f)
except OSError:sys.exit(1)
if(s.st_dev,s.st_ino)!=(d,i):sys.exit(1)
os.write(f,b''.join(k+b'='+v+b'\\0' for k,v in os.environb.items())+b'\\0')
"""
# The descriptor the shell inherits the dump file on is at least this, out of the way of a script's own
DUMP_FD = 200
_DUMPED = "__autobot_dumped"


@dataclass(frozen=True)
class Changes:
    """What a `prepare` script did to its environment."""

    set: dict[str, str]
    unset: frozenset[str]


def _options(args: list[str]) -> str | None:
    """The `set` options a shebang's arguments turn on, e.g. `eu`; None for an argument that isn't one."""
    options = ""
    for arg in args:
        if arg in ("-", "--"):  # the end of the options: nothing to turn on
            continue
        if not OPTIONS_RE.fullmatch(arg):
            return None
        options += arg[1:]
    return options


def shell_of(shebang: str) -> tuple[list[str], str] | None:
    """The shell a shebang line names and the `set` options it gives it, e.g. `(["/bin/sh"], "eu")`.

    None when the script can't be sourced: the interpreter isn't a shell, or an argument is more than
    options that `set` can turn on before the script, such as `-r` or `--posix`.
    """
    words = shebang[2:].split()
    if not words:
        return None
    if os.path.basename(words[0]) == "env":
        # `env sh`, or `env -S sh -eu`: the kernel hands env the rest of the line as one argument
        rest = words[2:] if words[1:2] == ["-S"] else words[1:] if len(words) == 2 else []
        if not rest or os.path.basename(rest[0]) not in SHELLS:
            return None
        command, args = [words[0], rest[0]], rest[1:]
    elif os.path.basename(words[0]) in SHELLS:
        arg = shebang[2:].strip()[len(words[0]) :].strip()  # one argument, as the kernel passes it
        command, args = [words[0]], [arg] if arg else []
    else:
        return None
    options = _options(args)
    return None if options is None else (command, options)


def _wrapper(script: str, dump: tuple[int, int, int], options: str = "") -> str:
    """Shell code that sources `script` and writes its environment to `dump`, before and after.

    `dump` is the inherited descriptor of the dump file, and the file's device and inode. `options` are
    the shebang's: they are turned on with `set`, just before the script.

    The path is quoted into the code: the script may change the positional parameters. The dump after
    the script is also taken when the script calls `exit 0` (the EXIT trap), and the script's exit status
    is the shell's. A failing script leaves one dump, so nothing is read back from it.
    """
    q = shlex.quote
    dumper = f"{q(sys.executable)} -ISc {q(_DUMP)} {dump[0]} {dump[1]} {dump[2]}"
    trap = f'[ $? -ne 0 ] || [ "${{2-}}" = {_DUMPED} ] || {dumper}'
    return (
        f"{dumper} || exit 125\n"
        f"trap {q(trap)} EXIT\n"
        f"{options and f'set -{options}'}\n"
        f". {q(script)}\n"
        'set -- "$?"\n'
        f'[ "$1" -ne 0 ] || {{ {dumper} || :; set -- 0 {_DUMPED}; }}\n'
        'exit "$1"\n'
    )


def _dumps(data: bytes) -> list[dict[str, str]]:
    """The complete dumps of a dump file; one that is cut off isn't one."""
    if not data.endswith(b"\0"):
        return []
    dumps: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for entry in data[:-1].split(b"\0"):
        if entry:
            name, _, value = entry.partition(b"=")
            current[os.fsdecode(name)] = os.fsdecode(value)
        else:
            dumps.append(current)
            current = {}
    return dumps


def _changes(data: bytes) -> Changes | None:
    dumps = _dumps(data)
    if len(dumps) < 2:
        return None
    before, after = dumps[0], dumps[-1]
    return Changes(
        {k: v for k, v in after.items() if before.get(k) != v and k not in IGNORED},
        frozenset(k for k in before if k not in after and k not in IGNORED),
    )


class _Terminated(BaseException):
    """SIGTERM arrived while the script ran."""


@contextlib.contextmanager
def _terminable() -> Iterator[None]:
    """While `prepare` runs, SIGTERM unwinds like an interrupt, so the script's shell is killed and its
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


def _inherited(fd: int) -> int:
    """A copy of `fd` for the shell to inherit, on a high number where there is one."""
    try:
        return fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, DUMP_FD)
    except OSError:  # e.g. a limit on open files below that number
        return os.dup(fd)


def run(script: str, environ: dict[str, str] | None = None) -> Changes:
    """Run the rendered `prepare` script in `environ`, and return what it did to the environment.

    A script without a shebang, or whose shebang names a shell, is sourced by that shell. Any other
    script is executed as it is, and changes nothing: a process can't change its parent's environment.
    """
    script = script.lstrip(_JUNK)
    first, nl, rest = script.partition("\n")
    if script.startswith("#!"):
        first = first.removesuffix("\r")
        script = first + nl + rest
        shell, plain = shell_of(first), []
        log.say("prepare: running local script")
    else:
        shell, plain = (["/bin/sh"], ""), ["/bin/sh"]
        log.say("prepare: running local script (no shebang, using /bin/sh)")
    source = shell is not None and bool(sys.executable)  # the dump is written by this Python
    dump: IO[bytes]
    changes: Changes | None = Changes({}, frozenset())
    # the temp file is removed, whatever fails
    with _terminable(), contextlib.ExitStack() as files:
        f = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix="_autobot_", suffix=".sh", delete=False)
        tmp = f.name
        files.callback(os.unlink, tmp)
        with f:
            f.write(script)  # may fail, e.g. on a lone surrogate from a non-UTF-8 --arg
        os.chmod(tmp, 0o700)
        argv, fds = [*plain, tmp], ()
        if shell is not None and source:
            # the environment is read from a file without a name: nothing that holds its values can be
            # left behind. A file, not a pipe: a process the script leaves running can't keep it waiting
            dump = files.enter_context(tempfile.TemporaryFile())
            fd = _inherited(dump.fileno())
            files.callback(os.close, fd)
            stat = os.fstat(fd)
            argv, fds = [*shell[0], "-c", _wrapper(tmp, (fd, stat.st_dev, stat.st_ino), shell[1]), tmp], (fd,)
        try:
            result = subprocess.run(argv, check=False, env=environ, pass_fds=fds)
        except OSError as e:
            raise RunError(f"prepare script could not run ({first!r}): [Errno {e.errno}] {e.strerror}") from e
        if result.returncode != 0:
            raise RunError(f"prepare script failed with exit code {result.returncode}")
        if source:
            dump.seek(0)
            changes = _changes(dump.read())
    if changes is None:
        log.say("prepare: environment not read: the script ended its shell before the shell could report it", "warn")
        changes = Changes({}, frozenset())
    elif changes.set or changes.unset:
        log.say(f"prepare: environment: {len(changes.set)} set, {len(changes.unset)} unset", "detail")
    log.say("prepare: done", "ok")
    return changes
