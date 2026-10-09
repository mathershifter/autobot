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
# The session's decoding writes one U+FFFD for the first bytes of a character that came without its
# last ones, however many they are, and a line editor counts a column for each. Readline, given only
# part of a character, writes those bytes and goes back over them with a backspace each (after any blanks
# it cleared the rest of the row with) before it writes the character, or more of its bytes.
_CUT_RE = re.compile("\ufffd( *)(\b+)(?=[\u0800-\U0010ffff])")


def _uncut(m: re.Match[str]) -> str:
    """The U+FFFD of a character's first 2 or 3 bytes, with as many backspaces as go back to its cell:
    one for it, where the editor wrote one for each of its bytes. The character that follows has more
    bytes than those, or the backspaces are read as they are."""
    over = len(m[2]) - len(m[1])
    return m[0][: 1 - over] if over in (2, 3) and over < _size(m.string[m.end()]) else m[0]


def _shown(text: str, cut: bool = False) -> str:
    """What a terminal shows of `text`, less the blanks: a backspace steps back one cell, a character
    replaces the one in its cell, and the other control characters show nothing. With `cut`, a U+FFFD
    is the first bytes of a character where the backspaces after it are those of its bytes."""
    if "\b" not in text:
        return "".join(_CONTROL_RE.sub("", text).split())
    if cut:
        text = _CUT_RE.sub(_uncut, text)
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
    captured line already shows of it. Text with a U+FFFD that is no echo with every backspace a cell
    is read once more, for characters that came in parts (`_CUT_RE`).
    """
    out = _strip(text, sent, False)
    if out is text and ("\ufffd" in text or "\ufffd" in sent):
        out = _strip(text, sent, True)
    return out


def _strip(text: str, sent: str, cut: bool) -> str:
    target = _shown(sent, cut)
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
        inside = False
        if ends == {0} and len(tail) <= limit:
            tail = _shown(tail, cut)
            if len(tail) > 1 and tail[0] == "<":
                if target.endswith(tail[1:]):
                    return "\n".join(lines[k + 1 :])
                inside = _inside(target, tail[1:])
        ends = _extend(target, ends, line, limit, cut)
        if len(target) in ends or (inside and not ends):
            return "\n".join(lines[k + 1 :])
        if not ends:
            break
    return text


def _inside(target: str, tail: str) -> bool:
    """Whether `tail`, what readline shows after the `<` of a line scrolled sideways, starts inside a
    character of `target` and is its end from there.

    Readline places the `<` by bytes, so it may stand for a byte inside a character. The bytes of that
    character after it are no character, and the session reads each as a U+FFFD: before the end of
    `target` that is shown, at most 3 of them, and fewer than the character before that end has bytes.
    """
    rest = tail.lstrip("\ufffd")
    cut = len(tail) - len(rest)
    at = len(target) - len(rest)
    return 0 < cut <= 3 and 0 < at < len(target) and target.endswith(rest) and cut < _size(target[at - 1])


def _size(ch: str) -> int:
    """The bytes `ch` is sent as: its UTF-8 encoding, and the `?` that goes out for a lone surrogate."""
    code = ord(ch)
    return 1 if code < 0x80 or 0xD800 <= code < 0xE000 else 2 if code < 0x800 else 3 if code < 0x10000 else 4


def _extend(target: str, starts: set[int], line: str, limit: int, cut: bool = False) -> set[int]:
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
        part = _shown(raw, cut) if raw else ""
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
    no line break yet, and what it shows is the start of the line or all of it. Read as `strip_echo` reads."""
    if not sent or "\n" in read:
        return False
    limit = ECHO_PART * len(sent) + 1024
    for cut in (False, True) if "\ufffd" in read or "\ufffd" in sent else (False,):
        target = _shown(sent, cut)
        if target and max(_extend(target, {0}, read, limit, cut), default=0) > 0:
            return True
    return False


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
        """Write `data` and flush. A failure ends the run (`log.OutputLost`), and no write is tried after it."""
        with log.writing(self._stream):
            if data:
                self._write(data)
            self._stream.flush()

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
        """Write out what is still held: nothing more will come to complete it. The stream stays open.
        Nothing is written to a stream that could not be written before: that has ended the run already."""
        held, self._held = self._held, ""
        if log.lost:  # nothing is written after the write that failed
            return
        self._out(held)
        if held:
            log.echoed(held)
