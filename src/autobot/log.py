"""Autobot's own messages: one console on stderr, a small palette and the `>> ` marker.

The session's output never comes through here: `screen.CleanWriter` writes it to stdout as the device sent it.
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
from collections.abc import Iterator

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


def _console() -> Console:
    # Every message is printed as a `Text`, never as a string: that is what keeps rich from reading markup
    # or emoji codes in it and from highlighting numbers and quotes. soft_wrap: a log line is one line,
    # whatever the terminal's width. Rich renders into a buffer, and `_print` writes that to stderr: rich
    # itself ends the process (`SystemExit`) when a write fails with a broken pipe
    styled = _styled()
    return Console(
        file=io.StringIO(), soft_wrap=True, force_terminal=styled, color_system="standard" if styled else None
    )


console = _console()
_open_line = False  # the session echo on stdout stopped in the middle of a line, as after every prompt


class OutputLost(BaseException):
    """A write of the operator's output failed: the session's echo on stdout, or a message on stderr.
    The run ends at once: no breakout runs and nothing more is sent. It is no `Exception`, so that no
    best-effort cleanup takes it for a failure to go on from."""

    def __init__(self, stream: object, error: BaseException):
        name = "stdout" if stream is sys.stdout else "stderr" if stream is sys.stderr else "the output"
        if isinstance(error, OSError) and error.errno is not None:
            why = f"[Errno {error.errno}] {error.strerror}"
        else:
            why = str(error) or type(error).__name__
        super().__init__(f"cannot write {name}: {why}")


@contextlib.contextmanager
def writing(stream: object) -> Iterator[None]:
    """Around a write to the operator's `stream`, or a flush of it: a failure of any kind (a reader that
    has gone, a full disk, a size limit, a closed stream) is raised as `OutputLost`."""
    try:
        yield
    except OSError as e:
        raise OutputLost(stream, e) from e
    except ValueError as e:
        if not getattr(stream, "closed", False):  # not what a closed stream raises: a bug
            raise
        raise OutputLost(stream, e) from e


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


def open_line() -> bool:
    """Whether a message on stderr would continue a line of the session's, e.g. its prompt."""
    return _open_line and _shared()


def _print(text: Text) -> None:
    global _open_line
    console.print(text)
    buffer = console.file
    rendered = buffer.getvalue()
    buffer.seek(0)
    buffer.truncate()
    if open_line():
        # start a new line. On stderr, so stdout stays what the session sent
        rendered, _open_line = "\n" + rendered, False
    if sys.stderr is None:  # file descriptor 2 is closed: there is nowhere to say it
        return
    with writing(sys.stderr):
        sys.stderr.write(rendered)
        sys.stderr.flush()


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
