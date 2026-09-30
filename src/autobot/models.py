from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Annotated, Any

import pydantic
from pydantic_core import PydanticCustomError

from .types import Duration, StringOrArray


class SendEach(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    each: str
    fields: list[str] | None = None

    @pydantic.field_validator("each")
    @classmethod
    def _validate_each(cls, v: str) -> str:
        if not re.fullmatch(r"vars(\.[^.]+)+", v):
            raise ValueError(f"each must be a path under vars, like vars.creds, got: {v}")
        return v


class Prompt(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    name: str
    expect: list[str | list[str]]
    send: list[str] | list[list[str]] | SendEach | None = None
    is_shell_prompt: bool = pydantic.Field(False, alias="return", strict=True)


class Function(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    script: list[Step]


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
    ignore_error: bool = pydantic.Field(False, strict=True)
    register_: str | None = pydantic.Field(None, alias="register")
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
    newline_count: int = pydantic.Field(alias="return", ge=1, strict=True)
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


class PluginStep(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="allow")
    plugin_key_: str | None = pydantic.Field(None, exclude=True)
    after: str | None = None
    when: str | None = None
    delay_before: Duration | None = None
    delay_after: Duration | None = None
    timeout: Duration | None = None

    @pydantic.model_validator(mode="before")
    @classmethod
    def _capture_plugin_key(cls, data: Any) -> Any:
        if isinstance(data, dict):
            from .registry import registry

            for key in data:
                if key not in _COMMON_PROPS and registry.has(key):
                    data["plugin_key_"] = key
                    break
        return data


_BUILTIN_KEYS = ("cmd", "sleep", "call", "block", "line", "return", "control")
_COMMON_PROPS = {"after", "when", "delay_before", "delay_after", "timeout", "plugin_key_"}


def _step_discriminator(v: Any) -> str | None:
    if isinstance(v, dict):
        for key in _BUILTIN_KEYS:
            if key in v:
                return key
        from .registry import registry

        for key in v:
            if key not in _COMMON_PROPS and registry.has(key):
                return "plugin"
    return None


Step = Annotated[
    Annotated[CmdStep, pydantic.Tag("cmd")]
    | Annotated[SleepStep, pydantic.Tag("sleep")]
    | Annotated[CallStep, pydantic.Tag("call")]
    | Annotated[BlockStep, pydantic.Tag("block")]
    | Annotated[LineStep, pydantic.Tag("line")]
    | Annotated[ReturnStep, pydantic.Tag("return")]
    | Annotated[ControlStep, pydantic.Tag("control")]
    | Annotated[PluginStep, pydantic.Tag("plugin")],
    pydantic.Discriminator(
        _step_discriminator,
        custom_error_type="invalid_step",
        custom_error_message=(
            "cannot determine step type; expected one of "
            f"{', '.join(_BUILTIN_KEYS)} or a registered plugin step"
        ),
    ),
]

Attach.model_rebuild()
Breakout.model_rebuild()
Block.model_rebuild()
Function.model_rebuild()


def _walk_steps(loc: tuple, steps: list[Step]) -> Iterator[tuple[tuple, Step]]:
    for i, step in enumerate(steps):
        yield (*loc, i), step
        if isinstance(step, BlockStep):
            block, here = step.block, (*loc, i, "block")
            yield from _walk_steps((*here, "enter"), block.enter)
            yield from _walk_steps((*here, "script"), block.script)
            if block.breakout:
                yield from _walk_steps((*here, "breakout", "script"), block.breakout.script)


class Config(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    autobot: str

    @pydantic.field_validator("autobot")
    @classmethod
    def _validate_autobot(cls, v: str) -> str:
        if not re.fullmatch(r"\d{4}-\d{2}", v):
            raise ValueError(f"autobot must be YYYY-MM format, got: {v}")
        return v
    env: dict[str, str] = {}
    vars: dict[str, Any] = {}
    prompts: list[Prompt] = []
    fn: dict[str, Function] = {}
    errors: list[str] = []
    attach: Attach
    script: list[Step]

    @pydantic.model_validator(mode="after")
    def _check_steps(self) -> Config:
        from .registry import registry

        roots: list[tuple[tuple, list[Step]]] = [
            (("attach", "script"), self.attach.script),
            (("script",), self.script),
        ]
        if self.attach.breakout:
            roots.append((("attach", "breakout", "script"), self.attach.breakout.script))
        roots += [(("fn", name, "script"), f.script) for name, f in self.fn.items()]

        errors: list[Any] = []
        for root, steps in roots:
            for loc, step in _walk_steps(root, steps):
                if isinstance(step, CallStep) and step.call not in self.fn:
                    errors.append({
                        "type": PydanticCustomError(
                            "undefined_function",
                            "call to undefined function '{name}'",
                            {"name": step.call},
                        ),
                        "loc": (*loc, "call"),
                        "input": step.call,
                    })
                elif isinstance(step, PluginStep):
                    try:
                        registry.validate_plugin_step(step)
                    except pydantic.ValidationError as e:
                        errors += [
                            {
                                # the plugin's text goes through ctx, so braces in it survive
                                "type": PydanticCustomError(
                                    err["type"], "{msg}", {"msg": err["msg"]}
                                ),
                                "loc": (*loc, *err["loc"]),
                                "input": err["input"],
                            }
                            for err in e.errors()
                        ]
        if errors:
            raise pydantic.ValidationError.from_exception_data(type(self).__name__, errors)
        return self
