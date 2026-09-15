from __future__ import annotations

import os
import re
import subprocess
import tempfile
import uuid
from typing import Any

from rich.console import Console

from .models import (
    BlockStep,
    CallStep,
    CmdStep,
    Config,
    ControlStep,
    LineStep,
    Prompt,
    ReturnStep,
    SendEach,
    SleepStep,
    Step,
)
from .session import PromptHandler, Session
from .types import ensure_list, render

console = Console(stderr=True)
class Runner:
    def __init__(self, config: Config, cli_args: dict[str, str]):
        self._config = config
        self._cli_args = cli_args
        self._default_timeout = 300
        self._env = self._resolve_env(config.env)
        self._session = Session([])
        handlers = [self._build_handler(p) for p in config.prompts]
        self._session._set_handlers(handlers)

    def _build_handler(self, prompt: Prompt) -> PromptHandler:
        patterns: list[str] = []
        for entry in prompt.expect:
            if isinstance(entry, list):
                patterns.extend(entry)
            else:
                patterns.append(entry)
        responses = self._build_responses(prompt.send)
        is_return = prompt.is_shell_prompt or prompt.send is None
        return PromptHandler(prompt.name, patterns, responses, is_return)

    def _build_responses(self, send) -> list[str]:
        if not send:
            return []
        if isinstance(send, SendEach):
            items = self._resolve(send.each)
            if send.fields:
                return [str(item[f]) for item in items for f in send.fields]
            return [str(item) for item in items]
        if isinstance(send[0], list):
            return [self._render(s) for attempt in send for s in attempt]
        return [self._render(s) for s in send]

    def _resolve_env(self, defaults: dict[str, str]) -> dict[str, str]:
        env = {k: os.environ.get(k, v) for k, v in defaults.items()}
        ctx = {"env": env, "vars": self._config.vars, "args": self._cli_args}
        for iteration in range(10):
            changed = False
            for k, v in env.items():
                rendered = render(v, ctx)
                if rendered != v:
                    env[k] = rendered
                    changed = True
            if not changed:
                break
        else:
            unresolved = [k for k, v in env.items() if "{{" in str(v)]
            if unresolved:
                raise ValueError(
                    f"env nesting too deep (>10 iterations), unresolved: {unresolved}"
                )
        return env

    @property
    def _ctx(self) -> dict:
        return {
            "env": self._env,
            "vars": self._config.vars,
            "args": self._cli_args,
            "session": self._session.ctx,
        }

    def _resolve(self, path: str) -> Any:
        parts = path.split(".")
        obj: Any = self._ctx
        for part in parts:
            if isinstance(obj, dict):
                obj = obj[part]
            else:
                obj = getattr(obj, part)
        return obj

    def _render(self, template: Any, extra_ctx: dict | None = None) -> str:
        ctx = self._ctx
        if extra_ctx:
            ctx = {**ctx, **extra_ctx}
        return render(template, ctx)

    @staticmethod
    def _run_prepare(script: str):
        console.print(">> prepare: running local script")
        with tempfile.NamedTemporaryFile(
            mode="w", prefix="_autobot_", suffix=".sh", delete=False
        ) as f:
            f.write(script)
            tmp = f.name
        try:
            os.chmod(tmp, 0o700)
            result = subprocess.run([tmp], check=False)
            if result.returncode != 0:
                raise RuntimeError(
                    f"prepare script failed with exit code {result.returncode}"
                )
        finally:
            os.unlink(tmp)
        console.print(">> prepare: done")

    def run(self):
        attach = self._config.attach
        spawn = self._render(attach.spawn)
        timeout = int(attach.timeout) if attach.timeout else self._default_timeout
        env = attach.env or {"TERM": "dumb", "NO_COLOR": "1"}

        if attach.prepare:
            self._run_prepare(self._render(attach.prepare))

        console.print(f">> attach: {spawn}")
        self._session.attach(spawn, env=env, timeout=timeout)
        try:
            if attach.script:
                self._run_steps(attach.script)
            self._run_steps(self._config.script)
        finally:
            if attach.breakout and attach.breakout.script:
                console.print(">> breakout: detaching")
                self._session.reset_handlers()
                try:
                    self._run_steps(attach.breakout.script)
                except (TimeoutError, EOFError, RuntimeError, OSError) as e:
                    console.print(
                        f">> breakout error ({type(e).__name__}): {e}")
            self._session.detach()

    def _run_steps(self, steps: list[Step]):
        for step in steps:
            self._run_step(step)

    def _get_timeout(self, step: Any) -> float:
        timeout = getattr(step, "timeout", None)
        return timeout if timeout is not None else self._default_timeout

    def _run_step(self, step: Step):
        timeout = self._get_timeout(step)

        after = getattr(step, "after", None)
        if after:
            self._session.expect([self._render(after)], timeout=timeout)

        when = getattr(step, "when", None)
        if when is not None:
            result = self._render(when)
            if result in ("", "false", "False", "0", "none"):
                return

        delay_before = getattr(step, "delay_before", None)
        if delay_before:
            self._session.sleep(delay_before)

        if isinstance(step, CmdStep):
            self._step_cmd(step, timeout)
        elif isinstance(step, SleepStep):
            self._step_sleep(step)
        elif isinstance(step, CallStep):
            self._step_call(step, timeout)
        elif isinstance(step, BlockStep):
            self._step_block(step)
        elif isinstance(step, LineStep):
            self._step_line(step)
        elif isinstance(step, ReturnStep):
            self._step_return(step)
        elif isinstance(step, ControlStep):
            self._step_control(step)

        delay_after = getattr(step, "delay_after", None)
        if delay_after:
            self._session.sleep(delay_after)

    def _step_cmd(self, step: CmdStep, timeout: float):
        if isinstance(step.cmd, str) and step.cmd.startswith("#!"):
            self._step_cmd_script(step, timeout)
            return
        if isinstance(step.cmd, str) and "\n" in step.cmd:
            lines = [l for l in step.cmd.splitlines() if l.strip()]
        else:
            lines = ensure_list(step.cmd)
        for i, line in enumerate(lines):
            cmd = self._render(line)
            if i > 0 or not step.after:
                self._session.get_prompt(timeout=timeout)
            self._session.sendline(cmd)
            console.print(f">> cmd: {cmd}")
        errors = self._config.errors or None
        output = ""
        try:
            output = self._session.get_prompt(timeout=timeout, errors=errors)
            assertions = ensure_list(step.assert_)
            if assertions:
                rendered = [self._render(a) for a in assertions]
                if not any(re.search(p, output) for p in rendered):
                    raise RuntimeError(
                        f"assertion failed: expected {rendered}"
                    )
            elif not errors:
                rc = self._session.check_rc(timeout=timeout)
                if rc != 0:
                    raise RuntimeError(f"command returned exit code {rc}")
        except RuntimeError:
            if not step.ignore_error:
                raise
            console.print(">> error ignored")
        if step.register_:
            self._config.vars[step.register_] = output.strip()
            console.print(f">> register: vars.{step.register_}")

    def _step_cmd_script(self, step: CmdStep, timeout: float):
        script = self._render(str(step.cmd))
        tmp = f"/tmp/_autobot_{uuid.uuid4().hex}"
        eof_marker = "AUTOBOT_SCRIPT_EOF"
        console.print(f">> script: writing to {tmp}")
        if not step.after:
            self._session.get_prompt(timeout=timeout)
        self._session.sendline(f"cat > {tmp} << '{eof_marker}'")
        for script_line in script.splitlines():
            self._session.sendline(script_line)
        self._session.sendline(eof_marker)
        self._session.get_prompt(timeout=timeout)
        self._session.sendline(f"chmod +x {tmp}")
        self._session.get_prompt(timeout=timeout)
        console.print(f">> script: executing {tmp}")
        self._session.sendline(tmp)
        errors = self._config.errors or None
        output = ""
        try:
            output = self._session.get_prompt(timeout=timeout, errors=errors)
            assertions = ensure_list(step.assert_)
            if assertions:
                rendered = [self._render(a) for a in assertions]
                if not any(re.search(p, output) for p in rendered):
                    raise RuntimeError(
                        f"assertion failed: expected {rendered}"
                    )
            elif not errors:
                rc = self._session.check_rc(timeout=timeout)
                if rc != 0:
                    raise RuntimeError(f"command returned exit code {rc}")
        except RuntimeError:
            if not step.ignore_error:
                raise
            console.print(">> error ignored")
        finally:
            try:
                self._session.get_prompt(timeout=timeout)
                self._session.sendline(f"rm -f {tmp}")
                self._session.get_prompt(timeout=timeout)
                console.print(f">> script: cleaned up {tmp}")
            except (TimeoutError, EOFError, OSError) as e:
                console.print(f">> script: cleanup of {tmp} failed ({type(e).__name__}): {e}")
        if step.register_:
            self._config.vars[step.register_] = output.strip()
            console.print(f">> register: vars.{step.register_}")

    def _step_sleep(self, step: SleepStep):
        console.print(f">> sleep: {step.sleep}s")
        self._session.sleep(step.sleep)

    def _step_call(self, step: CallStep, timeout: float):
        fn = self._config.fn.get(step.call)
        if not fn:
            raise ValueError(f"undefined function: {step.call}")
        self._run_steps(fn.script)
        console.print(f">> called {step.call}")

    def _step_block(self, step: BlockStep):
        console.print(f">> block enter: {step.block.name}")
        if step.block.prompts:
            saved_handlers = self._session._handlers
            block_handlers = [self._build_handler(p) for p in step.block.prompts]
            self._session._set_handlers(block_handlers)
        else:
            saved_handlers = None
        if step.block.enter:
            self._run_steps(step.block.enter)
        try:
            self._run_steps(step.block.script)
        finally:
            if step.block.breakout and step.block.breakout.script:
                console.print(f">> block breakout: {step.block.name}")
                self._session.reset_handlers()
                try:
                    self._run_steps(step.block.breakout.script)
                except (TimeoutError, EOFError, RuntimeError, OSError) as e:
                    console.print(f">> block breakout error ({type(e).__name__}): {e}")
            if saved_handlers is not None:
                self._session._set_handlers(saved_handlers)
        console.print(f">> block completed: {step.block.name}")

    def _step_line(self, step: LineStep):
        for line in ensure_list(step.line):
            self._session.sendline(self._render(line))

    def _step_return(self, step: ReturnStep):
        for _ in range(step.newline_count):
            self._session.sendline("")

    def _step_control(self, step: ControlStep):
        for char in ensure_list(step.control):
            self._session.sendcontrol(char)
            console.print(f">> control sent: ^{char.upper()}")
