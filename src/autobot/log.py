"""Autobot's own messages: one console on stderr, a small palette and the `>> ` marker.

The session's output never comes through here: `screen.CleanWriter` writes it to stdout as the device sent it.
"""

from __future__ import annotations

import errno
import os
import select
import stat
import sys
from collections.abc import Callable

from rich.console import Console
from rich.text import Text

from . import signals
from .types import RunError

MARK = ">>"

# What a `>> ` line means -> the style of the marker, of the label (up to the first ": ") and of the rest.
# Bold, dim and the terminal's own red, green, yellow and blue only, so the theme keeps them readable on a
# dark and on a light background. The words carry the meaning; the styles only repeat it.
KINDS = {
    "step": ("bold blue", "bold", ""),  # what autobot does next
    "group": ("bold blue", "bold", "bold"),  # where a block or a breakout starts
    "ok": ("bold green", "green", "green"),  # something completed
    "detail": ("dim", "dim", "dim"),  # bookkeeping
    "warn": ("bold yellow", "yellow", ""),  # a failure the run goes on from
    "fail": ("bold red", "red", ""),  # the failure that ends a step
}
ERROR = "bold red"
OK = "green"
WARN = "bold yellow"
INDENT = "  "
MAX_INDENT = 8  # levels: deeper steps stay at this one, so a deep recursion doesn't run off the screen

depth = 0  # how deep the running step is nested in blocks and calls; the runner keeps it


def _styled() -> bool:
    """Whether the messages are styled: these three things decide, and nothing else rich would consult."""
    if os.environ.get("NO_COLOR"):
        return False  # no escape sequences at all: rich by itself would keep bold and dim
    if os.environ.get("FORCE_COLOR"):
        return True
    try:
        terminal = sys.stderr.isatty()
    except (AttributeError, ValueError):  # no stderr, or a closed one
        return False
    return terminal and os.environ.get("TERM", "").lower() not in ("dumb", "unknown")


def _console() -> Console:
    # Every message is printed as a `Text`, never as a string: that is what keeps rich from reading markup
    # or emoji codes in it and from highlighting numbers and quotes. soft_wrap: a log line is one line,
    # whatever the terminal's width
    return _make(_styled())


def _make(styled: bool) -> Console:
    return Console(stderr=True, soft_wrap=True, force_terminal=styled, color_system="standard" if styled else None)


console = _console()
_open_line = False  # the session echo on stdout stopped in the middle of a line, as after every prompt

class OutputError(RunError):
    """The run's output could not be written, for a reason other than a reader that has gone: a full
    disk, a file size limit."""


gone = ""  # the first stream that could no longer be written, `stdout` or `stderr`
failure = ""  # why, when that was an error and not a reader that had gone: `cannot write stdout: ...`
on_gone: Callable[[str], None] | None = None  # the run's: told when the session's echo can't be written


def _away(stream: object, why: str = "") -> None:
    """Point the stream's file descriptor to /dev/null, so that no later write to it fails or waits, and
    note it as the stream that was lost, if it is the first."""
    global gone, failure
    try:
        fd = stream.fileno()  # type: ignore[attr-defined]
        null = os.open(os.devnull, os.O_WRONLY)
        os.dup2(null, fd)
        os.close(null)
    except (AttributeError, OSError, ValueError):  # no descriptor to point elsewhere
        fd = -1
    if not gone:
        gone = "stdout" if stream is sys.stdout or fd == 1 else "stderr" if stream is sys.stderr or fd == 2 else "output"
        failure = why and f"cannot write {gone}: {why}"


def _regular(stream: object) -> bool:
    try:
        return stat.S_ISREG(os.fstat(stream.fileno()).st_mode)  # type: ignore[attr-defined]
    except (AttributeError, OSError, ValueError):
        return False


def lost(stream: object, error: BaseException, tell: bool = True) -> bool:
    """Whether `error`, raised by a write to `stream`, means that the stream can't be written any more:
    any error of the operating system but one that says to try again, and a write to a closed stream.
    Then the stream is pointed to /dev/null and noted (`gone`), and with `tell` the run is told
    (`on_gone`), which may raise.

    Nobody reads the stream any more when the error is EPIPE, a pipe whose reader has exited, or EIO on
    anything but a regular file, a terminal that has hung up. Any other error is a failure to write
    (`failure`): a full disk, a file size limit, EIO from the disk under a file."""
    if isinstance(error, OSError) and not isinstance(error, (BlockingIOError, InterruptedError)):
        reader = error.errno == errno.EPIPE or (error.errno == errno.EIO and not _regular(stream))
        _away(stream, "" if reader else f"[Errno {error.errno}] {error.strerror}")
    elif isinstance(error, ValueError) and getattr(stream, "closed", False):
        _away(stream)
    else:
        return False
    if tell and on_gone:
        on_gone(gone)
    return True


def stalled(stream: object) -> bool:
    """Whether a write to `stream` would wait, in a run that a signal is ending: nothing the operator
    doesn't read may hold up the breakouts then. Such a stream is pointed to /dev/null like one whose
    reader has gone, for the rest of the run. In a run that no signal is ending this is never so: a
    slow reader slows the run, as it does any program."""
    if not signals.taken():
        return False
    try:
        if select.select([], [stream.fileno()], [], 0)[1]:  # type: ignore[attr-defined]
            return False
    except (AttributeError, OSError, ValueError):  # no descriptor to ask: the write says what is wrong
        return False
    _away(stream)
    return True


def put(stream: object, data: str = "", tell: bool = False) -> None:
    """Write `data` to the operator's stream and flush it, unless that can't be done (`stalled`, `lost`)."""
    if stalled(stream):
        return
    try:
        if data:
            stream.write(data)  # type: ignore[attr-defined]
        stream.flush()  # type: ignore[attr-defined]
    except (OSError, ValueError) as e:
        if not lost(stream, e, tell):
            raise


def flush() -> None:
    """Flush stdout and stderr before the process ends from a signal, where that can be done."""
    for stream in (sys.stdout, sys.stderr):
        try:
            put(stream)
        except (AttributeError, OSError, ValueError):  # no stream, or one that can't be flushed
            pass


def echoed(data: str) -> None:
    """Note where the session echo left stdout: at the start of a line, or in the middle of one."""
    global _open_line
    if data:
        _open_line = not data.endswith("\n")


def _shared() -> bool:
    """Whether stdout and stderr are the same terminal, pipe or file, so their lines interleave."""
    try:
        out, err = os.fstat(sys.stdout.fileno()), os.fstat(sys.stderr.fileno())
    except (OSError, ValueError, AttributeError):  # a stream without a file descriptor
        return False
    return (out.st_dev, out.st_ino) == (err.st_dev, err.st_ino)


def _print(text: Text) -> None:
    global _open_line
    # rich renders into a buffer of its own: a signal that raised in the middle of that would leave the
    # console holding this message and every later one. Rendering can't block; the write can, and a
    # signal may end it
    with signals.uninterrupted(), console.capture() as capture:
        console.print(text)
    rendered = capture.get()
    if _open_line and _shared():
        # the message would continue the session's line, e.g. its prompt: start a new one. On stderr,
        # so stdout stays what the session sent
        rendered, _open_line = "\n" + rendered, False
    # a message that can't be written stops nothing here: the run stops before its next step
    put(console.file, rendered)


def renew() -> None:
    """A console of its own for what is printed from here on: the one in use may have been left in the
    middle of a message by an interrupt that this module's caller could not defer."""
    global console
    console = _make(console.color_system is not None)  # styled as the one it replaces


def say(text: str, kind: str = "step") -> None:
    """Print one `>> ` line, indented by the running step's depth. `text` is printed as it is; only its
    label, up to the first `: `, is styled."""
    mark, label_style, rest_style = KINDS[kind]
    label, sep, rest = text.partition(": ")
    _print(Text.assemble((MARK, mark), " " + INDENT * min(depth, MAX_INDENT), (label + sep, label_style), (rest, rest_style)))


def error(head: str, detail: str | None = None, style: str = ERROR) -> None:
    """Print the CLI's verdict on a run: `head` stands out, `detail` is the message as it is."""
    _print(Text.assemble((head, style), "" if detail is None else f": {detail}"))


def verdict(subject: str, word: str, style: str) -> None:
    """Print what the CLI found about one of several subjects, e.g. `a.yaml: valid`: `word` stands out."""
    _print(Text.assemble(f"{subject}: ", (word, style)))


def note(label: str, text: str) -> None:
    """Print a line that belongs to the verdict above it, e.g. `  at script.0 (cmd: true)`."""
    _print(Text.assemble((f"  {label} ", "dim"), text))


def hint(text: str) -> None:
    """Print a dim last line of a report, e.g. where to find more."""
    _print(Text(f"  {text}", "dim"))


def problem(where: str, what: str, tag: str) -> None:
    """Print one entry of a list of problems: where it is, what it is, and its type."""
    _print(Text.assemble("  ", (where, "bold"), f": {what} ", (f"[{tag}]", "dim")))


def more(text: str) -> None:
    """Print further lines of a report as they are, unstyled."""
    _print(Text(text))
