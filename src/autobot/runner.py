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
from .types import check_template
from .types import render as render_template

# markup off: log lines echo commands, names and errors that may look like [tags]
console = Console(stderr=True, markup=False, soft_wrap=True)

register_builtins(registry)

_KINDS = {dict: "a mapping", list: "a list", str: "a string", int: "a number", float: "a number", bool: "a boolean"}


def _kind(value: Any) -> str:
    return "null" if value is None else _KINDS.get(type(value), f"a {type(value).__name__}")


def _scalar(value: Any) -> bool:
    return value is not None and not isinstance(value, (dict, list))


def send_each_sets(name: str, send: SendEach, vars: dict[str, Any]) -> list[list[str]]:
    """Credential sets from `send.each` (a `vars.a.b` path of mapping keys), one per item."""

    def fail(problem: str) -> ValueError:
        return ValueError(f"prompt '{name}': sendEach '{send.each}': {problem}")

    obj: Any = vars
    at = "vars"
    for key in send.each.split(".")[1:]:
        if not isinstance(obj, dict):
            raise fail(f"'{at}' is {_kind(obj)}, not a mapping")
        if key not in obj:
            raise fail(f"no key '{key}' in '{at}'")
        obj, at = obj[key], f"{at}.{key}"
    if not isinstance(obj, list):
        raise fail(f"'{at}' is {_kind(obj)}, not a list")

    sets: list[list[str]] = []
    for i, item in enumerate(obj):
        if not send.fields:
            if not _scalar(item):
                raise fail(f"item {i} is {_kind(item)}; without fields each item must be a string, number or boolean")
            sets.append([str(item)])
            continue
        if not isinstance(item, dict):
            raise fail(f"item {i} is {_kind(item)}, not a mapping")
        for f in send.fields:
            if f not in item:
                raise fail(f"item {i} has no field '{f}'")
            if not _scalar(item[f]):
                raise fail(f"item {i} field '{f}' is {_kind(item[f])}, not a string, number or boolean")
        sets.append([str(item[f]) for f in send.fields])
    return sets


class Runner:
    def __init__(self, config: Config, cli_args: dict[str, str]):
        registry.discover()
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
        slots: list[int | None] = []
        for entry in prompt.expect:
            if isinstance(entry, list):
                patterns.extend(entry)
                slots.extend(range(len(entry)))
            else:
                patterns.append(entry)
                slots.append(None)
        if isinstance(prompt.send, SendEach):
            responses = send_each_sets(prompt.name, prompt.send, self._config.vars)
        else:
            responses = self._build_responses(prompt.send)
        is_return = prompt.is_shell_prompt or prompt.send is None
        # literal send strings are templates, rendered as each one is sent
        render = None if isinstance(prompt.send, SendEach) else self.render
        return PromptHandler(prompt.name, patterns, responses, is_return, slots, render)

    def _build_responses(self, send) -> list[list[str]]:
        """Credential sets, one list per attempt; literal strings stay unrendered."""
        if not send:
            return []
        sets = send if isinstance(send[0], list) else [send]
        for s in (s for attempt in sets for s in attempt):
            check_template(s)
        return [list(attempt) for attempt in sets]

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
        timeout = self._get_timeout(attach)
        if attach.prepare:
            self._run_prepare(self.render(attach.prepare))

        console.print(f">> attach: {spawn}")
        try:
            self._session.attach(spawn, env=attach.env, timeout=timeout)
            try:
                if attach.script:
                    self.run_steps(attach.script)
                self.run_steps(self._config.script)
            finally:
                if attach.breakout and attach.breakout.script:
                    console.print(">> breakout: detaching")
                    try:
                        self._session.reset_handlers()
                        self.run_steps(attach.breakout.script)
                    except Exception as e:  # noqa: BLE001 - breakout is best-effort
                        console.print(f">> breakout error ({type(e).__name__}): {e}")
        finally:
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
            if result.strip().lower() in ("", "false", "0", "none"):
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
