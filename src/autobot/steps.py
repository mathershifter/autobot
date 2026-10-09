from __future__ import annotations

import base64
import re
import uuid
from typing import TYPE_CHECKING


from . import log
from .models import (
    BlockStep,
    CallStep,
    CmdStep,
    ControlStep,
    LineStep,
    ReturnStep,
    SleepStep,
)
from .session import CommandError
from .types import RunError, ScriptError, check_regex, ensure_list

if TYPE_CHECKING:
    from .protocols import RunnerContext
    from .registry import StepRegistry


# base64 chars per upload line; keeps each line (~600 chars) under the
# smallest common canonical-mode line limit (MAX_CANON 1024 on BSD/macOS,
# 4095 on Linux), which the session refuses to send past, and small enough
# for slow serial/terminal-server consoles.
SCRIPT_CHUNK = 512
SCRIPT_CLEANUP_TIMEOUT = 10.0


class StepFailure(RunError):
    """A command failure that `ignore_error` swallows (exit code, assert, upload)."""


class CmdExecutor:
    key = "cmd"
    model = CmdStep

    def execute(self, step: CmdStep, ctx: RunnerContext, timeout: float) -> None:
        if isinstance(step.cmd, str) and step.cmd.startswith("#!"):
            self._execute_script(step, ctx, timeout)
            return
        errors = ctx.config.errors or None
        output: list[str] = []
        items = ensure_list(step.cmd)
        # `cmd: []` sends nothing: a prompt wait could press Return or answer a prompt
        if items:
            self._first_prompt(step, ctx, timeout)
        # render before splitting: a Jinja block may span lines or add lines
        lines = [l for item in items for l in self._lines(ctx.render(item))]
        try:
            for i, cmd in enumerate(lines):
                if i > 0:
                    output.append(ctx.session.get_prompt(timeout=timeout, errors=errors))
                ctx.session.sendline(cmd, timeout=timeout)
                log.say(f"cmd: {cmd}")
            if lines:
                output.append(ctx.session.get_prompt(timeout=timeout, errors=errors))
            self._check(step, ctx, "".join(output), timeout, probe=bool(lines))
        except (CommandError, StepFailure) as e:
            if isinstance(e, CommandError):
                output.append(e.output)
            if not step.ignore_error:
                raise
            log.say(f"error ignored: {e}", "warn")
        self._register(step, ctx, "".join(output))

    @staticmethod
    def _first_prompt(step: CmdStep, ctx: RunnerContext, timeout: float) -> None:
        if step.after is None:
            ctx.session.get_prompt(timeout=timeout)
            return
        # `after` matched output, so the console isn't idle: a Return pressed here would go to whatever is
        # running. session.* stays what `after` set
        try:
            ctx.session.get_prompt(timeout=timeout, capture=False, solicit=False)
        except TimeoutError as e:
            raise TimeoutError(
                f"{e} after the after pattern matched (a cmd is sent at a shell prompt; use line to send without one)"
            ) from e

    @staticmethod
    def _lines(text: str) -> list[str]:
        # not splitlines(): it also splits at \x0b, \x0c, \x1c-\x1e, \x85, U+2028 and U+2029
        return [l for l in re.split(r"\r\n?|\n", text) if l.strip()] or [""]

    def _check(
        self, step: CmdStep, ctx: RunnerContext, output: str, timeout: float, probe: bool = True
    ) -> None:
        assertions = ensure_list(step.assert_)
        if assertions:
            rendered = [ctx.render(a) for a in assertions]
            for p in rendered:
                if not p:
                    raise ScriptError("assert: a pattern rendered to an empty regex, which matches any output")
                check_regex(p, "assert")
            if not any(re.search(p, output) for p in rendered):
                raise StepFailure(f"assertion failed: expected {rendered}")
        elif probe and not ctx.config.errors:
            rc = ctx.session.check_rc(timeout=timeout)
            if rc != 0:
                raise StepFailure(f"command returned exit code {rc}")

    def _register(self, step: CmdStep, ctx: RunnerContext, output: str) -> None:
        if step.register_:
            ctx.config.vars[step.register_] = output.strip()
            log.say(f"register: vars.{step.register_}", "detail")

    def _execute_script(self, step: CmdStep, ctx: RunnerContext, timeout: float) -> None:
        tmp = f"/tmp/_autobot_{uuid.uuid4().hex}"
        output = ""
        interrupt = False
        self._first_prompt(step, ctx, timeout)
        # rendered after the first prompt wait, like any cmd: session.* is what that wait set
        script = ctx.render(str(step.cmd))
        if not script.endswith("\n"):
            script += "\n"  # jinja drops the trailing newline
        try:
            log.say(f"script: writing to {tmp}", "detail")
            self._upload(ctx, script.encode(), tmp, timeout)
            log.say(f"script: executing {tmp}", "detail")
            ctx.session.sendline(tmp, timeout=timeout)
            output = ctx.session.get_prompt(timeout=timeout, errors=ctx.config.errors or None)
            self._check(step, ctx, output, timeout)
        except (CommandError, StepFailure) as e:
            if isinstance(e, CommandError):
                output = e.output
            if not step.ignore_error:
                raise
            log.say(f"error ignored: {e}", "warn")
        except BaseException:
            # e.g. a timeout: the script or an upload line may still be running
            interrupt = True
            raise
        finally:
            self._cleanup(ctx, tmp, min(timeout, SCRIPT_CLEANUP_TIMEOUT), interrupt)
        self._register(step, ctx, output)

    @staticmethod
    def _upload(ctx: RunnerContext, script: bytes, tmp: str, timeout: float) -> None:
        # One line per chunk, no heredoc: nothing triggers PS2, and the base64
        # alphabet needs no quoting and contains none of '>', '#', '$', which
        # prompt regexes commonly end with.
        b64 = base64.b64encode(script).decode()
        for i in range(0, len(b64), SCRIPT_CHUNK):
            ctx.session.sendline(
                f"(umask 077; printf %s {b64[i : i + SCRIPT_CHUNK]} | tee -a {tmp}.b64 | wc -c)",
                timeout=timeout,
            )
            ctx.session.get_prompt(timeout=timeout)
        ctx.session.sendline(
            f"(umask 077; base64 -d {tmp}.b64 | tee {tmp} | wc -c) && chmod 700 {tmp}"
            " && echo __AUTOBOT_UPLOAD_OK",
            timeout=timeout,
        )
        out = ctx.session.get_prompt(timeout=timeout)
        if not re.search(rf"(?m)^\s*{len(script)}\s*\n\s*__AUTOBOT_UPLOAD_OK\s*$", out):
            raise StepFailure(f"script upload to {tmp} failed: {out.strip()}")

    @staticmethod
    def _cleanup(ctx: RunnerContext, tmp: str, timeout: float, interrupt: bool) -> None:
        try:
            if interrupt:
                ctx.session.sendcontrol("c", timeout=timeout)
                log.say("script: interrupt sent: ^C", "warn")
            ctx.session.get_prompt(timeout=timeout, capture=False)
            ctx.session.sendline(f"rm -f {tmp} {tmp}.b64", timeout=timeout)
            ctx.session.get_prompt(timeout=timeout, capture=False)
            log.say(f"script: cleaned up {tmp}", "detail")
        except Exception as e:  # noqa: BLE001 - best-effort, must not mask the step error
            log.say(f"script: cleanup of {tmp} failed ({type(e).__name__}): {e}", "warn")


class SleepExecutor:
    key = "sleep"
    model = SleepStep

    def execute(self, step: SleepStep, ctx: RunnerContext, timeout: float) -> None:
        log.say(f"sleep: {step.sleep}s")
        ctx.session.sleep(step.sleep)


class CallExecutor:
    key = "call"
    model = CallStep

    def execute(self, step: CallStep, ctx: RunnerContext, timeout: float) -> None:
        fn = ctx.config.fn.get(step.call)
        if not fn:
            raise ValueError(f"undefined function: {step.call}")
        log.say(f"call: {step.call}", "group")
        ctx.run_steps(fn.script)


class BlockExecutor:
    key = "block"
    model = BlockStep

    def execute(self, step: BlockStep, ctx: RunnerContext, timeout: float) -> None:
        log.say(f"block enter: {step.block.name}", "group")
        if step.block.prompts:
            saved_handlers = ctx.session.save_handlers()
            block_handlers = [ctx.build_handler(p) for p in step.block.prompts]
            ctx.session.restore_handlers(block_handlers)
        else:
            saved_handlers = None
        try:
            try:
                if step.block.enter:
                    ctx.run_steps(step.block.enter)
                ctx.run_steps(step.block.script)
            finally:
                if step.block.breakout:
                    log.say(f"block breakout: {step.block.name}", "group")
                    ctx.run_breakout(step.block.breakout, "block breakout")
        finally:
            if saved_handlers is not None:
                ctx.session.restore_handlers(saved_handlers)
        log.say(f"block completed: {step.block.name}", "ok")


class LineExecutor:
    key = "line"
    model = LineStep

    def execute(self, step: LineStep, ctx: RunnerContext, timeout: float) -> None:
        for line in ensure_list(step.line):
            ctx.session.sendline(ctx.render(line), solicit=True, timeout=timeout)
            log.say("line sent")  # not the text: it may be a password, which the session doesn't echo


class ReturnExecutor:
    key = "return"
    model = ReturnStep

    def execute(self, step: ReturnStep, ctx: RunnerContext, timeout: float) -> None:
        for _ in range(step.newline_count):
            ctx.session.sendline("", solicit=True, timeout=timeout)
            log.say("return sent")


class ControlExecutor:
    key = "control"
    model = ControlStep

    def execute(self, step: ControlStep, ctx: RunnerContext, timeout: float) -> None:
        for char in ensure_list(step.control):
            ctx.session.sendcontrol(char, timeout=timeout)
            log.say(f"control sent: ^{char.upper()}")


def register_builtins(reg: StepRegistry):
    for cls in (
        CmdExecutor,
        SleepExecutor,
        CallExecutor,
        BlockExecutor,
        LineExecutor,
        ReturnExecutor,
        ControlExecutor,
    ):
        reg.register(cls(), builtin=True)
