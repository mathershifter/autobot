from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Mapping, MutableMapping, Sequence
    from contextlib import AbstractContextManager

    import pydantic

    from .log import OutputLost
    from .models import Config, Prompt, Step
    from .session import PromptHandler


class Reporter(Protocol):
    """Where Autobot says what it does, and what keeps a run's lost output. The CLI builds one
    (`autobot.log.ConsoleReporter`) and the runner, the session and the steps share it."""

    depth: int
    """How deep the running step is nested in blocks and calls: the runner sets it, `say` indents by it."""

    @property
    def lost(self) -> OutputLost | None:
        """The `OutputLost` of this run, once a write of the operator's output has failed, until `clear`."""
        ...

    def say(self, text: str, kind: str = "step") -> None:
        """Print one progress line, `>> <text>`, at the running step's depth. `kind` names what the line
        means: `step`, `group`, `ok`, `detail`, `warn` or `fail` (see SPEC, Output)."""
        ...

    def check(self) -> None:
        """Raise the run's `OutputLost` again, if there is one: nothing is written or sent after it."""
        ...

    def clear(self) -> OutputLost | None:
        """Forget the lost output, of a run that is over or about to start, and return it if there was one."""
        ...

    def echoed(self, data: str) -> None:
        """The session's echo wrote `data` to stdout: note whether that left a line open."""
        ...

    def writing(self, stream: object) -> AbstractContextManager[None]:
        """Around a write to the operator's `stream`: a failure is raised as `OutputLost`, and kept."""
        ...


@runtime_checkable
class SessionLike(Protocol):
    """What a step does with the session (`autobot.session.Session` is one): the built-in steps and plugins
    use these and nothing else. A prompt wait is `get_prompt`; `sendline` sends a line, `sendcontrol` a
    control character; `check_rc` is the `$?` check; `sleep` waits while the child is read."""

    @property
    def ctx(self) -> Mapping[str, str]:
        """`session.before` and `session.match` of the templates: what the last `after` or prompt wait set."""
        ...

    @property
    def posix(self) -> bool:
        """Whether the session is at a shell prompt of a `posix` prompt: the shell there has `$?`."""
        ...

    def get_prompt(
        self, timeout: float = 300, errors: Sequence[str] | None = None, capture: bool = True, solicit: bool = True
    ) -> str: ...

    def sendline(self, line: str = "", *, solicit: bool = False, timeout: float = 300) -> None: ...

    def sendcontrol(self, char: str, *, timeout: float = 300) -> None: ...

    def check_rc(self, timeout: float = 300, *, posix: bool = False) -> int: ...

    def sleep(self, seconds: float) -> None: ...


class RunnerContext(Protocol):
    @property
    def session(self) -> SessionLike:
        """The session of the run (see `SessionLike`)."""
        ...

    @property
    def config(self) -> Config:
        """The script as loaded. It is read, never written: what a run changes is in `vars`."""
        ...

    @property
    def vars(self) -> MutableMapping[str, Any]:
        """The run's `vars`: what templates read, and what `register` and `set` write. Each run starts
        with a copy of the script's `vars`."""
        ...

    @property
    def errors(self) -> Sequence[str]:
        """The top-level `errors` patterns."""
        ...

    @property
    def reporter(self) -> Reporter:
        """Where a step says what it does, as the built-in steps do with `ctx.reporter.say(...)`."""
        ...

    def function(self, name: str) -> list[Step]:
        """The steps of `fn.<name>`; `ValueError` when the script defines no such function."""
        ...

    def render(
        self, template: Any, extra_ctx: dict | None = None, *, condition: bool = False, keep_newline: bool = False
    ) -> str: ...

    def run_steps(self, steps: list) -> None: ...

    def run_breakout(self, steps: list, what: str = "breakout") -> None: ...

    def build_handler(self, prompt: Prompt) -> PromptHandler: ...


class StepExecutor(Protocol):
    @property
    def key(self) -> str: ...

    @property
    def model(self) -> type[pydantic.BaseModel]: ...

    def execute(self, step: Any, ctx: RunnerContext, timeout: float) -> None: ...
