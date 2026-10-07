from __future__ import annotations

import errno
import fcntl
import os
import re
import select
import signal
import sys
import termios
import time
from collections.abc import Callable

import pexpect

from . import log
from .types import ANSI_ESCAPE_RE, RunError, ScriptError


DEFAULT_ENV = {"TERM": "dumb", "NO_COLOR": "1"}

# The window of the spawned pty. A line editor on a terminal that wraps clears the screen and writes the
# prompt again for a line that doesn't fit on the screen, so the screen is tall: 40,000 characters. Not
# taller: a full-screen program draws every row, and a device may take its terminal length from this
# window and accept only so much.
PTY_ROWS = 500
PTY_COLS = 80

# How long a prompt wait holds a shell prompt that may be one the line editor wrote again inside its echo:
# the prompt is taken for the prompt once nothing arrives for this long. Readline writes the prompt and the
# rest of the line in one write; the time is for a slow line between a device and its console server.
HELD_GRACE = 1.0


# What a write to a pty whose other side is gone fails with: the session is closed, as an EOF on a read says.
CLOSED_ERRNOS = (errno.EIO, errno.EPIPE, errno.ENXIO)
# A write the pty refuses although it is reported writable is tried again after this long.
SEND_RETRY = 0.01

# The bytes of a line, its line break included, that the Linux terminal takes in canonical mode
# (N_TTY_BUF_SIZE). The terminals of most devices a script reaches are Linux ones.
LINUX_CANON = 4096
MACOS_CANON = 1024


def canon_limit(fd: int) -> int | None:
    """The bytes of one line, its line break included, that the terminal `fd` takes in canonical mode.
    None where the platform's limit isn't known."""
    if sys.platform.startswith("linux"):
        return LINUX_CANON  # not fpathconf: it reports 255 for a pty, the POSIX constant, which the kernel doesn't go by
    if sys.platform == "darwin":
        try:
            return os.fpathconf(fd, "PC_MAX_CANON")
        except (OSError, ValueError):
            return MACOS_CANON
    return None


_CONTROL_KEYS = {"@": 0, "`": 0, "[": 27, "{": 27, "\\": 28, "|": 28, "]": 29, "}": 29, "^": 30, "~": 30, "_": 31, "?": 127}


def control_byte(char: str) -> bytes:
    """The control character of a key: Ctrl+A to Ctrl+Z in either case, and the punctuation keys. Empty for
    any other key."""
    char = char.lower()
    if len(char) == 1 and "a" <= char <= "z":
        return bytes([ord(char) - ord("a") + 1])
    return bytes([_CONTROL_KEYS[char]]) if char in _CONTROL_KEYS else b""


def run_environ() -> dict[str, str]:
    """The environment of a run: the process's own, on a plain terminal. An entry without a name is left
    out: no process can be started with one."""
    return {**{k: v for k, v in os.environ.items() if k}, **DEFAULT_ENV}


# Stray carriage returns, NULs and BELs at the start of the unread output: a prompt wait drops them, so
# `^` in a prompt regex means the start of a line. The lookahead needs the next character to be there and
# to be none of these or a line break, so a `\r` whose `\n` is still to come is never taken for a stray one.
STRAY_RE = re.compile(r"\A[\r\x00\x07]+(?=[^\r\n\x00\x07])")


class CommandError(RunError):
    def __init__(self, message: str, output: str = ""):
        super().__init__(message)
        self.output = output


class LineTooLong(RunError):
    """A line that the terminal of the spawned process would cut: it is not sent."""


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


def _exit_note(cld: pexpect.spawn) -> str:
    """The closed child's exit status, or the signal that ended it. SIGHUP is left out: closing the pty sends it."""
    if cld.exitstatus is not None:
        return f" (exit status {cld.exitstatus})"
    if cld.signalstatus is not None and cld.signalstatus != signal.SIGHUP:
        try:
            return f" (killed by {signal.Signals(cld.signalstatus).name})"
        except ValueError:
            return f" (killed by signal {cld.signalstatus})"
    return ""


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
            self._write(data)
            self._stream.flush()
            log.echoed(data)

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
        self._stream.flush()

    def close(self):
        """Write out what is still held: nothing more will come to complete it. The stream stays open."""
        held, self._held = self._held, ""
        if held:
            self._write(held)
            log.echoed(held)
        self._stream.flush()


class PromptHandler:
    def __init__(
        self,
        name: str,
        patterns: list[str],
        responses: list[list[str]],
        is_return: bool,
        slots: list[int | None] | None = None,
    ):
        self.name = name
        self.is_return = is_return
        self.patterns = patterns
        # slot k: a match regex of fields entry k; None: an expect regex (sendEach without fields)
        self.slots = slots if slots is not None else [None] * len(patterns)
        self.start = 0
        self.end = len(patterns)
        self._sets = responses
        self.reset()

    @property
    def is_fresh(self) -> bool:
        return not self._fired

    def reset(self):
        self._set = 0
        self._used: set[int] = set()
        self._fired = False

    def respond(self, i: int) -> str:
        if not self._sets:
            raise RunError(f"prompt '{self.name}': no response available")
        slot = self.slots[i]
        if slot is None:
            slot = self._next_unused()
            if slot is None:
                self._advance()
                slot = self._next_unused()
        elif slot in self._used:
            self._advance()
        assert slot is not None  # every sendEach set has an item at every slot
        self._used.add(slot)
        self._fired = True
        return self._sets[self._set][slot]

    def _next_unused(self) -> int | None:
        current = self._sets[self._set]
        return next((k for k in range(len(current)) if k not in self._used), None)

    def _advance(self):
        if self._set + 1 >= len(self._sets):
            raise RunError(f"prompt '{self.name}': responses exhausted")
        self._set += 1
        self._used = set()


class SimpleHandler(PromptHandler):
    """A prompt with one `send` template, rendered and sent on any match of its patterns, each time."""

    def __init__(self, name: str, patterns: list[str], send: str, render: Callable[[str], str]):
        super().__init__(name, patterns, [[send]], False)
        self._send = send
        self._render = render

    def respond(self, i: int) -> str:
        try:
            value = self._render(self._send)
        except ScriptError as e:
            raise ScriptError(f"prompt '{self.name}': {e}") from e
        self._fired = True
        return value


class Session:
    def __init__(self, handlers: list[PromptHandler]):
        self._cld: pexpect.spawn | None = None
        self._echo: CleanWriter | None = None
        self._at_prompt = False
        self._prompt = ""
        self._sent: str | None = None
        self._solicit = True
        self._warned = False  # of a long line, once
        # a prompt an `after` wait read and get_prompt would hold: what was read up to it, its line, the match
        self._held: tuple[str, str, str] | None = None
        self._ctx: dict[str, str] = {"before": "", "match": ""}
        self._set_handlers(handlers)

    @property
    def ctx(self) -> dict[str, str]:
        return self._ctx

    def _set_handlers(self, handlers: list[PromptHandler]):
        self._handlers = handlers
        self._patterns: list = [r"\r\n", ANSI_ESCAPE_RE]
        for h in handlers:
            h.start = len(self._patterns)
            h.end = h.start + len(h.patterns)
            self._patterns.extend(h.patterns)
        # after the prompts: on a tie (a prompt regex that itself starts at a leading `\r`) the prompt wins
        self._stray = len(self._patterns)
        self._patterns.append(STRAY_RE)
        self._patterns.append(pexpect.TIMEOUT)
        self._patterns.append(pexpect.EOF)
        # the prompt on screen still counts only if the new prompts take it for a shell prompt
        self._at_prompt = self._at_prompt and self._is_shell_prompt(self._prompt)

    def _is_shell_prompt(self, text: str, whole: bool = False, sent: str | None = None) -> bool:
        """Whether get_prompt, reading only `text`, would stop at a shell prompt of the current handlers.

        `whole`: and the prompt's match ends where `text` ends, so nothing was read past the prompt.
        `sent`: the line sent before `text` came, when no line break came before `text`.
        """
        return self._scan(text, whole, sent)[0]

    def _scan(self, text: str, whole: bool, sent: str | None) -> tuple[bool, str | None]:
        """`_is_shell_prompt`, and what get_prompt would have captured if `text` ends with a prompt it holds."""
        try:
            regexes = [re.compile(p, re.DOTALL) if isinstance(p, str) else p for p in self._patterns[:-2]]
        except re.error:
            return False, None  # the next get_prompt reports it
        read = ""
        held = False
        while True:
            found = [(m.start(), i, m.end()) for i, r in enumerate(regexes) if (m := r.search(text))]
            if not found:
                return False, read if held else None
            start, i, end = min(found)
            read += text[:start]
            held = False
            if 1 < i < self._stray:
                is_return = next(h for h in self._handlers if h.start <= i < h.end).is_return
                if not (is_return and _mid_echo(read, sent)):
                    return is_return and (not whole or end == len(text)), None
                read += "\r"  # a prompt that may be written again inside the echo: get_prompt holds it
                held = end == len(text)
            elif i == 0:
                read += "\n"
            text = text[end:]  # a line break, escape sequence or stray character, consumed as get_prompt does

    def _forget(self):
        """Drop what is known of a child: its prompt, the last line sent to it and whether to solicit."""
        self._at_prompt = False
        self._prompt = ""
        self._sent = None
        self._solicit = True
        self._held = None

    def attach(self, spawn: str, env: dict[str, str] | None = None, timeout: float = 300):
        # nothing of an earlier child applies to this one
        self._forget()
        self._ctx["before"] = self._ctx["match"] = ""
        self._cld = pexpect.spawn(
            spawn,
            timeout=timeout,
            encoding="utf-8",
            codec_errors="replace",
            env=run_environ() if env is None else env,
            dimensions=(PTY_ROWS, PTY_COLS),
        )
        self._echo = self._cld.logfile_read = CleanWriter(sys.stdout)
        cld = self._cld
        try:
            # zero-width: wait for output but leave it buffered for get_prompt
            self._expect(
                r"(?=.)",
                timeout,
                f"the first output from '{spawn}' (attach.timeout)",
                closed=f"before any output from '{spawn}'",
            )
        except EOFError as e:
            self.detach(failing=True)  # reaps the child, so its exit status is known
            raise EOFError(f"{e}{_exit_note(cld)}") from e.__cause__
        except BaseException:
            self.detach(failing=True)
            raise

    def _expect(self, patterns, timeout: float, what: str, closed: str | None = None) -> int:
        if not self._cld:
            raise RuntimeError("not attached")
        try:
            return self._cld.expect(patterns, timeout=timeout)
        except pexpect.TIMEOUT as e:
            raise TimeoutError(f"timed out after {timeout}s waiting for {what}") from e
        except pexpect.EOF as e:
            raise EOFError(f"connection closed {closed or f'while waiting for {what}'}") from e

    def detach(self, failing: bool = False):
        """Close the child and forget it, whether or not the close works.

        A close that fails raises, unless `failing`: an error is already on its way, so this one is only logged.
        """
        cld, self._cld = self._cld, None
        echo, self._echo = self._echo, None
        self._forget()  # there is no session to be at a prompt of
        if not cld:
            return
        try:
            try:
                cld.close()
            finally:
                if echo:
                    echo.close()
        except Exception as e:  # noqa: BLE001 - must not replace the error that is propagating
            if not failing:
                raise
            log.say(f"close error ({type(e).__name__}): {e}", "warn")

    def get_prompt(
        self,
        timeout: float = 300,
        errors: list[str] | None = None,
        capture: bool = True,
        solicit: bool = True,
    ) -> str:
        """Wait for a shell prompt. `solicit=False`: never press Return for one, whatever was sent last."""
        if not self._cld:
            raise RuntimeError("not attached")
        if self._at_prompt:
            return ""

        sent, self._sent = self._sent, None
        # a command that is still running would answer a solicit newline with a second prompt
        solicited, self._solicit = not (self._solicit and solicit), True
        for h in self._handlers:
            h.reset()
        output: list[str] = []
        # a held prompt: one that may be written again inside the echo. The size of `output` with it, the
        # prompt's line and its match
        held: tuple[int, str, str] | None = None
        carried, self._held = self._held, None
        if carried:
            output.append(carried[0])
            held = (1, carried[1], carried[2])
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"timed out after {timeout}s waiting for {self._prompt_what()}")
            if held and held[0] != len(output):
                held = None  # the echo went on
            unread = self._cld.buffer
            i = self._cld.expect(self._patterns, timeout=min(HELD_GRACE if held else 5, remaining))
            before = str(self._cld.before or "")
            if i == 0:
                output.append(before.rstrip("\r") + "\n")
                continue
            if i == len(self._patterns) - 2:
                # unmatched text stays buffered; it comes back with the next match
                if held:
                    if self._cld.buffer == unread:
                        # nothing came after it for a whole poll: it was the prompt
                        self._prompt = held[1]
                        return self._finish(output, sent, errors, capture, held[2])
                    continue  # no Return is pressed at a prompt that is held
                if not solicited and all(h.is_fresh for h in self._handlers):
                    self._put_line("", deadline - time.monotonic(), timeout)
                    solicited = True
                continue
            if before:
                output.append(before)
            if i == 1 or i == self._stray:
                continue
            if i == len(self._patterns) - 1:
                raise EOFError(f"connection closed while waiting for {self._prompt_what()}")
            for h in self._handlers:
                if h.start <= i < h.end:
                    if h.is_return:
                        if _mid_echo("".join(output), sent):
                            # the line editor wrote the prompt again while echoing the line (readline does
                            # for a line that ends at the right margin): the echo goes on from the row's start
                            output.append("\r")
                            held = (len(output), before + str(self._cld.after), str(self._cld.after))
                            break
                        self._prompt = before + str(self._cld.after)
                        return self._finish(output, sent, errors, capture, str(self._cld.after or ""))
                    self._put_line(h.respond(i - h.start), deadline - time.monotonic(), timeout)
                    log.say(f"prompt answered: {h.name}")  # never the response
                    break

    def _prompt_what(self) -> str:
        names = ", ".join(f"'{h.name}'" for h in self._handlers if h.is_return) or "none defined"
        return f"a shell prompt ({names})"

    def _finish(
        self, output: list[str], sent: str | None, errors: list[str] | None, capture: bool, match: str
    ) -> str:
        self._at_prompt = True
        text = "".join(output)
        text = text[: text.rfind("\n") + 1]
        if sent:
            text = strip_echo(text, sent)
        if capture:
            self._ctx["before"] = text
            self._ctx["match"] = match
        for pattern in errors or []:
            m = re.search(pattern, text, re.MULTILINE)
            if m:
                raise CommandError(f"command error: {m.group(0)}".strip(), text)
        return text

    def save_handlers(self) -> list[PromptHandler]:
        return self._handlers

    def restore_handlers(self, handlers: list[PromptHandler]):
        self._set_handlers(handlers)

    def reset_handlers(self):
        for h in self._handlers:
            h.reset()

    def expect(self, patterns: list, timeout: float = 300, what: str | None = None) -> int:
        if not self._cld:
            raise RuntimeError("not attached")
        self._held = None
        idx = self._expect(patterns, timeout, what or " or ".join(f"'{p}'" for p in patterns))
        self._ctx["before"] = str(self._cld.before or "")
        self._ctx["match"] = str(self._cld.after or "")
        # a match that ends at a shell prompt, with nothing read after it, has read that prompt: the
        # session is at it, as after a prompt wait
        _, broke, line = (self._ctx["before"] + self._ctx["match"]).rpartition("\r\n")
        if not self._at_prompt and not self._cld.buffer:
            at_prompt, read = self._scan(line, True, None if broke else self._sent)
            if at_prompt:
                self._at_prompt, self._prompt = True, line
            elif read is not None:
                # the prompt may be one written again inside the echo: the next prompt wait holds it
                self._held = (read, line, self._ctx["match"])
        return idx

    def sendline(self, line: str = "", *, solicit: bool = False, timeout: float = 300):
        """Send a line, within `timeout`. `solicit` lets the next prompt wait send its solicit newline: for
        a raw send (`line`, `return`), not for a command whose prompt the wait is for."""
        if not self._cld:
            raise RuntimeError("not attached")
        state = (self._at_prompt, self._sent, self._solicit, self._held)
        self._at_prompt = False
        self._sent = line
        self._solicit = solicit
        self._held = None
        try:
            self._put_line(line, timeout)
        except LineTooLong:
            # nothing was sent: the session is where it was
            self._at_prompt, self._sent, self._solicit, self._held = state
            raise

    def _put_line(self, line: str, timeout: float, of: float | None = None):
        """Write a line and its line break to the child. `of`: the timeout of the wait the send is part
        of, when `timeout` is what is left of it."""
        cld = self._cld
        assert cld
        deadline = time.monotonic() + timeout
        if cld.delaybeforesend is not None:
            time.sleep(cld.delaybeforesend)
        data = (line + cld.linesep).encode(cld.encoding, cld.codec_errors)
        self._check_length(data)
        self._write(data, deadline, timeout if of is None else of, "a line")

    def _check_length(self, data: bytes):
        """Refuse a line that the child's terminal would cut, and say so once when a terminal further on may.

        A terminal in canonical mode keeps a line for the program until its line break, and only so many
        bytes of it: it drops the rest without a sign, while it echoes them all. The mode is the one the
        terminal is in now, when the line is about to be typed on it.
        """
        cld = self._cld
        assert cld
        try:
            attrs = termios.tcgetattr(cld.child_fd)
        except (termios.error, OSError, ValueError):
            return  # no terminal to ask, e.g. a closed one: the write says so
        breaks = rb"[\r\n]" if attrs[0] & termios.ICRNL else rb"\n"
        longest = max(len(part) for part in re.split(breaks, data))
        limit = canon_limit(cld.child_fd) if attrs[3] & termios.ICANON else None
        if limit:
            if longest >= limit:
                raise LineTooLong(
                    f"line of {longest} bytes not sent: the terminal reads whole lines (canonical mode) and "
                    f"takes {limit - 1} bytes of one, so the last {longest - limit + 1} would be dropped without an error"
                )
        elif longest >= LINUX_CANON and not self._warned:
            self._warned = True
            log.say(
                f"long line: {longest} bytes; a far side that reads whole lines, with no line editor, keeps only "
                f"the first {LINUX_CANON - 1} bytes of one (the usual limit on Linux) and drops the rest without an error",
                "warn",
            )

    def _put_control(self, char: str, timeout: float):
        self._write(control_byte(char), time.monotonic() + timeout, timeout, "a control character")

    def _write(self, data: bytes, deadline: float, timeout: float, what: str):
        """Write `data` to the child, by `deadline`, reading what the child writes whenever the pty takes
        no more: a child that echoes stops reading once nobody reads its echo.

        What is read is read as a wait reads it, and is the start of what the next wait reads. A write
        that fails leaves the session at no prompt and after no line: the part that was written is on the
        child's input line, so the next prompt wait presses no Return, which would enter it.
        """
        cld = self._cld
        assert cld
        fd = cld.child_fd
        done, refused = 0, False
        try:
            if fd < 0:  # closed here, not by the child
                raise OSError(errno.EBADF, os.strerror(errno.EBADF))
            flags = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
            try:
                while done < len(data):
                    try:
                        done += os.write(fd, data[done:])
                        refused = False
                        continue
                    except BlockingIOError:
                        pass
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(
                            f"timed out after {timeout}s while sending {what} ({done} of {len(data)} bytes sent)"
                        )
                    if refused:
                        time.sleep(min(SEND_RETRY, remaining))
                    readable, writable, _ = select.select([fd], [fd], [], remaining)
                    if readable:
                        # into pexpect's buffer and the operator echo, like the output a wait reads
                        cld.expect(pexpect.TIMEOUT, timeout=0)
                    refused = bool(writable) and not readable
            finally:
                fcntl.fcntl(fd, fcntl.F_SETFL, flags)
        except BaseException as e:
            self._sent = None
            self._solicit = False
            if isinstance(e, pexpect.EOF) or (isinstance(e, OSError) and e.errno in CLOSED_ERRNOS):
                raise EOFError(f"connection closed while sending {what}") from e
            raise

    def check_rc(self, timeout: float = 300) -> int:
        if not self._cld:
            raise RuntimeError("not attached")

        self.sendline("echo __AUTOBOT_RC=$?", timeout=timeout)
        try:
            # the lookahead waits for what follows the digits: a code split across two reads is read whole
            self._expect([r"__AUTOBOT_RC=(\d+)(?=\D)"], timeout, "the exit code of the command (echo $?)")
        except BaseException:
            self._solicit = True  # like a prompt wait that timed out: the next wait follows no command
            raise

        rc = int(self._cld.match.group(1))  # type: ignore
        self.get_prompt(timeout=timeout, capture=False)
        return rc

    def sendcontrol(self, char: str, timeout: float = 300):
        if not self._cld:
            raise RuntimeError("not attached")
        self._at_prompt = False
        self._solicit = True
        self._held = None
        self._put_control(char, timeout)

    def sleep(self, seconds: float):
        if not self._cld:
            raise RuntimeError("not attached")
        self._expect(pexpect.TIMEOUT, seconds, "the end of a sleep")
