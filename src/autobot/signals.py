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


# A signal or an interrupt was taken, and the next is held back until the run can take one again: while
# it unwinds, closes its session and reports, one more would raise at a point nothing is prepared for
_holding = False


def _take() -> bool:
    """The start of a handler: whether this signal is taken. Not while one is held: it was on its way
    before the block took effect. Taking one blocks the next."""
    global _holding
    if _holding:
        return False
    _holding = True
    signal.pthread_sigmask(signal.SIG_BLOCK, HELD)
    return True


def _unwind(signum: int, frame: object) -> None:
    if _take():
        raise Terminated(signum)


def _interrupt(signum: int, frame: object) -> None:
    if _take():
        raise KeyboardInterrupt


def holding() -> bool:
    """Whether a signal or an interrupt was taken and the next is held back."""
    return _holding


def hold() -> None:
    """Block the signals that end a run, for this thread: none of them raises anything from here on, and
    one that arrives stays pending. For the unwinding of a run that one of them is ending, and for the
    last lines of the process."""
    global _holding
    _holding = True
    signal.pthread_sigmask(signal.SIG_BLOCK, HELD)


def release() -> None:
    """Undo `hold`: a signal that is pending arrives now, and the next one is taken. For a part of the
    cleanup that a second signal may end, a breakout."""
    global _holding
    _holding = False
    signal.pthread_sigmask(signal.SIG_UNBLOCK, HELD)


class caught:
    """While the block runs, a signal of `SIGNALS` raises `Terminated` in the main thread, and SIGINT
    raises `KeyboardInterrupt` as ever. The one that is taken blocks the next (`hold`), so nothing
    raises a second time while the run unwinds, except where the run lets it (`release`).

    Only a signal that has its default action is caught (for SIGINT: Python's own handler), and only
    where a handler can be set: in the main thread. So one that is ignored (`nohup`) stays ignored, a
    handler of the program's own stays, and a block inside another catches nothing more. Gives the
    signals of `SIGNALS` this block catches.

    `held`: when a signal or an interrupt ends the block, the block leaves with the signals still
    blocked, so that however many more arrive, the caller gets to report the one and to `end` from it.
    Otherwise the block that set the handlers unblocks them as it leaves.

    A class, not a generator: a signal that raises before a generator is resumed leaves it to be
    finalized later, with the default actions put back at a moment when a signal must not end the process.
    """

    def __init__(self, held: bool = False):
        self._held = held
        self._mine: list[int] = []
        self._interrupt = False

    def __enter__(self) -> list[int]:
        try:
            for signum in SIGNALS:
                if signal.getsignal(signum) is signal.SIG_DFL:
                    signal.signal(signum, _unwind)
                    self._mine.append(signum)
            if signal.getsignal(signal.SIGINT) is signal.default_int_handler:
                signal.signal(signal.SIGINT, _interrupt)
                self._interrupt = True
        except ValueError:  # not the main thread
            pass
        return self._mine

    def __exit__(self, kind: object, error: BaseException | None, traceback: object) -> None:
        # one that arrives while the handlers are put back waits for the end of that, and for the end of
        # the process when one of them is what ends a `held` block
        mask = signal.pthread_sigmask(signal.SIG_BLOCK, HELD)
        ending = self._held and (_holding or isinstance(error, (Terminated, KeyboardInterrupt)))
        try:
            for signum in self._mine:
                signal.signal(signum, signal.SIG_DFL)
            if self._interrupt:
                signal.signal(signal.SIGINT, signal.default_int_handler)
        finally:
            if ending:
                hold()
            elif _holding and (self._mine or self._interrupt):
                release()
            else:
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
