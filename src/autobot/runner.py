from __future__ import annotations

import contextlib
import copy
from collections.abc import Callable, Iterator, Mapping, MutableMapping, Sequence
from typing import TYPE_CHECKING, Any, TextIO

from . import prepare
from . import registry as registry_mod
from .environment import resolve_env, typed
from .errors import BreakoutError, ScriptError
from .log import ConsoleReporter, OutputLost
from .models import Config, PluginStep, Prompt, Step, names_command
from .prompts import build_handler
from .report import Left, RunOutcome, StepRef, step_ref, trail
from .session import PromptHandler, Session
from .templating import Templater, check_regex
from .templating import templater as default_templater
from .terminal import run_environ
from .types import text

if TYPE_CHECKING:
    from .protocols import Reporter
    from .registry import StepRegistry


# what runs the `prepare` script: given the script, the environment it runs in and the reporter, it gives
# the environment the script leaves (see `run_prepare`, the runner's default)
Prepare = Callable[[str, dict[str, str], "Reporter"], dict[str, str]]


def run_prepare(script: str, environ: dict[str, str], reporter: Reporter) -> dict[str, str]:
    """Run the `prepare` script in `environ`, saying what happens, and give the environment it leaves."""
    reporter.say("prepare: running local script" + ("" if prepare.shebang(script) else " (no shebang, using /bin/sh)"))
    result = prepare.run(script, environ)
    if not result.read:
        reporter.say("prepare: its environment could not be read", "warn")
    reporter.say("prepare: done", "ok")
    return result.environ


class Runner:
    def __init__(
        self,
        config: Config,
        given: dict[str, str],
        *,
        registry: StepRegistry | None = None,
        reporter: Reporter | None = None,
        transcript: TextIO | None = None,
        environ: Mapping[str, str] | None = None,
        prepare: Prepare | None = None,
        templater: Templater | None = None,
    ):
        """`given`: the `--env` values, which come before those of the environment. The keyword arguments
        are what the run is wired to, each with the default a run has when none is given. `registry`: the
        steps the run dispatches to, `autobot.registry.registry` by default. `reporter`: where the run says
        what it does; the CLI's, or one of the runner's own on stderr. `transcript`: where the session's
        output goes, as the device sent it; the process's stdout. `environ`: the environment the run starts
        from, which `prepare` runs in, the `env` section reads and the process is spawned in; that of this
        process, on a plain terminal (`terminal.run_environ`). `prepare`: what runs the `prepare` script,
        `run_prepare`. `templater`: the Jinja2 environments templates are rendered with,
        `autobot.templating.templater`."""
        self._registry: StepRegistry = registry if registry is not None else registry_mod.registry
        self._registry.discover()
        self._config = config
        self._reporter: Reporter = reporter if reporter is not None else ConsoleReporter()
        self._prepare: Prepare = prepare if prepare is not None else run_prepare
        self._templater: Templater = templater if templater is not None else default_templater
        self._default_timeout = 300
        # the environment of the spawned process, autobot's own, plain; `prepare` and `env` get it between the defaults and `--env`
        self._environ = run_environ() if environ is None else dict(environ)
        for key in given:
            if key not in config.env:
                raise ScriptError(f"--env {key}: the script's env section declares no variable {key!r}")
        # an `--env` value depends on nothing `prepare` does, so it gets its type before anything runs
        self._typed = {key: typed(key, value, config.env[key], "--env") for key, value in given.items()}
        self._given = given
        self._env: dict[str, Any] = {}  # resolved by `run`, once `prepare` has run
        # the run's `vars`: one mapping, filled from the script's again by each `run` (see `_start`)
        self._vars: dict[str, Any] = {}
        # the top-level prompts are resolved here as well as at each run's start: a collection that can't
        # be, or a send template that doesn't parse, is an error before `prepare` runs
        self._session = Session(self._start(), reporter=self._reporter, transcript=transcript, environ=self._environ)
        self._stack: list[StepRef] = []
        self._unfinished: list[BaseException] = []  # what ended each breakout that did not finish
        # called when `prepare` has run and the process is about to be spawned, if set: the CLI's
        self.before_attach: Callable[[], None] | None = None

    @property
    def session(self) -> Session:
        return self._session

    @property
    def config(self) -> Config:
        return self._config

    @property
    def vars(self) -> MutableMapping[str, Any]:
        """The run's `vars`: the one mapping of this runner, which each `run` empties and fills again from
        the script's section, so a reference taken before `run()` reads the run's values after it."""
        return self._vars

    @property
    def env(self) -> Mapping[str, Any]:
        """The run's `env` as templates read it (SPEC, The environment in templates): the declared variables
        with their typed values, resolved by `run` once `prepare` has run; empty before that."""
        return self._env

    @property
    def errors(self) -> Sequence[str]:
        """The top-level `errors` patterns, read from the `Config` as it is now, like `vars` at a run's start."""
        return tuple(self._config.errors)

    @property
    def reporter(self) -> Reporter:
        return self._reporter

    @property
    def path(self) -> str:
        """The path of the running step (`script.3`, see SPEC, Errors while the script runs); empty outside one."""
        return self._stack[-1].path if self._stack else ""

    def function(self, name: str) -> list[Step]:
        fn = self._config.fn.get(name)
        if not fn:
            raise ValueError(f"undefined function: {name}")
        return fn.script

    def _start(self) -> list[PromptHandler]:
        """A run's start, and the runner's: `vars` filled again, in place, with a copy of the script's section,
        so that nothing a run registers or sets is seen by the next one or written into the config, and the
        top-level prompts resolved from it."""
        self._vars.clear()
        self._vars.update(copy.deepcopy(self._config.vars))
        return [self._handler(p) for p in self._config.prompts]

    @contextlib.contextmanager
    def prompts(self, prompts: list[Prompt]) -> Iterator[None]:
        """Swap the session's prompts for those of `prompts` while the body runs, and restore the previous
        ones on the way out, however the body ends: the block lifecycle's steps 1 and 5. The handlers are
        built first, from the run's `vars` as they are now; an error there swaps nothing."""
        handlers = [self._handler(p) for p in prompts]
        saved = self._session.save_handlers()
        self._session.restore_handlers(handlers)
        try:
            yield
        finally:
            self._session.restore_handlers(saved)

    def _handler(self, prompt: Prompt) -> PromptHandler:
        return build_handler(prompt, self._vars, self.render)

    def _resolve_env(self, environ: dict[str, str]) -> dict[str, Any]:
        """`env` in templates, from the `env` section, the typed `--env` values and `environ`, the environment
        `prepare` left (see `environment.resolve_env`)."""
        return resolve_env(self._config.env, self._typed, environ)

    @property
    def _ctx(self) -> dict:
        return {"env": self._env, "vars": self._vars, "session": self._session.ctx}

    def render(
        self, template: Any, extra_ctx: dict | None = None, *, condition: bool = False, keep_newline: bool = False
    ) -> str:
        """Render a template to text. A boolean isn't text, so an expression that gives one is a template
        error, unless `condition`: the text is then only read as a yes or no, as `when` reads it.
        `keep_newline`: the line break that ends the template is kept, as in a value that is stored."""
        ctx = self._ctx
        if extra_ctx:
            ctx = {**ctx, **extra_ctx}
        return self._templater.render(template, ctx, condition=condition, keep_newline=keep_newline)

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
        self._reporter.clear()
        try:
            self._run()
        finally:
            # lost output is the run's, and gone with it. It is what ended the run, whatever came after it
            # on the way out (an interrupt)
            lost = self._reporter.clear()
            if lost:
                self._mark(lost)
                raise lost

    def _run(self):
        attach = self._config.attach
        # the run's own `vars`, and the top-level prompts resolved from them, before `prepare` runs
        self._session.restore_handlers(self._start())
        self._templater.check_template(attach.spawn)  # a syntax error stops the run before `prepare`
        timeout = self._get_timeout(attach)
        self._env = self._resolve_run_env()
        spawn = self._spawn_command()

        if self.before_attach:
            self.before_attach()
        self._reporter.say(f"attach: {spawn}")
        try:
            self._attach_and_run(spawn, timeout)
        except BaseException as e:
            self._session.detach(failing=True)  # a close that fails must not replace this error
            self._mark(e)
            raise
        try:
            self._session.detach()
        except BaseException as e:
            self._mark(e)
            raise
        self._raise_if_unfinished()

    def _resolve_run_env(self) -> dict[str, Any]:
        """The run's `env`: `prepare` run, if there is one, in the environment the declared variables start
        with, and the variables resolved from what it left."""
        # what each declared variable starts with, as text: its default, the run's environment over it, `--env` over both
        defaults = {k: text(v) for k, v in self._config.env.items() if v is not None}
        environ = {**defaults, **self._environ, **self._given}
        if self._config.prepare:
            # as written, not rendered. No process can be started with an empty name, a `=` in a name or a NUL
            fit = {k: v for k, v in environ.items() if k and "=" not in k and "\0" not in k + v}
            environ = self._prepare(self._config.prepare, fit, self._reporter)
        return self._resolve_env(environ)

    def _attach_and_run(self, spawn: str, timeout: float):
        """Spawn the process and run `attach.script` and `script`; `attach.breakout` runs after them however
        they end, and then the warning for credentials no breakout followed."""
        attach = self._config.attach
        self._session.attach(spawn, env=self._environ, timeout=timeout)
        try:
            if attach.script:
                self.run_steps(attach.script, path="attach.script")
            self.run_steps(self._config.script, path="script")
        finally:
            if attach.breakout:
                self._reporter.say("breakout: detaching", "group")
                self.run_breakout(attach.breakout, path="attach.breakout")
            self._warn_no_logout()

    def _warn_no_logout(self):
        names = self._session.logins_open
        if names and not self._unfinished:  # a breakout that failed is reported, with the credentials
            self._reporter.say(
                f"no logout: the run sent credentials (prompt{'s' if len(names) != 1 else ''} "
                f"{', '.join(repr(n) for n in names)}), and no breakout ran after them to log out "
                "before the session is closed",
                "warn",
            )

    def _raise_if_unfinished(self):
        """A run whose breakouts all finished is complete; otherwise it ends with a `BreakoutError` for the
        first breakout that did not."""
        if self._unfinished:
            first = self._unfinished[0]
            error = BreakoutError(f"a breakout did not finish ({type(first).__name__}): {first}")
            RunOutcome.of(error).trail = trail(first)
            self._mark(error)
            raise error

    def _mark(self, error: BaseException):
        """Note on `error`, the one that ends the run, what the run may have left behind, for the CLI's report."""
        if self._unfinished:
            RunOutcome.of(error).left = Left(tuple(self._unfinished), self._session.logins, self._session.login_pending)

    def run_breakout(self, steps: list[Step], what: str = "breakout", *, path: str | None = None):
        """Run the steps of a breakout, best-effort: an error ends the breakout and is logged, not raised,
        so what comes after it still runs. It is kept, since the run can't count as completed. An interrupt
        ends the breakout as well, and so does lost output: they are kept, and go on. No breakout runs once
        the run's output is lost. `path` names the list as `run_steps` does."""
        self._reporter.check()
        try:
            self._session.reset_handlers()
            self.run_steps(steps, path=path)
            self._session.logins_covered()
        except (KeyboardInterrupt, OutputLost) as e:
            self._unfinished.append(e)
            raise
        except Exception as e:  # noqa: BLE001 - breakout is best-effort
            self._unfinished.append(e)
            self._reporter.say(f"{what} error ({type(e).__name__}): {e}", "warn")

    def run_steps(self, steps: list[Step], *, path: str | None = None):
        """Run `steps` in order. `path` is the list's path in the script, for the report (`script`,
        `fn.<name>.script`, `<block's path>.block.enter`); without it the list is one built in code, e.g.
        by a plugin, and is named after the step that runs it."""
        if path is None:
            path = f"{self._stack[-1].path}.{self._stack[-1].key}" if self._stack else "steps"
        for i, step in enumerate(steps):
            self._run_step(step, f"{path}.{i}")

    def _get_timeout(self, step: Any) -> float:
        timeout = getattr(step, "timeout", None)
        return timeout if timeout is not None else self._default_timeout

    def _run_step(self, step: Step, path: str = "steps.0"):
        self._stack.append(step_ref(step, path))
        self._reporter.depth = len(self._stack) - 1
        try:
            self._step(step)
        except BaseException as e:
            # where it failed, for the CLI's report: set once, by the innermost step, which also says so
            # at once, before any breakout runs
            outcome = RunOutcome.of(e)
            if outcome.trail is None:
                outcome.trail = tuple(self._stack)
                if isinstance(e, KeyboardInterrupt):
                    self._reporter.say("step interrupted", "fail")
                elif isinstance(e, Exception):
                    self._reporter.say(f"step failed ({type(e).__name__}): {e}", "fail")
            raise
        finally:
            self._stack.pop()
            self._reporter.depth = max(len(self._stack) - 1, 0)

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

        key = self._registry.key_for_step(step)
        executor = self._registry.get(key)
        # plugin models don't carry the common properties; keep `step` intact. The plugin's model is the
        # one that passed when the script was loaded, validated once (SPEC, Common Step Properties)
        model = step.validated(self._registry) if isinstance(step, PluginStep) else step
        executor.execute(model, self, timeout)

        delay_after = getattr(step, "delay_after", None)
        if delay_after:
            self._session.sleep(delay_after)
