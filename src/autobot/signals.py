"""SIGTERM and SIGHUP while a run goes on: they unwind it like an interrupt, so that its cleanup runs, and
the process then ends from the signal, as it would have without this."""

from __future__ import annotations

import contextlib
import errno
import os
import signal
import stat
import sys
import termios
from collections.abc import Iterator

SIGNALS = (signal.SIGTERM, signal.SIGHUP)


class Terminated(BaseException):
    """One of `SIGNALS` arrived while it was caught."""

    def __init__(self, signum: int):
        self.signum = signum
        self.name = signal.Signals(signum).name
        super().__init__(self.name)


def _unwind(signum: int, frame: object) -> None:
    if signum == signal.SIGHUP:
        _leave_terminal()
    raise Terminated(signum)


def _leave_terminal() -> None:
    """Point stdout and stderr to /dev/null where they are a terminal that has hung up: every write to
    one fails, and the cleanup that runs from here on writes the session's output and its own messages."""
    for fd in (1, 2):
        try:
            if not stat.S_ISCHR(os.fstat(fd).st_mode):
                continue
            termios.tcgetattr(fd)
        except termios.error as e:
            if e.args[0] != errno.EIO:  # not a terminal: a device such as /dev/null
                continue
            with contextlib.suppress(OSError):
                null = os.open(os.devnull, os.O_WRONLY)
                os.dup2(null, fd)
                os.close(null)
        except OSError:  # no such descriptor
            pass


@contextlib.contextmanager
def caught() -> Iterator[list[int]]:
    """While the block runs, a signal of `SIGNALS` raises `Terminated` in the main thread.

    Only a signal that has its default action is caught, and only where a handler can be set: in the main
    thread. So one that is ignored (`nohup`) stays ignored, a handler of the program's own stays, and a
    block inside another catches nothing more. Gives the signals this block catches.
    """
    mine: list[int] = []
    try:
        for signum in SIGNALS:
            if signal.getsignal(signum) is signal.SIG_DFL:
                signal.signal(signum, _unwind)
                mine.append(signum)
    except ValueError:  # not the main thread
        pass
    try:
        yield mine
    finally:
        for signum in mine:
            signal.signal(signum, signal.SIG_DFL)


def end(signum: int) -> None:
    """End the process from the signal itself: its default action. Returns only if the signal is blocked
    or didn't arrive."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (AttributeError, OSError, ValueError):  # no stream, or a closed one
            pass
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)


@contextlib.contextmanager
def terminable() -> Iterator[None]:
    """`caught`, and the process ends from the signal once the block has unwound, unless a block further
    out catches the signal: that one ends the process, or its caller does."""
    with caught() as mine:
        try:
            yield
        except Terminated as e:
            if e.signum in mine:
                end(e.signum)
            raise
