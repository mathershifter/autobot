"""Autobot's own messages: one console on stderr, a small palette and the `>> ` marker.

The session's output never comes through here: `screen.CleanWriter` writes it to stdout as the device sent it.
"""

from __future__ import annotations

import os
import sys

from rich.console import Console
from rich.text import Text

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


class OutputLost(BaseException):
    """A write of the run's own output failed: the session's echo on stdout, or a message on stderr. The
    run ends where it is, and no breakout runs. Not an `Exception`: nothing in a run is to carry on."""


class _Console(Console):
    def on_broken_pipe(self) -> None:
        raise  # the BrokenPipeError itself, for `_print`: rich would exit the process with status 1


def _console() -> Console:
    # Every message is printed as a `Text`, never as a string: that is what keeps rich from reading markup
    # or emoji codes in it and from highlighting numbers and quotes. soft_wrap: a log line is one line,
    # whatever the terminal's width
    styled = _styled()
    return _Console(stderr=True, soft_wrap=True, force_terminal=styled, color_system="standard" if styled else None)


console = _console()
_open_line = False  # the session echo on stdout stopped in the middle of a line, as after every prompt
lost = ""  # why the run's output can't be written, once a write has failed: `cannot write stdout: ...`


def failed(stream: object, error: BaseException) -> None:
    """A write to `stream`, stdout or stderr, raised `error`. If the stream can't be written (any error
    of the operating system, or a closed stream), the run ends: `OutputLost` is raised, once. The stream's
    file descriptor is pointed to /dev/null first, so that neither the rest of the run's end nor the
    interpreter's own last flush fails on it again. Any other error is raised as it is."""
    global lost
    if not isinstance(error, OSError) and not getattr(stream, "closed", False):
        raise error
    try:
        fd = stream.fileno()  # type: ignore[attr-defined]
        null = os.open(os.devnull, os.O_WRONLY)
        os.dup2(null, fd)
        os.close(null)
    except (AttributeError, OSError, ValueError):  # no descriptor to point elsewhere
        fd = -1
    if lost:
        return  # the run is ending from the first one
    name = "stdout" if stream is sys.stdout or fd == 1 else "stderr" if stream is sys.stderr or fd == 2 else "output"
    reason = f"[Errno {error.errno}] {error.strerror}" if isinstance(error, OSError) else "the stream is closed"
    lost = f"cannot write {name}: {reason}"
    raise OutputLost(lost) from error


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
    try:
        if _open_line and _shared():
            # the message would continue the session's line, e.g. its prompt: start a new one. On stderr,
            # so stdout stays what the session sent
            console.file.write("\n")
            _open_line = False
        console.print(text)
    except (OSError, ValueError) as e:
        failed(console.file, e)


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
