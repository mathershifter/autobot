from __future__ import annotations

import re
import signal
import sys
import time
from collections.abc import Callable

import pexpect

from . import log
from .screen import ANSI_ESCAPE_RE, STRAY_RE, CleanWriter, _mid_echo, strip_echo
from .terminal import LINUX_CANON, PTY_COLS, PTY_ROWS, Pty, closed, control_byte, run_environ
from .types import RunError, ScriptError


# How long a prompt wait holds a shell prompt that may be one the line editor wrote again inside its echo:
# the prompt is taken for the prompt once nothing arrives for this long. Readline writes the prompt and the
# rest of the line in one write; the time is for a slow line between a device and its console server.
HELD_GRACE = 1.0


class CommandError(RunError):
    def __init__(self, message: str, output: str = ""):
        super().__init__(message)
        self.output = output


class LineTooLong(RunError):
    """A line that the terminal of the spawned process would cut: it is not sent."""


class PartialLine(RunError):
    """A line that would be added to the part of an earlier one that is typed at the far side: it is not sent."""


PARTIAL = (
    "part of a line that could not be sent whole is typed at the far side, and a Return would enter it; "
    "send a control character that drops it first (control: c)"
)


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
        # part of a line is typed at the far side, after a send that failed: no line is sent, since its
        # Return would enter that part, until a control character has been sent
        self._partial = False
        # a prompt got no answer (it was refused, or the prompt had none left): the prompt is still waiting
        # for one, and a solicit newline would be an empty answer, until something is sent
        self._unanswered = False
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
        self._partial = False
        self._unanswered = False

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
                if not (solicited or self._partial or self._unanswered) and all(h.is_fresh for h in self._handlers):
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
                    try:
                        self._put_line(h.respond(i - h.start), deadline - time.monotonic(), timeout)
                    except (LineTooLong, PartialLine) as e:
                        self._unanswered = True
                        raise type(e)(f"prompt '{h.name}': {e}") from None
                    except Exception:
                        self._unanswered = True  # no response left, or one that could not be rendered or sent
                        raise
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
        except (LineTooLong, PartialLine):
            # nothing was sent: the session is where it was
            self._at_prompt, self._sent, self._solicit, self._held = state
            raise
        self._unanswered = False

    def _put_line(self, line: str, timeout: float, of: float | None = None):
        """Write a line and its line break to the child. `of`: the timeout of the wait the send is part
        of, when `timeout` is what is left of it."""
        cld = self._cld
        assert cld
        if self._partial:
            raise PartialLine(f"line not sent: {PARTIAL}")
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
        line = Pty(cld.child_fd).longest(data)
        if line is None:
            return
        longest, limit = line
        if limit:
            if longest >= limit:
                # the terminal of a child that has exited still reports its mode: there is nobody to cut the line for
                if cld.flag_eof or not cld.isalive():
                    raise EOFError("connection closed while sending a line")
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
        data = control_byte(char)
        if self._partial and data in (b"\r", b"\n"):  # Ctrl+M and Ctrl+J are the Return key
            raise PartialLine(f"control character not sent: {PARTIAL}")
        self._write(data, time.monotonic() + timeout, timeout, "a control character")
        self._partial = False  # the far side has been told to drop what was typed

    def _write(self, data: bytes, deadline: float, timeout: float, what: str):
        """Write `data` to the child, by `deadline`, reading what the child writes whenever the pty takes
        no more: a child that echoes stops reading once nobody reads its echo.

        What is read is read as a wait reads it, and is the start of what the next wait reads. A write
        that fails leaves the session at no prompt and after no line. The part that was written is on the
        child's input line: until a control character is sent, no prompt wait presses Return and no line
        is sent, which would enter it.
        """
        cld = self._cld
        assert cld
        pty = Pty(cld.child_fd)
        try:
            # into pexpect's buffer and the operator echo, like the output a wait reads
            pty.write(data, deadline, timeout, what, lambda: cld.expect(pexpect.TIMEOUT, timeout=0))
        except BaseException as e:
            self._sent = None
            self._solicit = False
            self._partial = self._partial or pty.done > 0
            if isinstance(e, pexpect.EOF) or closed(e):
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

    def sendcontrol(self, char: str, *, timeout: float = 300):
        if not self._cld:
            raise RuntimeError("not attached")
        self._at_prompt = False
        self._solicit = True
        self._held = None
        self._put_control(char, timeout)
        self._unanswered = False

    def sleep(self, seconds: float):
        if not self._cld:
            raise RuntimeError("not attached")
        self._expect(pexpect.TIMEOUT, seconds, "the end of a sleep")
