from __future__ import annotations

import importlib
import importlib.metadata
import os
import subprocess
import sys
from pathlib import Path

import pydantic
import pytest
import yaml

from autobot.models import Config
from autobot.registry import StepRegistry

registry_mod = importlib.import_module("autobot.registry")

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
    (tmp_path / "autobot_testplugin.py").write_text(PLUGIN)
    dist = tmp_path / "autobot_testplugin-0.1.dist-info"
    dist.mkdir()
    (dist / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: autobot-testplugin\nVersion: 0.1\n"
    )
    (dist / "entry_points.txt").write_text(
        "[autobot.steps]\necho = autobot_testplugin:EchoExecutor\n"
    )
    return tmp_path


def run_cli(script: dict, tmp_path: Path, pythonpath: Path | None = None):
    path = tmp_path / "script.autobot.yaml"
    path.write_text(yaml.safe_dump(script))
    env = dict(os.environ)
    if pythonpath:
        env["PYTHONPATH"] = os.pathsep.join(
            p for p in (str(pythonpath), env.get("PYTHONPATH")) if p
        )
    return subprocess.run(
        [sys.executable, "-m", "autobot.cli", str(path)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


def script(steps: list) -> dict:
    return {
        "autobot": "2026-08",
        "prompts": [{"name": "sh", "expect": [r"PROMPT\$ "], "return": True}],
        "attach": {
            "spawn": "bash --norc --noprofile -i",
            "timeout": 5,
            "env": {"PS1": "PROMPT$ ", "TERM": "dumb", "PATH": os.environ["PATH"]},
        },
        "script": steps,
    }


def test_plugin_step_runs_through_cli(plugin_path: Path, tmp_path: Path):
    res = run_cli(script([{"echo": "hi"}]), tmp_path, plugin_path)
    assert res.returncode == 0, res.stderr
    assert "PLUGIN_hi_42" in res.stdout


def test_typo_step_key_is_clean_cli_error(tmp_path: Path):
    res = run_cli(script([{"cmdd": "true"}]), tmp_path)
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


def test_discovery_is_lazy_and_idempotent(monkeypatch, plugin_path: Path):
    monkeypatch.syspath_prepend(str(plugin_path))
    calls = 0
    real = importlib.metadata.entry_points

    def entry_points(**kw):
        nonlocal calls
        calls += 1
        return real(**kw)

    reg = StepRegistry()
    monkeypatch.setattr(registry_mod, "registry", reg)
    monkeypatch.setattr(registry_mod.importlib.metadata, "entry_points", entry_points)

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
    assert calls == 1

