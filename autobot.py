#!/usr/bin/env python3

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from typing import Annotated, Any

import jinja2
import pexpect
import pydantic
import yaml

ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
DURATION_RE = re.compile(r"^(\d+(?:\.\d+)?)(ms|s|m|h)$")
DURATION_MULT = {"ms": 0.001, "s": 1, "m": 60, "h": 3600}
_jinja_env = jinja2.Environment(undefined=jinja2.StrictUndefined)


def parse_duration(value: Any) -> float:
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return float(value)
    m = DURATION_RE.match(str(value))
    if m:
        return float(m.group(1)) * DURATION_MULT[m.group(2)]
    raise ValueError(f"invalid duration: {value}")


Duration = Annotated[float, pydantic.BeforeValidator(parse_duration)]
StringOrArray = str | list[str]


class CleanWriter:
    def __init__(self, stream):
        self._stream = stream

    def write(self, data):
        data = ANSI_ESCAPE_RE.sub("", data)
        if data:
            self._stream.write(data)
            self._stream.flush()

    def flush(self):
        self._stream.flush()


def render(template: Any, ctx: dict) -> str:
    if not isinstance(template, str):
        return template
    try:
        return _jinja_env.from_string(template).render(ctx)
    except jinja2.UndefinedError as e:
        raise ValueError(f"template error: {e}")


def ensure_list(value: StringOrArray | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


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


class AttachConfig(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    spawn: str
    timeout: Duration | None = None
    env: dict[str, str] | None = None
    script: list[Step] = []
    breakout: Breakout | None = None


class CmdStep(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    cmd: StringOrArray
    when: str | None = None
    assert_: StringOrArray | None = pydantic.Field(None, alias="assert")
    delay_before: Duration | None = None
    delay_after: Duration | None = None
    timeout: Duration | None = None


class SleepStep(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    sleep: Duration


class CallStep(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    call: str
    when: str | None = None
    delay_before: Duration | None = None
    delay_after: Duration | None = None
    timeout: Duration | None = None


class Breakout(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    script: list[Step] = []


class BlockBody(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    name: str
    script: list[Step] = []
    breakout: Breakout | None = None


class BlockStep(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    block: BlockBody
    when: str | None = None
    delay_before: Duration | None = None
    delay_after: Duration | None = None
    timeout: Duration | None = None

    @pydantic.model_validator(mode="before")
    @classmethod
    def normalize_indent(cls, data: Any) -> Any:
        if isinstance(data, dict) and data.get("block") is None and "name" in data:
            data = dict(data)
            block: dict[str, Any] = {"name": data.pop("name"), "script": data.pop("script", [])}
            if "breakout" in data:
                block["breakout"] = data.pop("breakout")
            data["block"] = block
        return data


class LineStep(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    line: StringOrArray
    when: str | None = None
    delay_before: Duration | None = None
    delay_after: Duration | None = None


class ReturnStep(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    newline_count: int = pydantic.Field(1, alias="return")
    when: str | None = None
    delay_before: Duration | None = None
    delay_after: Duration | None = None


class ControlStep(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    control: StringOrArray
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

# Resolve forward references now that Step is defined.
AttachConfig.model_rebuild()
Breakout.model_rebuild()
BlockBody.model_rebuild()
Function.model_rebuild()


class ScriptConfig(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    version: str
    env: dict[str, str] = {}
    vars: dict[str, Any] = {}
    prompts: list[Prompt] = []
    fn: dict[str, Function] = {}
    attach: AttachConfig
    script: list[Step] = []


class PromptHandler:
    """One prompt = one concern. Flat pattern list, flat response queue."""

    def __init__(self, prompt: Prompt, start: int, render_fn, resolve_fn):
        self.prompt = prompt
        self.is_return = prompt.is_shell_prompt or prompt.send is None
        self.patterns: list[str] = []
        for entry in prompt.expect:
            if isinstance(entry, list):
                self.patterns.extend(entry)
            else:
                self.patterns.append(entry)
        self.start = start
        self.end = start + len(self.patterns)
        self._responses = self._build_responses(prompt.send, render_fn, resolve_fn)
        self._idx = 0

    @staticmethod
    def _build_responses(send, render_fn, resolve_fn) -> list[str]:
        if not send:
            return []
        if isinstance(send, SendEach):
            items = resolve_fn(send.each)
            if send.fields:
                return [str(item[f]) for item in items for f in send.fields]
            return [str(item) for item in items]
        if send and isinstance(send[0], list):
            return [render_fn(s) for attempt in send for s in attempt]
        return [render_fn(s) for s in send]

    @property
    def exhausted(self) -> bool:
        return bool(self._responses) and self._idx >= len(self._responses)

    def next_response(self) -> str | None:
        if not self._responses or self._idx >= len(self._responses):
            return None
        response = self._responses[self._idx]
        self._idx += 1
        return response

    @property
    def is_fresh(self) -> bool:
        return self._idx == 0

    def reset(self):
        self._idx = 0


class Session:
    def __init__(self, prompts: list[Prompt], render_fn, resolve_fn):
        self._cld: pexpect.spawn | None = None
        self._handlers: list[PromptHandler] = []
        self._patterns: list = [r"\r\n", ANSI_ESCAPE_RE]
        for p in prompts:
            h = PromptHandler(
                p, start=len(self._patterns), render_fn=render_fn, resolve_fn=resolve_fn
            )
            self._handlers.append(h)
            self._patterns.extend(h.patterns)
        self._patterns.append(pexpect.TIMEOUT)
        self._patterns.append(pexpect.EOF)

    def attach(self, spawn: str, env: dict[str, str] | None = None, timeout: int = 300):
        self._cld = pexpect.spawn(
            spawn,
            timeout=timeout,
            encoding="utf-8",
            codec_errors="replace",
            env=env or {"TERM": "dumb", "NO_COLOR": "1"},
        )
        self._cld.logfile_read = CleanWriter(sys.stdout)
        self._cld.expect(r".+", timeout=timeout)

    def detach(self):
        if self._cld:
            self._cld.close()
            self._cld = None

    def get_prompt(self, timeout: float = 300):
        if not self._cld:
            raise RuntimeError("not attached")
        for h in self._handlers:
            h.reset()
        deadline = time.monotonic() + timeout
        solicited = False
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("timed out waiting for prompt")
            i = self._cld.expect(self._patterns, timeout=min(5, remaining))
            if i == 0 or i == 1:
                continue
            if i == len(self._patterns) - 2:
                if not solicited and all(h.is_fresh for h in self._handlers):
                    self._cld.sendline("")
                    solicited = True
                continue
            if i == len(self._patterns) - 1:
                raise EOFError("connection closed")
            for h in self._handlers:
                if h.start <= i < h.end:
                    if h.is_return:
                        print(">> got prompt...")
                        return
                    if h.exhausted:
                        raise RuntimeError(
                            f"prompt '{h.prompt.name}': responses exhausted"
                        )
                    response = h.next_response()
                    if response is None:
                        raise RuntimeError(
                            f"prompt '{h.prompt.name}': no response available"
                        )
                    self._cld.sendline(response)
                    break

    def reset_handlers(self):
        for h in self._handlers:
            h.reset()

    def expect(self, patterns: list, timeout: float = 300) -> int:
        if not self._cld:
            raise RuntimeError("not attached")
        return self._cld.expect(patterns, timeout=timeout)

    def sendline(self, line: str = ""):
        if not self._cld:
            raise RuntimeError("not attached")
        self._cld.sendline(line)

    def sendcontrol(self, char: str):
        if not self._cld:
            raise RuntimeError("not attached")
        self._cld.sendcontrol(char)

    def sleep(self, seconds: float):
        if not self._cld:
            raise RuntimeError("not attached")
        self._cld.expect(pexpect.TIMEOUT, timeout=seconds)


class ScriptRunner:
    def __init__(self, config: ScriptConfig, cli_args: dict[str, str]):
        self._config = config
        self._cli_args = cli_args
        self._default_timeout = 300
        self._env = self._resolve_env(config.env)
        self._session = Session(config.prompts, self._render, self._resolve)

    def _resolve_env(self, defaults: dict[str, str]) -> dict[str, str]:
        env = {k: os.environ.get(k, v) for k, v in defaults.items()}
        ctx = {"env": env, "vars": self._config.vars, "args": self._cli_args}
        for _ in range(10):
            changed = False
            for k, v in env.items():
                rendered = render(v, ctx)
                if rendered != v:
                    env[k] = rendered
                    changed = True
            if not changed:
                break
        return env

    @property
    def _ctx(self) -> dict:
        return {
            "env": self._env,
            "vars": self._config.vars,
            "args": self._cli_args,
        }

    def _resolve(self, path: str) -> Any:
        parts = path.split(".")
        obj: Any = self._ctx
        for part in parts:
            if isinstance(obj, dict):
                obj = obj[part]
            else:
                obj = getattr(obj, part)
        return obj

    def _render(self, template: Any, extra_ctx: dict | None = None) -> str:
        ctx = self._ctx
        if extra_ctx:
            ctx = {**ctx, **extra_ctx}
        return render(template, ctx)

    def run(self):
        attach = self._config.attach
        spawn = self._render(attach.spawn)
        timeout = int(attach.timeout) if attach.timeout else self._default_timeout
        env = attach.env or {"TERM": "dumb", "NO_COLOR": "1"}

        print(f">> attach: {spawn}")
        self._session.attach(spawn, env=env, timeout=timeout)
        try:
            if attach.script:
                self._run_steps(attach.script)
            self._run_steps(self._config.script)
            if self._config.script:
                self._session.get_prompt()
        finally:
            if attach.breakout and attach.breakout.script:
                print(">> breakout: detaching")
                self._session.reset_handlers()
                try:
                    self._run_steps(attach.breakout.script)
                except (TimeoutError, EOFError, RuntimeError, OSError) as e:
                    print(
                        f">> breakout error ({type(e).__name__}): {e}", file=sys.stderr
                    )
            self._session.detach()

    def _run_steps(self, steps: list[Step]):
        for step in steps:
            self._run_step(step)

    def _get_timeout(self, step: Any) -> float:
        timeout = getattr(step, "timeout", None)
        return timeout if timeout is not None else self._default_timeout

    def _run_step(self, step: Step):
        timeout = self._get_timeout(step)

        when = getattr(step, "when", None)
        if when:
            self._session.expect([self._render(when)], timeout=timeout)

        delay_before = getattr(step, "delay_before", None)
        if delay_before:
            self._session.sleep(delay_before)

        if isinstance(step, CmdStep):
            self._step_cmd(step, timeout)
        elif isinstance(step, SleepStep):
            self._step_sleep(step)
        elif isinstance(step, CallStep):
            self._step_call(step, timeout)
        elif isinstance(step, BlockStep):
            self._step_block(step)
        elif isinstance(step, LineStep):
            self._step_line(step)
        elif isinstance(step, ReturnStep):
            self._step_return(step)
        elif isinstance(step, ControlStep):
            self._step_control(step)

        delay_after = getattr(step, "delay_after", None)
        if delay_after:
            self._session.sleep(delay_after)

    def _step_cmd(self, step: CmdStep, timeout: float):
        lines = ensure_list(step.cmd)
        for line in lines:
            cmd = self._render(line)
            if not step.when:
                self._session.get_prompt(timeout=timeout)

            self._session.sendline(cmd)
            print(f">> cmd: {cmd}")
        assertions = ensure_list(step.assert_)
        if assertions:
            self._session.expect(
                [self._render(a) for a in assertions], timeout=timeout
            )

    def _step_sleep(self, step: SleepStep):
        print(f">> sleep: {step.sleep}s")
        self._session.sleep(step.sleep)

    def _step_call(self, step: CallStep, timeout: float):
        fn = self._config.fn.get(step.call)
        if not fn:
            raise ValueError(f"undefined function: {step.call}")
        self._run_steps(fn.script)
        print(f">> called {step.call}")

    def _step_block(self, step: BlockStep):
        print(f">> block running: {step.block.name}")
        self._run_steps(step.block.script)
        if step.block.breakout:
            print(f">> block breakout: {step.block.name}")
            self._run_steps(step.block.breakout.script)
        print(f">> block completed: {step.block.name}")

    def _step_line(self, step: LineStep):
        for line in ensure_list(step.line):
            self._session.sendline(self._render(line))

    def _step_return(self, step: ReturnStep):
        for _ in range(step.newline_count):
            self._session.sendline("")

    def _step_control(self, step: ControlStep):
        for char in ensure_list(step.control):
            self._session.sendcontrol(char)
            print(f">> control sent: ^{char.upper()}")


def main():
    parser = argparse.ArgumentParser(description="Execute an autobot script.")
    parser.add_argument("script", type=str, help="Path to the YAML script file")
    parser.add_argument(
        "-a",
        "--arg",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Pass arguments to the script (e.g. --arg console_host=10.0.0.1)",
    )
    args = parser.parse_args()

    with open(str(args.script)) as f:
        config_dict = yaml.safe_load(f)

    try:
        config = ScriptConfig(**config_dict)
    except pydantic.ValidationError as e:
        print("Validation errors:", file=sys.stderr)
        print(e, file=sys.stderr)
        sys.exit(1)

    cli_args = {}
    for item in args.arg:
        if "=" not in item:
            parser.error(f"--arg requires KEY=VALUE format, got: {item}")
        key, value = item.split("=", 1)
        cli_args[key] = value

    runner = ScriptRunner(config, cli_args)
    runner.run()


if __name__ == "__main__":
    main()
