from __future__ import annotations

import re
import signal
import sys
import time
from collections.abc import Callable

import pexpect

from . import log
from .types import ANSI_ESCAPE_RE, RunError, ScriptError


DEFAULT_ENV = {"TERM": "dumb", "NO_COLOR": "1"}


# Stray carriage returns, NULs and BELs at the start of the unread output: a prompt wait drops them, so
# `^` in a prompt regex means the start of a line. The lookahead needs the next character to be there and
# to be none of these or a line break, so a `\r` whose `\n` is still to come is never taken for a stray one.
STRAY_RE = re.compile(r"\A[\r\x00\x07]+(?=[^\r\n\x00\x07])")


class CommandError(RunError):
    def __init__(self, message: str, output: str = ""):
        super().__init__(message)
        self.output = output


def _shown(text: str) -> str:
    """What a terminal shows of `text`, less the blanks: a backspace steps back one cell, a character
    replaces the one in its cell, and the other control characters show nothing."""
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
    lines = text.split("\n")
    ends = {0}  # how much of `target` the lines read so far may show
    for k, line in enumerate(lines):
        parts = [_shown(p) for p in line.split("\r")]
        # readline horizontal-scroll mode (e.g. TERM=dumb) redraws only the
        # visible tail of a long line, prefixed with '<'
        tail = parts[-1]
        if ends == {0} and len(tail) > 1 and tail[0] == "<" and target.endswith(tail[1:]):
            return "\n".join(lines[k + 1 :])
        ends = {end for start in ends for end in _extend(target, start, parts)}
        if len(target) in ends:
            return "\n".join(lines[k + 1 :])
        if not ends:
            break
    return text


def _extend(target: str, start: int, parts: list[str]) -> set[int]:
    """How much of `target` is shown after a captured line, `parts` at its `\\r`s, that began with `start` shown."""
    ends = {start}
    for j, part in enumerate(parts):
        ends = {
            max(end, at + len(part))
            for end in ends
            for at in (range(start, end + 1) if j else (end,))
            if target.startswith(part, at)
        }
    return ends


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

    def _is_shell_prompt(self, text: str, whole: bool = False) -> bool:
        """Whether get_prompt, reading only `text`, would stop at a shell prompt of the current handlers.

        `whole`: and the prompt's match ends where `text` ends, so nothing was read past the prompt.
        """
        try:
            regexes = [re.compile(p, re.DOTALL) if isinstance(p, str) else p for p in self._patterns[:-2]]
        except re.error:
            return False  # the next get_prompt reports it
        while True:
            found = [(m.start(), i, m.end()) for i, r in enumerate(regexes) if (m := r.search(text))]
            if not found:
                return False
            _, i, end = min(found)
            if 1 < i < self._stray:
                is_return = next(h for h in self._handlers if h.start <= i < h.end).is_return
                return is_return and (not whole or end == len(text))
            text = text[end:]  # a line break, escape sequence or stray character, consumed as get_prompt does

    def _forget(self):
        """Drop what is known of a child: its prompt, the last line sent to it and whether to solicit."""
        self._at_prompt = False
        self._prompt = ""
        self._sent = None
        self._solicit = True

    def attach(self, spawn: str, env: dict[str, str] | None = None, timeout: float = 300):
        # nothing of an earlier child applies to this one
        self._forget()
        self._ctx["before"] = self._ctx["match"] = ""
        self._cld = pexpect.spawn(
            spawn,
            timeout=timeout,
            encoding="utf-8",
            codec_errors="replace",
            env=dict(DEFAULT_ENV) if env is None else env,
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
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"timed out after {timeout}s waiting for {self._prompt_what()}")
            i = self._cld.expect(self._patterns, timeout=min(5, remaining))
            before = str(self._cld.before or "")
            if i == 0:
                output.append(before.rstrip("\r") + "\n")
                continue
            if i == len(self._patterns) - 2:
                # unmatched text stays buffered; it comes back with the next match
                if not solicited and all(h.is_fresh for h in self._handlers):
                    self._cld.sendline("")
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
                        self._prompt = before + str(self._cld.after)
                        return self._finish(output, sent, errors, capture)
                    self._cld.sendline(h.respond(i - h.start))
                    log.say(f"prompt answered: {h.name}")  # never the response
                    break

    def _prompt_what(self) -> str:
        names = ", ".join(f"'{h.name}'" for h in self._handlers if h.is_return) or "none defined"
        return f"a shell prompt ({names})"

    def _finish(
        self, output: list[str], sent: str | None, errors: list[str] | None, capture: bool
    ) -> str:
        self._at_prompt = True
        text = "".join(output)
        text = text[: text.rfind("\n") + 1]
        if sent:
            text = strip_echo(text, sent)
        if capture:
            self._ctx["before"] = text
            self._ctx["match"] = str(self._cld.after or "") if self._cld else ""
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
        idx = self._expect(patterns, timeout, what or " or ".join(f"'{p}'" for p in patterns))
        self._ctx["before"] = str(self._cld.before or "")
        self._ctx["match"] = str(self._cld.after or "")
        # a match that ends at a shell prompt, with nothing read after it, has read that prompt: the
        # session is at it, as after a prompt wait
        line = (self._ctx["before"] + self._ctx["match"]).rpartition("\r\n")[2]
        if not self._at_prompt and not self._cld.buffer and self._is_shell_prompt(line, whole=True):
            self._at_prompt, self._prompt = True, line
        return idx

    def sendline(self, line: str = "", *, solicit: bool = False):
        """Send a line. `solicit` lets the next prompt wait send its solicit newline: for a raw
        send (`line`, `return`), not for a command whose prompt the wait is for."""
        if not self._cld:
            raise RuntimeError("not attached")
        self._at_prompt = False
        self._sent = line
        self._solicit = solicit
        self._cld.sendline(line)

    def check_rc(self, timeout: float = 300) -> int:
        if not self._cld:
            raise RuntimeError("not attached")

        self.sendline("echo __AUTOBOT_RC=$?")
        try:
            # the lookahead waits for what follows the digits: a code split across two reads is read whole
            self._expect([r"__AUTOBOT_RC=(\d+)(?=\D)"], timeout, "the exit code of the command (echo $?)")
        except BaseException:
            self._solicit = True  # like a prompt wait that timed out: the next wait follows no command
            raise

        rc = int(self._cld.match.group(1))  # type: ignore
        self.get_prompt(timeout=timeout, capture=False)
        return rc

    def sendcontrol(self, char: str):
        if not self._cld:
            raise RuntimeError("not attached")
        self._at_prompt = False
        self._solicit = True
        self._cld.sendcontrol(char)

    def sleep(self, seconds: float):
        if not self._cld:
            raise RuntimeError("not attached")
        self._expect(pexpect.TIMEOUT, seconds, "the end of a sleep")
