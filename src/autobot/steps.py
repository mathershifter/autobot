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
        for i, line in enumerate(lines):
            cmd = ctx.render(line)
            if i > 0 or not step.after:
                ctx.session.get_prompt(timeout=timeout)
            ctx.session.sendline(cmd)
            console.print(f">> cmd: {cmd}")
        errors = ctx.config.errors or None
        output = ""
        try:
            output = ctx.session.get_prompt(timeout=timeout, errors=errors)
            assertions = ensure_list(step.assert_)
            if assertions:
                rendered = [ctx.render(a) for a in assertions]
                if not any(re.search(p, output) for p in rendered):
                    raise RuntimeError(
                        f"assertion failed: expected {rendered}"
                    )
            elif not errors:
                rc = ctx.session.check_rc(timeout=timeout)
                if rc != 0:
                    raise RuntimeError(f"command returned exit code {rc}")
        except RuntimeError:
            if not step.ignore_error:
                raise
            console.print(">> error ignored")
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
        errors = ctx.config.errors or None
        output = ""
        try:
            output = ctx.session.get_prompt(timeout=timeout, errors=errors)
            assertions = ensure_list(step.assert_)
            if assertions:
                rendered = [ctx.render(a) for a in assertions]
                if not any(re.search(p, output) for p in rendered):
                    raise RuntimeError(
                        f"assertion failed: expected {rendered}"
                    )
            elif not errors:
                rc = ctx.session.check_rc(timeout=timeout)
                if rc != 0:
                    raise RuntimeError(f"command returned exit code {rc}")
        except RuntimeError:
            if not step.ignore_error:
                raise
            console.print(">> error ignored")
        finally:
            try:
                ctx.session.get_prompt(timeout=timeout)
                ctx.session.sendline(f"rm -f {tmp}")
                ctx.session.get_prompt(timeout=timeout)
                console.print(f">> script: cleaned up {tmp}")
            except (TimeoutError, EOFError, OSError) as e:
                console.print(f">> script: cleanup of {tmp} failed ({type(e).__name__}): {e}")
        if step.register_:
            ctx.config.vars[step.register_] = output.strip()
            console.print(f">> register: vars.{step.register_}")


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
            saved_handlers = ctx.session._handlers
            block_handlers = [ctx.build_handler(p) for p in step.block.prompts]
            ctx.session._set_handlers(block_handlers)
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
                ctx.session._set_handlers(saved_handlers)
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
