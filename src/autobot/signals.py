"""SIGINT, SIGTERM and SIGHUP while a run goes on: they unwind it, so that its cleanup runs, and the
process then ends from the signal, as it would have without this.

No signal is ever blocked here. A signal that must not end what is running is dropped by its handler,
and only for a time that is bounded (`GRACE`), so there is no state in which the process can't be ended.
"""

from __future__ import annotations

import contextlib
import os
import signal
import time
from collections.abc import Iterator

SIGNALS = (signal.SIGTERM, signal.SIGHUP)

# After a signal is taken, and from the start of each breakout of a run that a signal is ending, further
# signals are dropped for this many seconds. A terminal that goes away sends two SIGHUPs, a supervisor
# may repeat its SIGTERM, an operator presses Ctrl-C twice: none of those is to end the breakout that the
# first signal started before it has sent its logout. One that arrives later is taken, and ends the
# breakout that is running.
GRACE = 5.0


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


_taken: int | None = None  # the last signal that was taken: the run is ending from it
_quiet = 0.0  # until then (monotonic) a further signal is dropped
_closing = False  # the breakouts are over: a signal that is not dropped ends the process at once

# Statements that a signal must not raise in the middle of are running (`uninterrupted`), and the one
# that arrived meanwhile
_busy = 0
_deferred: BaseException | None = None


def _signal(signum: int, error: BaseException) -> None:
    global _taken, _quiet, _deferred
    now = time.monotonic()
    if _taken is not None:
        if now < _quiet:
            return  # the same request again: what it asks for is under way
        if _closing:
            _die(signum)
    _taken, _quiet = signum, now + GRACE
    if _busy:
        _deferred = error
    else:
        raise error


def _die(signum: int) -> None:
    """End the process from the signal, now: its default action."""
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)


def _unwind(signum: int, frame: object) -> None:
    _signal(signum, Terminated(signum))


def _interrupt(signum: int, frame: object) -> None:
    _signal(signum, KeyboardInterrupt())


class uninterrupted:
    """A few statements that leave something broken if a signal raises in the middle of them, and that
    can't block: the rendering of a message, which rich buffers. A signal that this module catches and
    that arrives meanwhile is taken, and raises when they are done, whether or not they raised too."""

    def __enter__(self) -> None:
        global _busy
        _busy += 1

    def __exit__(self, kind: object, error: BaseException | None, traceback: object) -> None:
        global _busy, _deferred
        _busy -= 1
        if not _busy and _deferred is not None:
            waiting, _deferred = _deferred, None
            raise waiting from error


def taken() -> bool:
    """Whether a signal or an interrupt was taken: the run is ending from it."""
    return _taken is not None


def breakout() -> None:
    """A breakout starts. In a run that a signal is ending, further signals are dropped for `GRACE`
    seconds from here: one that arrived before, or arrives now, is not what ends this breakout."""
    global _quiet
    if _taken is not None:
        _quiet = time.monotonic() + GRACE


def closing() -> None:
    """The breakouts are over, and what is left is short: the close of the session, the report. In a run
    that a signal is ending, a signal that is not dropped ends the process at once from here on."""
    global _closing
    _closing = True


def last(error: BaseException) -> None:
    """`error`, an interrupt or a `Terminated`, is what ends the process, and the caller is about to say
    so: it counts as taken, and one more signal either is dropped (within `GRACE` of it) or ends the
    process at once, but never raises again. Python's own SIGINT handler would."""
    global _taken, _quiet
    if _taken is None:
        _taken = error.signum if isinstance(error, Terminated) else signal.SIGINT
        _quiet = time.monotonic() + GRACE
    closing()
    with contextlib.suppress(ValueError):  # not the main thread
        if signal.getsignal(signal.SIGINT) is signal.default_int_handler:
            signal.signal(signal.SIGINT, _interrupt)


class caught:
    """While the block runs, a signal of `SIGNALS` raises `Terminated` in the main thread, and SIGINT
    raises `KeyboardInterrupt`, by the rule of this module: the first is taken at once, one that follows
    within `GRACE` seconds of it or of the start of a breakout is dropped, a later one is taken, and once
    the breakouts are over it ends the process.

    Only a signal that has its default action is caught (for SIGINT: Python's own handler), and only
    where a handler can be set: in the main thread. So one that is ignored (`nohup`) stays ignored, a
    handler of the program's own stays, and a block inside another catches nothing more. Gives the
    signals of `SIGNALS` this block catches.

    `held`: when a signal or an interrupt ends the block, the handlers stay in place, so that however
    many more arrive, the caller gets to report the one and to `end` from it. Otherwise the block puts
    back what it found, and what follows it starts afresh.

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
        if self._mine or self._interrupt:
            _reset()
        return self._mine

    def __exit__(self, kind: object, error: BaseException | None, traceback: object) -> None:
        if self._held and (_taken is not None or isinstance(error, (Terminated, KeyboardInterrupt))):
            return  # the process is ending: `last` and `end` are the caller's
        for signum in self._mine:
            signal.signal(signum, signal.SIG_DFL)
        if self._interrupt:
            signal.signal(signal.SIGINT, signal.default_int_handler)
        if self._mine or self._interrupt:
            _reset()


def _reset() -> None:
    global _taken, _quiet, _closing, _deferred
    _taken, _quiet, _closing, _deferred = None, 0.0, False, None


def end(signum: int) -> None:
    """End the process from the signal itself: its default action. Returns only if the signal didn't
    arrive."""
    from . import log  # log uses this module

    log.flush()
    _die(signum)


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
