"""SIGTERM and SIGHUP while a run goes on: they unwind it like an interrupt, so that its cleanup runs, and
the process then ends from the signal, as it would have without this."""

from __future__ import annotations

import contextlib
import os
import signal
import sys
from collections.abc import Iterator

SIGNALS = (signal.SIGTERM, signal.SIGHUP)
# What is held back while a process that one of them is ending says so and ends: SIGINT is Python's own
HELD = (signal.SIGINT, *SIGNALS)


class Terminated(BaseException):
    """One of `SIGNALS` arrived while it was caught."""

    def __init__(self, signum: int):
        self.signum = signum
        self.name = signal.Signals(signum).name
        super().__init__(self.name)


class ReaderGone(Terminated):
    """Nobody reads the run's output any more (`stream` is `stdout` or `stderr`): the run stops as for a
    signal, and a process that ends from it ends from SIGPIPE, as one does whose reader has gone."""

    def __init__(self, stream: str):
        super().__init__(signal.SIGPIPE)
        self.stream = stream


def _unwind(signum: int, frame: object) -> None:
    raise Terminated(signum)


def hold() -> None:
    """Block the signals that end a run, for this thread: none of them raises anything from here on. For
    the last lines of a process that one of them is ending; one that arrives stays pending."""
    signal.pthread_sigmask(signal.SIG_BLOCK, HELD)


def release() -> None:
    """Undo `hold`."""
    signal.pthread_sigmask(signal.SIG_UNBLOCK, HELD)


@contextlib.contextmanager
def caught(held: bool = False) -> Iterator[list[int]]:
    """While the block runs, a signal of `SIGNALS` raises `Terminated` in the main thread.

    Only a signal that has its default action is caught, and only where a handler can be set: in the main
    thread. So one that is ignored (`nohup`) stays ignored, a handler of the program's own stays, and a
    block inside another catches nothing more. Gives the signals this block catches.

    `held`: when a signal or an interrupt ends the block, the block leaves with `hold` in effect, so
    that however many more arrive, the caller gets to report the one and to `end` from it.
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
        # one that arrives while the default actions are put back waits for the end of that, and for
        # the end of the process when one of them is what ends the block
        mask = signal.pthread_sigmask(signal.SIG_BLOCK, HELD if held else mine)
        ending = held and isinstance(sys.exception(), (Terminated, KeyboardInterrupt))
        try:
            for signum in mine:
                signal.signal(signum, signal.SIG_DFL)
        finally:
            if not ending:
                signal.pthread_sigmask(signal.SIG_SETMASK, mask)


def end(signum: int) -> None:
    """End the process from the signal itself: its default action. Returns only if the signal didn't
    arrive. Whatever else arrives meanwhile is held back, so that this signal is the one that ends it."""
    mask = signal.pthread_sigmask(signal.SIG_BLOCK, HELD)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (AttributeError, OSError, ValueError):  # no stream, or a closed one
            pass
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)
    signal.pthread_sigmask(signal.SIG_UNBLOCK, [signum])
    signal.pthread_sigmask(signal.SIG_SETMASK, mask - {signum})  # still here: as it was, less the signal


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
