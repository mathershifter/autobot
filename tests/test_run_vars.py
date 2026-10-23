"""Each run has `vars` of its own, and a plugin reaches them through its `ctx` (SPEC "set" (Scope), "Plugins":
`ctx.vars`, `ctx.errors`, `ctx.function`). Review findings 1.1 and 3.4: `register` and `set` wrote into the
`Config`, so a run started with what the run before it had stored."""

from __future__ import annotations

import copy
from typing import Any

import pydantic
import pytest
from conftest import SHELL_PROMPT, make_config, make_runner

from autobot.runner import Runner

# accumulates: a run that starts with the run before it's `out` registers one `x` more than it did
ECHO_OUT = {"cmd": "echo {{ vars.out | default('') }}x", "register": "out"}


class PokeStep(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    poke: str


class PokeExecutor:
    """Writes to the run's vars: a new key, and inside a value the script's section holds."""

    key = "poke"
    model = PokeStep

    def execute(self, step: PokeStep, ctx: Any, timeout: float) -> None:
        ctx.vars["items"].append(step.poke)
        ctx.vars["poked"] = step.poke


class LookStep(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    look: str


class LookExecutor:
    """Records `ctx.errors`, and runs the function `look` names through `ctx.function`."""

    key = "look"
    model = LookStep

    def __init__(self) -> None:
        self.errors: Any = None
        self.fn: Any = None
        self.inner_path: Any = None

    def execute(self, step: LookStep, ctx: Any, timeout: float) -> None:
        self.errors = ctx.errors
        self.fn = ctx.function(step.look)
        ctx.run_steps(self.fn)
        self.inner_path = ctx.path  # the step's path again, now the function's steps are done


class WhereStep(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    where: str


class WhereExecutor:
    """Records the path it runs at, so a test can see what `run_steps` named the list."""

    key = "where"
    model = WhereStep

    def __init__(self) -> None:
        self.path: Any = None

    def execute(self, step: WhereStep, ctx: Any, timeout: float) -> None:
        self.path = ctx.path


def test_a_runner_that_runs_again_starts_with_the_scripts_vars():
    """The second run of a `Runner` sees neither what the first registered nor what it set."""
    runner = make_runner([ECHO_OUT, {"set": {"made": "{{ vars.out }}-y"}}], vars={"keep": 1})
    runner.run()
    assert runner.vars == {"keep": 1, "out": "x", "made": "x-y"}
    runner.run()
    assert runner.vars == {"keep": 1, "out": "x", "made": "x-y"}  # not `xx`: the second run saw no `out`
    assert runner.config.vars == {"keep": 1}


def test_two_runners_over_one_config_share_nothing():
    """A second `Runner` over the same `Config` starts with the section as written."""
    config = make_config([ECHO_OUT, {"set": {"made": "x"}}], vars={"keep": 1})
    before = copy.deepcopy(config.vars)
    first = Runner(config, {})
    first.run()
    second = Runner(config, {})
    second.run()
    assert first.vars["out"] == second.vars["out"] == "x"
    assert first.vars is not second.vars
    assert config.vars == before


def test_runner_vars_is_one_mapping_filled_again_by_each_run():
    """SPEC "Python API": `Runner.vars` is one mapping, emptied and filled again at each run's start, so a
    reference taken before `run()` reads the run's values after it."""
    runner = make_runner([ECHO_OUT], vars={"keep": 1})
    before = runner.vars
    assert before == {"keep": 1}
    runner.run()
    assert runner.vars is before and before == {"keep": 1, "out": "x"}
    before["stale"] = True
    runner.run()
    assert runner.vars is before and before == {"keep": 1, "out": "x"}


def test_runner_errors_read_the_config_as_vars_do(register_plugin):
    """SPEC "Python API": `errors` is read from the `Config` as it is, as `vars` is copied from it at each
    run's start, so a change to the `Config` between runs is seen by the next run either way."""
    ex = register_plugin(LookExecutor())
    runner = make_runner([{"look": "f"}], fn={"f": {"script": []}}, errors=["^ERR"], vars={"a": 1})
    runner.run()
    assert list(ex.errors) == ["^ERR"] and runner.vars == {"a": 1}
    runner.config.errors.append("^FAIL")
    runner.config.vars["b"] = 2
    runner.run()
    assert list(ex.errors) == list(runner.errors) == ["^ERR", "^FAIL"] and runner.vars == {"a": 1, "b": 2}


def test_the_config_is_not_changed_by_a_run_even_inside_a_value(register_plugin):
    """A plugin's write to `ctx.vars`, in a new key or inside a list the section holds, is the run's: the
    templates after it read it, and the `Config` keeps its value."""
    register_plugin(PokeExecutor())
    script = [{"poke": "a"}, {"cmd": "echo {{ vars.poked }} {{ vars.items | join(',') }}", "register": "echoed"}]
    runner = make_runner(script, vars={"items": ["x"]})
    runner.run()
    assert runner.vars["echoed"] == "a x,a"
    assert runner.vars["items"] == ["x", "a"]
    assert runner.config.vars == {"items": ["x"]}
    runner.run()
    assert runner.vars["items"] == ["x", "a"]


def test_ctx_vars_is_the_templating_context(register_plugin):
    """`ctx.vars` is the mapping templates read as `vars`, the one `register` and `set` write."""
    register_plugin(PokeExecutor())
    runner = make_runner([ECHO_OUT, {"poke": "b"}, {"set": {"s": "{{ vars.poked }}"}}], vars={"items": []})
    runner.run()
    assert runner.render("{{ vars.out }} {{ vars.poked }} {{ vars.s }}") == "x b b"


def test_ctx_errors_and_ctx_function(register_plugin):
    """`ctx.errors` is the top-level patterns, read-only; `ctx.function(name)` is the list of `fn.<name>`,
    the very one of the `Config`, so the steps keep their paths."""
    ex = register_plugin(LookExecutor())
    fn = {"f": {"script": [{"set": {"from_f": "yes"}}]}}
    runner = make_runner([{"look": "f"}], fn=fn, errors=["^ERR", "^FAIL"])
    runner.run()
    assert list(ex.errors) == ["^ERR", "^FAIL"]
    assert not hasattr(ex.errors, "append")
    with pytest.raises(TypeError):
        ex.errors[0] = "x"  # type: ignore[index]
    assert ex.fn is runner.config.fn["f"].script
    assert runner.vars["from_f"] == "yes"


def test_run_steps_of_a_function_list_without_path_is_named_fn_name_script(register_plugin):
    """Review #86: a plugin that runs `ctx.function(name)` without `path=` names the list `fn.<name>.script`,
    as `call` does, not `<ctx.path>.<key>`. Any other list is named after the step."""
    look = register_plugin(LookExecutor())
    where = register_plugin(WhereExecutor())
    fn = {"f": {"script": [{"where": "in-fn"}]}}
    make_runner([{"look": "f"}], fn=fn).run()
    assert where.path == "fn.f.script.0"

    where.path = None
    make_runner([{"where": "top"}]).run()  # run() gives config.script the path "script"
    assert where.path == "script.0"


def test_ctx_function_of_an_undefined_name_is_a_value_error():
    """`ctx.function` raises what a `call` to an undefined function raises when a plugin runs one."""
    runner = make_runner([], fn={"f": {"script": []}})
    assert runner.function("f") == []
    with pytest.raises(ValueError, match="^undefined function: nope$"):
        runner.function("nope")


def test_top_level_send_each_is_resolved_for_each_run():
    """The collections of the top-level prompts are resolved at each run's start, from the run's vars: a
    second run answers the prompt again, with the first credential set, as if nothing had been sent."""
    pin = {"name": "pin", "expect": ["PIN: ?$"], "send": {"each": "vars.pins"}}
    script = [{"cmd": "read -p 'PIN: ' p; echo got-$p", "register": "got"}]
    runner = make_runner(script, prompts=[SHELL_PROMPT, pin], vars={"pins": ["4321"]})
    runner.run()
    assert runner.vars["got"].splitlines()[-1] == "got-4321"
    runner.run()
    assert runner.vars["got"].splitlines()[-1] == "got-4321"
    assert runner.config.vars == {"pins": ["4321"]}
