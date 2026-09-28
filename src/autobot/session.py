from __future__ import annotations

import re
import sys
import time

import pexpect

from .types import ANSI_ESCAPE_RE


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
        self, name: str, patterns: list[str], responses: list[str], is_return: bool
    ):
        self.name = name
        self.is_return = is_return
        self.patterns = patterns
        self.start = 0
        self.end = len(patterns)
        self._responses = responses
        self._idx = 0

    @property
    def exhausted(self) -> bool:
        return bool(self._responses) and self._idx >= len(self._responses)

    def next_response(self) -> str | None:
        if not self._responses or self._idx >= len(self._responses):
            return None
        response = self._responses[self._idx]
        self._idx += 1
        return response

    @property
    def is_fresh(self) -> bool:
        return self._idx == 0

    def reset(self):
        self._idx = 0


class Session:
    def __init__(self, handlers: list[PromptHandler]):
        self._cld: pexpect.spawn | None = None
        self._at_prompt = False
        self._sent: str | None = None
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
        self._at_prompt = False

    def attach(self, spawn: str, env: dict[str, str] | None = None, timeout: float = 300):
        self._cld = pexpect.spawn(
            spawn,
            timeout=timeout,
            encoding="utf-8",
            codec_errors="replace",
            env=env or {"TERM": "dumb", "NO_COLOR": "1"},
        )
        self._cld.logfile_read = CleanWriter(sys.stdout)
        try:
            self._expect(r".+", timeout=timeout)
        except BaseException:
            self.detach()
            raise

    def _expect(self, patterns, timeout: float) -> int:
        if not self._cld:
            raise RuntimeError("not attached")
        try:
            return self._cld.expect(patterns, timeout=timeout)
        except pexpect.TIMEOUT as e:
            raise TimeoutError(f"timed out after {timeout}s waiting for {patterns!r}") from e
        except pexpect.EOF as e:
            raise EOFError("connection closed") from e

    def detach(self):
        if self._cld:
            self._cld.close()
            self._cld = None

    def get_prompt(
        self, timeout: float = 300, errors: list[str] | None = None
    ) -> str:
        if self._at_prompt:
            return ""
        if not self._cld:
            raise RuntimeError("not attached")

        sent, self._sent = self._sent, None
        for h in self._handlers:
            h.reset()
        output: list[str] = []
        deadline = time.monotonic() + timeout
        solicited = False
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("timed out waiting for prompt")
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
                raise EOFError("connection closed")
            for h in self._handlers:
                if h.start <= i < h.end:
                    if h.is_return:
                        return self._finish(output, sent, errors)
                    if h.exhausted:
                        raise RuntimeError(
                            f"prompt '{h.name}': responses exhausted"
                        )
                    response = h.next_response()
                    if response is None:
                        raise RuntimeError(
                            f"prompt '{h.name}': no response available"
                        )
                    self._cld.sendline(response)
                    break

    def _finish(
        self, output: list[str], sent: str | None, errors: list[str] | None
    ) -> str:
        self._at_prompt = True
        text = "".join(output)
        text = text[: text.rfind("\n") + 1]
        if sent:
            text = strip_echo(text, sent)
        if text.strip():
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

    def expect(self, patterns: list, timeout: float = 300) -> int:
        if not self._cld:
            raise RuntimeError("not attached")
        idx = self._expect(patterns, timeout=timeout)
        self._ctx["before"] = str(self._cld.before or "")
        self._ctx["match"] = str(self._cld.after or "")
        return idx

    def sendline(self, line: str = ""):
        if not self._cld:
            raise RuntimeError("not attached")
        self._at_prompt = False
        self._sent = line
        self._cld.sendline(line)

    def check_rc(self, timeout: float = 300) -> int:
        if not self._cld:
            raise RuntimeError("not attached")

        self.sendline("echo __AUTOBOT_RC=$?")
        self._expect([r"__AUTOBOT_RC=(\d+)"], timeout=timeout)

        rc = int(self._cld.match.group(1))  # type: ignore
        self.get_prompt(timeout=timeout)
        return rc

    def sendcontrol(self, char: str):
        if not self._cld:
            raise RuntimeError("not attached")
        self._at_prompt = False
        self._cld.sendcontrol(char)

    def sleep(self, seconds: float):
        if not self._cld:
            raise RuntimeError("not attached")
        self._expect(pexpect.TIMEOUT, timeout=seconds)
