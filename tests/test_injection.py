"""What a `Runner` and a `Session` are wired to is given by keyword (SPEC "Signals and interrupts", As a
library): the registry, the reporter, the transcript stream, the environment, what runs `prepare` and the
templater, each with the default a run has when none is given."""

from __future__ import annotations

import io
import os
from typing import Any

import pydantic
import pytest
from conftest import BASH, SHELL_ENV, SpawnRecorded, make_config, make_doc, make_runner, run_script

from autobot.errors import ScriptError
from autobot.log import ConsoleReporter
from autobot.models import PLUGINS, Config
from autobot.registry import StepRegistry
from autobot.runner import Runner
from autobot.session import PromptHandler, Session
from autobot.steps import register_builtins
from autobot.templating import Templater, templater


def test_session_transcript_is_the_injected_stream(capsys: pytest.CaptureFixture[str]):
    """The session's output goes to `transcript`, as the device sent it less the escape sequences, and
    nothing of it to stdout."""
    out = io.StringIO()
    s = Session([PromptHandler("sh", [r"PROMPT\$ "], [], True)], transcript=out)
    s.attach(BASH, env=dict(SHELL_ENV), timeout=5)
    try:
        s.get_prompt(timeout=5)
        s.sendline("echo t-r-a-n-s-c-r-i-p-t", timeout=5)
        assert s.get_prompt(timeout=5) == "t-r-a-n-s-c-r-i-p-t\n"
    finally:
        s.detach()
    assert "t-r-a-n-s-c-r-i-p-t\r\n" in out.getvalue() and out.getvalue().endswith("PROMPT$ ")
    assert capsys.readouterr().out == ""


def test_runner_hands_the_transcript_to_its_session(capsys: pytest.CaptureFixture[str]):
    out = io.StringIO()
    runner = Runner(make_config([{"cmd": "echo via-runner", "register": "r"}]), {}, transcript=out)
    runner.run()
    assert runner.vars["r"] == "via-runner"
    assert "via-runner\r\n" in out.getvalue() and capsys.readouterr().out == ""


def test_session_environ_is_the_default_of_attach(spawned, monkeypatch: pytest.MonkeyPatch):
    """An `attach` without `env` spawns in the session's `environ`, with `TERM=dumb` and `NO_COLOR=1` always
    set over it for the spawn (SPEC, The environment of the spawned process); an `env` of the call's own
    still wins and is used verbatim; without either the run's environment is taken at the attach, not when
    the session is built."""
    monkeypatch.setenv("TERM", "xterm")
    spawned.stop = True
    with pytest.raises(SpawnRecorded):
        Session([], environ={"A": "b", "TERM": "vt100"}).attach("true")
    assert spawned[-1][1]["env"] == {"A": "b", "TERM": "dumb", "NO_COLOR": "1"}
    with pytest.raises(SpawnRecorded):
        Session([], environ={"A": "b"}).attach("true", env={"C": "d"})
    assert spawned[-1][1]["env"] == {"C": "d"}
    s = Session([])
    monkeypatch.setenv("AB_LATE", "1")
    with pytest.raises(SpawnRecorded):
        s.attach("true")
    assert spawned[-1][1]["env"] == {**os.environ, "TERM": "dumb", "NO_COLOR": "1"}


def test_runner_environ_is_the_runs_environment(spawned, monkeypatch: pytest.MonkeyPatch):
    """`environ` is what the declared variables read, what `prepare` runs in and what the process is spawned
    in; the process's own environment is not consulted."""
    monkeypatch.setenv("AB_A", "from-os")
    environ = {**SHELL_ENV, "AB_A": "from-environ"}
    runner = make_runner(
        [{"cmd": "echo $AB_A", "register": "a"}],
        env={"AB_A": None, "AB_P": None},
        environ=environ,
        prepare="AB_P=$AB_A-prepared\n",
    )
    runner.run()
    assert runner.env == {"AB_A": "from-environ", "AB_P": "from-environ-prepared"}
    assert runner.vars["a"] == "from-environ"
    # the spawn gets `TERM=dumb` and `NO_COLOR=1` over the injected environ (which already has TERM=dumb)
    assert spawned[-1][1]["env"] == {**environ, "NO_COLOR": "1"}


def test_runner_prepare_runs_the_script():
    """`prepare` gets the script as written, the environment the variables start with and the reporter, and
    what it gives is the environment `env` is resolved from; `run_prepare` is the default."""
    calls: list[tuple[str, dict[str, str], Any]] = []

    def fake(script: str, environ: dict[str, str], reporter: Any) -> dict[str, str]:
        calls.append((script, environ, reporter))
        return {**environ, "AB_A": "faked"}

    rep = ConsoleReporter()
    runner = Runner(make_config([], env={"AB_A": "dflt"}, prepare="AB_A=real\n"), {}, reporter=rep, prepare=fake)
    runner.run()
    assert runner.env == {"AB_A": "faked"}
    assert len(calls) == 1 and calls[0][0] == "AB_A=real\n" and calls[0][1]["AB_A"] == "dflt" and calls[0][2] is rep
    assert run_script([], env={"AB_A": "dflt"}, prepare="AB_A=real\n").env == {"AB_A": "real"}


def test_runner_templater_renders_the_templates():
    """`ctx.render` renders with the runner's templater: a filter registered on it alone is there, and the
    default templater, the module's, doesn't have it."""
    own = Templater()
    own.text.filters["shout"] = lambda s: str(s).upper()
    assert Runner(make_config([], vars={"v": "hi"}), {}, templater=own).render("{{ vars.v | shout }}") == "HI"
    with pytest.raises(ScriptError, match="^template error: No filter named 'shout'"):
        Runner(make_config([], vars={"v": "hi"}), {}).render("{{ vars.v | shout }}")
    assert "shout" not in templater.text.filters
    assert all({"contains", "search"} <= set(env.filters) for env in (own.text, own.condition, own.value))


class NotedStep(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    noted: str


def test_runner_registry_dispatches_the_steps():
    """The runner dispatches to the registry it is given; a step the default registry doesn't know runs
    there, and the default registry is not asked about it."""
    seen: list[tuple[str, str]] = []

    class Executor:
        key = "noted"
        model = NotedStep

        def execute(self, step: NotedStep, ctx: Any, timeout: float) -> None:
            seen.append((ctx.path, step.noted))

    reg = StepRegistry()
    register_builtins(reg)
    reg.register(Executor())
    doc = make_doc([{"noted": "a"}, {"cmd": "true"}, {"noted": "b"}])
    config = Config.model_validate(doc, context={PLUGINS: reg})
    Runner(config, {}, registry=reg).run()
    assert seen == [("script.0", "a"), ("script.2", "b")]
    with pytest.raises(ValueError, match="^unknown step type: noted$"):
        Runner(config, {}).run()


class NotedIntStep(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    noted: int


def test_runner_revalidates_a_step_of_another_registrys_model():
    """Review #89 (A1): a `Config` validated with one registry, run with another that registers the step's
    key with a different model, gives the executor its own model, not the cached one of the other registry."""
    seen: list[Any] = []

    def executor(key: str, model: type) -> Any:
        class Executor:
            def execute(self, step: Any, ctx: Any, timeout: float) -> None:
                seen.append(step.noted)

        Executor.key = key
        Executor.model = model
        return Executor()

    reg_str = StepRegistry()
    register_builtins(reg_str)
    reg_str.register(executor("noted", NotedStep))
    reg_int = StepRegistry()
    register_builtins(reg_int)
    reg_int.register(executor("noted", NotedIntStep))

    doc = make_doc([{"noted": "7"}])
    config = Config.model_validate(doc, context={PLUGINS: reg_str})  # validated as a str: "7"
    assert config.script[0].validated(reg_str).noted == "7"  # type: ignore[union-attr]
    Runner(config, {}, registry=reg_int).run()  # run with the int registry
    assert seen == [7] and type(seen[0]) is int  # the int registry's model, revalidated and cached
    assert config.script[0].validated(reg_int).noted == 7  # type: ignore[union-attr]
