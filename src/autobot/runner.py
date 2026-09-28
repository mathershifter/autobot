from __future__ import annotations

import os
import subprocess
import tempfile
from typing import Any

from rich.console import Console

from .models import Config, PluginStep, Prompt, SendEach, Step
from .registry import registry
from .session import PromptHandler, Session
from .steps import register_builtins
from .types import render as render_template

console = Console(stderr=True)

register_builtins(registry)


class Runner:
    _plugins_discovered = False

    def __init__(self, config: Config, cli_args: dict[str, str]):
        if not Runner._plugins_discovered:
            registry.discover()
            Runner._plugins_discovered = True
        self._config = config
        self._cli_args = cli_args
        self._default_timeout = 300
        self._env = self._resolve_env(config.env)
        self._session = Session([])
        handlers = [self.build_handler(p) for p in config.prompts]
        self._session.restore_handlers(handlers)

    @property
    def session(self) -> Session:
        return self._session

    @property
    def config(self) -> Config:
        return self._config

    def build_handler(self, prompt: Prompt) -> PromptHandler:
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
            return [self.render(s) for attempt in send for s in attempt]
        return [self.render(s) for s in send]

    def _resolve_env(self, defaults: dict[str, str]) -> dict[str, str]:
        env = {k: os.environ.get(k, v) for k, v in defaults.items()}
        ctx = {"env": env, "vars": self._config.vars, "args": self._cli_args}
        for iteration in range(10):
            changed = False
            for k, v in env.items():
                rendered = render_template(v, ctx)
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

    def render(self, template: Any, extra_ctx: dict | None = None) -> str:
        ctx = self._ctx
        if extra_ctx:
            ctx = {**ctx, **extra_ctx}
        return render_template(template, ctx)

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
        spawn = self.render(attach.spawn)
        timeout = int(attach.timeout) if attach.timeout else self._default_timeout
        env = attach.env or {"TERM": "dumb", "NO_COLOR": "1"}

        if attach.prepare:
            self._run_prepare(self.render(attach.prepare))

        console.print(f">> attach: {spawn}")
        self._session.attach(spawn, env=env, timeout=timeout)
        try:
            if attach.script:
                self.run_steps(attach.script)
            self.run_steps(self._config.script)
        finally:
            if attach.breakout and attach.breakout.script:
                console.print(">> breakout: detaching")
                self._session.reset_handlers()
                try:
                    self.run_steps(attach.breakout.script)
                except (TimeoutError, EOFError, RuntimeError, OSError) as e:
                    console.print(
                        f">> breakout error ({type(e).__name__}): {e}")
            self._session.detach()

    def run_steps(self, steps: list[Step]):
        for step in steps:
            self._run_step(step)

    def _get_timeout(self, step: Any) -> float:
        timeout = getattr(step, "timeout", None)
        return timeout if timeout is not None else self._default_timeout

    def _run_step(self, step: Step):
        timeout = self._get_timeout(step)

        after = getattr(step, "after", None)
        if after:
            self._session.expect([self.render(after)], timeout=timeout)

        when = getattr(step, "when", None)
        if when is not None:
            result = self.render(when)
            if result in ("", "false", "False", "0", "none"):
                return

        delay_before = getattr(step, "delay_before", None)
        if delay_before:
            self._session.sleep(delay_before)

        key = registry.key_for_step(step)
        executor = registry.get(key)
        # plugin models don't carry the common properties; keep `step` intact
        model = registry.validate_plugin_step(step) if isinstance(step, PluginStep) else step
        executor.execute(model, self, timeout)

        delay_after = getattr(step, "delay_after", None)
        if delay_after:
            self._session.sleep(delay_after)
