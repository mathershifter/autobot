"""The `set` step: assigns keys of `vars`, with values rendered as templates (SPEC "set — Set variables")."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pydantic
import pytest
import yaml
from conftest import SentLog, Timeline, make_doc, make_runner, run_script, run_vars

from autobot.models import Config, SetStep
from autobot.registry import PluginError, StepRegistry
from autobot.steps import register_builtins


def doc(step: dict[str, Any], **kw: Any) -> dict[str, Any]:
    return make_doc([step], step_timeout=None, **kw)


def errors(d: dict[str, Any]) -> list[tuple[tuple, str, str]]:
    with pytest.raises(pydantic.ValidationError) as e:
        Config.model_validate(d)
    return [(err["loc"], err["type"], err["msg"]) for err in e.value.errors()]


# -- validation ------------------------------------------------------------------


@pytest.mark.parametrize(
    "step",
    [
        {"set": {"a": "x"}},
        {"set": {"a": "{{ vars.b }}", "n": 3, "f": 0.5, "t": True, "z": None}},
        {"set": {"l": ["{{ vars.b }}", 1, [True]], "m": {"k": "{{ vars.b }}", "n": {"d": None}}}},
        {"set": {"a": "x"}, "after": "READY", "when": "{{ vars.b }}", "delay_before": "1s", "delay_after": 0},
    ],
    ids=["one", "scalars", "nested", "common-props"],
)
def test_p8_58_set_is_accepted_by_both(both_validate: Callable, step: dict[str, Any]):
    """SPEC "set": a mapping of names to any values, with after, when, delay_before and delay_after."""
    assert both_validate(doc(step)) == (True, True)
    assert isinstance(Config.model_validate(doc(step)).script[0], SetStep)


def test_p8_58_set_nested_mapping_keys_are_data(both_validate: Callable):
    """SPEC "YAML Script Structure": the keys of a mapping inside a value are data, whatever YAML makes of them."""
    assert both_validate(doc({"set": {"ports": {1: "up", 2.5: "x", None: "y"}}})) == (True, True)


@pytest.mark.parametrize(
    "step",
    [
        {"set": {}},
        {"set": {"": "x"}},
        {"set": {1: "x"}},
        {"set": []},
        {"set": "a=b"},
        {"set": None},
        {"set": {"a": "x"}, "timeout": "5s"},
        {"set": {"a": "x"}, "register": "r"},
        {"set": {"a": "x"}, "ignore_error": True},
        {"set": {"a": "x"}, "assert": "x"},
        {"set": {"a": "x"}, "after": ""},
        {"set": {"a": "x"}, "when": None},
    ],
    ids=["empty", "empty-name", "int-name", "list", "string", "null", "timeout", "register", "ignore_error",
         "assert", "empty-after", "null-when"],
)
def test_p8_58_set_rejections_are_shared_by_both(both_validate: Callable, step: dict[str, Any]):
    """SPEC "set" (Names, Common step properties): both the models and the schema reject these."""
    assert both_validate(doc(step)) == (False, False)


@pytest.mark.parametrize(
    ("step", "expected"),
    [
        ({"set": {}}, [(("script", 0, "set", "set"), "too_short", "set must assign at least one variable")]),
        ({"set": {"": "x"}}, [(("script", 0, "set", "set", "", "[key]"), "string_too_short", "String should have at least 1 character")]),
        ({"set": {1: "x"}}, [(("script", 0, "set", "set", 1, "[key]"), "string_type", "Input should be a valid string")]),
        ({"set": None}, [(("script", 0, "set", "set"), "dict_type", "Input should be a valid dictionary")]),
        ({"set": {"a": 1}, "timeout": 1}, [(("script", 0, "set", "timeout"), "extra_forbidden", "Extra inputs are not permitted")]),
        ({"set": {"a": 1}, "register": "r"}, [(("script", 0, "set", "register"), "extra_forbidden", "Extra inputs are not permitted")]),
    ],
    ids=["empty", "empty-name", "int-name", "null", "timeout", "register"],
)
def test_p8_58_set_error_locations(step: dict[str, Any], expected: list):
    """SPEC "set" (Names): the table of validation errors."""
    assert errors(doc(step)) == expected


def test_p8_58_set_top_level_vars_reject_an_empty_key_as_set_does(both_validate: Callable):
    """SPEC "YAML Script Structure", "Top-level fields" (`vars`): an empty key of the top-level `vars` is
    rejected by both, with the error a `set` step gives; a nested mapping's empty key is data."""
    bad = make_doc([], vars={"": "x", "ok": 1})
    assert both_validate(bad) == (False, False)
    assert errors(bad) == [(("vars", "", "[key]"), "string_too_short", "String should have at least 1 character")]
    assert both_validate(make_doc([], vars={"m": {"": "x"}})) == (True, True)


def test_p8_58_set_beside_a_cmd_is_a_cmd_with_an_extra_field():
    """SPEC "Common Step Properties": a built-in key takes precedence in the order of the step types."""
    assert errors(doc({"cmd": "true", "set": {"a": 1}})) == [
        (("script", 0, "cmd", "set"), "extra_forbidden", "Extra inputs are not permitted")
    ]


def test_p8_58_set_in_the_invalid_step_message():
    """SPEC "CLI": the list of step types names set."""
    [(loc, type_, msg)] = errors(doc({"sett": {"a": 1}}))
    assert type_ == "invalid_step"
    assert "cmd, sleep, call, block, line, return, control, set or a registered plugin step" in msg


def test_p8_58_set_is_a_builtin_and_a_reserved_plugin_key():
    """SPEC "Common Step Properties": `set` is a built-in step key, which no plugin may use."""
    reg = StepRegistry()
    register_builtins(reg)
    assert reg.key_for_step(SetStep.model_validate({"set": {"a": 1}})) == "set"

    class SetPlugin:
        key = "set"
        model = type("SetModel", (pydantic.BaseModel,), {"__annotations__": {"x": int}})

        def execute(self, step: Any, ctx: Any, timeout: float) -> None: ...

    with pytest.raises(PluginError, match=r"step key 'set' is reserved"):
        reg.register(SetPlugin())


# -- running -----------------------------------------------------------------------


def test_p8_58_set_renders_env_vars_and_registered_output():
    """SPEC "set": the values see env, vars and what earlier steps registered, where the step stands."""
    script = [
        {"cmd": "echo '  v1.2  '", "register": "version_output"},
        {
            "set": {
                "url": "https://{{ vars.host }}/api",
                "image": "{{ env.SITE }}.swi",
                "summary": "{{ vars.version_output | upper }}",
            }
        },
        {"cmd": "echo {{ vars.url }} {{ vars.image }}", "register": "echoed"},
    ]
    out = run_vars(script, vars={"host": "sw1"}, env={"SITE": "lab1"})
    assert out["url"] == "https://sw1/api"
    assert out["image"] == "lab1.swi"
    assert out["summary"] == "V1.2"
    assert out["echoed"] == "https://sw1/api lab1.swi"


def test_p8_58_set_sees_session_as_it_is_at_that_point():
    """SPEC "set": `session.before` is what the last prompt wait captured, when the step runs."""
    out = run_vars([{"cmd": "echo first"}, {"set": {"a": "{{ session.before }}"}}, {"cmd": "echo second"}, {"set": {"b": "{{ session.before }}"}}])
    assert out["a"] == "first\n"
    assert out["b"] == "second\n"


def test_p8_58_set_later_key_reads_earlier_key():
    """SPEC "set" (Order): the names are rendered and assigned in the order written."""
    out = run_vars([{"set": {"a": "x", "b": "{{ vars.a }}-y", "c": "{{ vars.b }}-z"}}])
    assert (out["a"], out["b"], out["c"]) == ("x", "x-y", "x-y-z")


def test_p8_58_set_replaces_a_value_and_reads_the_old_one():
    """SPEC "set": a name replaces the value it had, and its own template reads the value it replaces."""
    out = run_vars([{"set": {"n": "{{ vars.n }}{{ vars.n }}"}}, {"set": {"n": "{{ vars.n }}!"}}], vars={"n": "ab"})
    assert out["n"] == "abab!"


def test_p8_58_set_values_types():
    """SPEC "set" (Values): strings are rendered at any depth, keys are not, and other values are kept."""
    out = run_vars(
        [
            {
                "set": {
                    "num": 3,
                    "num_text": "{{ 3 }}",
                    "flag": True,
                    "nothing": None,
                    "real": 0.5,
                    "list": ["{{ vars.w }}", 1, ["{{ vars.w }}!", False]],
                    "map": {"{{ vars.w }}": "{{ vars.w }}", "inner": {"k": "{{ vars.w }}?"}, 1: "{{ vars.w }}"},
                }
            }
        ],
        vars={"w": "word"},
    )
    assert out["num"] == 3 and type(out["num"]) is int
    assert out["num_text"] == "3"
    assert out["flag"] is True
    assert out["nothing"] is None
    assert out["real"] == 0.5
    assert out["list"] == ["word", 1, ["word!", False]]
    assert out["map"] == {"{{ vars.w }}": "word", "inner": {"k": "word?"}, 1: "word"}


def test_p8_58_set_text_is_not_stripped_and_not_rendered_again():
    """SPEC "set" (Values): the rendered text is stored as it is, and a later template reads it as text."""
    out = run_vars(
        [{"set": {"padded": "  {{ vars.w }}\n", "raw": "{% raw %}{{ vars.w }}{% endraw %}"}}, {"set": {"again": "{{ vars.raw }}"}}],
        vars={"w": "word"},
    )
    assert out["padded"] == "  word\n"
    assert out["raw"] == "{{ vars.w }}"
    assert out["again"] == "{{ vars.w }}"


@pytest.mark.parametrize(
    ("value", "stored"),
    [
        ("one\ntwo\n", "one\ntwo\n"),
        ("{{ vars.w }}\n", "word\n"),
        ("a\r\nb\r\n", "a\nb\n"),
        ("a\rb", "a\nb"),
        ("{{ vars.crlf }}", "x\r\ny\r\n"),
        ("no break", "no break"),
        ("{{ 'x' -}}\n", "x"),
        ("{% if vars.w %}x{% endif -%}\n", "x"),
        ("{{ 'x' -}}\r\n", "x"),
        ("{{ 'x' }}\n\n", "x\n\n"),
        ("{{ 'x' -}}\n\n", "x"),
    ],
    ids=["block-scalar", "template", "crlf", "cr", "from-vars", "none", "expr-trim", "stmt-trim", "crlf-trim",
         "two-breaks", "two-breaks-trim"],
)
def test_p8_58_set_line_breaks(value: str, stored: str):
    """SPEC "set" (Values): line breaks of the template's text are `\\n`, the last one is kept unless
    whitespace control removes it, and a value read from vars keeps its own."""
    out = run_vars([{"set": {"v": value}}], vars={"w": "word", "crlf": "x\r\ny\r\n"})
    assert out["v"] == stored


def test_p8_58_set_trimmed_value_in_a_cmd_runs():
    """The review's case: a value whose `-}}` removes its line break, used in a quoted `cmd`, runs as one
    command (with the break put back, bash waited at its `>` prompt for the closing quote)."""
    out = run_vars([{"set": {"ws": "{{ 'x' -}}\n"}}, {"cmd": "echo \"[{{ vars.ws }}]\"", "register": "got"}])
    assert out["got"] == "[x]"


def test_p8_58_set_yaml_collections_are_stored_as_written(tmp_path: Path):
    """SPEC "set" (Values): `!!omap`, `!!pairs` and `!!set` are no list or mapping of the script, and
    nothing inside them is rendered."""
    from autobot import cli

    path = tmp_path / "s.autobot.yaml"
    path.write_text(
        "autobot: 2026-10\nattach: {spawn: sh}\nscript:\n  - set:\n"
        "      o: !!omap [{a: '{{ x }}'}, {b: 2}]\n"
        "      p: !!pairs [{a: '{{ x }}'}, {a: 3}]\n"
        "      s: !!set {'{{ x }}': null}\n"
    )
    r = make_runner([])
    step = Config.model_validate(cli._load(str(path))).script
    r.run_steps(step)
    assert r.vars == {"o": [("a", "{{ x }}"), ("b", 2)], "p": [("a", "{{ x }}"), ("a", 3)], "s": {"{{ x }}"}}


def test_p8_58_set_top_level_vars_are_not_rendered():
    """SPEC "Top-level fields" (`vars`): the values of the top-level section are used as written."""
    out = run_vars([{"set": {"copy": "{{ vars.t }}"}}], vars={"t": "{{ vars.x }}", "x": "y"})
    assert out["t"] == "{{ vars.x }}"
    assert out["copy"] == "{{ vars.x }}"


def test_p8_58_set_boolean_expression_is_a_template_error():
    """SPEC "set", "Booleans are not text": an expression that gives a boolean is refused in a value."""
    r = make_runner([{"set": {"same": "{{ vars.a == vars.b }}"}}], vars={"a": 1, "b": 1})
    with pytest.raises(ValueError, match=r"^template error: an expression gave a boolean \(true\)"):
        r.run()
    assert "same" not in r.vars


def test_p8_58_set_boolean_with_tojson_is_text():
    """SPEC "set": `| tojson` and `| string` say which text a boolean is."""
    out = run_vars([{"set": {"j": "{{ (vars.a == vars.b) | tojson }}", "s": "{{ (vars.a != vars.b) | string }}"}}], vars={"a": 1, "b": 1})
    assert (out["j"], out["s"]) == ("true", "False")


@pytest.mark.parametrize(
    ("bad", "match"),
    [
        ("{{ vars.missing }}", r"^template error: 'dict object' has no attribute 'missing'$"),
        ("{{ 1/0 }}", r"^template error: ZeroDivisionError: division by zero$"),
        ("{{ ", r"^template error: "),
        ("{{ env.NOPE }}", r"^template error: env has no key 'NOPE'"),
    ],
    ids=["undefined", "exception", "syntax", "env"],
)
def test_p8_58_set_failure_assigns_nothing(sent: SentLog, bad: str, match: str):
    """SPEC "set" (All or nothing): a template error aborts the step and the script, and no name of the
    step is assigned, not even those before the one that failed. The breakouts still run."""
    r = make_runner(
        [{"set": {"a": "new-a", "b": "{{ vars.a }}", "c": bad, "d": "new-d"}}, {"line": "echo never"}],
        vars={"a": "old-a"},
        breakout=[{"line": "echo bye"}],
    )
    with pytest.raises(ValueError, match=match):
        r.run()
    assert r.vars == {"a": "old-a"}
    assert sent.lines() == ["echo bye"]


def test_p8_58_set_in_a_list_value_failure_assigns_nothing():
    """SPEC "set" (All or nothing): a failure deep in a value also leaves vars unchanged."""
    r = make_runner([{"set": {"a": "x", "l": ["ok", {"k": "{{ nope }}"}]}}])
    with pytest.raises(ValueError, match=r"^template error: 'nope' is undefined$"):
        r.run()
    assert r.vars == {}


def test_p8_58_set_sends_nothing_and_waits_for_nothing(sent: SentLog, timeline: Timeline):
    """SPEC "set": the step makes no prompt wait and sends nothing."""
    run_script([{"set": {"a": "x"}}], attach_script=[])
    assert sent == []
    assert "get_prompt" not in timeline.names() and "expect" not in timeline.names()


def test_p8_58_set_when_false_skips_and_when_reads_earlier_set():
    """SPEC "Common Step Properties": `when` gates a set step, and a condition reads what a set assigned."""
    out = run_vars(
        [
            {"set": {"skipped": "x"}, "when": "false"},
            {"set": {"go": "yes"}},
            {"set": {"ran": "y"}, "when": "{{ vars.go == 'yes' }}"},
        ]
    )
    assert "skipped" not in out
    assert out["ran"] == "y"


def test_p8_58_set_after_and_delays(timeline: Timeline):
    """SPEC "Common Step Properties": after, then when, then delay_before, the step, delay_after."""
    script = [
        {"line": "printf 'RE%s\\n' ADY"},
        {"set": {"m": "{{ session.match }}"}, "after": "READY", "delay_before": "1s", "delay_after": "2s"},
    ]
    out = run_vars(script)
    assert out["m"] == "READY"
    assert timeline.has_subsequence([("expect", ["READY"]), ("sleep", 1.0), ("sleep", 2.0)])


def test_p8_58_set_scope_is_global_across_call_and_block():
    """SPEC "set" (Scope): a set in a function or a block assigns the run's one vars, and the value stays."""
    fn = {"f": {"script": [{"set": {"from_fn": "{{ vars.seed }}-fn"}}]}}
    script = [
        {"set": {"seed": "s"}},
        {"call": "f"},
        {"block": {"name": "b", "enter": [{"set": {"from_enter": "e"}}], "script": [{"set": {"from_block": "{{ vars.from_fn }}-b"}}],
                   "breakout": [{"set": {"from_breakout": "{{ vars.from_block }}-x"}}]}},
        {"cmd": "echo {{ vars.from_fn }} {{ vars.from_enter }} {{ vars.from_block }} {{ vars.from_breakout }}", "register": "all"},
    ]
    out = run_vars(script, fn=fn)
    assert out["all"] == "s-fn e s-fn-b s-fn-b-x"


def test_p8_58_set_feeds_a_block_send_each_collection(sent: SentLog):
    """SPEC "set" (Scope): a block's sendEach collection is resolved on entering the block, so it sees a set."""
    block = {
        "name": "b",
        "prompts": [
            {"name": "sh", "expect": [r"PROMPT\$ "], "return": True},
            {"name": "pin", "expect": ["PIN: ?$"], "send": {"each": "vars.pins"}},
        ],
        "script": [{"cmd": "read -p 'PIN: ' p; echo got-$p", "register": "got"}],
    }
    out = run_vars([{"set": {"pins": ["{{ vars.base }}7"]}}, {"block": block}], vars={"base": "123"})
    assert out["got"].splitlines()[-1] == "got-1237"


def test_p8_58_set_feeds_a_send_string(sent: SentLog):
    """SPEC "set" (Scope): a prompt's send string is rendered when it is sent, so it reads a set value."""
    prompts = [
        {"name": "sh", "expect": [r"PROMPT\$ "], "return": True},
        {"name": "q", "expect": ["Answer: ?$"], "send": "{{ vars.answer }}"},
    ]
    out = run_vars(
        [{"set": {"answer": "{{ vars.w }}-42"}}, {"cmd": "read -p 'Answer: ' a; echo got-$a", "register": "got"}],
        prompts=prompts,
        vars={"w": "x"},
    )
    assert out["got"].splitlines()[-1] == "got-x-42"


# -- output --------------------------------------------------------------------------


def test_p8_58_set_progress_line_names_without_values(tmp_path: Path):
    """SPEC "Output": `>> set: vars.<name>, ...`, in the order written, at the level of the step, never the values."""
    path = tmp_path / "script.autobot.yaml"
    script = [
        {"set": {"token": "s3cr3t-{{ vars.w }}", "url": "https://h/{{ vars.w }}"}},
        {"block": {"name": "b", "script": [{"set": {"inner": "hidden-value"}}]}},
    ]
    path.write_text(yaml.safe_dump(make_doc(script, vars={"w": "zzz"})))
    res = subprocess.run(
        [sys.executable, "-W", "ignore", "-m", "autobot.cli", str(path)],
        check=False, capture_output=True, text=True, timeout=60,
    )
    assert res.returncode == 0, res.stderr
    lines = res.stderr.splitlines()
    assert ">> set: vars.token, vars.url" in lines
    assert ">>   set: vars.inner" in lines
    assert "s3cr3t" not in res.stderr + res.stdout and "hidden-value" not in res.stderr + res.stdout


def test_p8_58_set_failure_prints_no_set_line(tmp_path: Path):
    """SPEC "set" (Output): the line is printed once the names are assigned; a failed step has none."""
    path = tmp_path / "script.autobot.yaml"
    path.write_text(yaml.safe_dump(make_doc([{"set": {"a": "x", "b": "{{ nope }}"}}])))
    res = subprocess.run(
        [sys.executable, "-W", "ignore", "-m", "autobot.cli", str(path)],
        check=False, capture_output=True, text=True, timeout=60,
    )
    assert res.returncode == 3, res.stderr
    assert not any(line.startswith(">> set:") for line in res.stderr.splitlines())
    assert ">> step failed (ScriptError): template error: 'nope' is undefined" in res.stderr.splitlines()
    assert "  at script.0 (set)" in res.stderr.splitlines()
