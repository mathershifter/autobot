"""P7-01..06: step registry and plugin execution (SPEC.md:286)."""

from __future__ import annotations

from typing import Any

import pydantic
import pytest
from conftest import ProbeExecutor, ProbeStep, Timeline, run_script

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
