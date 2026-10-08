"""Text as a terminal shows it: the escape sequences in what a session writes, and the echo of a sent line.

Only text is read here. The terminal device itself is in `terminal`, and the waits that use this in `session`.
"""

from __future__ import annotations

import re

from . import log

ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

# Stray carriage returns, NULs and BELs at the start of the unread output: a prompt wait drops them, so
# `^` in a prompt regex means the start of a line. The lookahead needs the next character to be there and
# to be none of these or a line break, so a `\r` whose `\n` is still to come is never taken for a stray one.
STRAY_RE = re.compile(r"\A[\r\x00\x07]+(?=[^\r\n\x00\x07])")


# Bounds on the reading of an echo, so that output which is no echo costs little whatever its size:
# the readings of a line kept at a time (there is more than one only where the sent line repeats itself),
# and, per character of the sent line, the characters of a part between two `\r` and the parts of a line.
ECHO_READINGS = 8
ECHO_PART = 8
ECHO_PARTS = 2
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _shown(text: str) -> str:
    """What a terminal shows of `text`, less the blanks: a backspace steps back one cell, a character
    replaces the one in its cell, and the other control characters show nothing."""
    if "\b" not in text:
        return "".join(_CONTROL_RE.sub("", text).split())
    cells: list[str] = []
    col = 0
    for ch in text:
        if ch == "\b":
            col = max(col - 1, 0)
        elif ch in " \t" or not (ch < " " or "\x7f" <= ch <= "\x9f"):
            cells[col : col + 1] = [ch]
            col += 1
    return "".join("".join(cells).split())


def strip_echo(text: str, sent: str) -> str:
    """`text` without the echo of the line `sent` at its start, or as it is when it doesn't start with one.

    The echo is what a line editor writes for the line: the line itself, broken where it wraps. Blanks
    don't count. A line break continues it. A `\\r` returns to the start of a row, and the width of a row
    isn't known: what follows either continues the echo or writes again, unchanged, part of what the
    captured line already shows of it.
    """
    target = _shown(sent)
    if not target:
        return text
    limit = ECHO_PART * len(sent) + 1024
    lines = text.split("\n")
    ends = {0}  # how much of `target` the lines read so far may show
    for k, line in enumerate(lines):
        if not line:
            continue
        # readline horizontal-scroll mode (e.g. TERM=dumb) redraws only the
        # visible tail of a long line, prefixed with '<'
        tail = line.rpartition("\r")[2]
        if ends == {0} and len(tail) <= limit:
            tail = _shown(tail)
            if len(tail) > 1 and tail[0] == "<" and target.endswith(tail[1:]):
                return "\n".join(lines[k + 1 :])
        ends = _extend(target, ends, line, limit)
        if len(target) in ends:
            return "\n".join(lines[k + 1 :])
        if not ends:
            break
    return text


def _extend(target: str, starts: set[int], line: str, limit: int) -> set[int]:
    """How much of `target` is shown after the captured `line`, which began with one of `starts` shown.

    Empty when the line is no part of an echo of it, or is past the bounds: a part longer than `limit`,
    or more parts than an echo of `target` has rows.
    """
    readings = {(start, start) for start in starts}  # where the line began, and how much is shown
    rewrite = False
    count = 0
    for raw in line.split("\r"):
        if len(raw) > limit:
            return set()
        part = _shown(raw) if raw else ""
        if part:
            count += 1
            if count > ECHO_PARTS * len(target) + 2:
                return set()
            readings = _write(target, readings, part, rewrite)
            if not readings:
                return set()
        rewrite = True
    return {end for _, end in readings}


def _write(target: str, readings: set[tuple[int, int]], part: str, rewrite: bool) -> set[tuple[int, int]]:
    """The readings after `part` is written: at the end of what is shown, or, after a `\\r` (`rewrite`),
    over text of its line that is shown, where it is the same text."""
    size = len(part)
    new: set[tuple[int, int]] = set()
    for start, end in readings:
        if target.startswith(part, end):
            new.add((start, end + size))
        if rewrite:
            if target.find(part, start, end) != -1:
                new.add((start, end))
            # those that go past the end, the nearest to it first
            high = end - 1 + size
            for _ in range(ECHO_READINGS):
                at = target.rfind(part, max(start, end - size + 1), high)
                if at == -1:
                    break
                new.add((start, at + size))
                high = at + size - 1
    if len(new) > ECHO_READINGS:
        new = set(sorted(new, key=lambda r: r[1])[-ECHO_READINGS:])
    return new


def _mid_echo(read: str, sent: str | None) -> bool:
    """Whether `read`, all that came since the line `sent` went out, is its echo still being written:
    no line break yet, and what it shows is the start of the line or all of it."""
    if not sent or "\n" in read:
        return False
    target = _shown(sent)
    return bool(target) and max(_extend(target, {0}, read, ECHO_PART * len(sent) + 1024), default=0) > 0


# the start of a sequence ANSI_ESCAPE_RE removes, cut off by the end of a read: ESC, or an unfinished CSI
_PARTIAL_ESCAPE_RE = re.compile(r"\x1B(?:\[[0-?]*[ -/]*)?\Z")
# longer than any real sequence; a longer run after an ESC is written out rather than held
ESCAPE_HOLD = 64


class CleanWriter:
    """The operator echo: what is read from the session, without the escape sequences ANSI_ESCAPE_RE removes."""

    def __init__(self, stream):
        self._stream = stream
        self._held = ""

    def write(self, data):
        data = ANSI_ESCAPE_RE.sub("", self._held + data)
        self._held = ""
        # a sequence may arrive in two reads: keep its start back until the next one completes it
        m = _PARTIAL_ESCAPE_RE.search(data)
        if m and len(data) - m.start() <= ESCAPE_HOLD:
            data, self._held = data[: m.start()], data[m.start() :]
        if data:
            self._out(data)
            log.echoed(data)

    def _out(self, data: str = ""):
        """Write `data` and flush. A stream that nobody reads any more is no error of the session's:
        `log.lost` points it elsewhere and tells the run."""
        if log.stalled(self._stream):
            return
        try:
            if data:
                self._write(data)
            self._stream.flush()
        except (OSError, ValueError) as e:
            if not log.lost(self._stream, e):
                raise

    def _write(self, data: str):
        try:
            self._stream.write(data)
        except UnicodeEncodeError:
            # the stream's encoding can't represent a character the session sent (e.g. an ASCII stdout):
            # write its escape rather than fail the step that was only reading
            encoding = getattr(self._stream, "encoding", None) or "ascii"
            self._stream.write(data.encode(encoding, "backslashreplace").decode(encoding))

    def flush(self):
        # pexpect flushes after every read, so this must not release what is held
        self._out()

    def close(self):
        """Write out what is still held: nothing more will come to complete it. The stream stays open."""
        held, self._held = self._held, ""
        self._out(held)
        if held:
            log.echoed(held)
