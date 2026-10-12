"""`prepare`: run the local script, and read the variables it prints."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import tempfile
from collections.abc import Collection, Iterator

from . import log
from .types import RunError, ScriptError

# Stripped before looking for a shebang: whitespace and line breaks, a BOM,
# and the zero-width characters that copy and paste leave behind.
_JUNK = " \t\r\n﻿​⁠"


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


def run(script: str, environ: dict[str, str] | None = None, declared: Collection[str] = ()) -> dict[str, str]:
    """Run the `prepare` script in `environ`, as its shebang says or under `/bin/sh`, and give the variables
    it sets: its standard output, lines of `NAME=value`, each for a name of `declared`."""
    script = script.lstrip(_JUNK)
    first, nl, rest = script.partition("\n")
    if script.startswith("#!"):
        first = first.removesuffix("\r")
        script = first + nl + rest
        shell = []
        log.say("prepare: running local script")
    else:
        shell = ["/bin/sh"]
        log.say("prepare: running local script (no shebang, using /bin/sh)")
    with _terminable():
        f = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix="_autobot_", suffix=".sh", delete=False)
        try:  # the temp file is removed, whatever fails
            with f:
                f.write(script)  # may fail: a lone surrogate can't be encoded
            os.chmod(f.name, 0o700)
            try:
                result = subprocess.run([*shell, f.name], check=False, env=environ, stdout=subprocess.PIPE)
            except OSError as e:
                raise RunError(f"prepare script could not run ({first!r}): [Errno {e.errno}] {e.strerror}") from e
        finally:
            with contextlib.suppress(FileNotFoundError):  # the script may remove itself
                os.unlink(f.name)
    if result.returncode != 0:
        raise RunError(f"prepare script failed with exit code {result.returncode}")
    printed: dict[str, str] = {}
    # read as UTF-8, and a byte that isn't is kept as `os.environ` keeps one
    output = result.stdout.decode("utf-8", "surrogateescape").replace("\r\n", "\n")
    for n, line in enumerate(output.split("\n"), 1):
        if not line:
            continue
        name, eq, value = line.partition("=")
        if not eq:  # reported without the line, and below without the value: either may be a secret
            raise ScriptError(f"prepare: line {n} of its output is not NAME=value")
        if name not in declared:
            raise ScriptError(f"prepare: line {n} of its output sets {name!r}, which the script's env section doesn't declare")
        printed[name] = value
    if printed:
        log.say(f"prepare: {len(printed)} set")
    log.say("prepare: done", "ok")
    return printed
