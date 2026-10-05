from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Annotated, Any

import pydantic
from pydantic_core import PydanticCustomError

from .types import Duration, NotNull, Omittable, StringOrArray, ensure_list

VERSION = "2026-10"


def _custom(type_: str, msg: str) -> PydanticCustomError:
    # the text goes through ctx, so braces in it survive
    return PydanticCustomError(type_, "{msg}", {"msg": msg})


def _error(type_: str, msg: str, loc: tuple, input_: Any) -> dict[str, Any]:
    return {"type": _custom(type_, msg), "loc": loc, "input": input_}


_EMPTY_REGEX = "a regex must not be empty: an empty regex matches at once, before any output"


_TEMPLATE_RE = re.compile(r"\{[{%#]")


def _regex_error(v: str) -> str | None:
    try:
        re.compile(v)
    except re.error as e:
        return f"invalid regex {v!r}: {e}"
    return None


def _compiles(v: str) -> str:
    if msg := _regex_error(v):
        raise _custom("invalid_regex", msg)
    return v


def _regex(v: str) -> str:
    if v == "":
        raise _custom("string_too_short", _EMPTY_REGEX)
    return _compiles(v)


def _error_regex(v: str) -> str:
    if v == "":
        raise _custom(
            "string_too_short",
            "an errors pattern must not be empty: an empty regex matches any output, so every command would fail",
        )
    return _compiles(v)


def _template_regex(v: str) -> str:
    # a template is a regex only once it is rendered; it is checked then
    return v if _TEMPLATE_RE.search(v) else _compiles(v)


# a character that isn't whitespace (the characters of str.isspace, written out so that the schema's
# pattern, an ECMA regex, means the same; the schema uses this exact pattern)
_BLANK = [(0x09, 0x0D), (0x1C, 0x1F), 0x20, 0x85, 0xA0, 0x1680, (0x2000, 0x200A), 0x2028, 0x2029, 0x202F, 0x205F, 0x3000]
NOT_BLANK = "[^%s]" % "".join(
    "-".join(f"\\u{c:04x}" for c in (r if isinstance(r, tuple) else (r,))) for r in _BLANK
)


def _spawn(v: str) -> str:
    if not re.search(NOT_BLANK, v):
        raise _custom("empty_command", "spawn must be a command, not an empty or blank string")
    return v


Regex = Annotated[str, pydantic.AfterValidator(_regex)]
ErrorRegex = Annotated[str, pydantic.AfterValidator(_error_regex)]
# `after`: a regex once rendered
After = Annotated[str, pydantic.AfterValidator(_template_regex)]


class FieldEntry(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    match: list[Regex]
    field: str

    @pydantic.model_validator(mode="before")
    @classmethod
    def _reject_field_name(cls, data: Any) -> Any:
        if isinstance(data, str):
            raise _custom(
                "fields_entry",
                "since 2026-10 a fields entry pairs a regex with a field: "
                f"write {{match: <regex>, field: {data}}} (see \"Migrating from 2026-08\" in SPEC.md)",
            )
        return data

    @pydantic.field_validator("match", mode="before")
    @classmethod
    def _match_list(cls, v: Any) -> Any:
        if isinstance(v, str):
            return [_regex(v)]
        if v == []:
            raise _custom("too_short", "match must be a regex or a non-empty list of regexes")
        return v


class SendEach(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    each: str
    fields: Omittable[Annotated[list[FieldEntry], pydantic.Field(min_length=1)]] = None

    @pydantic.field_validator("each")
    @classmethod
    def _validate_each(cls, v: str) -> str:
        if not re.fullmatch(r"vars(\.[^.]+)+", v):
            raise ValueError(f"each must be a path under vars, like vars.creds, got: {v}")
        return v


_MIGRATE = '(see "Migrating from 2026-08" in SPEC.md)'


class Prompt(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    name: str
    # entries are single non-empty regexes; a grouped or empty entry is kept here so _check_expect can name it
    expect: Omittable[list[str | list[str]]] = None
    send: Omittable[str | SendEach] = None
    is_shell_prompt: NotNull[bool] = pydantic.Field(False, alias="return", strict=True)

    @pydantic.field_validator("expect", mode="before")
    @classmethod
    def _expect(cls, v: Any) -> Any:
        return [v] if isinstance(v, str) else v

    @pydantic.field_validator("send", mode="wrap")
    @classmethod
    def _send(cls, v: Any, handler: pydantic.ValidatorFunctionWrapHandler) -> Any:
        # a mapping can only be a sendEach: report its errors at send.<key>, not under each union member
        if isinstance(v, dict):
            return SendEach.model_validate(v)
        if isinstance(v, list):
            raise _custom(
                "send_list",
                "send is a single string: for a simple prompt write send: '<response>'; "
                "to answer a sequence of prompts such as a login, use sendEach with fields "
                f"and keep the values in vars {_MIGRATE}",
            )
        if v is not None and not isinstance(v, str):
            raise _custom(
                "send_type",
                "send must be a string; quote it, e.g. send: 'yes' or send: '1234' "
                "(unquoted, YAML reads yes, no, on, off, true, false and numbers as booleans or numbers)",
            )
        return handler(v)

    @pydantic.model_validator(mode="wrap")
    @classmethod
    def _check_expect(cls, data: Any, handler: pydantic.ModelWrapValidatorHandler[Prompt]) -> Prompt:
        self = handler(data)
        # a single regex is reported at expect, an entry of a list at expect.<i>
        single = isinstance(data, dict) and isinstance(data.get("expect"), str)
        send = self.send if isinstance(self.send, SendEach) else None
        errors = []
        if self.is_shell_prompt and self.send is not None:
            errors.append(_error(
                "return_with_send",
                "a return prompt is a shell prompt and sends nothing; remove send or return",
                ("send",), self.model_dump(by_alias=True)["send"],
            ))
        if send and send.fields:
            if self.expect is not None:
                errors.append(_error(
                    "expect_with_fields",
                    "a prompt whose sendEach has fields has no expect: the patterns are the fields' match regexes",
                    ("expect",), self.expect,
                ))
        elif self.expect is None:
            errors.append({"type": "missing", "loc": ("expect",), "input": self.model_dump(by_alias=True)})
        elif not self.expect:
            errors.append(_error("too_short", "expect must be a regex or a non-empty list of regexes", ("expect",), []))
        else:
            for i, entry in enumerate(self.expect):
                if isinstance(entry, list):
                    errors.append(_error(
                        "grouped_expect",
                        "each expect entry is a single regex, and the regexes are alternatives; "
                        f"to answer a sequence of prompts such as a login, use sendEach with fields {_MIGRATE}",
                        ("expect", i), entry,
                    ))
                elif entry == "":
                    errors.append(_error("string_too_short", _EMPTY_REGEX, ("expect",) if single else ("expect", i), ""))
                elif msg := _regex_error(entry):
                    errors.append(_error("invalid_regex", msg, ("expect",) if single else ("expect", i), entry))
        if errors:
            raise pydantic.ValidationError.from_exception_data(cls.__name__, errors)
        return self


class Function(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    script: list[Step]


class Attach(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    prepare: Omittable[str] = None
    spawn: Annotated[str, pydantic.AfterValidator(_spawn)]
    timeout: Omittable[Duration] = None
    env: Omittable[dict[str, str]] = None
    script: NotNull[list[Step]] = []
    breakout: Omittable[Breakout] = None


class CmdStep(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    cmd: StringOrArray
    after: Omittable[After] = None
    when: Omittable[str] = None
    assert_: Omittable[StringOrArray] = pydantic.Field(None, alias="assert")
    ignore_error: NotNull[bool] = pydantic.Field(False, strict=True)
    register_: Omittable[Annotated[str, pydantic.StringConstraints(min_length=1)]] = pydantic.Field(
        None, alias="register"
    )
    delay_before: Omittable[Duration] = None
    delay_after: Omittable[Duration] = None
    timeout: Omittable[Duration] = None

    @pydantic.field_validator("assert_")
    @classmethod
    def _assert(cls, v: StringOrArray | None) -> StringOrArray | None:
        for pattern in ensure_list(v):
            if pattern == "":
                raise _custom(
                    "string_too_short",
                    "an assert pattern must not be empty: an empty regex matches any output, "
                    "so the assert would check nothing",
                )
            _template_regex(pattern)
        return v


class SleepStep(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    sleep: Duration


class CallStep(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    call: str
    after: Omittable[After] = None
    when: Omittable[str] = None
    delay_before: Omittable[Duration] = None
    delay_after: Omittable[Duration] = None
    timeout: Omittable[Duration] = None


class Breakout(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    script: NotNull[list[Step]] = []


class Block(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    name: str
    prompts: NotNull[list[Prompt]] = []
    enter: NotNull[list[Step]] = []
    script: NotNull[list[Step]] = []
    breakout: Omittable[Breakout] = None


class BlockStep(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    block: Block
    after: Omittable[After] = None
    when: Omittable[str] = None
    delay_before: Omittable[Duration] = None
    delay_after: Omittable[Duration] = None
    timeout: Omittable[Duration] = None


class LineStep(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    line: StringOrArray
    after: Omittable[After] = None
    when: Omittable[str] = None
    delay_before: Omittable[Duration] = None
    delay_after: Omittable[Duration] = None


class ReturnStep(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    newline_count: int = pydantic.Field(alias="return", ge=1, strict=True)
    after: Omittable[After] = None
    when: Omittable[str] = None
    delay_before: Omittable[Duration] = None
    delay_after: Omittable[Duration] = None


# what sendcontrol maps to a control character: Ctrl+A..Z in either case, and the punctuation keys
CONTROL_RE = re.compile(r"[A-Za-z@`\[{\\|\]}^~_?]")


class ControlStep(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    control: StringOrArray
    after: Omittable[After] = None
    when: Omittable[str] = None
    delay_before: Omittable[Duration] = None
    delay_after: Omittable[Duration] = None
    timeout: Omittable[Duration] = None

    @pydantic.field_validator("control")
    @classmethod
    def _control(cls, v: StringOrArray) -> StringOrArray:
        for char in ensure_list(v):
            if not CONTROL_RE.fullmatch(char):
                raise _custom(
                    "control_char",
                    f"a control value is one character, a letter or one of @ ` [ {{ \\ | ] }} ^ ~ _ ?, got '{char}'",
                )
        return v


class PluginStep(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="allow")
    plugin_key_: str | None = pydantic.Field(None, exclude=True)
    after: Omittable[After] = None
    when: Omittable[str] = None
    delay_before: Omittable[Duration] = None
    delay_after: Omittable[Duration] = None
    timeout: Omittable[Duration] = None

    @pydantic.model_validator(mode="before")
    @classmethod
    def _capture_plugin_key(cls, data: Any) -> Any:
        if isinstance(data, dict):
            from .registry import registry

            if "plugin_key_" in data:
                raise _custom("extra_forbidden", "plugin_key_ is internal and can't be set in a script")
            for key in data:
                if key not in _COMMON_PROPS and registry.has(key):
                    return {**data, "plugin_key_": key}
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
        if v == "2026-08":
            raise _custom(
                "unsupported_version",
                f"autobot 2026-08 is no longer supported; use {VERSION} (see \"Migrating from 2026-08\" in SPEC.md)",
            )
        if v != VERSION:
            raise _custom("unsupported_version", f"unsupported autobot version {v!r}; expected {VERSION}")
        return v
    env: NotNull[dict[str, str]] = {}
    vars: NotNull[dict[str, Any]] = {}
    prompts: NotNull[list[Prompt]] = []
    fn: NotNull[dict[str, Function]] = {}
    errors: NotNull[list[ErrorRegex]] = []
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
                    # the step's other keys reach the first plugin's model as fields, and an extra="allow"
                    # model would take a second plugin's key as data, so that step would never run
                    keys = [k for k in step.model_extra or {} if registry.has(k)]
                    if len(keys) > 1:
                        msg = f"step has more than one plugin key: {', '.join(keys)}"
                        errors.append(_error("invalid_step", msg, loc, step.model_dump(exclude_unset=True)))
                        continue
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
