"""The Python API (SPEC "Python API"): `Runner.attached()` holds the session open for a body of the program's
own, `Runner.steps(doc)` validates step documents as a script's `script` list is validated, `run()` is
`attached()` around the script, and the package exports what a driving program needs by name.

Tests prefixed ``test_p5_NN_`` and ``test_p7_NN_`` map to test plan rows P5-NN and P7-NN.
"""

from __future__ import annotations

import contextlib
import errno
import io
import subprocess
import sys
from pathlib import Path
from typing import Any

import pydantic
import pytest
from conftest import SentLog, Timeline, handler_names, make_config, make_doc, make_runner

from autobot import models
from autobot.errors import BreakoutError, StepFailure
from autobot.log import ConsoleReporter, OutputLost
from autobot.models import BlockStep, CallStep, CmdStep, Config, PluginStep, Prompt, ReturnStep, SetStep, SleepStep
from autobot.registry import StepRegistry
from autobot.report import StepRef, left, trail
from autobot.runner import Runner
from autobot.steps import register_builtins

STUCK = {"cmd": "true", "after": "NEVER_APPEARS", "timeout": 1}
NOT_NESTING = r"^the runner is attached already: attached\(\) and run\(\) don't nest \(run steps inside the body with run_steps\)$"


def cmd(line: str, **kw: Any) -> dict[str, Any]:
    return {"cmd": line, "timeout": "5s", **kw}


# -- P5-115: what entry and exit do, in order ----------------------------------------------------------


def test_p5_115_attached_enters_through_attach_script_and_exits_through_the_breakout(
    tmp_path: Path, timeline: Timeline, children, capsys: pytest.CaptureFixture[str]
):
    """SPEC "Driving a session: attached()": entry is `prepare`, the spawn (after `before_attach`) and
    `attach.script`, exit is `attach.breakout` and the close; the script's own steps never run; the runner
    is what is yielded, with `env` resolved."""
    log = tmp_path / "log"
    runner = make_runner(
        [cmd(f"echo script >> {log}")],
        env={"AB_A": "dflt"},
        prepare=f"#!/bin/sh\necho prepare >> {log}\nAB_A=prepared\n",
        attach_script=[cmd(f"echo attach >> {log}")],
        breakout=[cmd(f"echo breakout >> {log}")],
    )
    seen: list[tuple[str, int, str]] = []
    runner.before_attach = lambda: seen.append(("before_attach", timeline.names().count("attach"), log.read_text()))
    with runner.attached() as run:
        assert run is runner
        assert run.env == {"AB_A": "prepared"}
        assert log.read_text().split() == ["prepare", "attach"]
        run.run_steps(run.steps([cmd(f"echo body >> {log}")]))
        assert children == []  # the session is still open
    assert log.read_text().split() == ["prepare", "attach", "body", "breakout"]
    assert seen == [("before_attach", 0, "prepare\n")]  # once `prepare` has run, before the spawn
    assert timeline.names().count("attach") == 1
    assert len(children) == 1 and not children[0].isalive()
    lines = [line for line in capsys.readouterr().err.splitlines() if line.startswith(">> ")]
    assert [line.partition(" >> ")[0] for line in lines] == [  # each `cmd` line less its `>> <log>`
        ">> prepare: running local script",
        ">> prepare: done",
        f">> attach: {runner.config.attach.spawn}",
        ">> cmd: echo attach",
        ">> cmd: echo body",
        ">> breakout: detaching",
        ">> cmd: echo breakout",
    ]


@pytest.mark.parametrize("stage", ["prepare", "spawn", "attach.script"])
def test_p5_115_error_on_entry_leaves_before_the_body(tmp_path: Path, timeline: Timeline, children, stage: str):
    """An error on entry leaves the `with` statement before its body, as the error it is: a failed `prepare`
    before anything is spawned, a failed spawn wait with the process closed and no breakout, a failed
    `attach.script` with the breakout run and the session closed."""
    bo = tmp_path / "bo"
    kw: dict[str, Any] = {"breakout": [cmd(f"touch {bo}")]}
    if stage == "prepare":
        kw["prepare"], error, match = "#!/bin/sh\nexit 3\n", RuntimeError, "^prepare script failed with exit code 3$"
    elif stage == "spawn":
        kw["spawn"], error, match = "false", EOFError, "^connection closed before any output from 'false'"
    else:
        kw["attach_script"], error, match = [cmd("false")], StepFailure, "exit code 1"
    runner = make_runner([], **kw)
    entered = False
    with pytest.raises(error, match=match) as ei:
        with runner.attached():
            entered = True
    assert not entered
    assert ("attach" in timeline.names()) == (stage != "prepare")
    assert bo.exists() == (stage == "attach.script")
    assert len(children) == (0 if stage == "prepare" else 1) and not any(c.isalive() for c in children)
    expected = (StepRef("attach.script.0", "cmd", "cmd: false"),) if stage == "attach.script" else ()
    assert trail(ei.value) == expected and left(ei.value) is None


# -- P5-116: how the body's ending comes out ------------------------------------------------------------


def test_p5_116_body_error_reaches_the_caller_annotated_and_the_breakout_runs(
    tmp_path: Path, children, capsys: pytest.CaptureFixture[str]
):
    """SPEC "On exit": an exception of the body's leaves the `with` statement as it is, with `trail` the
    steps that were running and `left` what the breakout left; the breakout runs, and its own failure
    never replaces the body's error. An error the body raises itself has an empty trail."""
    bo = tmp_path / "bo"
    runner = make_runner([], breakout=[cmd(f"touch {bo}"), STUCK])
    with pytest.raises(StepFailure, match="exit code 1") as ei:
        with runner.attached() as run:
            run.run_steps(run.steps([cmd("false")]))
    error = ei.value
    assert trail(error) == (StepRef("steps.0", "cmd", "cmd: false"),)
    assert bo.exists()
    gone = left(error)
    assert gone is not None and [type(e) for e in gone.breakouts] == [TimeoutError] and gone.logins == () and not gone.pending
    assert trail(gone.breakouts[0]) == (StepRef("attach.breakout.1", "cmd", "cmd: true"),)
    assert "breakout error (TimeoutError)" in capsys.readouterr().err
    assert len(children) == 1 and not children[0].isalive()

    class Mine(Exception):
        pass

    bo.unlink()
    runner = make_runner([], breakout=[cmd(f"touch {bo}")])
    with pytest.raises(Mine) as ei:
        with runner.attached():
            raise Mine("mine")
    assert trail(ei.value) == () and left(ei.value) is None and bo.exists()
    assert len(children) == 2 and not children[1].isalive()


def test_p5_116_interrupt_in_the_body_runs_the_breakout_and_goes_on(tmp_path: Path, children):
    """A `KeyboardInterrupt` in the body is an error like any other here: the breakout runs, the session
    is closed, and the interrupt leaves the `with` statement."""
    bo = tmp_path / "bo"
    runner = make_runner([], breakout=[cmd(f"touch {bo}")])
    with pytest.raises(KeyboardInterrupt) as ei:
        with runner.attached() as run:
            run.run_steps(run.steps([cmd("true")]))
            raise KeyboardInterrupt
    assert bo.exists() and left(ei.value) is None
    assert len(children) == 1 and not children[0].isalive()


def test_p5_116_failing_breakout_after_a_normal_body_is_a_breakout_error(children, capsys: pytest.CaptureFixture[str]):
    """A body that ended normally leaves normally when every breakout finished, and otherwise the exit
    raises `BreakoutError` for the first breakout that did not, as `run()` does."""
    runner = make_runner([], breakout=[cmd("false")])
    with pytest.raises(BreakoutError, match=r"^a breakout did not finish \(StepFailure\): ") as ei:
        with runner.attached() as run:
            run.run_steps(run.steps([cmd("true")]))
    error = ei.value
    assert trail(error) == (StepRef("attach.breakout.0", "cmd", "cmd: false"),)
    gone = left(error)
    assert gone is not None and len(gone.breakouts) == 1 and isinstance(gone.breakouts[0], StepFailure)
    assert "breakout error (StepFailure)" in capsys.readouterr().err
    assert len(children) == 1 and not children[0].isalive()
    with make_runner([], breakout=[cmd("true")]).attached() as run:
        run.run_steps(run.steps([cmd("true")]))
    assert len(children) == 2 and not children[1].isalive()


class Breaking(io.StringIO):
    """A stream that fails from the write that has `mark` in it."""

    def __init__(self, mark: str = "NOW"):
        super().__init__()
        self.mark, self.broken = mark, False

    def write(self, data: str) -> int:
        self.broken = self.broken or self.mark in data
        if self.broken:
            raise BrokenPipeError(errno.EPIPE, "Broken pipe")
        return super().write(data)


@pytest.mark.parametrize("ending", ["lost", "other", "swallowed"])
def test_p5_116_lost_output_is_what_the_run_ends_with(monkeypatch: pytest.MonkeyPatch, children, sent: SentLog, ending: str):
    """SPEC "Output that can't be written": the `OutputLost` the reporter kept is what the `with` statement
    raises, whether the body let it through, raised something else after it or swallowed it; no breakout
    runs after it, and the loss is forgotten once it is raised."""
    runner = make_runner([], breakout=[{"cmd": "echo bye"}])
    monkeypatch.setattr(sys, "stdout", Breaking())
    with pytest.raises(OutputLost, match=r"^cannot write stdout: \[Errno 32\] Broken pipe$") as ei:
        with runner.attached() as run:
            try:
                run.run_steps(run.steps([cmd("echo N''OW")]))
            except OutputLost:
                if ending == "other":
                    raise ValueError("after the loss") from None
                if ending == "lost":
                    raise
    monkeypatch.undo()
    assert isinstance(ei.value.__cause__, BrokenPipeError)
    if ending == "other":
        assert isinstance(ei.value.__context__, ValueError)
    assert "echo bye" not in sent.lines() and runner.reporter.lost is None
    assert len(children) == 1 and not children[0].isalive()


# -- P5-117: inside the body, misuse, and a runner used again -------------------------------------------


def test_p5_117_run_steps_in_the_body_is_named_steps():
    """SPEC "Inside the body": a list run outside any step is named `steps.<index>`, a `path` names it
    itself, a function's own list is `fn.<name>.script`, and a `call` nests as in a script."""
    runner = make_runner([], fn={"f": {"script": [cmd("false")]}})
    false = StepRef("fn.f.script.0", "cmd", "cmd: false")
    with runner.attached() as run:
        assert run.path == ""
        with pytest.raises(StepFailure) as ei:
            run.run_steps(run.steps([cmd("true"), cmd("false")]))
        assert trail(ei.value) == (StepRef("steps.1", "cmd", "cmd: false"),)
        with pytest.raises(StepFailure) as ei:
            run.run_steps(run.steps([cmd("false")]), path="inventory")
        assert trail(ei.value) == (StepRef("inventory.0", "cmd", "cmd: false"),)
        with pytest.raises(StepFailure) as ei:
            run.run_steps(run.function("f"))
        assert trail(ei.value) == (false,)
        with pytest.raises(StepFailure) as ei:
            run.run_steps(run.steps([{"call": "f"}]))
        assert trail(ei.value) == (StepRef("steps.0", "call", "call: f"), false)
        assert run.path == ""


def test_p5_117_the_body_has_what_a_plugin_has():
    """SPEC "Inside the body": `render`, `vars`, `function`, `prompts`, `session`, `reporter`, `config`,
    `errors` and `path` work as they do for a plugin's `ctx`, and `vars` is the run's after the exit."""
    rep = ConsoleReporter()
    runner = make_runner([], vars={"v": "hi"}, env={"AB_A": "dflt"}, reporter=rep, fn={"f": {"script": [{"sleep": "10ms"}]}})
    with runner.attached() as run:
        assert run.render("{{ vars.v }}-{{ env.AB_A }}") == "hi-dflt"
        run.vars["v"] = "set in the body"
        run.run_steps(run.steps([cmd("echo {{ vars.v }}", register="out")]))
        assert run.vars["out"] == "set in the body"
        assert run.function("f") is runner.config.fn["f"].script
        assert run.reporter is rep and run.config is runner.config and run.session is runner.session
        assert list(run.errors) == [] and run.path == ""
        assert handler_names(run) == ["sh"]
        with run.prompts([Prompt.model_validate({"name": "blk", "expect": [r"PROMPT\$ "], "return": True})]):
            assert handler_names(run) == ["blk"]
            run.run_steps(run.steps([cmd("true")]))
        assert handler_names(run) == ["sh"]
        run.session.sendline("echo direct", timeout=5)
        assert run.session.get_prompt(timeout=5) == "direct\n"
    assert runner.vars == {"v": "set in the body", "out": "set in the body"}


def test_p5_117_misuse_is_a_runtime_error(children):
    """SPEC "Misuse is an error": `attached()` and `run()` while attached are a `RuntimeError` that leaves the
    session as it is; outside the lifecycle the session's own `not attached` is raised by the session and
    by the first step that needs it, while a step that needs none runs."""
    runner = make_runner([cmd("echo never")])
    with runner.attached() as run:
        with pytest.raises(RuntimeError, match=NOT_NESTING):
            with runner.attached():
                pass
        with pytest.raises(RuntimeError, match=NOT_NESTING):
            runner.run()
        run.run_steps(run.steps([cmd("echo still", register="s")]))
        assert run.vars["s"] == "still" and children == []
    assert len(children) == 1 and not children[0].isalive()
    with pytest.raises(RuntimeError, match="^not attached$"):
        runner.session.sendline("echo x")
    with pytest.raises(RuntimeError, match="^not attached$"):
        runner.session.get_prompt(timeout=1)
    with pytest.raises(RuntimeError, match="^not attached$") as ei:
        runner.run_steps(runner.steps([cmd("true")]))
    assert trail(ei.value) == (StepRef("steps.0", "cmd", "cmd: true"),)
    with pytest.raises(RuntimeError, match="^not attached$"):
        runner.run_steps(runner.steps([{"sleep": "10ms"}]))
    runner.run_steps(runner.steps([{"set": {"a": 1}}]))
    assert runner.vars["a"] == 1 and runner.render("{{ vars.a }}") == "1"
    assert len(children) == 1


def test_p5_117_runner_is_usable_again_after_attached(tmp_path: Path, children):
    """A runner may be used again when `attached()` has exited, as after `run()`: the next lifecycle starts
    with the script's `vars` and nothing left of the earlier run."""
    flag = tmp_path / "flag"
    runner = make_runner([cmd("true")], vars={"n": 0}, breakout=[cmd(f"test -e {flag}")])
    with pytest.raises(StepFailure) as first:
        with runner.attached() as run:
            run.vars["n"] = 1
            run.run_steps(run.steps([cmd("false")]))
    assert left(first.value) is not None and runner.vars == {"n": 1}
    flag.touch()
    with runner.attached() as run:
        assert run.vars == {"n": 0}
        run.run_steps(run.steps([cmd("true")]))
    runner.run()
    assert len(children) == 3 and not any(c.isalive() for c in children)


def test_p5_117_run_is_attached_around_the_script(monkeypatch: pytest.MonkeyPatch):
    """SPEC "The one-shot run()": `run()` enters `attached()`, runs `config.script` as `script` inside it,
    and exits; `attach.script` and `attach.breakout` are the lifecycle's."""
    calls: list[Any] = []
    orig_attached, orig_run_steps = Runner.attached, Runner.run_steps

    @contextlib.contextmanager
    def attached(self: Runner):
        calls.append("enter")
        with orig_attached(self) as run:
            yield run
        calls.append("exit")

    def run_steps(self: Runner, steps: list, *, path: str | None = None) -> None:
        if not self._stack:
            calls.append(("run_steps", steps is self.config.script, path))
        orig_run_steps(self, steps, path=path)

    monkeypatch.setattr(Runner, "attached", attached)
    monkeypatch.setattr(Runner, "run_steps", run_steps)
    runner = make_runner([cmd("echo a", register="a")], attach_script=[cmd("true")], breakout=[cmd("true")])
    runner.run()
    assert runner.vars["a"] == "a"
    assert calls == [
        "enter",
        ("run_steps", False, "attach.script"),
        ("run_steps", True, "script"),
        ("run_steps", False, "attach.breakout"),
        "exit",
    ]


# -- P7-30: steps(doc) ------------------------------------------------------------------------------------

FN = {"f": {"script": [{"cmd": "true"}]}}


def test_p7_30_steps_validates_as_config_validates_script():
    """SPEC "Steps from plain data": the models `Config` gives for the same `script`, and for a mistake the
    same errors, at locations that start at the list."""
    runner = make_runner([], fn=FN)
    doc = [
        {"cmd": "echo hi", "register": "r", "timeout": "5s"},
        {"sleep": "500ms"},
        {"call": "f", "when": "{{ vars.x }}"},
        {"set": {"a": 1}},
        {"block": {"name": "b", "script": [{"line": "x"}], "breakout": [{"control": "c"}]}},
        {"return": 2},
    ]
    got = runner.steps(doc)
    assert [type(s) for s in got] == [CmdStep, SleepStep, CallStep, SetStep, BlockStep, ReturnStep]
    assert got == Config.model_validate(make_doc(doc, fn=FN, step_timeout=None)).script
    assert runner.steps([]) == []
    with pytest.raises(pydantic.ValidationError) as ei:
        runner.steps({"cmd": "x"})
    assert [(e["type"], e["loc"]) for e in ei.value.errors()] == [("list_type", ())]


@pytest.mark.parametrize(
    "step",
    [
        {"cmd": "x", "after": "("},
        {"cmd": "x", "assert": ["ok", "("]},
        {"block": {"name": "b", "prompts": [{"name": "p", "expect": "(", "return": True}]}},
        {"call": "nope"},
        {"block": {"name": "b", "script": [{"call": "nope"}]}},
        {"nope": 1},
        {"cmd": "x", "timeout": "soon"},
        {"return": 0},
        {"cmd": "x", "ech0": 1},
    ],
    ids=["after-regex", "assert-regex", "prompt-regex", "undefined-call", "nested-undefined-call", "unknown-key", "duration", "return-zero", "extra-key"],
)
def test_p7_30_steps_rejects_what_config_rejects(step: dict[str, Any]):
    """An invalid regex, an undefined `call` (also inside a block), an unknown key and a bad field are the
    errors `Config.model_validate` gives, with the `script` prefix off the location."""
    runner = make_runner([], fn=FN)
    with pytest.raises(pydantic.ValidationError) as mine:
        runner.steps([step])
    with pytest.raises(pydantic.ValidationError) as theirs:
        Config.model_validate(make_doc([step], fn=FN, step_timeout=None))
    strip = lambda errs: [(e["type"], e["loc"], e["msg"]) for e in errs]  # noqa: E731
    assert strip(mine.value.errors()) == [(t, loc[1:], m) for t, loc, m in strip(theirs.value.errors())]
    assert all(loc[0] == "script" for _, loc, _ in strip(theirs.value.errors()))


class NotedStep(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    noted: str


class NotedExecutor:
    key = "noted"
    model = NotedStep

    def __init__(self) -> None:
        self.seen: list[tuple[str, str]] = []

    def execute(self, step: NotedStep, ctx: Any, timeout: float) -> None:
        self.seen.append((ctx.path, step.noted))


def test_p7_30_steps_checks_plugin_steps_against_the_runners_registry():
    """A plugin step is discriminated and validated against the runner's registry, once, with the plugin's
    errors at the step's location; a runner on a registry without the plugin rejects the step; the step runs
    on the runner's plugin inside `attached()`."""
    reg = StepRegistry()
    register_builtins(reg)
    executor = NotedExecutor()
    reg.register(executor)
    runner = Runner(make_config([]), {}, registry=reg)
    [step] = runner.steps([{"noted": "a", "timeout": "5s"}])
    assert isinstance(step, PluginStep) and step.plugin_key_ == "noted" and step.timeout == 5
    assert step.validated(reg).noted == "a" and step.validated(reg) is step.validated(reg)
    with pytest.raises(pydantic.ValidationError) as ei:
        runner.steps([{"noted": 1}])
    assert [(e["type"], e["loc"]) for e in ei.value.errors()] == [("string_type", (0, "noted"))]
    with pytest.raises(pydantic.ValidationError) as ei:
        Runner(make_config([]), {}).steps([{"noted": "a"}])
    assert [(e["type"], e["loc"]) for e in ei.value.errors()] == [("invalid_step", (0,))]
    assert models._plugins.get() is None
    with runner.attached() as run:
        run.run_steps(run.steps([{"noted": "b"}]))
    assert executor.seen == [("steps.0", "b")]


# -- P7-31: the package's names -------------------------------------------------------------------------


def test_p7_31_package_exports_what_a_driving_program_needs():
    """SPEC "Python API": `trail`, `left`, `OutputLost`, `Prompt` and the module `errors` come from the package
    by name, on first use, with `import autobot` still loading no submodule."""
    code = """
import sys
import autobot
assert not {m for m in sys.modules if m.startswith("autobot.")}
from autobot import Config, OutputLost, Prompt, Runner, errors, left, registry, trail
assert errors is sys.modules["autobot.errors"] is autobot.errors
assert registry is sys.modules["autobot.registry"]
assert trail.__module__ == left.__module__ == "autobot.report"
assert OutputLost.__module__ == "autobot.log" and Prompt.__module__ == "autobot.models"
assert issubclass(errors.BreakoutError, errors.RunError) and issubclass(OutputLost, BaseException)
assert {"errors", "left", "trail", "OutputLost", "Prompt"} <= set(autobot.__all__) <= set(dir(autobot))
print("ok")
"""
    res = subprocess.run([sys.executable, "-c", code], check=False, capture_output=True, text=True, timeout=60)
    assert (res.returncode, res.stdout.strip(), res.stderr) == (0, "ok", "")
