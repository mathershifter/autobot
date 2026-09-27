from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    import pydantic

    from .models import Config, Prompt
    from .session import PromptHandler, Session


class RunnerContext(Protocol):
    @property
    def session(self) -> Session: ...

    @property
    def config(self) -> Config: ...

    def render(self, template: Any, extra_ctx: dict | None = None) -> str: ...

    def run_steps(self, steps: list) -> None: ...

    def build_handler(self, prompt: Prompt) -> PromptHandler: ...


class StepExecutor(Protocol):
    key: str
    model: type[pydantic.BaseModel]

    def execute(self, step: Any, ctx: RunnerContext, timeout: float) -> None: ...
