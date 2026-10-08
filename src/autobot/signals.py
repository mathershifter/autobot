"""SIGTERM and SIGHUP during a CLI run: the run is not cleaned up. One message says so, and the process
ends from the signal at once.

The handler does nothing that can wait or fail the run: it writes the message without blocking, ignores
whatever goes wrong with that, puts the signal's default action back and sends itself the signal. No
signal is ever blocked.
"""

from __future__ import annotations

import fcntl
import os
import signal
from collections.abc import Callable

SIGNALS = (signal.SIGTERM, signal.SIGHUP)
# What an end without breakouts leaves, for the message of a signal and of output that can't be written
UNKNOWN = (
    "the session was closed without running the breakouts; "
    "the device may be in an unknown state and may still be logged in"
)

where: Callable[[], str] | None = None  # the CLI's: the `at` line of the step that is running, or nothing


def _end(signum: int, frame: object) -> None:
    try:
        text = f"Interrupted ({signal.Signals(signum).name}): {UNKNOWN}\n"
        if where:
            text += where()
        _say(text.encode("utf-8", "replace"))
    except BaseException:  # noqa: BLE001 - nothing may keep the process from ending
        pass
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)


def _say(data: bytes) -> None:
    """Write to stderr what it takes right now, and nothing if it takes nothing: a full pipe, a terminal
    that is stopped."""
    flags = fcntl.fcntl(2, fcntl.F_GETFL)
    fcntl.fcntl(2, fcntl.F_SETFL, flags | os.O_NONBLOCK)
    try:
        os.write(2, data)
    finally:
        fcntl.fcntl(2, fcntl.F_SETFL, flags)


def install() -> list[int]:
    """Set the handler for each of `SIGNALS` that has its default action, where a handler can be set: in
    the main thread. One that is ignored (`nohup`) stays ignored. Gives the signals it was set for."""
    mine: list[int] = []
    try:
        for signum in SIGNALS:
            if signal.getsignal(signum) is signal.SIG_DFL:
                signal.signal(signum, _end)
                mine.append(signum)
    except ValueError:  # not the main thread
        pass
    return mine


def restore(mine: list[int]) -> None:
    """Put the default actions back for the signals `install` gave."""
    for signum in mine:
        signal.signal(signum, signal.SIG_DFL)
