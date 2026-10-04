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
    assert r.render("{{ 'abc' | contains('b') }}") == "True"
    assert r.render("{{ 'abc' | contains('x') }}") == "False"
    assert r.render("{{ 123 | contains('2') }}") == "True"


def test_p3_07_filter_search():
    """SPEC.md:334: search filter."""
    r = make_runner([])
    assert r.render("{{ 'v1.2' | search('\\\\d+\\\\.\\\\d+') }}") == "True"
    assert r.render("{{ 'vX' | search('\\\\d') }}") == "False"


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
    assert r.render("{{ 'a' in vars }} {{ vars == {'a': 1, 'b': {'c': 2}} }}") == "True True"
    assert r.render("{{ env.E }} {{ env.items() | list }} {{ args.get('k') }}") == "d [('E', 'd')] v"
    with pytest.raises(ValueError, match="^template error: "):
        r.render("{{ vars.nope }}")


@pytest.mark.parametrize("name", ["values", "items"])
def test_p3_19_register_under_a_dict_method_name(name: str):
    """SPEC "register": `register: values` is readable as `{{ vars.values }}` in later steps."""
    out = run_vars([{"cmd": "echo out", "register": name}, {"cmd": f"echo got-{{{{ vars.{name} }}}}", "register": "r"}])
    assert out["r"] == "got-out"
    assert type(out) is dict
