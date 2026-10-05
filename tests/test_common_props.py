"""P3: common step properties and the templating context (SPEC.md:272-334)."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import pytest
from conftest import (
    ProbeExecutor,
    SentLog,
    Timeline,
    make_runner,
    run_script,
    run_vars,
)

COMMON = {"after": "x", "when": "true", "delay_before": "1s", "delay_after": "1s"}


def doc(step: dict[str, Any]) -> dict[str, Any]:
    return {
        "autobot": "2026-10",
        "attach": {"spawn": "sh"},
        "fn": {"f": {"script": []}},
        "script": [step],
    }


def test_p3_01_evaluation_order(timeline: Timeline):
    """SPEC.md:288: after -> when -> delay_before -> execute -> delay_after."""
    run_script(
        [
            {"line": "printf 'pre%s\\n' READY"},
            {
                "line": "echo x",
                "after": "preREADY",
                # only true once `after` has filled session.before
                "when": "{{ session.before | contains('pre') }}",
                "delay_before": "1s",
                "delay_after": "2s",
            },
        ]
    )
    assert timeline.has_subsequence(
        [("expect", ["preREADY"]), ("sleep", 1.0), ("sendline", "echo x"), ("sleep", 2.0)]
    )


def test_p3_02_when_false_skips_delays_and_execution(timeline: Timeline, sent: SentLog):
    """SPEC.md:288, 292: a skipped step runs neither delays nor the step."""
    run_script([{"line": "echo x", "when": "false", "delay_before": "1s", "delay_after": "1s"}])
    assert timeline.sleeps() == []
    assert "echo x" not in sent.lines()


@pytest.mark.parametrize(
    "when", ["", "false", "False", "0", "none", "{{ '' }}", "false\n"],
    ids=["empty", "false", "False", "0", "none", "rendered-empty", "false-newline"],
)
def test_p3_03_when_falsy_values(sent: SentLog, when: str):
    """SPEC.md:279, 292: the listed falsy strings skip the step."""
    run_script([{"line": "echo x", "when": when}])
    assert "echo x" not in sent.lines()


@pytest.mark.parametrize("when", ["true", "True", "1", "yes", "no", "{{ 1 == 1 }}"])
def test_p3_04_when_truthy_values(sent: SentLog, when: str):
    """SPEC.md:292: anything not in the falsy list runs the step."""
    run_script([{"line": "echo x", "when": when}])
    assert "echo x" in sent.lines()


def test_p3_05_when_none_value(sent: SentLog):
    """SPEC "Conditional execution with when" (decision #6): a null renders ``None``, falsy."""
    r = make_runner([{"line": "echo x", "when": "{{ vars.v }}"}], vars={"v": None})
    assert r.render("{{ vars.v }}") == "None"
    r.run()
    assert "echo x" not in sent.lines()


@pytest.mark.parametrize(
    ("when", "runs"),
    [
        (" false ", False),
        ("\tFALSE\n", False),
        ("NONE", False),
        (" None ", False),
        (" 0 ", False),
        ("   ", False),
        ("False", False),
        ("no", True),
        ("off", True),
        (" no ", True),
    ],
)
def test_p3_05_when_surrounding_whitespace_and_case(sent: SentLog, when: str, runs: bool):
    """SPEC "Conditional execution with when" (decision #6): stripped, lowercased.

    Padded and uppercase falsy words skip the step; ``no``/``off`` still run it.
    """
    run_script([{"line": "echo x", "when": when}])
    assert ("echo x" in sent.lines()) is runs


def test_p3_06_filter_contains():
    """SPEC.md:333: contains filter."""
    r = make_runner([])
    assert r.render("{{ 'abc' | contains('b') }}") == "true"
    assert r.render("{{ 'abc' | contains('x') }}") == "false"
    assert r.render("{{ 123 | contains('2') }}") == "true"


def test_p3_07_filter_search():
    """SPEC.md:334: search filter."""
    r = make_runner([])
    assert r.render("{{ 'v1.2' | search('\\\\d+\\\\.\\\\d+') }}") == "true"
    assert r.render("{{ 'vX' | search('\\\\d') }}") == "false"


def test_p3_08_range_global():
    """SPEC.md:327: range is a template global."""
    out = run_vars([{"cmd": "echo {{ range(3) | list | length }}", "register": "out"}])
    assert out["out"] == "3"


def test_p3_09_after_sets_session_before_and_match(probe: ProbeExecutor):
    """SPEC.md:278, 324-325: after fills session.before/match before when."""
    run_script(
        [
            # the echoed command contains neither 'Version' nor V<digits>
            {"line": "printf '%sion: V%s\\n' Vers 42"},
            {"probe": "p", "after": "V\\d+", "when": "{{ session.before | contains('Version') }}"},
        ]
    )
    seen = probe.by_name("p")["ctx"]
    assert seen["match"] == "V42"
    assert "Version: " in seen["before"]


def test_p3_10_template_context_env_vars_args(monkeypatch: pytest.MonkeyPatch):
    """SPEC.md:319-323: env, vars and args are in the template context."""
    monkeypatch.delenv("AB_TPL_A", raising=False)
    out = run_vars(
        [{"cmd": "echo {{ env.AB_TPL_A }}-{{ vars.b }}-{{ args.c }}", "register": "out"}],
        env={"AB_TPL_A": "a"},
        vars={"b": "b"},
        args={"c": "c"},
    )
    assert out["out"] == "a-b-c"


@pytest.mark.parametrize(
    "prop", [{"after": "x"}, {"when": "true"}, {"delay_before": 1}, {"delay_after": 1}, {"timeout": 1}],
    ids=lambda p: next(iter(p)),
)
def test_p3_11_sleep_rejects_common_props(both_validate: Callable, prop: dict[str, Any]):
    """SPEC.md:274: sleep supports none of the common properties."""
    assert both_validate(doc({"sleep": 1, **prop})) == (False, False)


@pytest.mark.parametrize("step", [{"line": "x"}, {"return": 1}], ids=["line", "return"])
def test_p3_12_line_and_return_reject_timeout(both_validate: Callable, step: dict[str, Any]):
    """SPEC.md:284: line and return do not support timeout."""
    assert both_validate(doc({**step, "timeout": 1})) == (False, False)


@pytest.mark.parametrize(
    ("step", "timeout"),
    [
        ({"cmd": "x"}, True),
        ({"call": "f"}, True),
        ({"block": {"name": "b"}}, True),
        ({"line": "x"}, False),
        ({"return": 1}, False),
        ({"control": "c"}, True),
    ],
    ids=["cmd", "call", "block", "line", "return", "control"],
)
def test_p3_13_non_sleep_steps_accept_common_props(
    both_validate: Callable, step: dict[str, Any], timeout: bool
):
    """SPEC.md:274-282: every other step accepts the common properties."""
    props = {**COMMON, **({"timeout": "5s"} if timeout else {})}
    assert both_validate(doc({**step, **props})) == (True, True)


def test_p3_14_step_timeout_overrides_default():
    """SPEC.md:282: timeout overrides the default for the step."""
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        run_script([{"cmd": "sleep 3", "timeout": 1}])
    assert time.monotonic() - start < 2.5


def test_p3_15_call_honors_when_and_delays(timeline: Timeline):
    """SPEC.md:274: call supports when and delays."""
    fn = {"f": {"script": [{"cmd": "echo inf", "register": "r", "timeout": "5s"}]}}
    out = run_vars([{"call": "f", "when": "false"}], fn=fn)
    assert "r" not in out
    timeline.clear()
    out = run_vars([{"call": "f", "delay_before": "1s"}], fn=fn)
    assert out["r"] == "inf"
    assert timeline.has_subsequence([("sleep", 1.0), ("sendline", "echo inf")])


def test_p3_16_block_when_false_skips_prompt_swap(timeline: Timeline, sent: SentLog):
    """SPEC.md:274: a skipped block neither swaps prompts nor runs enter."""
    run_script(
        [
            {
                "block": {
                    "name": "b",
                    "prompts": [{"name": "blk", "expect": ["BLK"], "return": True}],
                    "enter": [{"line": "echo ENTER"}],
                },
                "when": "false",
            }
        ]
    )
    assert "restore_handlers" not in [e[0] for e in timeline.since("attach")]
    assert "echo ENTER" not in sent.lines()


def test_p3_17_after_timeout_uses_step_timeout():
    """SPEC.md:278, 282: the after wait is bounded by the step timeout."""
    start = time.monotonic()
    with pytest.raises(TimeoutError, match=r"^timed out after 1(\.0)?s waiting for the after pattern 'NEVER'$"):
        run_script([{"cmd": "true", "after": "NEVER", "timeout": 1}])
    assert time.monotonic() - start < 2.5


def test_p3_18_after_eof_names_pattern():
    """SPEC "cmd" ignore_error: the connection closing during the after wait names the rendered pattern."""
    with pytest.raises(EOFError, match=r"^connection closed while waiting for the after pattern 'login: x'$"):
        run_script([{"line": "exit"}, {"cmd": "true", "after": "login: {{ 'x' }}", "timeout": 5}])


def test_p3_20_after_rendering_to_an_invalid_regex_is_a_script_error(sent: SentLog):
    """SPEC "Common Step Properties": an `after` that renders to an invalid regex is a ValueError, and the step sends nothing."""
    with pytest.raises(ValueError, match=r"^after: invalid regex 'x\(': missing \), unterminated subpattern"):
        run_script([{"cmd": "echo hi", "after": "x{{ '(' }}"}])
    assert sent.commands() == []


# -- P3-21: an `after` that renders to nothing ---------------------------------


@pytest.mark.parametrize(
    "after", ["{{ vars.p }}", "{{ '' }}", "{# nothing #}", "{% if vars.p %}READY{% endif %}"], ids=["var", "literal", "comment", "if"]
)
def test_p3_21_after_rendering_to_an_empty_regex_is_a_script_error(sent: SentLog, timeline: Timeline, after: str):
    """SPEC "Common Step Properties": an `after` that renders to nothing is a ValueError, as for `assert`.

    It used to skip the wait: the `cmd` was sent before the first prompt and `register` stored `''`.
    """
    r = make_runner([{"cmd": "echo hi", "after": after, "register": "out"}], vars={"p": "", "out": "unset"})
    with pytest.raises(ValueError, match=r"^after: the pattern rendered to an empty regex, which matches at once$"):
        r.run()
    assert sent.lines() == []
    assert "expect" not in timeline.names() and "get_prompt" not in timeline.names()
    assert r.config.vars["out"] == "unset"


@pytest.mark.parametrize("step", [{"line": "echo hi"}, {"return": 1}, {"control": "c"}, {"call": "f"}, {"block": {"name": "b"}}], ids=lambda s: next(iter(s)))
def test_p3_21_empty_rendered_after_aborts_every_step_type(sent: SentLog, step: dict[str, Any]):
    """The runner checks `after` before any step type runs, and `when` isn't evaluated first."""
    with pytest.raises(ValueError, match=r"^after: the pattern rendered to an empty regex"):
        run_script([{**step, "after": "{{ vars.p }}", "when": "{{ nope }}"}], vars={"p": ""}, fn={"f": {"script": []}})
    assert sent == []


def test_p3_21_empty_rendered_after_is_not_ignorable_and_breakout_runs(sent: SentLog):
    """Like a template error it aborts the step even with `ignore_error`; the attach breakout still runs."""
    with pytest.raises(ValueError, match=r"^after: the pattern rendered to an empty regex"):
        run_script(
            [{"cmd": "echo hi", "after": "{{ vars.p }}", "ignore_error": True}],
            vars={"p": ""},
            breakout=[{"line": "echo bye"}],
        )
    assert sent.lines() == ["echo bye"]


def test_p3_21_after_rendering_to_a_pattern_still_waits(timeline: Timeline):
    """The same template with a value waits for it, and the command's output is registered."""
    script = [{"line": "printf 'pre%s\\n' READY"}, {"cmd": "echo hi", "after": "{{ vars.p }}", "register": "out"}]
    out = run_vars(script, vars={"p": "preREADY"})
    assert out["out"] == "hi"
    assert ("expect", ["preREADY"]) in timeline


# -- P3-22: a boolean a template puts out is YAML's true or false ---------------

FLAGS = {"on": True, "off": False, "nothing": None, "n": 1, "word": "True", "many": [True, False, None], "map": {"k": True}}


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        ("{{ vars.on }} {{ vars.off }}", "true false"),
        ("{{ 1 == 1 }} {{ 1 == 2 }} {{ not vars.on }}", "true false false"),
        ("{{ true }} {{ false }} {{ True }}", "true false true"),
        ("{{ vars.on and vars.n == 1 }} {{ vars.nope is defined }}", "true false"),
        ("{{ 'b' in 'abc' }} {{ vars.n is odd }}", "true true"),
        ("{{ 'abc' | contains('b') }} {{ 'abc' | search('^b') }}", "true false"),
        ("{{ vars.on if vars.n else vars.off }}", "true"),
        ("flag={{ vars.on }}!", "flag=true!"),
    ],
)
def test_p3_22_boolean_output_is_yaml_spelling(template: str, expected: str):
    """SPEC "Jinja2 Templating": an expression whose value is a boolean is written `true` or `false`.

    It used to be Python's `True` or `False`.
    """
    assert make_runner([], vars=dict(FLAGS)).render(template) == expected


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        # not a boolean: a string, a number, or text an expression built itself
        ("{{ vars.word }} {{ vars.n }} {{ 0 }}", "True 1 0"),
        ("{{ vars.on | string }} {{ vars.on ~ '' }} {{ 'x' ~ vars.off }}", "True True xFalse"),
        ("{{ '%s' | format(vars.on) }} {{ [vars.on] | join(',') }}", "True True"),
        # inside a list or a mapping the values are written as Python writes them
        ("{{ vars.many }} {{ vars.map }}", "[True, False, None] {'k': True}"),
        # JSON has its own spelling, as before
        ("{{ vars.many | tojson }} {{ vars.on | tojson }}", "[true, false, null] true"),
        # a null is not a boolean: it is still written None
        ("{{ vars.nothing }} {{ none }}", "None None"),
        # plain text is never touched
        ("True False", "True False"),
    ],
)
def test_p3_22_only_a_boolean_value_of_an_expression_changes(template: str, expected: str):
    """Only the value an expression puts out, and only when it is exactly a boolean."""
    assert make_runner([], vars=dict(FLAGS)).render(template) == expected


def test_p3_22_conditions_use_the_value_not_the_text():
    """`{% if %}`, tests and filters see the boolean itself; the spelling is only how it is written out."""
    r = make_runner([], vars=dict(FLAGS))
    assert r.render("{% if vars.on %}yes{% else %}no{% endif %}{% if vars.off %}yes{% else %}no{% endif %}") == "yesno"
    assert r.render("{{ 'a' if vars.off == false else 'b' }}{{ 'a' if vars.on is sameas true else 'b' }}") == "aa"
    assert r.render("{{ vars.on | int }} {{ (vars.on, vars.off) | select | list | length }}") == "1 1"
    assert r.render("{% set f = vars.on %}{{ f }} {{ f is boolean }}") == "true true"
    # the filters read a boolean the same way it is written
    assert r.render("{{ vars.on | contains('true') }} {{ vars.off | search('^false$') }}") == "true true"


@pytest.mark.parametrize(("flag", "runs"), [(True, True), (False, False)])
def test_p3_22_when_gate_is_unchanged(sent: SentLog, flag: bool, runs: bool):
    """`when` lowercases the result, so `true`/`false` gate a step exactly as `True`/`False` did."""
    run_script(
        [
            {"line": "echo flag", "when": "{{ vars.flag }}"},
            {"line": "echo eq", "when": "{{ vars.flag == true }}"},
            {"line": "echo block", "when": "{% if vars.flag %}run{% endif %}"},
        ],
        vars={"flag": flag},
    )
    assert sent.lines() == (["echo flag", "echo eq", "echo block"] if runs else [])


def test_p3_22_boolean_reaches_the_device_as_yaml(sent: SentLog):
    """In every templated value: a `cmd`, a `line`, an `assert`, an `env` default and a prompt's `send`."""
    out = run_vars(
        [
            {"cmd": "echo got-{{ vars.on }}-{{ env.E }}", "assert": "^got-{{ vars.on }}-{{ 1 == 2 }}$", "register": "out"},
            {"line": "echo {{ vars.on }}"},
        ],
        vars={"on": True},
        env={"E": "{{ 1 == 2 }}"},
    )
    assert out["out"] == "got-true-false"
    assert "echo true" in sent.lines()
    r = make_runner([], vars={"on": True}, prompts=[{"name": "q", "expect": "ok\\?", "send": "{{ vars.on }}"}])
    assert r.session.save_handlers()[0].respond(0) == "true"


# -- P3-19: keys named like dict methods -------------------------------------

DICT_METHODS = ["values", "items", "keys", "get", "copy", "update", "pop", "clear"]


@pytest.mark.parametrize("name", DICT_METHODS)
def test_p3_19_key_named_like_a_dict_method_renders_the_key(monkeypatch: pytest.MonkeyPatch, name: str):
    """SPEC "Jinja2 Templating": `vars.values` is the key `values`, not `<built-in method values of dict ...>`."""
    monkeypatch.delenv(name, raising=False)
    env = {name: "e-{{ 'x' }}", "REF": f"{{{{ env.{name} }}}}!"}
    r = make_runner([], vars={name: "v"}, env=env, args={name: "a"})
    assert r.render(f"{{{{ vars.{name} }}}} {{{{ env.{name} }}}} {{{{ args.{name} }}}}") == "v e-x a"
    assert r.render(f"{{{{ vars['{name}'] }}}} {{{{ env['{name}'] }}}}") == "v e-x"
    assert r.render("{{ env.REF }}") == "e-x!"  # while env is resolved, too
    # filters and nested mappings aren't affected by the key's name
    assert r.render("{{ vars | items | list }} {{ vars | tojson }} {{ vars | length }}") == (
        f"[('{name}', 'v')] {{\"{name}\": \"v\"}} 1"
    )
    nested = make_runner([], vars={"site": {name: 1}})
    assert nested.render(f"{{{{ vars.site.{name} }}}}") == "1"


def test_p3_19_dict_methods_still_work_without_such_a_key():
    """SPEC "Jinja2 Templating": with no key of that name, `vars.items()` and the like are the dict methods."""
    r = make_runner([], vars={"a": 1, "b": {"c": 2}}, env={"E": "{{ env.get('NOPE', 'd') }}"}, args={"k": "v"})
    assert r.render("{% for k, v in vars.items() %}{{ k }}={{ v }};{% endfor %}") == "a=1;b={'c': 2};"
    assert r.render("{{ vars.keys() | list }} {{ vars.get('nope', 'd') }} {{ vars | length }}") == "['a', 'b'] d 2"
    assert r.render("{{ vars }} {{ vars | tojson }}") == "{'a': 1, 'b': {'c': 2}} {\"a\": 1, \"b\": {\"c\": 2}}"
    assert r.render("{{ 'a' in vars }} {{ vars == {'a': 1, 'b': {'c': 2}} }}") == "true true"
    assert r.render("{{ env.E }} {{ env.items() | list }} {{ args.get('k') }}") == "d [('E', 'd')] v"
    with pytest.raises(ValueError, match="^template error: "):
        r.render("{{ vars.nope }}")


@pytest.mark.parametrize("name", ["values", "items"])
def test_p3_19_register_under_a_dict_method_name(name: str):
    """SPEC "register": `register: values` is readable as `{{ vars.values }}` in later steps."""
    out = run_vars([{"cmd": "echo out", "register": name}, {"cmd": f"echo got-{{{{ vars.{name} }}}}", "register": "r"}])
    assert out["r"] == "got-out"
    assert type(out) is dict
