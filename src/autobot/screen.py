"""Text as a terminal shows it: the escape sequences in what a session writes, and the echo of a sent line.

Only text is read here. The terminal device itself is in `terminal`, and the waits that use this in `session`.
"""

from __future__ import annotations

import re

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


def mid_echo(read: str, sent: str | None) -> bool:
    """Whether `read`, all that came since the line `sent` went out, is its echo still being written:
    no line break yet, and what it shows is the start of the line or all of it. Read as `strip_echo` reads.

    A prompt wait uses it for a shell prompt matched inside the echo (see `session`): with `sent` of
    `show version`, a `read` of `show ver` is the echo so far (True), and so is `show version` whole
    (True: a prompt written again at the end of the line may follow); `show version\\r\\n` has its line
    break (False), `sho ver` shows no start of the line (False), and with `sent` None nothing was sent
    (False)."""
    if not sent or "\n" in read:
        return False
    limit = ECHO_PART * len(sent) + 1024
    for cut in (False, True) if "\ufffd" in read or "\ufffd" in sent else (False,):
        target = _shown(sent, cut)
        if target and max(_extend(target, {0}, read, limit, cut), default=0) > 0:
            return True
    return False


# What the transcript holds back at most for an echo that hasn't ended: complete lines, plus one per
# 20 characters of the sent line for one that wraps row by row, and characters over the part limit.
ECHO_HOLD_LINES = 64
ECHO_HOLD_CHARS = 8192


def _classify(text: str, sent: str, cut: bool) -> tuple[str, str | set[int]]:
    """`_strip`, but it says what it found: ``("done", remainder)`` when the echo of `sent` is at the start
    of `text` and ends at a line break in it (remainder is what follows), ``("no", text)`` when the start
    is ruled out as the echo, and ``("maybe", ends)`` when `text` could still be an echo that isn't
    finished, `ends` being how much of the line it may show so far (`{0}`: nothing yet).

    `text` is whole lines only: the caller holds back the line that has no break yet, so a completion here
    is always at a line break.
    """
    target = _shown(sent, cut)
    if not target:
        return "no", text  # the sent line shows nothing: there is no echo to leave out
    limit = ECHO_PART * len(sent) + 1024
    lines = text.split("\n")
    ends = {0}
    for k, line in enumerate(lines):
        if not line:
            continue
        tail = line.rpartition("\r")[2]
        inside = False
        if ends == {0} and len(tail) <= limit:
            stail = _shown(tail, cut)
            if len(stail) > 1 and stail[0] == "<":
                if target.endswith(stail[1:]):
                    return "done", "\n".join(lines[k + 1 :])
                inside = _inside(target, stail[1:])
        ends = _extend(target, ends, line, limit, cut)
        if len(target) in ends or (inside and not ends):
            return "done", "\n".join(lines[k + 1 :])
        if not ends:
            return "no", text
    return "maybe", ends


def echo_split(buf: str, sent: str) -> tuple[str, bool]:
    """Read the start of `buf` as the echo of the line `sent`, for the operator transcript.

    Returns ``(emit, waiting)``. While `buf` may still be an unfinished echo, ``("", True)``: keep it back,
    nothing is written yet. Once decided, ``(emit, False)``: `emit` is `buf` without the echo when it is
    recognized (the echo ends at a line break), or `buf` unchanged when the start is no echo (echo disabled,
    text after it on its line, the forms `strip_echo` doesn't recognize), or when the hold has reached its
    bound (`ECHO_HOLD_LINES` lines, `ECHO_HOLD_CHARS` characters over the part limit). The caller writes
    `emit` and stops holding. `buf` is the text read since the line was sent, with escape sequences already
    removed, as the transcript writes it; `sent` is compared the same way.

    The whole lines are read as `strip_echo` reads them, with every variant it tries (a text with U+FFFD
    is read once more for characters that came in parts); the line that has no break yet is held while it
    shows nothing, or while what it shows goes on from where the whole lines left the echo.
    """
    if not buf:
        return "", True
    limit = ECHO_PART * len(sent) + 1024
    variants = (False, True) if ("\ufffd" in buf or "\ufffd" in sent) else (False,)
    nl = buf.rfind("\n")
    complete, partial = (buf[: nl + 1], buf[nl + 1 :]) if nl >= 0 else ("", buf)
    maybe = False
    for cut in variants:
        ends: set[int] = {0}
        if complete:
            status, value = _classify(complete, sent, cut)
            if status == "done":
                return str(value) + partial, False
            if status == "no":
                continue
            ends = value  # type: ignore[assignment]
        target = _shown(sent, cut)
        if not target:
            continue
        shown = _shown(partial, cut)
        # nothing shown yet (a CR, control characters), a line readline scrolled sideways, or text that
        # goes on from where the whole lines left the echo: still the echo, possibly
        if not shown or (ends == {0} and shown[0] == "<") or _extend(target, ends, partial, limit, cut):
            maybe = True
    if maybe and (len(buf) > limit + ECHO_HOLD_CHARS or buf.count("\n") > ECHO_HOLD_LINES + len(sent) // 20):
        return buf, False  # past the bound and still undecided: what is held is written, whatever it is
    return ("", True) if maybe else (buf, False)
