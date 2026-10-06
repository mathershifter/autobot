from __future__ import annotations

import datetime
import os
import subprocess
import tempfile
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from jinja2 import StrictUndefined
from rich.console import Console

from .models import BlockStep, Config, PluginStep, Prompt, SendEach, Step, names_command
from .registry import registry
from .session import PromptHandler, Session, SimpleHandler
from .steps import register_builtins
from .types import EnvError, RunError, ScriptError, check_regex, check_template, text
from .types import render as render_template

# markup off: log lines echo commands, names and errors that may look like [tags]
console = Console(stderr=True, markup=False, soft_wrap=True)

register_builtins(registry)

# Stripped before looking for a shebang: whitespace and line breaks, a BOM,
# and the zero-width characters that copy and paste leave behind.
_PREPARE_JUNK = " \t\r\n\ufeff\u200b\u2060"

_KINDS = {
    dict: "a mapping", list: "a list", str: "a string", int: "a number", float: "a number", bool: "a boolean",
    datetime.date: "a timestamp", datetime.datetime: "a timestamp", bytes: "binary data", set: "a set",
}


def _kind(value: Any) -> str:
    return "null" if value is None else _KINDS.get(type(value), f"a {type(value).__name__}")


def _scalar(value: Any) -> bool:
    return isinstance(value, (str, int, float)) and not isinstance(value, bool)


def _not_text(value: bool) -> str:
    # YAML reads yes, on and True as the same boolean, so the value as written isn't known here
    return (
        f"a boolean ({text(value)}), which is never sent as text; "
        "quote the value to send it as written, e.g. 'true' or 'yes'"
    )


def send_each_sets(name: str, send: SendEach, vars: dict[str, Any]) -> list[list[str]]:
    """Credential sets from `send.each` (a `vars.a.b` path of mapping keys), one per item."""

    def fail(problem: str) -> ScriptError:
        return ScriptError(f"prompt '{name}': sendEach '{send.each}': {problem}")

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

    fields = [e.field for e in send.fields or []]
    sets: list[list[str]] = []
    for i, item in enumerate(obj):
        if not fields:
            if isinstance(item, bool):
                raise fail(f"item {i} is {_not_text(item)}")
            if not _scalar(item):
                raise fail(f"item {i} is {_kind(item)}; without fields each item must be a string or number")
            sets.append([str(item)])
            continue
        if not isinstance(item, dict):
            raise fail(f"item {i} is {_kind(item)}, not a mapping")
        for f in fields:
            if f not in item:
                raise fail(f"item {i} has no field '{f}'")
            if isinstance(item[f], bool):
                raise fail(f"item {i} field '{f}' is {_not_text(item[f])}")
            if not _scalar(item[f]):
                raise fail(f"item {i} field '{f}' is {_kind(item[f])}, not a string or number")
        sets.append([str(item[f]) for f in fields])
    return sets


ENV_DEPTH = 50
WHAT_MAX = 72


@dataclass(frozen=True)
class StepRef:
    """A step of the script, for error reports: its path, its key and what it does."""

    path: str
    key: str
    what: str
    plugin: bool = False


def _ref(step: Any, path: str) -> StepRef:
    if isinstance(step, PluginStep):
        key = step.plugin_key_ or "plugin"
        return StepRef(path, key, key, True)
    fields = getattr(type(step), "model_fields", None)
    if not fields:  # not a step model: the registry reports it
        return StepRef(path, type(step).__name__, type(step).__name__)
    # a built-in step's first field is its key
    name, info = next(iter(fields.items()))
    key, value = info.alias or name, getattr(step, name)
    more = ""
    if key == "block":
        value = value.name
    elif key == "cmd" and isinstance(value, list):
        more = f" (+{len(value) - 1} more)" if len(value) > 1 else ""
        value = value[0] if value else ""
    elif key not in ("cmd", "call"):  # a `line` may be a password; the others say nothing more than the key
        return StepRef(path, key, key)
    # as written, not rendered, and only the first line
    first = next((l for l in str(value).splitlines() if l.strip()), "").strip()
    if len(first) > WHAT_MAX:
        first = first[:WHAT_MAX] + "..."
    return StepRef(path, key, f"{key}: {first}{more}" if first or more else key)


def trail(error: BaseException) -> tuple[StepRef, ...]:
    """The steps that were running when `error` was raised, outermost first; empty outside a step."""
    return getattr(error, "autobot_trail", ())


class _EnvRefs(Mapping[str, Any]):
    """`env` while it's resolved: a default is rendered once, when first read, so a cycle is caught.

    Values in `fixed` (from the OS environment) are used as they are, never rendered.
    """

    def __init__(self, raw: dict[str, str], fixed: dict[str, str], ctx: dict[str, Any]):
        self.__raw = raw
        self.__ctx = {**ctx, "env": self}
        self.__done: dict[str, Any] = dict(fixed)
        self.__path: list[str] = []

    def __getitem__(self, key: str) -> Any:
        if key not in self.__raw:
            return StrictUndefined(hint=f"env has no key '{key}'")
        if key not in self.__done:
            if key in self.__path:
                cycle = [*self.__path[self.__path.index(key):], key]
                raise EnvError(f"env cycle: {' -> '.join(cycle)}")
            if len(self.__path) == ENV_DEPTH:
                raise EnvError(f"env nesting deeper than {ENV_DEPTH} levels: {self.__path[0]} -> ... -> {key}")
            self.__path.append(key)
            try:
                self.__done[key] = render_template(self.__raw[key], self.__ctx)
            finally:
                self.__path.pop()
        return self.__done[key]

    def __contains__(self, key: object) -> bool:
        return key in self.__raw

    def get(self, key: str, default: Any = None) -> Any:
        return self[key] if key in self.__raw else default

    def __iter__(self) -> Iterator[str]:
        return iter(self.__raw)

    def __len__(self) -> int:
        return len(self.__raw)


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
        self._stack: list[StepRef] = []
        # the path of every step list of the script, by the list's identity: executors pass run_steps the list
        self._paths: dict[int, str] = {}
        self._index(config.attach.script, "attach.script")
        self._index(config.script, "script")
        self._index(config.attach.breakout, "attach.breakout")
        for name, fn in config.fn.items():
            self._index(fn.script, f"fn.{name}.script")

    def _index(self, steps: list[Step], path: str):
        self._paths[id(steps)] = path
        for i, step in enumerate(steps):
            if isinstance(step, BlockStep):
                for part in ("enter", "script", "breakout"):
                    self._index(getattr(step.block, part), f"{path}.{i}.block.{part}")

    @property
    def session(self) -> Session:
        return self._session

    @property
    def config(self) -> Config:
        return self._config

    def build_handler(self, prompt: Prompt) -> PromptHandler:
        send = prompt.send
        expect = [e for e in prompt.expect or [] if isinstance(e, str)]  # grouped entries are rejected
        if send is None:  # a shell prompt (a return prompt can't have send)
            return PromptHandler(prompt.name, expect, [], True)
        if isinstance(send, str):
            # a literal send is a template, rendered each time it is sent
            check_template(send)
            return SimpleHandler(prompt.name, expect, send, self.render)
        patterns: list[str] = []
        slots: list[int | None] = []
        if send.fields:
            # entry k's regexes are alternatives that send field k of the current item
            for k, entry in enumerate(send.fields):
                patterns.extend(entry.match)
                slots.extend([k] * len(entry.match))
        patterns.extend(expect)
        slots.extend([None] * len(expect))
        responses = send_each_sets(prompt.name, send, self._config.vars)
        return PromptHandler(prompt.name, patterns, responses, False, slots)

    def _resolve_env(self, defaults: dict[str, str]) -> dict[str, str]:
        fixed = {k: os.environ[k] for k in defaults if k in os.environ}
        refs = _EnvRefs(defaults, fixed, {"vars": self._config.vars, "args": self._cli_args})
        return {k: refs[k] for k in defaults}

    @property
    def _ctx(self) -> dict:
        return {
            "env": self._env,
            "vars": self._config.vars,
            "args": self._cli_args,
            "session": self._session.ctx,
        }

    def render(self, template: Any, extra_ctx: dict | None = None, *, condition: bool = False) -> str:
        """Render a template to text. A boolean isn't text, so an expression that gives one is a template
        error, unless `condition`: the text is then only read as a yes or no, as `when` reads it."""
        ctx = self._ctx
        if extra_ctx:
            ctx = {**ctx, **extra_ctx}
        return render_template(template, ctx, condition=condition)

    @staticmethod
    def _run_prepare(script: str):
        script = script.lstrip(_PREPARE_JUNK)
        first, nl, rest = script.partition("\n")
        if script.startswith("#!"):
            first = first.removesuffix("\r")
            script = first + nl + rest
            argv: list[str] = []
            console.print(">> prepare: running local script")
        else:
            argv = ["/bin/sh"]
            console.print(">> prepare: running local script (no shebang, using /bin/sh)")
        f = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix="_autobot_", suffix=".sh", delete=False
        )
        tmp = f.name
        try:
            with f:
                f.write(script)  # may fail, e.g. on a lone surrogate from a non-UTF-8 --arg
            os.chmod(tmp, 0o700)
            try:
                result = subprocess.run([*argv, tmp], check=False)
            except OSError as e:
                raise RunError(
                    f"prepare script could not run ({first!r}): [Errno {e.errno}] {e.strerror}"
                ) from e
            if result.returncode != 0:
                raise RunError(
                    f"prepare script failed with exit code {result.returncode}"
                )
        finally:
            os.unlink(tmp)
        console.print(">> prepare: done")

    def run(self):
        attach = self._config.attach
        # pexpect takes leading whitespace for an empty first word, e.g. from a template that renders to nothing
        spawn = self.render(attach.spawn).lstrip()
        if not names_command(spawn):
            raise ScriptError(f"attach.spawn rendered to an empty command: {attach.spawn!r}")
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
                if attach.breakout:
                    console.print(">> breakout: detaching")
                    try:
                        self._session.reset_handlers()
                        self.run_steps(attach.breakout)
                    except Exception as e:  # noqa: BLE001 - breakout is best-effort
                        console.print(f">> breakout error ({type(e).__name__}): {e}")
        except BaseException:
            self._session.detach(failing=True)  # a close that fails must not replace this error
            raise
        self._session.detach()

    def run_steps(self, steps: list[Step]):
        path = self._paths.get(id(steps))
        if path is None:
            # a list built in code, e.g. by a plugin: named after the step that runs it
            path = f"{self._stack[-1].path}.{self._stack[-1].key}" if self._stack else "steps"
        for i, step in enumerate(steps):
            self._run_step(step, f"{path}.{i}")

    def _get_timeout(self, step: Any) -> float:
        timeout = getattr(step, "timeout", None)
        return timeout if timeout is not None else self._default_timeout

    def _run_step(self, step: Step, path: str = "steps.0"):
        self._stack.append(_ref(step, path))
        try:
            self._step(step)
        except BaseException as e:
            # where it failed, for the CLI's report: set once, by the innermost step
            if not hasattr(e, "autobot_trail"):
                e.autobot_trail = tuple(self._stack)  # type: ignore[attr-defined]
            raise
        finally:
            self._stack.pop()

    def _step(self, step: Step):
        timeout = self._get_timeout(step)

        after = getattr(step, "after", None)
        if after is not None:
            pattern = self.render(after)
            if not pattern:
                raise ScriptError("after: the pattern rendered to an empty regex, which matches at once")
            check_regex(pattern, "after")
            self._session.expect([pattern], timeout=timeout, what=f"the after pattern '{pattern}'")

        when = getattr(step, "when", None)
        if when is not None:
            result = self.render(when, condition=True)
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
