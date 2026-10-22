from __future__ import annotations

import copy
import math
import re
import signal
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pexpect

from .errors import CommandError, LineTooLong, PartialLine, RunError, ScriptError
from .log import ConsoleReporter, OutputLost
from .screen import ANSI_ESCAPE_RE, STRAY_RE, _mid_echo, strip_echo
from .terminal import LINUX_CANON, PTY_COLS, PTY_ROWS, Pty, closed, control_byte, run_environ
from .transcript import CleanWriter

if TYPE_CHECKING:
    from .protocols import Reporter


# How long a prompt wait reads for a match before it looks at the time: after the first such period with no
# match it sends its one solicit newline, if it may (SPEC: the engine polls in 5-second intervals).
POLL = 5.0

# How long a prompt wait holds a shell prompt that may be one the line editor wrote again inside its echo:
# the prompt is taken for the prompt once nothing arrives for this long. Readline writes the prompt and the
# rest of the line in one write; the time is for a slow line between a device and its console server.
HELD_GRACE = 1.0

# How long after a control character a line is sent at the earliest. A shell or a CLI drops the line it is
# reading when it gets round to the interrupt, and what it has read of the next line by then goes with
# it: `logout` sent in the same breath as Ctrl-C arrives as `ogout`. Nothing shows when the far side is
# done, so the line waits this long; it is no guarantee on a far side that is slower than that.
CONTROL_SETTLE = 0.5


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
    # its responses are credentials (a `sendEach`): a run that sent one has logged in, or tried to
    credentials = True

    def __init__(
        self,
        name: str,
        patterns: list[str],
        responses: list[list[str]],
        is_return: bool,
        slots: list[int | None] | None = None,
        posix: bool = False,
    ):
        self.name = name
        self.is_return = is_return
        self.posix = posix  # a shell prompt of a POSIX shell: it has $?
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

    credentials = False  # an answer to a question: a confirmation, a pager

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


@dataclass
class HeldPrompt:
    """A shell prompt that a prompt wait holds: one that may be the prompt the line editor wrote again
    inside its echo (see HELD_GRACE). `read` is what an `after` wait read up to it, which the prompt wait
    that takes the prompt up starts its output with (empty for a prompt the wait read itself); `line` is
    the prompt's line as it was matched, `match` the match and `handler` the handler whose regex matched it."""

    read: str
    line: str
    match: str
    handler: PromptHandler


@dataclass
class PromptState:
    """What the session knows of the shell prompt on screen.

    `at_prompt` is True from a wait that ended at a shell prompt, or an `after` wait that read one, until
    anything is sent, the child is forgotten, or a swap of the handlers no longer takes the prompt for one.
    `text` is the line of the last shell prompt reached, as matched, and `handler` the one of the current
    handlers whose regex matched it, or None; it is read only while `at_prompt`. `held` is a prompt an
    `after` wait handed on to the next prompt wait: it is set only while `at_prompt` is False, and anything
    sent drops it."""

    at_prompt: bool = False
    text: str = ""
    handler: PromptHandler | None = None
    held: HeldPrompt | None = None


@dataclass
class SendState:
    """What the session knows of the last send.

    `line` is the last line sent, until the next prompt wait takes it to strip its echo, or a write fails.
    `solicit` is whether the next prompt wait may press Return for a prompt: False after a command (a
    sendline without `solicit`) or a failed write, until a prompt wait starts, a control character is sent
    or a `$?` check fails. `partial` is True only after a write of which the pty took some bytes but not all
    of them, until a control character is sent: no line is sent and no Return pressed while it is.
    `unanswered` is True only after a prompt whose answer could not be sent, until something is sent: no
    Return is pressed while it is. `control_at` is when the last control character was written (monotonic;
    -inf before any). `writes` counts the sends that got as far as the write, over the life of the session."""

    line: str | None = None
    solicit: bool = True
    partial: bool = False
    unanswered: bool = False
    control_at: float = -math.inf
    writes: int = 0


@dataclass
class LoginState:
    """The prompts answered with credentials since the process was spawned. `logins`: by name, each once, in
    the order they were first answered; `open`: those answered since a breakout last finished, with repeats;
    `pending`: credentials were sent and no shell prompt was reached since. It starts over at the next spawn
    and is kept when the child is closed."""

    logins: list[str] = field(default_factory=list)
    open: list[str] = field(default_factory=list)
    pending: bool = False


@dataclass
class ScanResult:
    """What a prompt wait, reading only a given text, would make of it. `at_prompt`: it would stop at a
    shell prompt; `read`: what it would have captured up to a prompt it holds at the end of the text, None
    when it holds none; `handler`: the handler of the shell prompt it would stop at, or of the one it holds."""

    at_prompt: bool = False
    read: str | None = None
    handler: PromptHandler | None = None


@dataclass
class PromptWait:
    """One prompt wait (`get_prompt`) while it reads. `sent` is the line sent before it, whose echo is
    stripped from the output; `solicited`: the one solicit newline was sent, or must not be; `deadline` and
    `timeout` are the wait's; `output` is what was read so far, as captured; `held` is a shell prompt that
    may be inside the echo, and `held_at` the size of `output` when it was held: once more is read, the
    echo went on and the prompt is dropped."""

    sent: str | None
    solicited: bool
    deadline: float
    timeout: float
    output: list[str] = field(default_factory=list)
    held: HeldPrompt | None = None
    held_at: int = 0


class PatternTable:
    """The patterns of a prompt wait, in the order pexpect gets them: the line break, an escape sequence,
    the regexes of each handler in turn (a handler's `start` to `end`), a run of stray characters, then
    TIMEOUT and EOF. pexpect takes the earliest match, and the first of the list on a tie: a prompt regex
    that itself starts at a leading `\\r` beats the stray run. The regexes are compiled once, as pexpect
    compiles a string (`re.DOTALL`). One that doesn't compile leaves them all as strings, so that the next
    prompt wait reports the error (SPEC: a prompt regex that isn't valid), and a scan finds nothing."""

    def __init__(self, handlers: list[PromptHandler]):
        self._handlers = handlers
        patterns: list[str | re.Pattern[str]] = [r"\r\n", ANSI_ESCAPE_RE]
        self.line_break = 0
        self.escape = 1
        for h in handlers:
            h.start = len(patterns)
            h.end = h.start + len(h.patterns)
            patterns.extend(h.patterns)
        self.stray = len(patterns)
        patterns.append(STRAY_RE)
        self.timeout = len(patterns)
        self.eof = len(patterns) + 1
        self.regexes: list[re.Pattern[str]] | None
        try:
            self.regexes = [re.compile(p, re.DOTALL) if isinstance(p, str) else p for p in patterns]
        except re.error:
            self.regexes = None
        # what a wait hands to pexpect
        self.patterns: list = [*(patterns if self.regexes is None else self.regexes), pexpect.TIMEOUT, pexpect.EOF]

    def handler_for(self, i: int) -> PromptHandler | None:
        """The handler whose regex is at index `i`, or None for a pattern of the engine's own."""
        return next((h for h in self._handlers if h.start <= i < h.end), None)


class Session:
    def __init__(self, handlers: list[PromptHandler], *, reporter: Reporter | None = None):
        """`reporter`: where the session says what it does, and what keeps the run's lost output; the
        runner's, or one of the session's own on stderr."""
        self._reporter: Reporter = reporter if reporter is not None else ConsoleReporter()
        self._cld: pexpect.spawn | None = None
        self._echo: CleanWriter | None = None
        self._prompt = PromptState()
        self._send = SendState()
        self._login = LoginState()
        self._warned = False  # of a long line, once
        self._ctx: dict[str, str] = {"before": "", "match": ""}
        self._set_handlers(handlers)

    @property
    def ctx(self) -> dict[str, str]:
        return self._ctx

    @property
    def reporter(self) -> Reporter:
        return self._reporter

    @property
    def logins(self) -> tuple[str, ...]:
        """The prompts that were answered with credentials since the process was spawned, by name. They
        are still known once the session is closed."""
        return tuple(self._login.logins)

    @property
    def logins_open(self) -> tuple[str, ...]:
        """The prompts answered with credentials since a breakout last finished (`logins_covered`), or
        since the spawn. By name, each once, in the order they were first answered."""
        return tuple(dict.fromkeys(self._login.open))

    def logins_covered(self):
        """A breakout has finished: it may have logged out of what was logged in to before it."""
        self._login.open = []

    @property
    def login_pending(self) -> bool:
        """Whether credentials were sent and no shell prompt was reached after them: a login that was
        refused, or one that the run didn't see the end of."""
        return self._login.pending

    def _set_handlers(self, handlers: list[PromptHandler]):
        """Swap the handlers: the patterns are rebuilt, and the prompt on screen and a held prompt, judged
        by the old handlers, are judged again by the new ones. A swap never puts the session at a prompt."""
        self._handlers = handlers
        self._table = PatternTable(handlers)
        self._rejudge_prompt()
        self._rejudge_held()

    def _rejudge_prompt(self):
        """The prompt on screen still counts only if the new handlers take it for a shell prompt. Reads
        `_prompt.text`, `_prompt.at_prompt` and the unread rest of the prompt's line; sets `_prompt.at_prompt`
        (never to True) and `_prompt.handler`, which is kept when it is among the new handlers."""
        found = self._scan(self._prompt.text)
        self._prompt.at_prompt = self._prompt.at_prompt and found.at_prompt
        if any(h is self._prompt.handler for h in self._handlers):
            return
        # the handler that matched the prompt is gone, and no wait matched the new regexes: the handler
        # is the one a wait would stop at in the prompt's line. The prompt's text ends where its match
        # ended, so a regex that ends with `$` may match it where it does not match the output: it is
        # read with the rest of the line, which is unread. Only when nothing matches that is the text
        # read alone, as it is for `at_prompt`
        rest = (self._cld.buffer if self._cld else "").partition("\r\n")[0]
        line = self._scan(self._prompt.text + rest).handler if rest else None
        self._prompt.handler = (line or found.handler) if found.at_prompt else None

    def _rejudge_held(self):
        """A prompt an `after` wait handed on was judged by the old handlers, and is judged again as it was,
        against the line sent before it. Reads `_prompt.held` and `_send.line`; sets `_prompt.held`: the
        prompt stays held, for its new handler and with what the new handlers read up to it, if its line
        still ends with a shell prompt, and is dropped otherwise."""
        held = self._prompt.held
        if not held:
            return
        found = self._scan(held.line, whole=True, sent=self._send.line)
        ends = found.at_prompt or found.read is not None
        if found.handler and ends:
            read = held.read if found.read is None else found.read
            self._prompt.held = HeldPrompt(read, held.line, held.match, found.handler)
        else:
            self._prompt.held = None

    @property
    def posix(self) -> bool:
        """Whether the session is at a shell prompt of a `posix` prompt of the current handlers: the shell
        there has `$?`. False once anything is sent, until the next wait that ends at such a prompt."""
        h = self._shell()
        return bool(h and h.posix)

    def _shell(self) -> PromptHandler | None:
        """The handler whose regex matched the shell prompt the session is at, if it is at one."""
        return self._prompt.handler if self._prompt.at_prompt else None

    def _scan(self, text: str, whole: bool = False, sent: str | None = None) -> ScanResult:
        """Read `text` as a prompt wait would, with the current handlers, without reading from the child.

        `whole`: a shell prompt counts only if its match ends where `text` ends, so nothing was read past
        it. `sent`: the line sent before `text` came, when no line break came before `text`: a shell prompt
        inside its echo is held, not stopped at.
        """
        table = self._table
        if table.regexes is None:
            return ScanResult()  # the next prompt wait reports the regex
        read = ""
        held = False
        holder: PromptHandler | None = None
        while True:
            found = [(m.start(), i, m.end()) for i, r in enumerate(table.regexes) if (m := r.search(text))]
            if not found:
                return ScanResult(False, read if held else None, holder if held else None)
            start, i, end = min(found)
            read += text[:start]
            held = False
            h = table.handler_for(i)
            if h is not None:
                if not (h.is_return and _mid_echo(read, sent)):
                    at_prompt = h.is_return and (not whole or end == len(text))
                    return ScanResult(at_prompt, None, h if h.is_return else None)
                read += "\r"  # a prompt that may be written again inside the echo: get_prompt holds it
                held, holder = end == len(text), h
            elif i == table.line_break:
                read += "\n"
            text = text[end:]  # a line break, escape sequence or stray character, consumed as get_prompt does

    def _forget(self):
        """Drop what is known of a child: its prompt, the last line sent to it and whether to solicit."""
        self._prompt = PromptState()
        self._send = SendState(writes=self._send.writes)  # the count is of the session, not of a child

    def attach(self, spawn: str, env: dict[str, str] | None = None, timeout: float = 300):
        # nothing of an earlier child applies to this one
        self._forget()
        self._ctx["before"] = self._ctx["match"] = ""
        self._login = LoginState()
        self._cld = pexpect.spawn(
            spawn,
            timeout=timeout,
            encoding="utf-8",
            codec_errors="replace",
            env=run_environ() if env is None else env,
            dimensions=(PTY_ROWS, PTY_COLS),
        )
        self._echo = self._cld.logfile_read = CleanWriter(sys.stdout, self._reporter)
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
        except (Exception, OutputLost) as e:  # noqa: BLE001 - must not replace the error that is propagating
            if not failing:
                raise
            if isinstance(e, OutputLost):
                self._reporter.clear()  # found only now, by a run that another error is ending: it ends nothing
            self._reporter.say(f"close error ({type(e).__name__}): {e}", "warn")

    def get_prompt(
        self,
        timeout: float = 300,
        errors: Sequence[str] | None = None,
        capture: bool = True,
        solicit: bool = True,
    ) -> str:
        """Wait for a shell prompt. `solicit=False`: never press Return for one, whatever was sent last.

        Each poll ends at the earliest match among the table's patterns. A line break, an escape sequence
        and stray characters are consumed, the text before them captured; a prompt with responses is
        answered; a shell prompt ends the wait, or is held when it may be inside the echo of the line sent;
        a poll with no match takes a held prompt for the prompt, or sends the one solicit newline.
        """
        if not self._cld:
            raise RuntimeError("not attached")
        if self._prompt.at_prompt:
            return ""
        wait = self._begin_wait(timeout, solicit)
        table = self._table
        while True:
            remaining = wait.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"timed out after {timeout}s waiting for {self._prompt_what()}")
            if wait.held and wait.held_at != len(wait.output):
                wait.held = None  # the echo went on
            unread = self._cld.buffer
            i = self._cld.expect(table.patterns, timeout=min(HELD_GRACE if wait.held else POLL, remaining))
            before = str(self._cld.before or "")
            if i == table.line_break:
                wait.output.append(before.rstrip("\r") + "\n")
                continue
            if i == table.timeout:
                match = self._on_timeout(wait, unread)
                if match is not None:
                    return self._finish(wait, match, errors, capture)
                continue
            if before:
                wait.output.append(before)
            if i == table.eof:
                raise EOFError(f"connection closed while waiting for {self._prompt_what()}")
            h = table.handler_for(i)  # None: an escape sequence or stray characters, consumed
            if h is None:
                continue
            if h.is_return:
                match = self._on_shell_prompt(wait, h, before)
                if match is not None:
                    return self._finish(wait, match, errors, capture)
            else:
                self._answer(wait, h, i)

    def _begin_wait(self, timeout: float, solicit: bool) -> PromptWait:
        """Start a prompt wait. Takes the last line sent (`_send.line`, cleared: its echo is stripped from
        the output) and whether the wait may solicit (`_send.solicit`, set back to True: a command that is
        still running would answer a solicit newline with a second prompt); takes up a prompt an `after`
        wait handed on (`_prompt.held`, cleared), with what was read up to it, as if the wait had read it;
        and starts the handlers' response selection over."""
        sent, self._send.line = self._send.line, None
        solicited, self._send.solicit = not (self._send.solicit and solicit), True
        for h in self._handlers:
            h.reset()
        wait = PromptWait(sent, solicited, time.monotonic() + timeout, timeout)
        carried, self._prompt.held = self._prompt.held, None
        if carried:
            wait.output.append(carried.read)
            wait.held, wait.held_at = carried, 1
        return wait

    def _on_timeout(self, wait: PromptWait, unread: str) -> str | None:
        """A poll ended with no match; the unmatched text stays buffered and comes back with the next match.
        Reads `wait.held` and the child's buffer against `unread`, what it held before the poll: a held
        prompt after which nothing at all arrived was the prompt, so `_prompt.text` and `_prompt.handler`
        are set to it and its match is returned. Otherwise None: no Return is pressed while a prompt is
        held; with none held, the one solicit newline is sent (`wait.solicited`) when the wait may, no
        handler has fired, no part of a line is typed at the far side (`_send.partial`) and no prompt is
        waiting for an answer (`_send.unanswered`)."""
        cld = self._cld
        assert cld
        if wait.held:
            if cld.buffer == unread:
                self._prompt.text, self._prompt.handler = wait.held.line, wait.held.handler
                return wait.held.match
            return None
        if not (wait.solicited or self._send.partial or self._send.unanswered) and all(
            h.is_fresh for h in self._handlers
        ):
            self._put_line("", wait.deadline - time.monotonic(), wait.timeout)
            wait.solicited = True
        return None

    def _on_shell_prompt(self, wait: PromptWait, h: PromptHandler, before: str) -> str | None:
        """A shell prompt regex of `h` matched, `before` being what this read had before it. Reads
        `wait.output` and `wait.sent`: when the output so far is the start of the line sent, the line editor
        may have written the prompt again while echoing the line (readline does, for a line that ends at
        the right margin), so the prompt is held (`wait.held`, `wait.held_at`), a `\\r` goes into the output,
        from which the echo goes on, and None is returned. Otherwise the session is at the prompt:
        `_prompt.text` and `_prompt.handler` are set and the match is returned."""
        cld = self._cld
        assert cld
        after = str(cld.after)
        if _mid_echo("".join(wait.output), wait.sent):
            wait.output.append("\r")
            wait.held = HeldPrompt("", before + after, after, h)
            wait.held_at = len(wait.output)
            return None
        self._prompt.text, self._prompt.handler = before + after, h
        return after

    def _answer(self, wait: PromptWait, h: PromptHandler, i: int) -> None:
        """A prompt with responses matched, at the regex of index `i`. Sends the response `h` picks for it,
        within what is left of the wait (`wait.deadline`, `wait.timeout`). Sets `_send.unanswered` when the
        response could not be sent (none left, or one that could not be rendered or sent): the prompt is
        still waiting for one. Records a credentials prompt in `_login` as answered, open and pending."""
        try:
            self._put_line(h.respond(i - h.start), wait.deadline - time.monotonic(), wait.timeout)
        except (LineTooLong, PartialLine) as e:
            self._send.unanswered = True
            raise type(e)(f"prompt '{h.name}': {e}") from None
        except BaseException:
            self._send.unanswered = True
            raise
        self._reporter.say(f"prompt answered: {h.name}")  # never the response
        if h.credentials:
            self._login.open.append(h.name)
            self._login.pending = True
            if h.name not in self._login.logins:
                self._login.logins.append(h.name)

    def _prompt_what(self) -> str:
        names = ", ".join(f"'{h.name}'" for h in self._handlers if h.is_return) or "none defined"
        return f"a shell prompt ({names})"

    def _finish(self, wait: PromptWait, match: str, errors: Sequence[str] | None, capture: bool) -> str:
        """The wait ended at a shell prompt (`_prompt.text` and `_prompt.handler` are set): the session is at
        it (`_prompt.at_prompt`) and no login is pending. The captured output is what was read up to its
        last line break, less the echo of the line sent (`wait.sent`); `capture` stores it and `match` in
        `ctx`. An `errors` pattern that matches the output is a CommandError."""
        self._prompt.at_prompt = True
        self._login.pending = False
        text = "".join(wait.output)
        text = text[: text.rfind("\n") + 1]
        if wait.sent:
            text = strip_echo(text, wait.sent)
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
        self._prompt.held = None
        idx = self._expect(patterns, timeout, what or " or ".join(f"'{p}'" for p in patterns))
        self._ctx["before"] = str(self._cld.before or "")
        self._ctx["match"] = str(self._cld.after or "")
        # a match that ends at a shell prompt, with nothing read after it, has read that prompt: the
        # session is at it, as after a prompt wait
        _, line_break, line = (self._ctx["before"] + self._ctx["match"]).rpartition("\r\n")
        if not self._prompt.at_prompt and not self._cld.buffer:
            # the echo of the line sent can be in the line only when no line break came before it
            sent = self._send.line if line_break == "" else None
            found = self._scan(line, whole=True, sent=sent)
            if found.at_prompt:
                self._prompt.at_prompt, self._prompt.text, self._prompt.handler = True, line, found.handler
                self._login.pending = False
            elif found.read is not None and found.handler:
                # the prompt may be one written again inside the echo: the next prompt wait holds it
                self._prompt.held = HeldPrompt(found.read, line, self._ctx["match"], found.handler)
        return idx

    def sendline(self, line: str = "", *, solicit: bool = False, timeout: float = 300):
        """Send a line, within `timeout`. `solicit` lets the next prompt wait send its solicit newline: for
        a raw send (`line`, `return`), not for a command whose prompt the wait is for."""
        if not self._cld:
            raise RuntimeError("not attached")
        prompt, send = copy.copy(self._prompt), copy.copy(self._send)
        self._prompt.at_prompt = False
        self._prompt.held = None
        self._send.line = line
        self._send.solicit = solicit
        try:
            self._put_line(line, timeout)
        except (LineTooLong, PartialLine, KeyboardInterrupt):
            if self._send.writes == send.writes:  # it never got to the write: the session is where it was
                self._prompt, self._send = prompt, send
            raise
        self._send.unanswered = False

    def _put_line(self, line: str, timeout: float, of: float | None = None):
        """Write a line and its line break to the child. `of`: the timeout of the wait the send is part
        of, when `timeout` is what is left of it."""
        cld = self._cld
        assert cld
        if self._send.partial:
            raise PartialLine(f"line not sent: {PARTIAL}")
        settle = self._send.control_at + CONTROL_SETTLE - time.monotonic()
        if settle > 0:
            time.sleep(settle)  # the far side is still dealing with the control character
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
            self._reporter.say(
                f"long line: {longest} bytes; a far side that reads whole lines, with no line editor, keeps only "
                f"the first {LINUX_CANON - 1} bytes of one (the usual limit on Linux) and drops the rest without an error",
                "warn",
            )

    def _put_control(self, char: str, timeout: float):
        data = control_byte(char)
        if self._send.partial and data in (b"\r", b"\n"):  # Ctrl+M and Ctrl+J are the Return key
            raise PartialLine(f"control character not sent: {PARTIAL}")
        self._write(data, time.monotonic() + timeout, timeout, "a control character")
        self._send.partial = False  # the far side has been told to drop what was typed
        self._send.control_at = time.monotonic()

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
        self._reporter.check()  # nothing is sent once the run's output is lost
        self._send.writes += 1
        pty = Pty(cld.child_fd)
        try:
            # into pexpect's buffer and the operator echo, like the output a wait reads
            pty.write(data, deadline, timeout, what, lambda: cld.expect(pexpect.TIMEOUT, timeout=0))
        except BaseException as e:
            self._send.line = None
            self._send.solicit = False
            self._send.partial = self._send.partial or pty.done > 0
            if isinstance(e, pexpect.EOF) or closed(e):
                raise EOFError(f"connection closed while sending {what}") from e
            raise

    def check_rc(self, timeout: float = 300, *, posix: bool = False) -> int:
        """`posix`: the check is made only because the prompt is a `posix` one. A timeout then names it."""
        if not self._cld:
            raise RuntimeError("not attached")

        h = self._shell() if posix else None
        self.sendline("echo __AUTOBOT_RC=$?", timeout=timeout)
        try:
            # the lookahead waits for what follows the digits: a code split across two reads is read whole
            self._expect([r"__AUTOBOT_RC=(\d+)(?=\D)"], timeout, "the exit code of the command (echo $?)")
        except BaseException as e:
            self._send.solicit = True  # like a prompt wait that timed out: the next wait follows no command
            if h and h.posix and isinstance(e, TimeoutError):
                # a regex of the prompt that also matches a CLI's prompt sends the check to the CLI
                raise TimeoutError(f"{e} at prompt '{h.name}' (posix: true): is it a POSIX shell?") from e
            raise

        rc = int(self._cld.match.group(1))  # type: ignore
        self.get_prompt(timeout=timeout, capture=False)
        return rc

    def sendcontrol(self, char: str, *, timeout: float = 300):
        if not self._cld:
            raise RuntimeError("not attached")
        self._prompt.at_prompt = False
        self._send.solicit = True
        self._prompt.held = None
        self._put_control(char, timeout)
        self._send.unanswered = False

    def sleep(self, seconds: float):
        if not self._cld:
            raise RuntimeError("not attached")
        self._expect(pexpect.TIMEOUT, seconds, "the end of a sleep")
