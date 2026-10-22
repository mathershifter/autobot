from __future__ import annotations

import base64
import contextlib
import re
import uuid
from typing import TYPE_CHECKING, Any

from .errors import CommandError, ScriptError, StepFailure
from .models import (
    BlockStep,
    CallStep,
    CmdStep,
    ControlStep,
    LineStep,
    ReturnStep,
    SetStep,
    SleepStep,
)
from .types import check_regex, ensure_list

if TYPE_CHECKING:
    from .protocols import RunnerContext
    from .registry import StepRegistry


# base64 chars per upload line; keeps each line (~600 chars) under the
# smallest common canonical-mode line limit (MAX_CANON 1024 on BSD/macOS,
# 4095 on Linux), which the session refuses to send past, and small enough
# for slow serial/terminal-server consoles.
SCRIPT_CHUNK = 512
SCRIPT_CLEANUP_TIMEOUT = 10.0


class CmdExecutor:
    key = "cmd"
    model = CmdStep

    def execute(self, step: CmdStep, ctx: RunnerContext, timeout: float) -> None:
        if isinstance(step.cmd, str) and step.cmd.startswith("#!"):
            self._execute_script(step, ctx, timeout)
            return
        errors = ctx.errors or None
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
                ctx.reporter.say(f"cmd: {cmd}")
            if lines:
                output.append(ctx.session.get_prompt(timeout=timeout, errors=errors))
            self._check(step, ctx, "".join(output), timeout, probe=bool(lines))
        except (CommandError, StepFailure) as e:
            if isinstance(e, CommandError):
                output.append(e.output)
            if not step.ignore_error:
                raise
            ctx.reporter.say(f"error ignored: {e}", "warn")
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
        self, step: CmdStep, ctx: RunnerContext, output: str, timeout: float, probe: bool = True, shell: bool = False
    ) -> None:
        # `errors` are for a CLI that has no $?, so they stand in for the check, except in a POSIX shell,
        # which has one. `shell`: the command can only have run in one
        assertions = ensure_list(step.assert_)
        if assertions:
            rendered = [ctx.render(a) for a in assertions]
            for p in rendered:
                if not p:
                    raise ScriptError("assert: a pattern rendered to an empty regex, which matches any output")
                check_regex(p, "assert")
            if not any(re.search(p, output) for p in rendered):
                raise StepFailure(f"assertion failed: expected {rendered}")
        elif probe and (shell or not ctx.errors or ctx.session.posix):
            rc = ctx.session.check_rc(timeout=timeout, posix=bool(ctx.errors) and not shell)
            if rc != 0:
                raise StepFailure(f"command returned exit code {rc}")

    def _register(self, step: CmdStep, ctx: RunnerContext, output: str) -> None:
        if step.register_:
            ctx.vars[step.register_] = output.strip()
            ctx.reporter.say(f"register: vars.{step.register_}", "detail")

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
            ctx.reporter.say(f"script: writing to {tmp}", "detail")
            self._upload(ctx, script.encode(), tmp, timeout)
            ctx.reporter.say(f"script: executing {tmp}", "detail")
            ctx.session.sendline(tmp, timeout=timeout)
            output = ctx.session.get_prompt(timeout=timeout, errors=ctx.errors or None)
            self._check(step, ctx, output, timeout, shell=True)
        except (CommandError, StepFailure) as e:
            if isinstance(e, CommandError):
                output = e.output
            if not step.ignore_error:
                raise
            ctx.reporter.say(f"error ignored: {e}", "warn")
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
                ctx.reporter.say("script: interrupt sent: ^C", "warn")
            ctx.session.get_prompt(timeout=timeout, capture=False)
            ctx.session.sendline(f"rm -f {tmp} {tmp}.b64", timeout=timeout)
            ctx.session.get_prompt(timeout=timeout, capture=False)
            ctx.reporter.say(f"script: cleaned up {tmp}", "detail")
        except Exception as e:  # noqa: BLE001 - best-effort, must not mask the step error
            ctx.reporter.say(f"script: cleanup of {tmp} failed ({type(e).__name__}): {e}", "warn")


class SleepExecutor:
    key = "sleep"
    model = SleepStep

    def execute(self, step: SleepStep, ctx: RunnerContext, timeout: float) -> None:
        ctx.reporter.say(f"sleep: {step.sleep}s")
        ctx.session.sleep(step.sleep)


class CallExecutor:
    key = "call"
    model = CallStep

    def execute(self, step: CallStep, ctx: RunnerContext, timeout: float) -> None:
        steps = ctx.function(step.call)
        ctx.reporter.say(f"call: {step.call}", "group")
        ctx.run_steps(steps)


class BlockExecutor:
    key = "block"
    model = BlockStep

    def execute(self, step: BlockStep, ctx: RunnerContext, timeout: float) -> None:
        ctx.reporter.say(f"block enter: {step.block.name}", "group")
        # a block without prompts leaves the session's as they are: no swap, no restore
        swap = ctx.prompts(step.block.prompts) if step.block.prompts else contextlib.nullcontext()
        with swap:
            try:
                if step.block.enter:
                    ctx.run_steps(step.block.enter)
                ctx.run_steps(step.block.script)
            finally:
                if step.block.breakout:
                    ctx.reporter.say(f"block breakout: {step.block.name}", "group")
                    ctx.run_breakout(step.block.breakout, "block breakout")
        ctx.reporter.say(f"block completed: {step.block.name}", "ok")


class LineExecutor:
    key = "line"
    model = LineStep

    def execute(self, step: LineStep, ctx: RunnerContext, timeout: float) -> None:
        for line in ensure_list(step.line):
            ctx.session.sendline(ctx.render(line), solicit=True, timeout=timeout)
            ctx.reporter.say("line sent")  # not the text: it may be a password, which the session doesn't echo


class ReturnExecutor:
    key = "return"
    model = ReturnStep

    def execute(self, step: ReturnStep, ctx: RunnerContext, timeout: float) -> None:
        for _ in range(step.newline_count):
            ctx.session.sendline("", solicit=True, timeout=timeout)
            ctx.reporter.say("return sent")


class ControlExecutor:
    key = "control"
    model = ControlStep

    def execute(self, step: ControlStep, ctx: RunnerContext, timeout: float) -> None:
        for char in ensure_list(step.control):
            ctx.session.sendcontrol(char, timeout=timeout)
            ctx.reporter.say(f"control sent: ^{char.upper()}")


class SetExecutor:
    key = "set"
    model = SetStep

    def execute(self, step: SetStep, ctx: RunnerContext, timeout: float) -> None:
        # in the order written, on a copy: a later key reads an earlier one, and a failure assigns nothing
        work = dict(ctx.vars)
        for name, value in step.set_.items():
            work[name] = self._render(value, ctx, {"vars": work})
        for name in step.set_:
            ctx.vars[name] = work[name]
        # the names, not the values: they may be secrets
        ctx.reporter.say(f"set: {', '.join(f'vars.{name}' for name in step.set_)}")

    def _render(self, value: Any, ctx: RunnerContext, extra: dict) -> Any:
        if isinstance(value, str):
            # the line break that ends the template is part of the value, as in the top-level vars
            return ctx.render(value, extra, keep_newline=True)
        if isinstance(value, list):
            return [self._render(v, ctx, extra) for v in value]
        if isinstance(value, dict):
            return {k: self._render(v, ctx, extra) for k, v in value.items()}
        # the pairs of `!!omap` and `!!pairs` (tuples) and a `!!set` are stored as written
        return value


def register_builtins(reg: StepRegistry):
    for cls in (
        CmdExecutor,
        SleepExecutor,
        CallExecutor,
        BlockExecutor,
        LineExecutor,
        ReturnExecutor,
        ControlExecutor,
        SetExecutor,
    ):
        reg.register(cls(), builtin=True)
