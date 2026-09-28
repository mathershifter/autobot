from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pydantic
import pytest
from conftest import make_doc, plugin_dist, run_cli

from autobot.models import Config
from autobot.registry import StepRegistry

PLUGIN = '''
from __future__ import annotations

import pydantic


class EchoStep(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    echo: str


class EchoExecutor:
    key = "echo"
    model = EchoStep

    def execute(self, step, ctx, timeout):
        ctx.session.get_prompt(timeout=timeout)
        ctx.session.sendline(f"echo PLUGIN_{step.echo}_$((40+2))")
        ctx.session.get_prompt(timeout=timeout)
'''


@pytest.fixture
def plugin_path(tmp_path: Path) -> Path:
    root = tmp_path / "plugins"
    root.mkdir()
    return plugin_dist(root, "echo", PLUGIN, "EchoExecutor")


def test_plugin_step_runs_through_cli(plugin_path: Path, tmp_path: Path):
    res = run_cli(make_doc([{"echo": "hi"}]), tmp_path, pythonpath=plugin_path)
    assert res.returncode == 0, res.stderr
    assert "PLUGIN_hi_42" in res.stdout


def test_typo_step_key_is_clean_cli_error(tmp_path: Path):
    res = run_cli(make_doc([{"cmdd": "true"}]), tmp_path)
    assert res.returncode == 1
    assert "Validation errors" in res.stderr
    assert "cannot determine step type" in res.stderr
    assert "Traceback" not in res.stderr


def test_typo_step_key_is_validation_error():
    with pytest.raises(pydantic.ValidationError) as ei:
        Config.model_validate(
            {"autobot": "2026-08", "attach": {"spawn": "x"}, "script": [{"cmdd": "x"}]}
        )
    assert ei.value.errors()[0]["type"] == "invalid_step"


def test_discovery_is_lazy_and_idempotent(
    monkeypatch: pytest.MonkeyPatch, plugin_path: Path, isolated_registry: StepRegistry
):
    monkeypatch.syspath_prepend(str(plugin_path))
    reg = isolated_registry

    cfg = Config.model_validate(
        {"autobot": "2026-08", "attach": {"spawn": "x"}, "script": [{"echo": "hi"}, {"echo": "yo"}]}
    )
    assert cfg.script[0].plugin_key_ == "echo"  # type: ignore[union-attr]
    assert reg.validate_plugin_step(cfg.script[1]).echo == "yo"
    with pytest.raises(pydantic.ValidationError):
        Config.model_validate(
            {"autobot": "2026-08", "attach": {"spawn": "x"}, "script": [{"nope": 1}]}
        )
    reg.discover()
    assert reg.discover_calls == 1  # type: ignore[attr-defined]


def test_p7_07_schema_command_includes_plugin_defs(plugin_path: Path):
    """``autobot schema`` merges plugin models into the step oneOf (see #16)."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(plugin_path), env.get("PYTHONPATH")) if p)
    res = subprocess.run(
        [sys.executable, "-m", "autobot.cli", "schema"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert res.returncode == 0, res.stderr
    schema = json.loads(res.stdout)
    assert "echoStep" in schema["$defs"]
    one_of = schema["$defs"]["step"]["oneOf"]
    assert one_of[-2] == {"$ref": "#/$defs/echoStep"}
    assert one_of[-1] == {"$ref": "#/$defs/pluginStep"}
