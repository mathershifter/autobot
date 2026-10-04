"""P7-09..12, P7-14, P7-15: the schema `autobot schema` generates for installed plugins agrees with the models.

SPEC "CLI": each plugin gets `$defs.<key>Step` in `step.oneOf`, and its key leaves the `pluginStep`
catch-all. SPEC "Common Step Properties": the common props apply to plugin steps; a plugin's own
fields follow its model.
"""

from __future__ import annotations

import copy
from typing import Any

import pydantic
import pytest
from conftest import ProbeExecutor, model_ok

from autobot import models
from autobot.cli import add_plugin_steps

COMMON = sorted(models._COMMON_PROPS - {"plugin_key_"})


class Opts(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    level: int


class NestStep(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    nest: str
    port: int | None = None
    opts: Opts | None = None


class FreeStep(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="allow")
    free: str


class _Executor:
    def __init__(self, key: str, model: type[pydantic.BaseModel]) -> None:
        self.key, self.model = key, model

    def execute(self, step, ctx, timeout):  # pragma: no cover - never runs
        pass


@pytest.fixture
def plugins(register_plugin) -> list[Any]:
    return [
        register_plugin(ProbeExecutor()),
        register_plugin(_Executor("nest", NestStep)),
        register_plugin(_Executor("free", FreeStep)),
    ]


@pytest.fixture
def generated(schema: dict[str, Any], plugins: list[Any]) -> dict[str, Any]:
    return add_plugin_steps(copy.deepcopy(schema), plugins)


@pytest.fixture
def generated_validator(generated: dict[str, Any]) -> Any:
    jsonschema = pytest.importorskip("jsonschema", reason="jsonschema not installed; parity tests skipped")
    return jsonschema.Draft202012Validator(generated)


def s(*steps: Any) -> dict[str, Any]:
    return {"autobot": "2026-10", "attach": {"spawn": "ssh host"}, "script": list(steps)}


GOOD_COMMON = {"after": "ready", "when": "{{ x }}", "delay_before": "500ms", "delay_after": 2, "timeout": "5s"}
BAD_COMMON = {"after": 3, "when": True, "delay_before": "5 s", "delay_after": -1, "timeout": False}

ACCEPT = {
    "plain": s({"probe": "x"}),
    **{f"common-{k}": s({"probe": "x", k: v}) for k, v in GOOD_COMMON.items()},
    "all-common": s({"probe": "x", **GOOD_COMMON}),
    "in-block-and-fn": {
        **s({"block": {"name": "b", "script": [{"probe": "x", "timeout": 1}]}}, {"call": "f"}),
        "fn": {"f": {"script": [{"nest": "a", "when": "y"}]}},
    },
    "nested-model": s({"nest": "a", "opts": {"level": 1}}),
    "own-optional-null": s({"nest": "a", "port": None, "opts": None}),
    "extra-allow-plugin": s({"free": "x", "anything": [1]}),
    "extra-allow-plugin-common": s({"free": "x", "timeout": 1}),
}

REJECT = {
    **{f"null-{k}": s({"probe": "x", k: None}) for k in COMMON},
    **{f"bad-{k}": s({"probe": "x", k: v}) for k, v in BAD_COMMON.items()},
    "unknown-extra-key": s({"probe": "x", "bogus": 1}),
    "plugin-field-type": s({"probe": 1}),
    "plugin-field-null": s({"probe": None}),
    "nested-model-type": s({"nest": "a", "opts": {"level": "high"}}),
    "nested-model-extra": s({"nest": "a", "opts": {"level": 1, "lvl": 2}}),
    "two-plugin-keys": s({"probe": "x", "nest": "a"}),
    "plugin-and-builtin": s({"cmd": "x", "probe": "y"}),
    "extra-allow-plugin-null-common": s({"free": "x", "timeout": None}),
    "extra-allow-plugin-field-type": s({"free": 1}),
    "internal-plugin-key": s({"probe": "x", "plugin_key_": "probe"}),
}


def both(validator: Any, doc: dict[str, Any]) -> tuple[bool, bool]:
    return model_ok(doc), validator.is_valid(doc)


@pytest.mark.parametrize("doc", ACCEPT.values(), ids=ACCEPT.keys())
def test_p7_09_generated_schema_parity_accept(generated_validator: Any, doc: dict[str, Any]):
    """A known plugin's step is checked against its model plus the common props; both accept."""
    assert both(generated_validator, doc) == (True, True)


@pytest.mark.parametrize("doc", REJECT.values(), ids=REJECT.keys())
def test_p7_09_generated_schema_parity_reject(generated_validator: Any, doc: dict[str, Any]):
    assert both(generated_validator, doc) == (False, False)


@pytest.mark.parametrize(
    "step", [{"nope": 1}, {"nope": 1, "timeout": "5s"}, {"timeout": 5}], ids=["unknown", "unknown-common", "common-only"]
)
def test_p7_10_unknown_step_key_falls_to_plugin_step(
    generated_validator: Any, schema_validator: Any, step: dict[str, Any]
):
    """Keys no plugin registers still match `pluginStep`, as in the static schema; the model rejects them."""
    doc = s(step)
    assert both(generated_validator, doc) == (False, True)
    assert schema_validator.is_valid(doc)


@pytest.mark.parametrize("key", COMMON)
def test_p7_10_unknown_step_key_bad_common_prop_rejected(generated_validator: Any, schema_validator: Any, key: str):
    """`pluginStep` applies `stepCommon` in both schemas, so an unknown key with a bad common prop is rejected."""
    for value in (None, BAD_COMMON[key]):
        doc = s({"nope": 1, key: value})
        assert both(generated_validator, doc) == (False, False)
        assert not schema_validator.is_valid(doc)


def test_p7_11_generated_schema_shape(generated: dict[str, Any], generated_validator: Any):
    """The generated schema is valid 2020-12; each plugin def reuses `stepCommon`, and `pluginStep` excludes its key."""
    type(generated_validator).check_schema(generated)
    defs = generated["$defs"]
    assert defs["step"]["oneOf"][-4:] == [
        {"$ref": "#/$defs/probeStep"},
        {"$ref": "#/$defs/nestStep"},
        {"$ref": "#/$defs/freeStep"},
        {"$ref": "#/$defs/pluginStep"},
    ]
    assert defs["pluginStep"]["not"]["anyOf"][-3:] == [{"required": [k]} for k in ("probe", "nest", "free")]
    for key in ("probe", "nest", "free"):
        assert defs[f"{key}Step"]["allOf"][0] == {"$ref": "#/$defs/stepCommon"}
    assert defs["nestStep"]["$defs"]["Opts"]["properties"] == {"level": {"title": "Level", "type": "integer"}}


def test_p7_11_step_common_matches_builtins_and_model(schema: dict[str, Any]):
    """`stepCommon` is the one definition of the common props: same keys as `PluginStep`, same rules as `cmdStep`."""
    common = schema["$defs"]["stepCommon"]["properties"]
    assert sorted(common) == COMMON
    cmd = schema["$defs"]["cmdStep"]["properties"]
    assert common == {k: cmd[k] for k in COMMON}


def test_p7_12_validation_leaves_input_unchanged(plugins: list[Any]):
    """The model doesn't write its internal `plugin_key_` into the caller's document."""
    doc = ACCEPT["in-block-and-fn"]
    before = copy.deepcopy(doc)
    cfg = models.Config.model_validate(doc)
    assert doc == before
    assert cfg.script[0].block.script[0].plugin_key_ == "probe"  # type: ignore[union-attr]


def test_p7_12_internal_plugin_key_rejected(plugins: list[Any]):
    """`plugin_key_` is internal: a script that sets it is rejected, as the schema rejects it."""
    with pytest.raises(pydantic.ValidationError) as ei:
        models.Config.model_validate(s({"probe": "x", "plugin_key_": "nest"}))
    [err] = ei.value.errors()
    assert (err["loc"], err["type"]) == (("script", 0, "plugin"), "extra_forbidden")


class TreeStep(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    tree: str
    children: list[TreeStep] = []


class LooseStep(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="allow")
    loose: str


@pytest.fixture
def edge_plugins(register_plugin) -> list[Any]:
    return [
        register_plugin(_Executor("tree", TreeStep)),
        register_plugin(_Executor("free", FreeStep)),
        register_plugin(_Executor("loose", LooseStep)),
    ]


@pytest.fixture
def edge_generated(schema: dict[str, Any], edge_plugins: list[Any]) -> dict[str, Any]:
    return add_plugin_steps(copy.deepcopy(schema), edge_plugins)


@pytest.fixture
def edge_validator(edge_generated: dict[str, Any]) -> Any:
    jsonschema = pytest.importorskip("jsonschema", reason="jsonschema not installed; parity tests skipped")
    return jsonschema.Draft202012Validator(edge_generated)


TREE = {"tree": "a", "children": [{"tree": "b", "children": [{"tree": "c"}]}, {"tree": "d"}]}

TREE_ACCEPT = {
    "plain": s({"tree": "a"}),
    "timeout": s({"tree": "a", "timeout": 5}),
    "all-common": s({"tree": "a", **GOOD_COMMON}),
    "nested-children": s(TREE),
    "nested-children-common": s({**TREE, "timeout": 5, "when": "x"}),
}

TREE_REJECT = {
    "unknown-extra-key": s({"tree": "a", "bogus": 1}),
    "null-common": s({"tree": "a", "timeout": None}),
    "bad-common": s({"tree": "a", "timeout": False}),
    "child-common-prop": s({"tree": "a", "children": [{"tree": "b", "timeout": 5}]}),
    "child-unknown-key": s({"tree": "a", "children": [{"tree": "b", "children": [{"tree": "c", "x": 1}]}]}),
    "child-missing-key": s({"tree": "a", "children": [{"children": []}]}),
    "child-field-type": s({"tree": "a", "children": [{"tree": 1}]}),
}


@pytest.mark.parametrize("doc", TREE_ACCEPT.values(), ids=TREE_ACCEPT.keys())
def test_p7_14_recursive_plugin_parity_accept(edge_validator: Any, doc: dict[str, Any]):
    """A self-referencing plugin model takes the common props at the step, and children at any depth."""
    assert both(edge_validator, doc) == (True, True)


@pytest.mark.parametrize("doc", TREE_REJECT.values(), ids=TREE_REJECT.keys())
def test_p7_14_recursive_plugin_parity_reject(edge_validator: Any, doc: dict[str, Any]):
    """Nested children are the plugin's model, not steps: they stay closed and take no common props."""
    assert both(edge_validator, doc) == (False, False)


def test_p7_14_recursive_plugin_schema_shape(edge_generated: dict[str, Any], edge_validator: Any):
    """The root `$ref` is inlined (open, so `stepCommon` applies); the closed def stays for the nested refs."""
    type(edge_validator).check_schema(edge_generated)
    tree = edge_generated["$defs"]["treeStep"]
    root = tree["allOf"][1]
    assert "$ref" not in root and "additionalProperties" not in root
    assert tree["unevaluatedProperties"] is False
    assert tree["$defs"]["TreeStep"]["additionalProperties"] is False
    ref = "#/$defs/treeStep/$defs/TreeStep"
    assert root["properties"]["children"]["items"] == {"$ref": ref}
    assert tree["$defs"]["TreeStep"]["properties"]["children"]["items"] == {"$ref": ref}


# -- P7-22..23: definition names that can't collide or break a $ref ----------

ODD_KEYS = ["a/b", "a~b", "~1", "a b", "a%41", "a#b", "a{b}", "{model}", "é", 'a"b', "a?b=c"]


def _odd_executor(key: str, recursive: bool) -> _Executor:
    fields: dict[str, Any] = {"value": (str, pydantic.Field(alias=key)), "opts": (Opts | None, None)}
    if recursive:
        fields["children"] = (list["OddStep"], [])
    model = pydantic.create_model("OddStep", __config__=pydantic.ConfigDict(extra="forbid"), **fields)
    return _Executor(key, model)


@pytest.mark.parametrize("recursive", [False, True], ids=["nested", "recursive"])
@pytest.mark.parametrize("key", ODD_KEYS)
def test_p7_22_plugin_key_escaped_in_schema_refs(schema: dict[str, Any], register_plugin, key: str, recursive: bool):
    """SPEC "CLI": a key with `/`, `~` or other URI characters still gets a working `$defs.<key>Step`.

    The `$ref`s are JSON pointers in a URI fragment, so the name is escaped there (`a/b` was a broken pointer).
    """
    jsonschema = pytest.importorskip("jsonschema", reason="jsonschema not installed; parity tests skipped")
    executor = register_plugin(_odd_executor(key, recursive))
    generated = add_plugin_steps(copy.deepcopy(schema), [executor])
    assert f"{key}Step" in generated["$defs"]
    validator = jsonschema.Draft202012Validator(generated)
    type(validator).check_schema(generated)
    good = {key: "x", "opts": {"level": 1}, "timeout": 5}
    if recursive:
        good["children"] = [{key: "y", "opts": {"level": 2}}]
    assert both(validator, s(good)) == (True, True)
    for bad in ({key: 1}, {key: "x", "opts": {"level": "high"}}, {key: "x", "bogus": 1}, {key: "x", "timeout": "5 s"}):
        assert both(validator, s(bad)) == (False, False)


def test_p7_23_plugin_key_plugin_is_reserved(isolated_registry: Any):
    """SPEC "Common Step Properties": `plugin` would name its definition `pluginStep`, the catch-all's name.

    `add_plugin_steps` used to overwrite the catch-all and then fail with `KeyError: 'not'`.
    """
    from autobot.registry import PluginError

    executor = _Executor("plugin", pydantic.create_model("PStep", plugin=(str, ...)))
    with pytest.raises(PluginError) as ei:
        isolated_registry.register(executor)
    assert str(ei.value) == (
        f"plugin {__name__}._Executor: step key 'plugin' is reserved (the schema's pluginStep definition "
        "and the plugin step type in validation errors use the name)"
    )
    assert not isolated_registry.plugin_executors()


def test_p7_23_no_plugin_key_can_take_a_static_def_name(schema: dict[str, Any], isolated_registry: Any):
    """Every static `$defs` name that a `<key>Step` could equal belongs to a key the registry rejects."""
    from autobot.registry import PluginError

    names = [n for n in schema["$defs"] if n.endswith("Step")]
    assert "pluginStep" in names and "cmdStep" in names
    for name in names:
        key = name.removesuffix("Step")
        with pytest.raises(PluginError, match="is reserved"):
            isolated_registry.register(_Executor(key, pydantic.create_model("KStep", value=(str, ...))))


TWO_KEYS = {
    "plain": ({"free": "x", "loose": "y"}, ("script", 0), "free, loose"),
    "reversed": ({"loose": "y", "free": "x"}, ("script", 0), "loose, free"),
    "with-common-and-extra": ({"timeout": 5, "free": "x", "other": 1, "loose": "y"}, ("script", 0), "free, loose"),
    "in-block": (
        {"block": {"name": "b", "script": [{"free": "x", "loose": "y"}]}},
        ("script", 0, "block", "script", 0),
        "free, loose",
    ),
}


@pytest.mark.parametrize("step,loc,keys", TWO_KEYS.values(), ids=TWO_KEYS.keys())
def test_p7_15_two_plugin_keys_rejected(edge_validator: Any, step: dict[str, Any], loc: tuple, keys: str):
    """Two `extra="allow"` plugin keys: one `invalid_step` error at the step; the generated schema rejects it too."""
    doc = s(step)
    with pytest.raises(pydantic.ValidationError) as ei:
        models.Config.model_validate(doc)
    [err] = ei.value.errors()
    assert (err["type"], err["loc"], err["msg"]) == ("invalid_step", loc, f"step has more than one plugin key: {keys}")
    assert not edge_validator.is_valid(doc)


def test_p7_15_two_plugin_key_interactions(edge_validator: Any):
    """A built-in key still wins (plugin keys are extra fields), a script-set `plugin_key_` is rejected first,
    and a key no plugin registers is only data for an `extra="allow"` plugin."""
    cases = [
        ({"cmd": "x", "free": "y", "loose": "z"}, [("script", 0, "cmd", "free"), ("script", 0, "cmd", "loose")]),
        ({"free": "x", "loose": "y", "plugin_key_": "free"}, [("script", 0, "plugin")]),
    ]
    for step, locs in cases:
        doc = s(step)
        with pytest.raises(pydantic.ValidationError) as ei:
            models.Config.model_validate(doc)
        assert [(e["loc"], e["type"]) for e in ei.value.errors()] == [(loc, "extra_forbidden") for loc in locs]
        assert not edge_validator.is_valid(doc)
    assert both(edge_validator, s({"free": "x", "nope": 1})) == (True, True)
