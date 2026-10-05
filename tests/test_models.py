"""P6-01..09: pydantic model validation (SPEC.md:12-53, 87-270, 307-313)."""

from __future__ import annotations

import copy
from typing import Any

import pydantic
import pytest
from conftest import ProbeExecutor

from autobot.models import (
    BlockStep,
    CallStep,
    CmdStep,
    Config,
    ControlStep,
    LineStep,
    PluginStep,
    ReturnStep,
    SleepStep,
)
from autobot.registry import StepRegistry

BASE: dict[str, Any] = {"autobot": "2026-10", "attach": {"spawn": "x"}, "script": []}


def with_(path: str, value: Any, base: dict[str, Any] | None = None) -> dict[str, Any]:
    """Copy of BASE with ``value`` set at a dotted ``path``."""
    doc = copy.deepcopy(base or BASE)
    *parents, last = path.split(".")
    node = doc
    for p in parents:
        node = node.setdefault(p, {})
    node[last] = value
    return doc


def errors_of(doc: dict[str, Any]) -> list[dict[str, Any]]:
    with pytest.raises(pydantic.ValidationError) as ei:
        Config.model_validate(doc)
    return ei.value.errors()


def error_types(doc: dict[str, Any]) -> set[str]:
    with pytest.raises(pydantic.ValidationError) as ei:
        Config.model_validate(doc)
    return {e["type"] for e in ei.value.errors()}


def test_p6_01_yaml_aliases():
    """SPEC.md:38, 104, 125, 258: YAML keys map to their Python fields."""
    cfg = Config.model_validate(
        {
            **BASE,
            "prompts": [{"name": "p", "expect": ["x"], "return": True}],
            "script": [{"cmd": "x", "assert": "y", "register": "z"}, {"return": 3}],
        }
    )
    cmd, ret = cfg.script
    assert isinstance(cmd, CmdStep) and cmd.assert_ == "y" and cmd.register_ == "z"
    assert cfg.prompts[0].is_shell_prompt is True
    assert isinstance(ret, ReturnStep) and ret.newline_count == 3


@pytest.mark.parametrize("key", ["assert_", "register_"])
def test_p6_02_python_field_names_rejected(key: str):
    """SPEC.md:18: only the YAML names are accepted."""
    assert "extra_forbidden" in error_types(with_("script", [{"cmd": "x", key: "y"}]))


EXTRA_CASES = {
    "top-level": with_("bogus", 1),
    "attach": with_("attach.bogus", 1),
    "breakout": with_("attach.breakout", {"script": [], "bogus": 1}),
    "prompt": with_("prompts", [{"name": "p", "expect": ["x"], "bogus": 1}]),
    "sendEach": with_(
        "prompts", [{"name": "p", "expect": ["x"], "send": {"each": "vars.x", "bogus": 1}}]
    ),
    "fn": with_("fn", {"f": {"script": [], "bogus": 1}}),
    "block": with_("script", [{"block": {"name": "b", "bogus": 1}}]),
    "block-breakout": with_("script", [{"block": {"name": "b", "breakout": {"bogus": 1}}}]),
    "cmd": with_("script", [{"cmd": "x", "bogus": 1}]),
    "sleep": with_("script", [{"sleep": 1, "bogus": 1}]),
    "call": with_("script", [{"call": "f", "bogus": 1}]),
    "block-step": with_("script", [{"block": {"name": "b"}, "bogus": 1}]),
    "line": with_("script", [{"line": "x", "bogus": 1}]),
    "return": with_("script", [{"return": 1, "bogus": 1}]),
    "control": with_("script", [{"control": "c", "bogus": 1}]),
}


@pytest.mark.parametrize("doc", list(EXTRA_CASES.values()), ids=list(EXTRA_CASES))
def test_p6_03_extra_keys_forbidden_everywhere(doc: dict[str, Any]):
    """SPEC.md:18: unknown keys are rejected at every level."""
    assert "extra_forbidden" in error_types(doc)


MISSING_CASES = {
    "autobot": {k: v for k, v in BASE.items() if k != "autobot"},
    "attach": {k: v for k, v in BASE.items() if k != "attach"},
    "script": {k: v for k, v in BASE.items() if k != "script"},
    "attach.spawn": with_("attach", {}),
    "prompt.name": with_("prompts", [{"expect": ["x"]}]),
    "prompt.expect": with_("prompts", [{"name": "p"}]),
    "block.name": with_("script", [{"block": {}}]),
}


@pytest.mark.parametrize("doc", list(MISSING_CASES.values()), ids=list(MISSING_CASES))
def test_p6_04_required_fields(doc: dict[str, Any]):
    """SPEC.md:22-31, 70-73, 190-192: required fields."""
    assert "missing" in error_types(doc)


@pytest.mark.parametrize(
    ("version", "ok"),
    [
        ("2026-10", True),
        ("2026-11", False),
        ("2026-8", False),
        ("26-10", False),
        ("2026/10", False),
        ("202610", False),
        (" 2026-10", False),
    ],
)
def test_p6_05_version_pattern(version: str, ok: bool):
    """SPEC "Top-level fields": autobot is exactly 2026-10."""
    doc = with_("autobot", version)
    if ok:
        Config.model_validate(doc)
    else:
        [err] = errors_of(doc)
        assert (err["loc"], err["type"]) == (("autobot",), "unsupported_version")
        assert err["msg"] == f"unsupported autobot version {version!r}; expected 2026-10"


def test_p6_05_old_version_names_the_version_to_use():
    """SPEC "Top-level fields": the earlier version gets a message of its own."""
    [err] = errors_of(with_("autobot", "2026-08"))
    assert (err["loc"], err["type"]) == (("autobot",), "unsupported_version")
    assert err["msg"] == "autobot 2026-08 is no longer supported; use 2026-10"


def test_p6_06_step_discrimination(isolated_registry: StepRegistry, register_plugin):
    """SPEC.md:87-270: each step key selects its model; plugins get PluginStep."""
    register_plugin(ProbeExecutor())
    cases = [
        ({"cmd": "x"}, CmdStep),
        ({"sleep": 1}, SleepStep),
        ({"call": "f"}, CallStep),
        ({"block": {"name": "b"}}, BlockStep),
        ({"line": "x"}, LineStep),
        ({"return": 1}, ReturnStep),
        ({"control": "c"}, ControlStep),
        ({"probe": "x"}, PluginStep),
    ]
    base = with_("fn", {"f": {"script": []}})
    cfg = Config.model_validate(with_("script", [c[0] for c in cases], base))
    assert [type(s) for s in cfg.script] == [c[1] for c in cases]
    assert cfg.script[-1].plugin_key_ == "probe"  # type: ignore[union-attr]
    assert "extra_forbidden" in error_types(with_("script", [{"cmd": "a", "line": "b"}]))


SEND_OK = {
    "string": "yes",
    "sendEach": {"each": "vars.pins"},
}


@pytest.mark.parametrize("send", list(SEND_OK.values()), ids=list(SEND_OK))
def test_p6_07_send_forms(send: Any):
    """SPEC prompts: send is a string or a sendEach."""
    Config.model_validate(with_("prompts", [{"name": "p", "expect": ["x"], "send": send}]))


def test_p6_07_send_each_fields_form():
    """SPEC sendEach: with fields there is no expect; the entries give the patterns."""
    fields = [{"match": ["a", "b"], "field": "u"}, {"match": "c", "field": "p"}]
    cfg = Config.model_validate(with_("prompts", [{"name": "p", "send": {"each": "vars.creds", "fields": fields}}]))
    prompt = cfg.prompts[0]
    assert prompt.expect is None
    assert [(e.match, e.field) for e in prompt.send.fields] == [(["a", "b"], "u"), (["c"], "p")]  # type: ignore[union-attr]


# -- P6-52: simple prompts, one send string (SPEC "prompts") -----------------

AT = ("prompts", 0)
SEND_LIST_MSG = (
    "send is a single string: for a simple prompt write send: '<response>'; to answer a sequence of "
    "prompts such as a login, use sendEach with fields and keep the values in vars"
)
SEND_TYPE_MSG = (
    "send must be a string; quote it, e.g. send: 'yes' or send: '1234' "
    "(unquoted, YAML reads yes, no, on, off, true, false and numbers as booleans or numbers)"
)
GROUPED_MSG = (
    "each expect entry is a single regex, and the regexes are alternatives; "
    "to answer a sequence of prompts such as a login, use sendEach with fields"
)


def simple(**kw: Any) -> dict[str, Any]:
    return with_("prompts", [{"name": "p", **kw}])


SIMPLE_OK = {
    "alternatives": simple(expect=["continue?", "are you sure?"], send="yes"),
    "expect-string": simple(expect="continue?", send="yes"),
    "empty-send": simple(expect=["continue?"], send=""),
    "template": simple(expect=["continue?"], send="{{ vars.answer }}"),
    "shell-expect-string": simple(expect="x", **{"return": True}),
    "block": with_("script", [{"block": {"name": "b", "prompts": [{"name": "p", "expect": "x", "send": "y"}]}}]),
}


@pytest.mark.parametrize("doc", list(SIMPLE_OK.values()), ids=list(SIMPLE_OK))
def test_p6_52_simple_prompt_accepted(doc: dict[str, Any]):
    """SPEC prompts: expect is a regex or a list of regexes; send is one string (it may be empty)."""
    Config.model_validate(doc)


def test_p6_52_expect_string_is_a_one_item_list():
    """SPEC prompts: a single expect regex is the same as a list holding it."""
    cfg = Config.model_validate(SIMPLE_OK["expect-string"])
    assert (cfg.prompts[0].expect, cfg.prompts[0].send) == (["continue?"], "yes")


SIMPLE_BAD = {
    "flat-send": (simple(expect=["x"], send=["a", "b"]), (*AT, "send"), "send_list", SEND_LIST_MSG),
    "one-item-send": (simple(expect=["x"], send=["a"]), (*AT, "send"), "send_list", SEND_LIST_MSG),
    "list-of-lists-send": (simple(expect=["x"], send=[["a", "b"]]), (*AT, "send"), "send_list", SEND_LIST_MSG),
    "mixed-send": (simple(expect=["x"], send=["a", ["b"]]), (*AT, "send"), "send_list", SEND_LIST_MSG),
    "empty-list-send": (simple(expect=["x"], send=[]), (*AT, "send"), "send_list", SEND_LIST_MSG),
    "bool-send": (simple(expect=["x"], send=True), (*AT, "send"), "send_type", SEND_TYPE_MSG),
    "int-send": (simple(expect=["x"], send=1234), (*AT, "send"), "send_type", SEND_TYPE_MSG),
    "float-send": (simple(expect=["x"], send=2.5), (*AT, "send"), "send_type", SEND_TYPE_MSG),
    "grouped-expect": (simple(expect=[["a", "b"]], send="y"), (*AT, "expect", 0), "grouped_expect", GROUPED_MSG),
    "mixed-expect": (simple(expect=["x", ["a", "b"]], send="y"), (*AT, "expect", 1), "grouped_expect", GROUPED_MSG),
    "grouped-expect-shell": (simple(expect=[["a", "b"]]), (*AT, "expect", 0), "grouped_expect", GROUPED_MSG),
    "grouped-expect-return": (
        simple(expect=["x", ["a", "b"]], **{"return": True}), (*AT, "expect", 1), "grouped_expect", GROUPED_MSG,
    ),
    "block-flat-send": (
        with_("script", [{"block": {"name": "b", "prompts": [{"name": "p", "expect": "x", "send": ["a"]}]}}]),
        ("script", 0, "block", "block", "prompts", 0, "send"), "send_list", SEND_LIST_MSG,
    ),
}


@pytest.mark.parametrize(("doc", "loc", "type_", "msg"), list(SIMPLE_BAD.values()), ids=list(SIMPLE_BAD))
def test_p6_52_removed_prompt_forms_rejected(doc: dict[str, Any], loc: tuple, type_: str, msg: str):
    """SPEC prompts: a send list, a non-string send and a grouped expect are errors."""
    [err] = errors_of(doc)
    assert (err["loc"], err["type"], err["msg"]) == (loc, type_, msg)


@pytest.mark.parametrize(
    ("value", "seconds"),
    [(5, 5.0), ("5s", 5.0), ("500ms", 0.5), ("2m", 120.0), ("1h", 3600.0), ("1.5s", 1.5), (1.5, 1.5)],
)
def test_p6_09_duration_values_accepted(value: Any, seconds: float):
    """SPEC.md:307-313: bare numbers and unit-suffixed strings."""
    cfg = Config.model_validate(with_("script", [{"sleep": value}]))
    assert cfg.script[0].sleep == seconds  # type: ignore[union-attr]


@pytest.mark.parametrize("value", ["5", "5x", "", "ms", "1 s", "-1s"])
def test_p6_09_duration_values_rejected(value: str):
    """SPEC.md:307-313: other strings are not durations."""
    with pytest.raises(pydantic.ValidationError, match="invalid duration"):
        Config.model_validate(with_("script", [{"sleep": value}]))


# -- P6-48: sendEach fields entries (SPEC "sendEach") --------------------------

EACH = {"each": "vars.creds"}
UP = [{"match": "login:", "field": "username"}, {"match": "Password:", "field": "password"}]
MATCH_MSG = "match must be a regex or a non-empty list of regexes"
WITH_FIELDS_MSG = "a prompt whose sendEach has fields has no expect: the patterns are the fields' match regexes"


def fields_prompt(fields: Any, **kw: Any) -> dict[str, Any]:
    return with_("prompts", [{"name": "p", "send": {**EACH, "fields": fields}, **kw}])


SEND_EACH_OK = {
    "string-match": fields_prompt([{"match": "Password:", "field": "password"}]),
    "list-match": fields_prompt([{"match": ["login:", "Username:"], "field": "username"}]),
    "two-entries": fields_prompt(UP),
    "same-field-twice": fields_prompt([{"match": "a", "field": "pw"}, {"match": "b", "field": "pw"}]),
    "dotted-field-is-a-key": fields_prompt([{"match": "a", "field": "site.user"}]),
    "no-fields-regexes": with_("prompts", [{"name": "p", "expect": ["PIN:", "Passcode:"], "send": EACH}]),
    "block-fields": with_("script", [{"block": {"name": "b", "prompts": [
        {"name": "p", "send": {**EACH, "fields": UP}}]}}]),
}


@pytest.mark.parametrize("doc", list(SEND_EACH_OK.values()), ids=list(SEND_EACH_OK))
def test_p6_48_send_each_fields_accepted(doc: dict[str, Any]):
    """SPEC sendEach: fields entries without expect; without fields, expect regexes."""
    Config.model_validate(doc)


SEND_EACH_BAD = {
    "empty-fields": (fields_prompt([]), (*AT, "send", "fields"), "too_short", None),
    "empty-match": (
        fields_prompt([UP[0], {"match": [], "field": "p"}]),
        (*AT, "send", "fields", 1, "match"), "too_short", MATCH_MSG,
    ),
    "match-not-string": (
        fields_prompt([{"match": ["a", 1], "field": "p"}]),
        (*AT, "send", "fields", 0, "match", 1), "string_type", None,
    ),
    "match-mapping": (
        fields_prompt([{"match": {"a": 1}, "field": "p"}]),
        (*AT, "send", "fields", 0, "match"), "list_type", None,
    ),
    "missing-field": (fields_prompt([{"match": "a"}]), (*AT, "send", "fields", 0, "field"), "missing", None),
    "missing-match": (fields_prompt([{"field": "p"}]), (*AT, "send", "fields", 0, "match"), "missing", None),
    "field-not-string": (
        fields_prompt([{"match": "a", "field": 1}]), (*AT, "send", "fields", 0, "field"), "string_type", None,
    ),
    "extra-key": (
        fields_prompt([{"match": "a", "field": "p", "x": 1}]),
        (*AT, "send", "fields", 0, "x"), "extra_forbidden", None,
    ),
    "old-list-form": (
        fields_prompt(["username", "password"]),
        (*AT, "send", "fields", 1), "fields_entry",
        (
            "a fields entry pairs a regex with a field: write {match: <regex>, field: password}"
        ),
    ),
    "expect-with-fields": (fields_prompt(UP, expect=["x"]), (*AT, "expect"), "expect_with_fields", WITH_FIELDS_MSG),
    "empty-expect-with-fields": (fields_prompt(UP, expect=[]), (*AT, "expect"), "expect_with_fields", WITH_FIELDS_MSG),
    "no-expect-no-send": (with_("prompts", [{"name": "p"}]), (*AT, "expect"), "missing", None),
    "no-expect-literal-send": (with_("prompts", [{"name": "p", "send": "a"}]), (*AT, "expect"), "missing", None),
    "no-expect-no-fields": (with_("prompts", [{"name": "p", "send": EACH}]), (*AT, "expect"), "missing", None),
    "grouped-without-fields": (
        with_("prompts", [{"name": "p", "expect": ["x", ["a", "b"]], "send": EACH}]),
        (*AT, "expect", 1), "grouped_expect", GROUPED_MSG,
    ),
    "block-expect-with-fields": (
        with_("script", [{"block": {"name": "b", "prompts": [
            {"name": "p", "expect": ["x"], "send": {**EACH, "fields": UP}}]}}]),
        # the step union's tag ("block") comes before the block field
        ("script", 0, "block", "block", "prompts", 0, "expect"), "expect_with_fields", WITH_FIELDS_MSG,
    ),
    "each-path": (
        with_("prompts", [{"name": "p", "send": {"each": "creds", "fields": UP}}]),
        (*AT, "send", "each"), "value_error", None,
    ),
}


@pytest.mark.parametrize(("doc", "loc", "type_", "msg"), list(SEND_EACH_BAD.values()), ids=list(SEND_EACH_BAD))
def test_p6_48_send_each_fields_rejected(doc: dict[str, Any], loc: tuple, type_: str, msg: str | None):
    """SPEC sendEach: each rule is a model error at the offending key (old-list-form: one per entry)."""
    errs = errors_of(doc)
    assert {e["type"] for e in errs} == {type_}, errs
    err = errs[-1]
    assert err["loc"] == loc
    if msg is not None:
        assert err["msg"] == msg


# -- P6-50: a return prompt has no send (SPEC "prompts") ----------------------

RETURN_MSG = "a return prompt is a shell prompt and sends nothing; remove send or return"


def return_prompt(**kw: Any) -> dict[str, Any]:
    return with_("prompts", [{"name": "p", "return": True, **kw}])


RETURN_OK = {
    "return-without-send": return_prompt(expect=["x"]),
    "return-false-send": with_("prompts", [{"name": "p", "expect": ["x"], "return": False, "send": "a"}]),
    "return-false-fields": with_("prompts", [{"name": "p", "return": False, "send": {**EACH, "fields": UP}}]),
}


@pytest.mark.parametrize("doc", list(RETURN_OK.values()), ids=list(RETURN_OK))
def test_p6_50_return_without_send_accepted(doc: dict[str, Any]):
    """SPEC prompts: `return: true` without send, and send with `return: false`, stay valid."""
    Config.model_validate(doc)


RETURN_BAD = {
    "string": (return_prompt(expect=["x"], send="a"), AT),
    "empty": (return_prompt(expect=["x"], send=""), AT),
    "send-each": (return_prompt(expect=["x"], send=EACH), AT),
    "send-each-fields": (return_prompt(send={**EACH, "fields": UP}), AT),
    "block": (
        with_("script", [{"block": {"name": "b", "prompts": [
            {"name": "p", "expect": ["x"], "return": True, "send": "a"}]}}]),
        ("script", 0, "block", "block", "prompts", 0),
    ),
}


@pytest.mark.parametrize(("doc", "at"), list(RETURN_BAD.values()), ids=list(RETURN_BAD))
def test_p6_50_return_with_send_rejected(doc: dict[str, Any], at: tuple):
    """SPEC prompts: `return: true` with any send form is one `return_with_send` error at send."""
    [err] = errors_of(doc)
    assert (err["loc"], err["type"], err["msg"]) == ((*at, "send"), "return_with_send", RETURN_MSG)


def test_p6_50_return_with_send_reported_with_other_prompt_errors():
    """SPEC prompts: the rule is checked with the expect rules, so both errors are reported."""
    errs = errors_of(return_prompt(expect=["x"], send={**EACH, "fields": UP}))
    assert [(e["loc"], e["type"]) for e in errs] == [
        ((*AT, "send"), "return_with_send"),
        ((*AT, "expect"), "expect_with_fields"),
    ]
    errs = errors_of(return_prompt(send="a"))
    assert [(e["loc"], e["type"]) for e in errs] == [((*AT, "send"), "return_with_send"), ((*AT, "expect"), "missing")]
    errs = errors_of(return_prompt(expect=[["a", "b"]], send="a"))
    assert [(e["loc"], e["type"]) for e in errs] == [
        ((*AT, "send"), "return_with_send"),
        ((*AT, "expect", 0), "grouped_expect"),
    ]


def test_p6_50_invalid_send_reported_on_its_own():
    """SPEC prompts: a send_list or send_type error is reported alone; the prompt rules wait for a valid send."""
    for send, type_ in ((["a"], "send_list"), (True, "send_type")):
        [err] = errors_of(return_prompt(send=send))
        assert (err["loc"], err["type"]) == ((*AT, "send"), type_)


# -- P6-57: expect and match are never empty (SPEC "prompts", "sendEach") ------

EXPECT_MSG = "expect must be a regex or a non-empty list of regexes"
EMPTY_MSG = "a regex must not be empty: an empty regex matches at once, before any output"
BLOCK_AT = ("script", 0, "block", "block", "prompts", 0)

EMPTY_BAD = {
    "empty-list": (simple(expect=[], send="y"), [((*AT, "expect"), "too_short", EXPECT_MSG)]),
    "empty-list-shell": (simple(expect=[]), [((*AT, "expect"), "too_short", EXPECT_MSG)]),
    "empty-list-send-each": (simple(expect=[], send=EACH), [((*AT, "expect"), "too_short", EXPECT_MSG)]),
    "empty-list-block": (
        with_("script", [{"block": {"name": "b", "prompts": [{"name": "p", "expect": []}]}}]),
        [((*BLOCK_AT, "expect"), "too_short", EXPECT_MSG)],
    ),
    "empty-string": (simple(expect="", send="y"), [((*AT, "expect"), "string_too_short", EMPTY_MSG)]),
    "empty-entry": (simple(expect=["x", ""]), [((*AT, "expect", 1), "string_too_short", EMPTY_MSG)]),
    "empty-only-entry": (simple(expect=[""]), [((*AT, "expect", 0), "string_too_short", EMPTY_MSG)]),
    "empty-and-grouped-entries": (
        simple(expect=["", ["a"]]),
        [((*AT, "expect", 0), "string_too_short", EMPTY_MSG), ((*AT, "expect", 1), "grouped_expect", GROUPED_MSG)],
    ),
    "empty-list-return-with-send": (
        return_prompt(expect=[], send="a"),
        [((*AT, "send"), "return_with_send", RETURN_MSG), ((*AT, "expect"), "too_short", EXPECT_MSG)],
    ),
    "empty-string-return-with-send": (
        return_prompt(expect="", send="a"),
        [((*AT, "send"), "return_with_send", RETURN_MSG), ((*AT, "expect"), "string_too_short", EMPTY_MSG)],
    ),
    # with fields, expect must be absent: an empty expect is that error, not an empty-expect one
    "empty-list-with-fields": (fields_prompt(UP, expect=[]), [((*AT, "expect"), "expect_with_fields", WITH_FIELDS_MSG)]),
    "empty-string-with-fields": (
        fields_prompt(UP, expect=""), [((*AT, "expect"), "expect_with_fields", WITH_FIELDS_MSG)],
    ),
    "empty-entry-with-fields": (
        fields_prompt(UP, expect=[""]), [((*AT, "expect"), "expect_with_fields", WITH_FIELDS_MSG)],
    ),
    "empty-match": (
        fields_prompt([UP[0], {"match": "", "field": "p"}]),
        [((*AT, "send", "fields", 1, "match"), "string_too_short", EMPTY_MSG)],
    ),
    "empty-match-entry": (
        fields_prompt([{"match": ["login:", ""], "field": "u"}]),
        [((*AT, "send", "fields", 0, "match", 1), "string_too_short", EMPTY_MSG)],
    ),
    "empty-match-block": (
        with_("script", [{"block": {"name": "b", "prompts": [
            {"name": "p", "send": {**EACH, "fields": [{"match": "", "field": "u"}]}}]}}]),
        [((*BLOCK_AT, "send", "fields", 0, "match"), "string_too_short", EMPTY_MSG)],
    ),
}


@pytest.mark.parametrize(("doc", "expected"), list(EMPTY_BAD.values()), ids=list(EMPTY_BAD))
def test_p6_57_empty_expect_or_match_rejected(doc: dict[str, Any], expected: list[tuple[tuple, str, str]]):
    """SPEC prompts, sendEach: `expect: []` never fires, and an empty regex fires at once; both are errors."""
    assert [(e["loc"], e["type"], e["msg"]) for e in errors_of(doc)] == expected


def test_p6_57_whitespace_regex_accepted():
    """SPEC prompts: only the empty string is rejected; a regex of a single space is a regex."""
    Config.model_validate(simple(expect=" ", send="y"))
    Config.model_validate(fields_prompt([{"match": [" "], "field": "u"}]))
