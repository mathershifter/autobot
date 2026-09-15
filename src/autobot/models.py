from __future__ import annotations

import re
from typing import Annotated, Any

import pydantic

from .types import Duration, StringOrArray


class SendEach(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    each: str
    fields: list[str] | None = None


class Prompt(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    name: str
    expect: list[str | list[str]]
    send: list[str] | list[list[str]] | SendEach | None = None
    is_shell_prompt: bool = pydantic.Field(False, alias="return")


class Function(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    script: list[Step] = []


class Attach(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    prepare: str | None = None
    spawn: str
    timeout: Duration | None = None
    env: dict[str, str] | None = None
    script: list[Step] = []
    breakout: Breakout | None = None


class CmdStep(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    cmd: StringOrArray
    after: str | None = None
    when: str | None = None
    assert_: StringOrArray | None = pydantic.Field(None, alias="assert")
    ignore_error: bool = False
    delay_before: Duration | None = None
    delay_after: Duration | None = None
    timeout: Duration | None = None


class SleepStep(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    sleep: Duration


class CallStep(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    call: str
    after: str | None = None
    when: str | None = None
    delay_before: Duration | None = None
    delay_after: Duration | None = None
    timeout: Duration | None = None


class Breakout(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    script: list[Step] = []


class Block(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    name: str
    prompts: list[Prompt] = []
    enter: list[Step] = []
    script: list[Step] = []
    breakout: Breakout | None = None


class BlockStep(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    block: Block
    after: str | None = None
    when: str | None = None
    delay_before: Duration | None = None
    delay_after: Duration | None = None
    timeout: Duration | None = None


class LineStep(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    line: StringOrArray
    after: str | None = None
    when: str | None = None
    delay_before: Duration | None = None
    delay_after: Duration | None = None


class ReturnStep(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    newline_count: int = pydantic.Field(1, alias="return")
    after: str | None = None
    when: str | None = None
    delay_before: Duration | None = None
    delay_after: Duration | None = None


class ControlStep(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    control: StringOrArray
    after: str | None = None
    when: str | None = None
    delay_before: Duration | None = None
    delay_after: Duration | None = None
    timeout: Duration | None = None


def _step_discriminator(v: Any) -> str:
    if isinstance(v, dict):
        for key in (
            "cmd",
            "sleep",
            "call",
            "block",
            "line",
            "return",
            "control",
        ):
            if key in v:
                return key
    raise ValueError(f"cannot determine step type: {v}")


Step = Annotated[
    Annotated[CmdStep, pydantic.Tag("cmd")]
    | Annotated[SleepStep, pydantic.Tag("sleep")]
    | Annotated[CallStep, pydantic.Tag("call")]
    | Annotated[BlockStep, pydantic.Tag("block")]
    | Annotated[LineStep, pydantic.Tag("line")]
    | Annotated[ReturnStep, pydantic.Tag("return")]
    | Annotated[ControlStep, pydantic.Tag("control")],
    pydantic.Discriminator(_step_discriminator),
]

Attach.model_rebuild()
Breakout.model_rebuild()
Block.model_rebuild()
Function.model_rebuild()


class Config(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    autobot: str

    @pydantic.field_validator("autobot")
    @classmethod
    def _validate_autobot(cls, v: str) -> str:
        if not re.match(r"^\d{4}-\d{2}$", v):
            raise ValueError(f"autobot must be YYYY-MM format, got: {v}")
        return v
    env: dict[str, str] = {}
    vars: dict[str, Any] = {}
    prompts: list[Prompt] = []
    fn: dict[str, Function] = {}
    errors: list[str] = []
    attach: Attach
    script: list[Step]
