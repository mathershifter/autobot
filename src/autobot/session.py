from __future__ import annotations

import sys
import time

import pexpect

from .models import Prompt, SendEach
from .types import ANSI_ESCAPE_RE


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
    def __init__(self, prompt: Prompt, start: int, render_fn, resolve_fn):
        self.prompt = prompt
        self.is_return = prompt.is_shell_prompt or prompt.send is None
        self.patterns: list[str] = []
        for entry in prompt.expect:
            if isinstance(entry, list):
                self.patterns.extend(entry)
            else:
                self.patterns.append(entry)
        self.start = start
        self.end = start + len(self.patterns)
        self._responses = self._build_responses(prompt.send, render_fn, resolve_fn)
        self._idx = 0

    @staticmethod
    def _build_responses(send, render_fn, resolve_fn) -> list[str]:
        if not send:
            return []
        if isinstance(send, SendEach):
            items = resolve_fn(send.each)
            if send.fields:
                return [str(item[f]) for item in items for f in send.fields]
            return [str(item) for item in items]
        if isinstance(send[0], list):
            return [render_fn(s) for attempt in send for s in attempt]
        return [render_fn(s) for s in send]

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
    def __init__(self, prompts: list[Prompt], render_fn, resolve_fn):
        self._cld: pexpect.spawn | None = None
        self._handlers: list[PromptHandler] = []
        self._patterns: list = [r"\r\n", ANSI_ESCAPE_RE]
        for p in prompts:
            h = PromptHandler(
                p, start=len(self._patterns), render_fn=render_fn, resolve_fn=resolve_fn
            )
            self._handlers.append(h)
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
        self._cld.expect(r".+", timeout=timeout)

    def detach(self):
        if self._cld:
            self._cld.close()
            self._cld = None

    def get_prompt(
        self, timeout: float = 300, errors: list[str] | None = None
    ):
        if self._at_prompt:
            return
        if not self._cld:
            raise RuntimeError("not attached")

        if errors:
            patterns = self._patterns[:-2] + errors + self._patterns[-2:]
            error_start = len(self._patterns) - 2
            error_end = error_start + len(errors)
        else:
            patterns = self._patterns
            error_start = error_end = 0

        for h in self._handlers:
            h.reset()
        deadline = time.monotonic() + timeout
        solicited = False
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("timed out waiting for prompt")
            i = self._cld.expect(patterns, timeout=min(5, remaining))
            if i == 0 or i == 1:
                continue
            if i == len(patterns) - 2:
                if not solicited and all(h.is_fresh for h in self._handlers):
                    self._cld.sendline("")
                    solicited = True
                continue
            if i == len(patterns) - 1:
                raise EOFError("connection closed")
            if error_start <= i < error_end:
                raise RuntimeError(
                    f"command error: {self._cld.after}".strip()
                )
            for h in self._handlers:
                if h.start <= i < h.end:
                    if h.is_return:
                        self._at_prompt = True
                        return
                    if h.exhausted:
                        raise RuntimeError(
                            f"prompt '{h.prompt.name}': responses exhausted"
                        )
                    response = h.next_response()
                    if response is None:
                        raise RuntimeError(
                            f"prompt '{h.prompt.name}': no response available"
                        )
                    self._cld.sendline(response)
                    break

    def reset_handlers(self):
        for h in self._handlers:
            h.reset()

    def expect(self, patterns: list, timeout: float = 300) -> int:
        if not self._cld:
            raise RuntimeError("not attached")
        return self._cld.expect(patterns, timeout=timeout)

    def sendline(self, line: str = ""):
        if not self._cld:
            raise RuntimeError("not attached")
        self._at_prompt = False
        self._cld.sendline(line)

    def check_rc(self, timeout: float = 300) -> int:
        if not self._cld:
            raise RuntimeError("not attached")

        self.sendline("echo __AUTOBOT_RC=$?")
        self._cld.expect([r"__AUTOBOT_RC=(\d+)"], timeout=timeout)

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
        self._cld.expect(pexpect.TIMEOUT, timeout=seconds)
