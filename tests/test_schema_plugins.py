"""P7-09..12: the schema `autobot schema` generates for installed plugins agrees with the models.

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
