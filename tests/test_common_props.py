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
    assert r.render("{{ 'abc' | contains('b') }}", condition=True) == "True"
    assert r.render("{{ 'abc' | contains('x') }}", condition=True) == "False"
    assert r.render("{{ 123 | contains('2') }}", condition=True) == "True"
    assert r.render("{{ true | contains('True') }}", condition=True) == "True"  # a non-string is read as str() gives it


def test_p3_07_filter_search():
    """SPEC.md:334: search filter."""
    r = make_runner([])
    assert r.render("{{ 'v1.2' | search('\\\\d+\\\\.\\\\d+') }}", condition=True) == "True"
    assert r.render("{{ 'vX' | search('\\\\d') }}", condition=True) == "False"


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


# -- P3-22: a boolean is not text ----------------------------------------------

FLAGS = {"on": True, "off": False, "nothing": None, "n": 1, "word": "True", "many": [True, False, None], "map": {"k": True}}


def boolean_error(shown: str) -> str:
    return (
        f"template error: an expression gave a boolean ({shown}), which is never written as text; "
        "quote the value in the script, or say which text is meant, "
        "e.g. {{ value | string }} (True) or {{ value | tojson }} (true)"
    )


@pytest.mark.parametrize(
    ("template", "shown"),
    [
        ("set debug {{ vars.on }}", "true"),
        ("{{ vars.off }}", "false"),
        ("{{ 1 == 1 }}", "true"),
        ("{{ vars.n == 2 }}", "false"),
        ("{{ not vars.on }}", "false"),
        ("{{ true }}", "true"),
        ("{{ False }}", "false"),
        ("{{ vars.on and vars.n == 1 }}", "true"),
        ("{{ vars.nope is defined }}", "false"),
        ("{{ 'b' in 'abc' }}", "true"),
        ("{{ 'abc' | contains('b') }}", "true"),
        ("{{ 'abc' | search('^b') }}", "false"),
        ("{{ vars.on if vars.n else vars.off }}", "true"),
        ("{% for f in vars.many[:2] %}{{ f }}{% endfor %}", "true"),
        ("{{ vars.n }} {{ vars.off }}", "false"),
    ],
)
def test_p3_22_boolean_as_text_is_a_template_error(template: str, shown: str):
    """SPEC "Jinja2 Templating": an expression whose value is a boolean can't be written as text.

    `set debug {{ vars.debug }}` with `debug: true` used to send `set debug True`.
    """
    with pytest.raises(ValueError) as ei:
        make_runner([], vars=dict(FLAGS)).render(template)
    assert str(ei.value) == boolean_error(shown)


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        # saying which text is meant
        ("{{ vars.on | string }} {{ vars.off | string }}", "True False"),
        ("{{ vars.on | tojson }} {{ vars.off | tojson }}", "true false"),
        ("{{ vars.on | string | lower }} {{ vars.on | int }}", "true 1"),
        ("{{ 'on' if vars.on else 'off' }} {{ 'yes' if vars.n == 2 else 'no' }}", "on no"),
        ("{{ vars.on ~ '' }} {{ 'x' ~ vars.off }} {{ '%s' | format(vars.on) }}", "True xFalse True"),
        ("{{ [vars.on, vars.off] | join(',') }}", "True,False"),
        # a boolean inside a list or a mapping that is written out whole
        ("{{ vars.many }} {{ vars.map }}", "[True, False, None] {'k': True}"),
        ("{{ vars.many | tojson }}", "[true, false, null]"),
        # not booleans: a string, a number, a null, plain text
        ("{{ vars.word }} {{ vars.n }} {{ 0 }} {{ 1 }}", "True 1 0 1"),
        ("{{ vars.nothing }} {{ none }}", "None None"),
        ("True false yes", "True false yes"),
        # used inside the template, never written out
        ("{% if vars.on %}yes{% else %}no{% endif %}{% if vars.off %}yes{% else %}no{% endif %}", "yesno"),
        ("{% if vars.nope is defined %}a{% else %}b{% endif %}{% if vars.n == 1 and not vars.off %}c{% endif %}", "bc"),
        ("{% set f = vars.on %}{% if f is sameas true %}set{% endif %}", "set"),
        ("{{ 'a' if vars.off == false else 'b' }}{{ (vars.on, vars.off) | select | list | length }}", "a1"),
        ("{{ 'found' if 'abc' | contains('b') else 'missing' }} {{ 'm' if vars.word | search('^T') else 'n' }}", "found m"),
        ("{% for f in vars.many %}{{ 'T' if f else 'F' }}{% endfor %}", "TFF"),
    ],
)
def test_p3_22_explicit_text_and_conditions_still_render(template: str, expected: str):
    """Only the value an expression writes out is checked, and only when it is exactly a boolean.

    `| string`, `| tojson` and a conditional expression are the ways to say which text is meant.
    """
    assert make_runner([], vars=dict(FLAGS)).render(template) == expected


@pytest.mark.parametrize(
    ("when", "runs"),
    [
        ("{{ vars.on }}", True),
        ("{{ vars.off }}", False),
        ("{{ vars.on == true }}", True),
        ("{{ vars.n == 2 }}", False),
        ("{{ vars.nope is defined }}", False),
        ("{{ vars.word | contains('rue') }}", True),
        ("{{ vars.word | search('^x') }}", False),
        ("{{ vars.on and not vars.off }}", True),
        ("{{ vars.nothing }}", False),
        ("{% if vars.on %}run{% endif %}", True),
        ("{{ vars.off }} {{ vars.off }}", True),  # the text `False False` is none of the falsy words
    ],
)
def test_p3_22_when_reads_a_boolean_as_before(sent: SentLog, when: str, runs: bool):
    """SPEC "Conditional execution with when": `when` is a condition, so a boolean result is what it is for."""
    run_script([{"line": "echo x", "when": when}], vars=dict(FLAGS))
    assert ("echo x" in sent.lines()) is runs


def test_p3_22_condition_rendering_is_available_to_plugins():
    """`render(..., condition=True)` is the runner's own path for `when`; a plugin with a condition field uses it."""
    r = make_runner([], vars=dict(FLAGS))
    assert r.render("{{ vars.on }} {{ 1 == 2 }} {{ vars.word | contains('T') }}", condition=True) == "True False True"
    assert r.render("{{ vars.n }}-{{ flag }}", {"flag": False}, condition=True) == "1-False"
    with pytest.raises(ValueError, match=r"^template error: an expression gave a boolean \(false\)"):
        r.render("{{ flag }}", {"flag": False})
    with pytest.raises(ValueError, match="^template error: "):
        r.render("{{ vars.nope }}", condition=True)


TEXT_FIELDS: dict[str, Callable[[str], dict[str, Any]]] = {
    "cmd": lambda t: {"script": [{"cmd": f"echo {t}"}]},
    "cmd-list": lambda t: {"script": [{"cmd": ["echo ok", f"echo {t}"]}]},
    "embedded-script": lambda t: {"script": [{"cmd": f"#!/bin/sh\necho {t}\n"}]},
    "assert": lambda t: {"script": [{"cmd": "echo ok", "assert": t}]},
    "after": lambda t: {"script": [{"line": "echo ok", "after": t}]},
    "line": lambda t: {"script": [{"line": f"echo {t}"}]},
    "spawn": lambda t: {"script": [], "spawn": f"bash {t}"},
    "prepare": lambda t: {"script": [], "prepare": f"#!/bin/sh\necho {t}\n"},
    "env": lambda t: {"script": [], "env": {"AB_FLAG": t}},
}


@pytest.mark.parametrize("field", list(TEXT_FIELDS))
def test_p3_22_boolean_in_every_templated_text_is_refused(sent: SentLog, monkeypatch: pytest.MonkeyPatch, field: str):
    """Every templated value that becomes text follows the rule, and what it would have sent is never sent."""
    monkeypatch.delenv("AB_FLAG", raising=False)
    kw = TEXT_FIELDS[field]("{{ vars.debug }}")
    named = "env.AB_FLAG: " if field == "env" else ""  # an error in a default names the default
    with pytest.raises(ValueError, match=rf"^{named}template error: an expression gave a boolean \(true\), "):
        run_script(kw.pop("script"), vars={"debug": True}, **kw)
    assert not any("True" in line or "true" in line for line in sent.lines())


def test_p3_22_boolean_in_a_prompt_send_is_refused():
    """A prompt's `send` is rendered when it is sent; the error names the prompt, like any template error there."""
    r = make_runner([], vars={"on": True}, prompts=[{"name": "q", "expect": "ok\\?", "send": "{{ vars.on }}"}])
    with pytest.raises(ValueError, match=r"^prompt 'q': template error: an expression gave a boolean \(true\), "):
        r.session.save_handlers()[0].respond(0)


def test_p3_22_boolean_error_is_not_ignorable(sent: SentLog):
    """Like every template error it aborts the step, also with `ignore_error`, and the quoted value works."""
    with pytest.raises(ValueError, match="^template error: an expression gave a boolean"):
        run_script([{"cmd": "echo {{ vars.debug }}", "ignore_error": True}], vars={"debug": True})
    out = run_vars(
        [{"cmd": "echo {{ vars.debug }}-{{ vars.flag | tojson }}-{{ 'on' if vars.flag else 'off' }}", "register": "out"}],
        vars={"debug": "true", "flag": True},
    )
    assert out["out"] == "true-true-on"


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
    assert r.render("{{ 'a' in vars }} {{ vars == {'a': 1, 'b': {'c': 2}} }}", condition=True) == "True True"
    assert r.render("{{ env.E }} {{ env.items() | first }} {{ args.get('k') }}") == "d ('E', 'd') v"
    with pytest.raises(ValueError, match="^template error: "):
        r.render("{{ vars.nope }}")


@pytest.mark.parametrize("name", ["values", "items"])
def test_p3_19_register_under_a_dict_method_name(name: str):
    """SPEC "register": `register: values` is readable as `{{ vars.values }}` in later steps."""
    out = run_vars([{"cmd": "echo out", "register": name}, {"cmd": f"echo got-{{{{ vars.{name} }}}}", "register": "r"}])
    assert out["r"] == "got-out"
    assert type(out) is dict


# -- P3-26: a missing --arg, and an error in an `env` default ------------------------------

NO_HOST = "template error: args has no key 'host'; pass it with --arg host=VALUE"


@pytest.mark.parametrize("template", ["{{ args.host }}", "{{ args['host'] }}", "ssh {{ args.host.name }}", "{{ args.host | upper }}"])
def test_p3_26_missing_arg_says_how_to_pass_it(template: str):
    """SPEC "Jinja2 Templating": an argument that wasn't given is undefined, and the error names `args`, the
    key and the option."""
    r = make_runner([], args={"other": "1"})
    for condition in (False, True):
        with pytest.raises(ValueError) as ei:
            r.render(template, condition=condition)
        assert str(ei.value) == NO_HOST


def test_p3_26_missing_arg_is_undefined_like_any_other():
    """`default`, `is defined`, `in` and `args.get(...)` deal with it as on any mapping, and `args` is a
    mapping of the arguments and nothing else."""
    r = make_runner([], args={"k": "v", "get": "G"})
    assert r.render("{{ args.host | default('d') }} {{ args.get }} {{ args.k }} {{ args['k'] }}") == "d G v v"
    assert r.render("{{ 'y' if args.host is defined else 'n' }}{{ 'y' if 'host' in args else 'n' }}") == "nn"
    assert r.render("{{ 'y' if args.k is defined else 'n' }}{{ 'y' if 'k' in args else 'n' }}") == "yy"
    assert r.render("{{ args }} {{ args | tojson }} {{ args | length }}") == "{'k': 'v', 'get': 'G'} {\"get\": \"G\", \"k\": \"v\"} 2"
    assert r.render("{% for k, v in args.items() %}{{ k }}={{ v }};{% endfor %}{{ args | items | list | length }}") == "k=v;get=G;2"
    plain = make_runner([], args={"k": "v"})
    assert plain.render("{{ args.get('host', 'd') }} {{ args.get('k') }} {{ args.keys() | list }}") == "d v ['k']"
    with pytest.raises(ValueError, match="^template error: args has no key 1$"):
        plain.render("{{ args[1] }}")


def test_p3_26_missing_arg_at_load_time_and_at_run_time(sent: SentLog):
    """In an `env` default the error comes when the script is loaded, and names the default; in a step, when
    the step runs. With the argument both render."""
    with pytest.raises(ValueError) as ei:
        make_runner([], env={"AB_H": "{{ args.host }}"})
    assert str(ei.value) == f"env.AB_H: {NO_HOST}"
    with pytest.raises(ValueError) as ei:
        run_script([{"line": "echo {{ args.host }}"}])
    assert str(ei.value) == NO_HOST and sent.lines() == []
    r = run_script([{"line": "echo {{ args.host }}-{{ env.AB_H }}"}], env={"AB_H": "{{ args.host }}"}, args={"host": "sw1"})
    assert "echo sw1-sw1" in sent.lines()
    assert type(r.config.vars) is dict


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"AB_A": "{{ 1/0 }}"}, "env.AB_A: template error: ZeroDivisionError: division by zero"),
        ({"AB_A": "{{ nope( }}"}, "env.AB_A: template error: unexpected '}', expected ')'"),
        ({"AB_A": "{{ vars.nope }}"}, "env.AB_A: template error: 'dict object' has no attribute 'nope'"),
        ({"AB_A": "{{ env.AB_NOPE }}"}, "env.AB_A: template error: env has no key 'AB_NOPE'"),
        ({"AB_A": "{{ ''.__class__ }}"}, "env.AB_A: template error: not allowed in a template: access to attribute '__class__' of 'str' object is unsafe."),
        # the default that has the error is named, once, not the ones that read it
        ({"AB_A": "x{{ env.AB_B }}", "AB_B": "{{ env.AB_C }}", "AB_C": "{{ 1/0 }}"}, "env.AB_C: template error: ZeroDivisionError: division by zero"),
        ({"AB_C": "{{ 'a' | search('(') }}", "AB_A": "{{ env.AB_C }}"}, "env.AB_C: template error: search: invalid regex '(': missing ), unterminated subpattern at position 0"),
        # a cycle and a chain that is too deep name their keys themselves
        ({"AB_A": "{{ env.AB_B }}", "AB_B": "{{ env.AB_A }}"}, "env cycle: AB_A -> AB_B -> AB_A"),
    ],
)
def test_p3_26_error_in_an_env_default_names_the_default(monkeypatch: pytest.MonkeyPatch, env: dict[str, str], message: str):
    """SPEC "Top-level fields": `env.<KEY>: ` before the error of the default that has it."""
    for key in ("AB_A", "AB_B", "AB_C", "AB_NOPE"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(ValueError) as ei:
        make_runner([], env=env)
    assert str(ei.value) == message
