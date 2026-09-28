from __future__ import annotations

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

console = Console(stderr=True)


class CmdExecutor:
    key = "cmd"
    model = CmdStep

    def execute(self, step: CmdStep, ctx: RunnerContext, timeout: float) -> None:
        if isinstance(step.cmd, str) and step.cmd.startswith("#!"):
            self._execute_script(step, ctx, timeout)
            return
        if isinstance(step.cmd, str) and "\n" in step.cmd:
            lines = [l for l in step.cmd.splitlines() if l.strip()]
        else:
            lines = ensure_list(step.cmd)
        errors = ctx.config.errors or None
        output: list[str] = []
        if not step.after:
            ctx.session.get_prompt(timeout=timeout)
        try:
            for i, line in enumerate(lines):
                cmd = ctx.render(line)
                if i > 0:
                    output.append(ctx.session.get_prompt(timeout=timeout, errors=errors))
                ctx.session.sendline(cmd)
                console.print(f">> cmd: {cmd}")
            output.append(ctx.session.get_prompt(timeout=timeout, errors=errors))
            self._check(step, ctx, "".join(output), timeout)
        except RuntimeError as e:
            if isinstance(e, CommandError):
                output.append(e.output)
            if not step.ignore_error:
                raise
            console.print(f">> error ignored: {e}")
        self._register(step, ctx, "".join(output))

    def _check(self, step: CmdStep, ctx: RunnerContext, output: str, timeout: float) -> None:
        assertions = ensure_list(step.assert_)
        if assertions:
            rendered = [ctx.render(a) for a in assertions]
            if not any(re.search(p, output) for p in rendered):
                raise RuntimeError(f"assertion failed: expected {rendered}")
        elif not ctx.config.errors:
            rc = ctx.session.check_rc(timeout=timeout)
            if rc != 0:
                raise RuntimeError(f"command returned exit code {rc}")

    def _register(self, step: CmdStep, ctx: RunnerContext, output: str) -> None:
        if step.register_:
            ctx.config.vars[step.register_] = output.strip()
            console.print(f">> register: vars.{step.register_}")

    def _execute_script(self, step: CmdStep, ctx: RunnerContext, timeout: float) -> None:
        script = ctx.render(str(step.cmd))
        tmp = f"/tmp/_autobot_{uuid.uuid4().hex}"
        eof_marker = "AUTOBOT_SCRIPT_EOF"
        console.print(f">> script: writing to {tmp}")
        if not step.after:
            ctx.session.get_prompt(timeout=timeout)
        ctx.session.sendline(f"cat > {tmp} << '{eof_marker}'")
        for script_line in script.splitlines():
            ctx.session.sendline(script_line)
        ctx.session.sendline(eof_marker)
        ctx.session.get_prompt(timeout=timeout)
        ctx.session.sendline(f"chmod +x {tmp}")
        ctx.session.get_prompt(timeout=timeout)
        console.print(f">> script: executing {tmp}")
        ctx.session.sendline(tmp)
        output = ""
        try:
            output = ctx.session.get_prompt(timeout=timeout, errors=ctx.config.errors or None)
            self._check(step, ctx, output, timeout)
        except RuntimeError as e:
            if isinstance(e, CommandError):
                output = e.output
            if not step.ignore_error:
                raise
            console.print(f">> error ignored: {e}")
        finally:
            try:
                ctx.session.get_prompt(timeout=timeout)
                ctx.session.sendline(f"rm -f {tmp}")
                ctx.session.get_prompt(timeout=timeout)
                console.print(f">> script: cleaned up {tmp}")
            except (TimeoutError, EOFError, OSError) as e:
                console.print(f">> script: cleanup of {tmp} failed ({type(e).__name__}): {e}")
        self._register(step, ctx, output)


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
        if step.block.enter:
            ctx.run_steps(step.block.enter)
        try:
            ctx.run_steps(step.block.script)
        finally:
            if step.block.breakout and step.block.breakout.script:
                console.print(f">> block breakout: {step.block.name}")
                ctx.session.reset_handlers()
                try:
                    ctx.run_steps(step.block.breakout.script)
                except (TimeoutError, EOFError, RuntimeError, OSError) as e:
                    console.print(f">> block breakout error ({type(e).__name__}): {e}")
            if saved_handlers is not None:
                ctx.session.restore_handlers(saved_handlers)
        console.print(f">> block completed: {step.block.name}")


class LineExecutor:
    key = "line"
    model = LineStep

    def execute(self, step: LineStep, ctx: RunnerContext, timeout: float) -> None:
        for line in ensure_list(step.line):
            ctx.session.sendline(ctx.render(line))


class ReturnExecutor:
    key = "return"
    model = ReturnStep

    def execute(self, step: ReturnStep, ctx: RunnerContext, timeout: float) -> None:
        for _ in range(step.newline_count):
            ctx.session.sendline("")


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
