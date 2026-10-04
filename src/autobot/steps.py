from __future__ import annotations

import base64
import re
import uuid
from typing import TYPE_CHECKING

from rich.console import Console

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
from .types import ensure_list

if TYPE_CHECKING:
    from .protocols import RunnerContext
    from .registry import StepRegistry

# markup off: log lines echo commands, names and errors that may look like [tags]
console = Console(stderr=True, markup=False, soft_wrap=True)

# base64 chars per upload line; keeps each line (~600 chars) under the
# smallest common canonical-mode line limit (MAX_CANON 1024 on BSD/macOS,
# 4095 on Linux) and small enough for slow serial/terminal-server consoles.
SCRIPT_CHUNK = 512
SCRIPT_CLEANUP_TIMEOUT = 10.0


class StepFailure(RuntimeError):
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
        if not step.after:
            ctx.session.get_prompt(timeout=timeout)
        # render before splitting: a Jinja block may span lines or add lines
        lines = [l for item in ensure_list(step.cmd) for l in self._lines(ctx.render(item))]
        try:
            for i, cmd in enumerate(lines):
                if i > 0:
                    output.append(ctx.session.get_prompt(timeout=timeout, errors=errors))
                ctx.session.sendline(cmd)
                console.print(f">> cmd: {cmd}")
            output.append(ctx.session.get_prompt(timeout=timeout, errors=errors))
            self._check(step, ctx, "".join(output), timeout)
        except (CommandError, StepFailure) as e:
            if isinstance(e, CommandError):
                output.append(e.output)
            if not step.ignore_error:
                raise
            console.print(f">> error ignored: {e}")
        self._register(step, ctx, "".join(output))

    @staticmethod
    def _lines(text: str) -> list[str]:
        return [l for l in text.splitlines() if l.strip()] or [""]

    def _check(self, step: CmdStep, ctx: RunnerContext, output: str, timeout: float) -> None:
        assertions = ensure_list(step.assert_)
        if assertions:
            rendered = [ctx.render(a) for a in assertions]
            if not any(re.search(p, output) for p in rendered):
                raise StepFailure(f"assertion failed: expected {rendered}")
        elif not ctx.config.errors:
            rc = ctx.session.check_rc(timeout=timeout)
            if rc != 0:
                raise StepFailure(f"command returned exit code {rc}")

    def _register(self, step: CmdStep, ctx: RunnerContext, output: str) -> None:
        if step.register_:
            ctx.config.vars[step.register_] = output.strip()
            console.print(f">> register: vars.{step.register_}")

    def _execute_script(self, step: CmdStep, ctx: RunnerContext, timeout: float) -> None:
        tmp = f"/tmp/_autobot_{uuid.uuid4().hex}"
        output = ""
        interrupt = False
        if not step.after:
            ctx.session.get_prompt(timeout=timeout)
        # rendered after the first prompt wait, like any cmd: session.* is what that wait set
        script = ctx.render(str(step.cmd))
        if not script.endswith("\n"):
            script += "\n"  # jinja drops the trailing newline
        try:
            console.print(f">> script: writing to {tmp}")
            self._upload(ctx, script.encode(), tmp, timeout)
            console.print(f">> script: executing {tmp}")
            ctx.session.sendline(tmp)
            output = ctx.session.get_prompt(timeout=timeout, errors=ctx.config.errors or None)
            self._check(step, ctx, output, timeout)
        except (CommandError, StepFailure) as e:
            if isinstance(e, CommandError):
                output = e.output
            if not step.ignore_error:
                raise
            console.print(f">> error ignored: {e}")
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
                f"(umask 077; printf %s {b64[i : i + SCRIPT_CHUNK]} | tee -a {tmp}.b64 | wc -c)"
            )
            ctx.session.get_prompt(timeout=timeout)
        ctx.session.sendline(
            f"(umask 077; base64 -d {tmp}.b64 | tee {tmp} | wc -c) && chmod 700 {tmp}"
            " && echo __AUTOBOT_UPLOAD_OK"
        )
        out = ctx.session.get_prompt(timeout=timeout)
        if not re.search(rf"(?m)^\s*{len(script)}\s*\n\s*__AUTOBOT_UPLOAD_OK\s*$", out):
            raise StepFailure(f"script upload to {tmp} failed: {out.strip()}")

    @staticmethod
    def _cleanup(ctx: RunnerContext, tmp: str, timeout: float, interrupt: bool) -> None:
        try:
            if interrupt:
                ctx.session.sendcontrol("c")
                console.print(">> script: interrupt sent: ^C")
            ctx.session.get_prompt(timeout=timeout, capture=False)
            ctx.session.sendline(f"rm -f {tmp} {tmp}.b64")
            ctx.session.get_prompt(timeout=timeout, capture=False)
            console.print(f">> script: cleaned up {tmp}")
        except Exception as e:  # noqa: BLE001 - best-effort, must not mask the step error
            console.print(f">> script: cleanup of {tmp} failed ({type(e).__name__}): {e}")


class SleepExecutor:
    key = "sleep"
    model = SleepStep

    def execute(self, step: SleepStep, ctx: RunnerContext, timeout: float) -> None:
        console.print(f">> sleep: {step.sleep}s")
        ctx.session.sleep(step.sleep)


class CallExecutor:
    key = "call"
    model = CallStep

    def execute(self, step: CallStep, ctx: RunnerContext, timeout: float) -> None:
        fn = ctx.config.fn.get(step.call)
        if not fn:
            raise ValueError(f"undefined function: {step.call}")
        ctx.run_steps(fn.script)
        console.print(f">> called {step.call}")


class BlockExecutor:
    key = "block"
    model = BlockStep

    def execute(self, step: BlockStep, ctx: RunnerContext, timeout: float) -> None:
        console.print(f">> block enter: {step.block.name}")
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
                if step.block.breakout and step.block.breakout.script:
                    console.print(f">> block breakout: {step.block.name}")
                    try:
                        ctx.session.reset_handlers()
                        ctx.run_steps(step.block.breakout.script)
                    except Exception as e:  # noqa: BLE001 - breakout is best-effort
                        console.print(f">> block breakout error ({type(e).__name__}): {e}")
        finally:
            if saved_handlers is not None:
                ctx.session.restore_handlers(saved_handlers)
        console.print(f">> block completed: {step.block.name}")


class LineExecutor:
    key = "line"
    model = LineStep

    def execute(self, step: LineStep, ctx: RunnerContext, timeout: float) -> None:
        for line in ensure_list(step.line):
            ctx.session.sendline(ctx.render(line), solicit=True)


class ReturnExecutor:
    key = "return"
    model = ReturnStep

    def execute(self, step: ReturnStep, ctx: RunnerContext, timeout: float) -> None:
        for _ in range(step.newline_count):
            ctx.session.sendline("", solicit=True)


class ControlExecutor:
    key = "control"
    model = ControlStep

    def execute(self, step: ControlStep, ctx: RunnerContext, timeout: float) -> None:
        for char in ensure_list(step.control):
            ctx.session.sendcontrol(char)
            console.print(f">> control sent: ^{char.upper()}")


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
