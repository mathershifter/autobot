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


def test_p6_05_old_version_points_to_migration():
    """SPEC "Migrating from 2026-08": the old version names the section to read."""
    [err] = errors_of(with_("autobot", "2026-08"))
    assert (err["loc"], err["type"]) == (("autobot",), "unsupported_version")
    assert err["msg"] == 'autobot 2026-08 is no longer supported; use 2026-10 (see "Migrating from 2026-08" in SPEC.md)'


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
    "flat": ["a", "b"],
    "list-of-lists": [["a", "b"], ["c", "d"]],
    "sendEach": {"each": "vars.pins"},
}


@pytest.mark.parametrize("send", list(SEND_OK.values()), ids=list(SEND_OK))
def test_p6_07_send_forms(send: Any):
    """SPEC.md:39-53: the three send forms are accepted."""
    Config.model_validate(with_("prompts", [{"name": "p", "expect": ["x"], "send": send}]))


def test_p6_07_send_each_fields_form():
    """SPEC sendEach: with fields there is no expect; the entries give the patterns."""
    fields = [{"match": ["a", "b"], "field": "u"}, {"match": "c", "field": "p"}]
    cfg = Config.model_validate(with_("prompts", [{"name": "p", "send": {"each": "vars.creds", "fields": fields}}]))
    prompt = cfg.prompts[0]
    assert prompt.expect is None
    assert [(e.match, e.field) for e in prompt.send.fields] == [(["a", "b"], "u"), (["c"], "p")]  # type: ignore[union-attr]


def test_p6_07_send_mixed_rejected():
    """SPEC.md:39-43: a send list mixing strings and lists is rejected."""
    with pytest.raises(pydantic.ValidationError):
        Config.model_validate(
            with_("prompts", [{"name": "p", "expect": ["x"], "send": ["a", ["b"]]}])
        )


@pytest.mark.parametrize(
    "expect", [["a", "b"], [["a", "b"]], ["a", ["b", "c"]]], ids=["strings", "grouped", "mixed"]
)
def test_p6_08_expect_forms(expect: list):
    """SPEC.md:37: each expect entry is a string or a list of strings."""
    cfg = Config.model_validate(with_("prompts", [{"name": "p", "expect": expect}]))
    assert cfg.prompts[0].expect == expect


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
AT = ("prompts", 0)
MATCH_MSG = "match must be a regex or a non-empty list of regexes"
WITH_FIELDS_MSG = "a prompt whose sendEach has fields has no expect: the patterns are the fields' match regexes"
GROUPED_MSG = (
    "with sendEach without fields, each expect entry is a single regex; use fields to answer several prompts"
)


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
            "since 2026-10 a fields entry pairs a regex with a field: "
            'write {match: <regex>, field: password} (see "Migrating from 2026-08" in SPEC.md)'
        ),
    ),
    "expect-with-fields": (fields_prompt(UP, expect=["x"]), (*AT, "expect"), "expect_with_fields", WITH_FIELDS_MSG),
    "empty-expect-with-fields": (fields_prompt(UP, expect=[]), (*AT, "expect"), "expect_with_fields", WITH_FIELDS_MSG),
    "no-expect-no-send": (with_("prompts", [{"name": "p"}]), (*AT, "expect"), "missing", None),
    "no-expect-literal-send": (with_("prompts", [{"name": "p", "send": ["a"]}]), (*AT, "expect"), "missing", None),
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
