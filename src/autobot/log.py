"""Autobot's own messages: one console on stderr, a small palette and the `>> ` marker.

The session's output never comes through here: `session.CleanWriter` writes it to stdout as the device sent it.
"""

from __future__ import annotations

import os

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
}
ERROR = "bold red"
WARN = "bold yellow"


def _console() -> Console:
    # markup, highlighting and emoji codes off: a line shows commands, names and errors as they are.
    # soft_wrap: a log line is one line, whatever the terminal's width
    options: dict = {"stderr": True, "markup": False, "highlight": False, "emoji": False, "soft_wrap": True}
    if os.environ.get("NO_COLOR"):
        # no escape sequences at all: rich by itself would keep bold and dim
        options["color_system"] = None
    elif os.environ.get("FORCE_COLOR") and os.environ.get("TERM", "").lower() in ("dumb", "unknown"):
        options["color_system"] = "standard"  # rich forces the terminal, but not a color system for these
    return Console(**options)


console = _console()


def say(text: str, kind: str = "step") -> None:
    """Print one `>> ` line. `text` is printed as it is; only its label, up to the first `: `, is styled."""
    mark, label_style, rest_style = KINDS[kind]
    label, sep, rest = text.partition(": ")
    console.print(Text.assemble((MARK, mark), " ", (label + sep, label_style), (rest, rest_style)))


def error(head: str, detail: str = "", style: str = ERROR) -> None:
    """Print the CLI's verdict on a run: `head` stands out, `detail` is the message as it is."""
    console.print(Text.assemble((head, style), f": {detail}" if detail else ""))


def note(label: str, text: str) -> None:
    """Print a line that belongs to the verdict above it, e.g. `  at script.0 (cmd: true)`."""
    console.print(Text.assemble((f"  {label} ", "dim"), text))


def more(text: str) -> None:
    """Print further lines of a report as they are, unstyled."""
    console.print(Text(text))
