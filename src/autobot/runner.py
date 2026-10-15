from __future__ import annotations

import datetime
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from jinja2 import StrictUndefined

from . import log, prepare
from .models import BlockStep, Config, PluginStep, Prompt, SendEach, Step, names_command
from .registry import registry
from .session import PromptHandler, Session, SimpleHandler
from .steps import register_builtins
from .terminal import run_environ
from .types import RunError, ScriptError, check_regex, check_template, text
from .types import render as render_template


register_builtins(registry)

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


class BreakoutError(RunError):
    """The script completed, and a breakout did not: what it was to undo, a login for one, may be left."""


@dataclass(frozen=True)
class Left:
    """What a run may have left behind: the errors that ended its breakouts, in the order they were raised,
    and the prompts it answered with credentials."""

    breakouts: tuple[BaseException, ...]
    logins: tuple[str, ...]
    pending: bool = False  # no shell prompt was reached after the credentials: the login may not have happened


def left(error: BaseException) -> Left | None:
    """What the run that `error` ended may have left behind; None when every breakout finished."""
    return getattr(error, "autobot_left", None)


_TRUTH = {"1": True, "true": True, "yes": True, "on": True, "0": False, "false": False, "no": False, "off": False, "": False}
_TYPES = {bool: "a boolean", int: "an integer", float: "a number"}


def _typed(key: str, value: str, default: Any, source: str) -> Any:
    """Text from `source` (`--env` or the environment) as a value of the type of the default of `key`."""
    try:
        if isinstance(default, bool):
            return _TRUTH[value.strip().lower()]
        if isinstance(default, int):
            if not re.fullmatch(r"[+-]?[0-9]+", value.strip()):
                raise ValueError
            return int(value)
        if isinstance(default, float):
            if not math.isfinite(number := float(value)):
                raise ValueError
            return number
    except (KeyError, ValueError):
        # without the value, which may be a secret, and without the exception that holds it
        raise ScriptError(f"env.{key}: the value from {source} is not {_TYPES[type(default)]}") from None
    return value


class _Env(dict):  # holds its keys and nothing else: a template reads attributes too
    __slots__ = ()

    def __missing__(self, key: Any) -> Any:
        return StrictUndefined(hint=f"env has no key {key!r}: the script's env section doesn't declare it")


class Runner:
    def __init__(self, config: Config, given: dict[str, str]):
        """`given`: the `--env` values, which come before those of the environment."""
        registry.discover()
        self._config = config
        self._default_timeout = 300
        # the environment of the spawned process, autobot's own, plain; `prepare` and `env` get it between the defaults and `--env`
        self._environ = run_environ()
        for key in given:
            if key not in config.env:
                raise ScriptError(f"--env {key}: the script's env section declares no variable {key!r}")
        # an `--env` value depends on nothing `prepare` does, so it gets its type before anything runs
        self._typed = {key: _typed(key, value, config.env[key], "--env") for key, value in given.items()}
        self._given = given
        self._env: dict[str, Any] = {}  # resolved by `run`, once `prepare` has run
        self._session = Session([])
        handlers = [self.build_handler(p) for p in config.prompts]
        self._session.restore_handlers(handlers)
        self._stack: list[StepRef] = []
        self._unfinished: list[BaseException] = []  # what ended each breakout that did not finish
        # called when `prepare` has run and the process is about to be spawned, if set: the CLI's
        self.before_attach: Callable[[], None] | None = None
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
            return PromptHandler(prompt.name, expect, [], True, posix=prompt.posix)
        if isinstance(send, str):
            # a literal send is a template, rendered each time it is sent
            try:
                check_template(send)
            except ScriptError as e:  # named like the error of rendering it, and of a sendEach
                raise ScriptError(f"prompt '{prompt.name}': {e}") from e
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

    def _resolve_env(self, environ: dict[str, str]) -> dict[str, Any]:
        """`env` in templates: the variables the `env` section declares, and nothing else. Each has the
        value of `--env`, else the one of `environ` (the environment `prepare` left, or the one it would
        have been given), as the type of its default, else its default."""
        env = _Env()
        for key, default in self._config.env.items():
            if key in self._typed:
                env[key] = self._typed[key]
            elif key in environ:
                env[key] = _typed(key, environ[key], default, "the environment")
            elif default is None:
                raise ScriptError(f"env.{key} is required: give it with --env {key}=VALUE or in the environment")
            else:
                env[key] = default
        return env

    @property
    def _ctx(self) -> dict:
        return {"env": self._env, "vars": self._config.vars, "session": self._session.ctx}

    def render(
        self, template: Any, extra_ctx: dict | None = None, *, condition: bool = False, keep_newline: bool = False
    ) -> str:
        """Render a template to text. A boolean isn't text, so an expression that gives one is a template
        error, unless `condition`: the text is then only read as a yes or no, as `when` reads it.
        `keep_newline`: the line break that ends the template is kept, as in a value that is stored."""
        ctx = self._ctx
        if extra_ctx:
            ctx = {**ctx, **extra_ctx}
        return render_template(template, ctx, condition=condition, keep_newline=keep_newline)

    _run_prepare = staticmethod(prepare.run)

    def _spawn_command(self) -> str:
        attach = self._config.attach
        # pexpect takes leading whitespace for an empty first word, e.g. from a template that renders to nothing
        spawn = self.render(attach.spawn).lstrip()
        if not names_command(spawn):
            raise ScriptError(f"attach.spawn rendered to an empty command: {attach.spawn!r}")
        if "\0" in spawn:
            raise ScriptError(f"attach.spawn rendered to a command line with a NUL character: {attach.spawn!r}")
        return spawn

    def run(self):
        self._unfinished = []  # nothing of an earlier run of this runner counts for this one
        log.clear()
        try:
            self._run()
        finally:
            # lost output is the run's, and gone with it. It is what ended the run, whatever came after it
            # on the way out (an interrupt)
            lost = log.clear()
            if lost:
                self._mark(lost)
                raise lost

    def _run(self):
        attach = self._config.attach
        check_template(attach.spawn)  # a syntax error stops the run before `prepare`
        timeout = self._get_timeout(attach)
        # what each declared variable starts with, as text: its default, the run's environment over it, `--env` over both
        defaults = {k: text(v) for k, v in self._config.env.items() if v is not None}
        environ = {**defaults, **self._environ, **self._given}
        if self._config.prepare:
            # as written, not rendered. No process can be started with an empty name, a `=` in a name or a NUL
            fit = {k: v for k, v in environ.items() if k and "=" not in k and "\0" not in k + v}
            environ = self._run_prepare(self._config.prepare, fit)
        self._env = self._resolve_env(environ)
        spawn = self._spawn_command()

        if self.before_attach:
            self.before_attach()
        log.say(f"attach: {spawn}")
        try:
            self._session.attach(spawn, env=self._environ, timeout=timeout)
            try:
                if attach.script:
                    self.run_steps(attach.script)
                self.run_steps(self._config.script)
            finally:
                if attach.breakout:
                    log.say("breakout: detaching", "group")
                    self.run_breakout(attach.breakout)
                names = self._session.logins_open
                if names and not self._unfinished:  # a breakout that failed is reported, with the credentials
                    log.say(
                        f"no logout: the run sent credentials (prompt{'s' if len(names) != 1 else ''} "
                        f"{', '.join(repr(n) for n in names)}), and no breakout ran after them to log out "
                        "before the session is closed",
                        "warn",
                    )
        except BaseException as e:
            self._session.detach(failing=True)  # a close that fails must not replace this error
            self._mark(e)
            raise
        try:
            self._session.detach()
        except BaseException as e:
            self._mark(e)
            raise
        if self._unfinished:
            first = self._unfinished[0]
            error = BreakoutError(f"a breakout did not finish ({type(first).__name__}): {first}")
            error.autobot_trail = trail(first)  # type: ignore[attr-defined]
            self._mark(error)
            raise error

    def _mark(self, error: BaseException):
        """Note on `error`, the one that ends the run, what the run may have left behind, for the CLI's report."""
        if self._unfinished:
            error.autobot_left = Left(  # type: ignore[attr-defined]
                tuple(self._unfinished), self._session.logins, self._session.login_pending
            )

    def run_breakout(self, steps: list[Step], what: str = "breakout"):
        """Run the steps of a breakout, best-effort: an error ends the breakout and is logged, not raised,
        so what comes after it still runs. It is kept, since the run can't count as completed. An interrupt
        ends the breakout as well, and so does lost output: they are kept, and go on. No breakout runs once
        the run's output is lost."""
        log.check()
        try:
            self._session.reset_handlers()
            self.run_steps(steps)
            self._session.logins_covered()
        except (KeyboardInterrupt, log.OutputLost) as e:
            self._unfinished.append(e)
            raise
        except Exception as e:  # noqa: BLE001 - breakout is best-effort
            self._unfinished.append(e)
            log.say(f"{what} error ({type(e).__name__}): {e}", "warn")

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
        log.depth = len(self._stack) - 1
        try:
            self._step(step)
        except BaseException as e:
            # where it failed, for the CLI's report: set once, by the innermost step, which also says so
            # at once, before any breakout runs
            if not hasattr(e, "autobot_trail"):
                e.autobot_trail = tuple(self._stack)  # type: ignore[attr-defined]
                if isinstance(e, KeyboardInterrupt):
                    log.say("step interrupted", "fail")
                elif isinstance(e, Exception):
                    log.say(f"step failed ({type(e).__name__}): {e}", "fail")
            raise
        finally:
            self._stack.pop()
            log.depth = max(len(self._stack) - 1, 0)

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
