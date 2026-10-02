"""P7-01..06, P7-13: step registry and plugin execution (SPEC.md:286)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pydantic
import pytest
from conftest import (
    ProbeExecutor,
    ProbeStep,
    Timeline,
    make_doc,
    plugin_dist,
    run_cli,
    run_script,
)

from autobot.models import Config, PluginStep
from autobot.registry import StepRegistry

BUILTINS = {"cmd", "sleep", "call", "block", "line", "return", "control"}


def test_p7_01_builtins_registered_and_not_plugins(isolated_registry: StepRegistry):
    """The seven builtin step types are registered and are not plugins."""
    reg = isolated_registry
    assert BUILTINS <= set(reg.keys())
    assert not {e.key for e in reg.plugin_executors()} & BUILTINS


def test_p7_02_builtin_only_config_does_not_discover(isolated_registry: StepRegistry):
    """SPEC.md:286: plugin discovery is lazy; builtin-only configs never trigger it."""
    Config.model_validate(
        {
            "autobot": "2026-10",
            "attach": {"spawn": "x", "script": [{"line": "x"}]},
            "fn": {"f": {"script": [{"cmd": "x"}]}},
            "script": [
                {"cmd": "x"},
                {"sleep": 1},
                {"call": "f"},
                {"block": {"name": "b", "script": [{"return": 1}]}},
                {"control": "c"},
            ],
        }
    )
    assert isolated_registry.discover_calls == 0  # type: ignore[attr-defined]


def test_p7_03_unknown_key_raises(isolated_registry: StepRegistry):
    """An unknown key raises; discovery runs once however often it is asked."""
    reg = isolated_registry
    for _ in range(2):
        with pytest.raises(ValueError, match="unknown step type: nope"):
            reg.get("nope")
    assert reg.discover_calls == 1  # type: ignore[attr-defined]


def test_p7_04_plugin_model_receives_only_plugin_fields(probe: ProbeExecutor, timeline: Timeline):
    """SPEC.md:286: common properties go to the runner, not the plugin model."""
    run_script(
        [
            {"line": "printf 'MA%s\\n' RK"},
            {
                "probe": "x",
                "after": "MARK",
                "when": "true",
                "delay_before": "1s",
                "delay_after": "2s",
                "timeout": "7s",
            },
        ]
    )
    rec = probe.by_name("x")
    step = rec["step"]
    assert type(step) is ProbeStep
    assert step.model_dump() == {"probe": "x"}
    for prop in ("after", "when", "delay_before", "delay_after", "timeout"):
        assert not hasattr(step, prop)
    assert rec["timeout"] == 7
    assert timeline.has_subsequence([("expect", ["MARK"]), ("sleep", 1.0), ("sleep", 2.0)])


def test_p7_05_validate_plugin_step_requires_key(isolated_registry: StepRegistry):
    """A PluginStep without a plugin key cannot be validated."""
    with pytest.raises(ValueError, match="no plugin key"):
        isolated_registry.validate_plugin_step(PluginStep.model_validate({}))


class BoomStep(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    boom: str


class BoomExecutor:
    key = "boom"
    model = BoomStep

    def execute(self, step: Any, ctx: Any, timeout: float) -> None:
        raise RuntimeError("boom")


def test_p7_06_plugin_step_error_propagates(register_plugin, children):
    """A plugin error aborts the run and the session is still closed."""
    register_plugin(BoomExecutor())
    with pytest.raises(RuntimeError, match="boom"):
        run_script([{"boom": "x"}])
    assert len(children) == 1
    assert not children[0].isalive()


COMMON = ("after", "when", "delay_before", "delay_after", "timeout")


class _Executor:
    def __init__(self, key: str, model: type[pydantic.BaseModel]) -> None:
        self.key, self.model = key, model

    def execute(self, step: Any, ctx: Any, timeout: float) -> None:  # pragma: no cover - never runs
        pass


def _clash(key: str, model: type[pydantic.BaseModel], names: str) -> str:
    return (
        f"plugin {__name__}._Executor, step key {key!r}: model {model.__name__} reuses common step property names, "
        f"which the runner handles and never passes to the plugin: {names}"
    )


@pytest.mark.parametrize("name", COMMON)
def test_p7_13_plugin_field_named_like_common_prop_rejected(isolated_registry: StepRegistry, name: str):
    """SPEC "Plugin steps": a plugin field can't reuse a common name, which the runner would never pass on."""
    model = pydantic.create_model("ClashStep", clash=(str, ...), **{name: (str, "")})
    with pytest.raises(TypeError) as ei:
        isolated_registry.register(_Executor("clash", model))
    assert str(ei.value) == _clash("clash", model, name)
    assert "clash" not in isolated_registry._executors


class AliasStep(pydantic.BaseModel):
    alias: str
    limit: int = pydantic.Field(0, alias="timeout")
    gate: str = pydantic.Field("", validation_alias=pydantic.AliasChoices("gate", "when"))
    wait: str = pydantic.Field("", validation_alias=pydantic.AliasPath("after", 0))
    delay_after: int = pydantic.Field(0, alias="pause_after")


def test_p7_13_plugin_field_alias_named_like_common_prop_rejected(isolated_registry: StepRegistry):
    """Every key a script could use to set a field counts: its name, `alias` and each `validation_alias`."""
    with pytest.raises(TypeError) as ei:
        isolated_registry.register(_Executor("alias", AliasStep))
    names = "timeout (field 'limit'), when (field 'gate'), after (field 'wait'), delay_after"
    assert str(ei.value) == _clash("alias", AliasStep, names)
    assert "alias" not in isolated_registry._executors


@pytest.mark.parametrize("key", [*sorted(BUILTINS), *COMMON, "plugin_key_"])
def test_p7_13_reserved_plugin_key_rejected(isolated_registry: StepRegistry, key: str):
    """A built-in or common name as a plugin key would replace a built-in or never be routed: rejected."""
    before = dict(isolated_registry._executors)
    model = pydantic.create_model("KeyStep", value=(str, ...))
    with pytest.raises(TypeError) as ei:
        isolated_registry.register(_Executor(key, model))
    assert str(ei.value) == (
        f"plugin {__name__}._Executor: step key {key!r} is reserved (a built-in step or a common step property)"
    )
    assert isolated_registry._executors == before


CLASH_PLUGIN = '''
import pydantic


class ClashStep(pydantic.BaseModel):
    clash: str
    timeout: int = 0


class ClashExecutor:
    key = "clash"
    model = ClashStep

    def execute(self, step, ctx, timeout):
        pass
'''


@pytest.mark.parametrize("command", ["run-builtin", "run-plugin", "schema"])
def test_p7_13_cli_fails_fast_on_clashing_plugin(tmp_path: Path, command: str):
    """Discovery rejects the plugin before anything runs, in `run` and `schema`: a traceback, like an import error."""
    root = tmp_path / "plugins"
    root.mkdir()
    plugin_dist(root, "clash", CLASH_PLUGIN, "ClashExecutor")
    marker = tmp_path / "prepared"
    if command == "schema":
        env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, (str(root), os.environ.get("PYTHONPATH"))))}
        res = subprocess.run(
            [sys.executable, "-m", "autobot.cli", "schema"],
            check=False,
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
    else:
        step = {"cmd": "true"} if command == "run-builtin" else {"clash": "x"}
        res = run_cli(make_doc([step], prepare=f"touch {marker}"), tmp_path, pythonpath=root)
    assert res.returncode == 1
    assert res.stdout == ""
    assert "Traceback" in res.stderr
    assert res.stderr.strip().splitlines()[-1] == (
        "TypeError: plugin autobot_testplugin_clash.ClashExecutor, step key 'clash': model ClashStep reuses "
        "common step property names, which the runner handles and never passes to the plugin: timeout"
    )
    assert not marker.exists()
