from __future__ import annotations

import re
import signal
import sys
import time
from collections.abc import Callable

import pexpect

from .types import ANSI_ESCAPE_RE

DEFAULT_ENV = {"TERM": "dumb", "NO_COLOR": "1"}


class CommandError(RuntimeError):
    def __init__(self, message: str, output: str = ""):
        super().__init__(message)
        self.output = output


def _norm(text: str) -> str:
    return "".join(text.split())


def strip_echo(text: str, sent: str) -> str:
    target = _norm(sent)
    if not target:
        return text
    lines = text.split("\n")
    seen = ""
    for k, line in enumerate(lines):
        # readline horizontal-scroll mode (e.g. TERM=dumb) redraws only the
        # visible tail of a long line, prefixed with '<'
        tail = line.rsplit("\r", 1)[-1].lstrip()
        if not seen and tail.startswith("<"):
            shown = _norm(tail[1:])
            if shown and target.endswith(shown):
                return "\n".join(lines[k + 1 :])
        seen += _norm(line)
        if seen == target:
            return "\n".join(lines[k + 1 :])
        if not target.startswith(seen):
            break
    return text


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


class CleanWriter:
    def __init__(self, stream):
        self._stream = stream

    def write(self, data):
        data = ANSI_ESCAPE_RE.sub("", data)
        if data:
            self._stream.write(data)
            self._stream.flush()

    def flush(self):
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
            raise RuntimeError(f"prompt '{self.name}': no response available")
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
            raise RuntimeError(f"prompt '{self.name}': responses exhausted")
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
        except ValueError as e:
            raise ValueError(f"prompt '{self.name}': {e}") from e
        self._fired = True
        return value


class Session:
    def __init__(self, handlers: list[PromptHandler]):
        self._cld: pexpect.spawn | None = None
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
        self._patterns.append(pexpect.TIMEOUT)
        self._patterns.append(pexpect.EOF)
        # the prompt on screen still counts only if the new prompts take it for a shell prompt
        self._at_prompt = self._at_prompt and self._is_shell_prompt(self._prompt)

    def _is_shell_prompt(self, text: str) -> bool:
        """Whether get_prompt, reading only `text`, would stop at a shell prompt of the current handlers."""
        try:
            regexes = [re.compile(p, re.DOTALL) if isinstance(p, str) else p for p in self._patterns[:-2]]
        except re.error:
            return False  # the next get_prompt reports it
        while True:
            found = [(m.start(), i, m.end()) for i, r in enumerate(regexes) if (m := r.search(text))]
            if not found:
                return False
            _, i, end = min(found)
            if i > 1:
                return next(h for h in self._handlers if h.start <= i < h.end).is_return
            text = text[end:]  # a line break or escape sequence, consumed as get_prompt does

    def attach(self, spawn: str, env: dict[str, str] | None = None, timeout: float = 300):
        self._cld = pexpect.spawn(
            spawn,
            timeout=timeout,
            encoding="utf-8",
            codec_errors="replace",
            env=dict(DEFAULT_ENV) if env is None else env,
        )
        self._cld.logfile_read = CleanWriter(sys.stdout)
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
            self.detach()  # reaps the child, so its exit status is known
            raise EOFError(f"{e}{_exit_note(cld)}") from e.__cause__
        except BaseException:
            self.detach()
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

    def detach(self):
        if self._cld:
            self._cld.close()
            self._cld = None

    def get_prompt(
        self,
        timeout: float = 300,
        errors: list[str] | None = None,
        capture: bool = True,
    ) -> str:
        if self._at_prompt:
            return ""
        if not self._cld:
            raise RuntimeError("not attached")

        sent, self._sent = self._sent, None
        # a command that is still running would answer a solicit newline with a second prompt
        solicited, self._solicit = not self._solicit, True
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
            if i == 1:
                continue
            if i == len(self._patterns) - 1:
                raise EOFError(f"connection closed while waiting for {self._prompt_what()}")
            for h in self._handlers:
                if h.start <= i < h.end:
                    if h.is_return:
                        self._prompt = before + str(self._cld.after)
                        return self._finish(output, sent, errors, capture)
                    self._cld.sendline(h.respond(i - h.start))
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
