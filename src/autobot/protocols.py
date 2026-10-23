from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import MutableMapping, Sequence

    import pydantic

    from .models import Config, Prompt, Step
    from .session import PromptHandler, Session


class RunnerContext(Protocol):
    @property
    def session(self) -> Session: ...

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
