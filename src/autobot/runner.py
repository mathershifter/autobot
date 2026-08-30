from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import uuid
from typing import Any

from .models import (
    BlockStep,
    CallStep,
    CmdStep,
    ControlStep,
    LineStep,
    ReturnStep,
    ScriptConfig,
    SleepStep,
    Step,
)
from .session import Session
from .types import ensure_list, render


class ScriptRunner:
    def __init__(self, config: ScriptConfig, cli_args: dict[str, str]):
        self._config = config
        self._cli_args = cli_args
        self._default_timeout = 300
        self._env = self._resolve_env(config.env)
        self._session = Session(config.prompts, self._render, self._resolve)

    def _resolve_env(self, defaults: dict[str, str]) -> dict[str, str]:
        env = {k: os.environ.get(k, v) for k, v in defaults.items()}
        ctx = {"env": env, "vars": self._config.vars, "args": self._cli_args}
        for _ in range(10):
            changed = False
            for k, v in env.items():
                rendered = render(v, ctx)
                if rendered != v:
                    env[k] = rendered
                    changed = True
            if not changed:
                break
        return env

    @property
    def _ctx(self) -> dict:
        return {
            "env": self._env,
            "vars": self._config.vars,
            "args": self._cli_args,
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
        print(">> prepare: running local script")
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
        print(">> prepare: done")

    def run(self):
        attach = self._config.attach
        spawn = self._render(attach.spawn)
        timeout = int(attach.timeout) if attach.timeout else self._default_timeout
        env = attach.env or {"TERM": "dumb", "NO_COLOR": "1"}

        if attach.prepare:
            self._run_prepare(self._render(attach.prepare))

        print(f">> attach: {spawn}")
        self._session.attach(spawn, env=env, timeout=timeout)
        try:
            if attach.script:
                self._run_steps(attach.script)
            self._run_steps(self._config.script)
        finally:
            if attach.breakout and attach.breakout.script:
                print(">> breakout: detaching")
                self._session.reset_handlers()
                try:
                    self._run_steps(attach.breakout.script)
                except (TimeoutError, EOFError, RuntimeError, OSError) as e:
                    print(
                        f">> breakout error ({type(e).__name__}): {e}", file=sys.stderr
                    )
            self._session.detach()

    def _run_steps(self, steps: list[Step]):
        for step in steps:
            self._run_step(step)

    def _get_timeout(self, step: Any) -> float:
        timeout = getattr(step, "timeout", None)
        return timeout if timeout is not None else self._default_timeout

    def _run_step(self, step: Step):
        timeout = self._get_timeout(step)

        when = getattr(step, "when", None)
        if when:
            self._session.expect([self._render(when)], timeout=timeout)

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
        for line in lines:
            cmd = self._render(line)
            if not step.when:
                self._session.get_prompt(timeout=timeout)
            self._session.sendline(cmd)
            print(f">> cmd: {cmd}")
        assertions = ensure_list(step.assert_)
        if assertions:
            self._session.expect(
                [self._render(a) for a in assertions], timeout=timeout
            )
        errors = self._config.errors or None
        try:
            self._session.get_prompt(timeout=timeout, errors=errors)
            if not errors:
                rc = self._session.check_rc(timeout=timeout)
                if rc != 0:
                    raise RuntimeError(f"command returned exit code {rc}")
        except RuntimeError:
            if not step.ignore_error:
                raise
            print(">> error ignored", file=sys.stderr)

    def _step_cmd_script(self, step: CmdStep, timeout: float):
        script = self._render(str(step.cmd))
        tmp = f"/tmp/_autobot_{uuid.uuid4().hex}"
        eof_marker = "AUTOBOT_SCRIPT_EOF"
        print(f">> script: writing to {tmp}")
        if not step.when:
            self._session.get_prompt(timeout=timeout)
        self._session.sendline(f"cat > {tmp} << '{eof_marker}'")
        for script_line in script.splitlines():
            self._session.sendline(script_line)
        self._session.sendline(eof_marker)
        self._session.get_prompt(timeout=timeout)
        self._session.sendline(f"chmod +x {tmp}")
        self._session.get_prompt(timeout=timeout)
        print(f">> script: executing {tmp}")
        self._session.sendline(tmp)
        assertions = ensure_list(step.assert_)
        if assertions:
            self._session.expect(
                [self._render(a) for a in assertions], timeout=timeout
            )
        errors = self._config.errors or None
        try:
            self._session.get_prompt(timeout=timeout, errors=errors)
            if not errors:
                rc = self._session.check_rc(timeout=timeout)
                if rc != 0:
                    raise RuntimeError(f"command returned exit code {rc}")
        except RuntimeError:
            if not step.ignore_error:
                raise
            print(">> error ignored", file=sys.stderr)
        finally:
            self._session.get_prompt(timeout=timeout)
            self._session.sendline(f"rm -f {tmp}")
            print(f">> script: cleaned up {tmp}")

    def _step_sleep(self, step: SleepStep):
        print(f">> sleep: {step.sleep}s")
        self._session.sleep(step.sleep)

    def _step_call(self, step: CallStep, timeout: float):
        fn = self._config.fn.get(step.call)
        if not fn:
            raise ValueError(f"undefined function: {step.call}")
        self._run_steps(fn.script)
        print(f">> called {step.call}")

    def _step_block(self, step: BlockStep):
        print(f">> block enter: {step.block.name}")
        if step.block.enter:
            self._run_steps(step.block.enter)
        try:
            self._run_steps(step.block.script)
        finally:
            if step.block.breakout and step.block.breakout.script:
                print(f">> block breakout: {step.block.name}")
                self._session.reset_handlers()
                try:
                    self._run_steps(step.block.breakout.script)
                except (TimeoutError, EOFError, RuntimeError, OSError) as e:
                    print(
                        f">> block breakout error ({type(e).__name__}): {e}",
                        file=sys.stderr,
                    )
        print(f">> block completed: {step.block.name}")

    def _step_line(self, step: LineStep):
        for line in ensure_list(step.line):
            self._session.sendline(self._render(line))

    def _step_return(self, step: ReturnStep):
        for _ in range(step.newline_count):
            self._session.sendline("")

    def _step_control(self, step: ControlStep):
        for char in ensure_list(step.control):
            self._session.sendcontrol(char)
            print(f">> control sent: ^{char.upper()}")
