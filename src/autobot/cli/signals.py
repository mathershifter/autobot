"""How the process ends from a signal: SIGTERM and SIGHUP while the session is open, and the SIGINT the CLI
sends itself after an interrupt (see SPEC, Errors while the script runs)."""

from __future__ import annotations

import contextlib
import os
import select
import signal
import sys
from collections.abc import Iterator
from typing import NoReturn

from ..log import ConsoleReporter
from .report import EXIT_INTERRUPTED, UNCLEAN


def discard_unwritten() -> bool:
    """Before the process exits: what a stream still holds and can't be written is discarded, by pointing
    its file descriptor to /dev/null, and the answer is whether there was any. The interpreter flushes
    the streams as it exits, and a flush that fails there prints a traceback of its own and turns the
    exit status into 120."""
    failed = False
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (AttributeError, OSError, ValueError) as e:
            failed = failed or isinstance(e, OSError)
            with contextlib.suppress(AttributeError, OSError, ValueError):
                null = os.open(os.devnull, os.O_WRONLY)
                os.dup2(null, stream.fileno())
                os.close(null)
                stream.flush()
    return failed


def interrupted() -> NoReturn:
    """End as a process that SIGINT killed: a shell reports status 130, and a loop around autobot stops,
    which it wouldn't for a process that exits with a status of its own."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (AttributeError, OSError, ValueError):  # no stream, or a closed one
            pass
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    os.kill(os.getpid(), signal.SIGINT)
    sys.exit(EXIT_INTERRUPTED)  # the signal is blocked or didn't arrive


@contextlib.contextmanager
def ended_by_signal(rep: ConsoleReporter) -> Iterator[None]:
    """While the session is open, SIGTERM and SIGHUP end the process at once, as they do by default,
    after one line on stderr that says what that leaves. Nothing is cleaned up and nothing more is sent.

    Only a signal that has its default action gets the handler (one that is ignored stays ignored, as
    under `nohup`), and only in the main thread, where a handler can be set.
    """
    try:
        fd = sys.stderr.fileno()
    except (AttributeError, OSError, ValueError):  # no stderr: file descriptor 2 may be anything
        fd = -1

    def ended(signum: int, frame: object) -> None:
        # one write, straight to the file descriptor, and only if it takes one now: nothing here waits,
        # raises or touches the run. A message that can't be written is not written
        try:
            # a background job that writes to its terminal is stopped by this signal (`stty tostop`)
            signal.signal(signal.SIGTTOU, signal.SIG_IGN)
            if select.select([], [fd], [], 0)[1]:
                start = "\n" if rep.open_line() else ""
                os.write(fd, f"{start}Interrupted ({signal.Signals(signum).name}): {UNCLEAN}\n".encode())
        except (OSError, ValueError):
            pass
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)
        os._exit(128 + signum)  # the signal didn't arrive

    replaced = []
    try:
        for sig in (signal.SIGTERM, signal.SIGHUP):
            if signal.getsignal(sig) is signal.SIG_DFL:
                signal.signal(sig, ended)
                replaced.append(sig)
    except ValueError:  # not the main thread
        pass
    try:
        yield
    finally:
        for sig in replaced:
            signal.signal(sig, signal.SIG_DFL)
