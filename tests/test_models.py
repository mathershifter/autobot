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

BASE: dict[str, Any] = {"autobot": "2026-08", "attach": {"spawn": "x"}, "script": []}


def with_(path: str, value: Any, base: dict[str, Any] | None = None) -> dict[str, Any]:
    """Copy of BASE with ``value`` set at a dotted ``path``."""
    doc = copy.deepcopy(base or BASE)
    *parents, last = path.split(".")
    node = doc
    for p in parents:
        node = node.setdefault(p, {})
    node[last] = value
    return doc


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
        ("2026-08", True),
        ("2026-8", False),
        ("26-08", False),
        ("2026/08", False),
        ("202608", False),
        (" 2026-08", False),
    ],
)
def test_p6_05_version_pattern(version: str, ok: bool):
    """SPEC.md:24: autobot is YYYY-MM."""
    doc = with_("autobot", version)
    if ok:
        Config.model_validate(doc)
    else:
        with pytest.raises(pydantic.ValidationError, match="YYYY-MM"):
            Config.model_validate(doc)


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
    cfg = Config.model_validate(with_("script", [c[0] for c in cases]))
    assert [type(s) for s in cfg.script] == [c[1] for c in cases]
    assert cfg.script[-1].plugin_key_ == "probe"  # type: ignore[union-attr]
    assert "extra_forbidden" in error_types(with_("script", [{"cmd": "a", "line": "b"}]))


SEND_OK = {
    "flat": ["a", "b"],
    "list-of-lists": [["a", "b"], ["c", "d"]],
    "sendEach-fields": {"each": "vars.creds", "fields": ["u", "p"]},
    "sendEach": {"each": "vars.pins"},
}


@pytest.mark.parametrize("send", list(SEND_OK.values()), ids=list(SEND_OK))
def test_p6_07_send_forms(send: Any):
    """SPEC.md:39-53: the three send forms are accepted."""
    Config.model_validate(with_("prompts", [{"name": "p", "expect": ["x"], "send": send}]))


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
