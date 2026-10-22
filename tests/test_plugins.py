from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pydantic
import pytest
from conftest import make_doc, plugin_dist, run_cli

from autobot import models
from autobot.models import PLUGINS, Config
from autobot.registry import StepRegistry
from autobot.steps import register_builtins

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
            {"autobot": "2026-10", "attach": {"spawn": "x"}, "script": [{"cmdd": "x"}]}
        )
    assert ei.value.errors()[0]["type"] == "invalid_step"


def test_discovery_is_lazy_and_idempotent(
    monkeypatch: pytest.MonkeyPatch, plugin_path: Path, isolated_registry: StepRegistry
):
    monkeypatch.syspath_prepend(str(plugin_path))
    reg = isolated_registry

    cfg = Config.model_validate(
        {"autobot": "2026-10", "attach": {"spawn": "x"}, "script": [{"echo": "hi"}, {"echo": "yo"}]}
    )
    assert cfg.script[0].plugin_key_ == "echo"  # type: ignore[union-attr]
    assert reg.validate_plugin_step(cfg.script[1]).echo == "yo"
    with pytest.raises(pydantic.ValidationError):
        Config.model_validate(
            {"autobot": "2026-10", "attach": {"spawn": "x"}, "script": [{"nope": 1}]}
        )
    reg.discover()
    assert reg.discover_calls == 1  # type: ignore[attr-defined]


class _Counting:
    """A plugin lookup over a registry that counts what validation asks of it."""

    def __init__(self, reg: StepRegistry) -> None:
        self._has, self._validate = reg.has, reg.validate_plugin_step  # bound now: the test patches the registry
        self.validated, self.asked = 0, []

    def has(self, key: str) -> bool:
        self.asked.append(key)
        return self._has(key)

    def validate_plugin_step(self, step: Any) -> Any:
        self.validated += 1
        return self._validate(step)


def test_plugin_context_and_default_registry_agree(isolated_registry: StepRegistry, probe, monkeypatch: pytest.MonkeyPatch):
    """SPEC "As a library": `Config.model_validate(doc)` checks the plugin steps against the default registry,
    and `context={PLUGINS: lookup}` against the lookup given. With the same plugins both give the same
    `Config`, and either way the plugin's model is validated once per step, at load."""
    doc = make_doc([{"probe": "a", "timeout": "5s"}, {"probe": "b"}], timeout=None)
    counted = _Counting(isolated_registry)
    monkeypatch.setattr(isolated_registry, "validate_plugin_step", counted.validate_plugin_step)
    by_default = Config.model_validate(doc)
    assert counted.validated == 2
    by_context = Config.model_validate(doc, context={PLUGINS: counted})
    assert counted.validated == 4
    assert by_default == by_context
    assert [s.plugin_key_ for s in by_context.script] == ["probe", "probe"]  # type: ignore[union-attr]
    assert "probe" in counted.asked
    assert models._plugins.get() is None  # the context's lookup is the validation's alone


def test_plugin_context_decides_what_a_plugin_step_is(isolated_registry: StepRegistry, probe):
    """The lookup given decides which keys are plugin steps, and the plugin's own fields are checked
    against it, at the step's path, as with the default registry."""
    doc = make_doc([{"probe": "a"}], timeout=None)
    builtins_only = StepRegistry()
    register_builtins(builtins_only)
    with pytest.raises(pydantic.ValidationError) as ei:
        Config.model_validate(doc, context={PLUGINS: builtins_only})
    assert [(e["type"], e["loc"]) for e in ei.value.errors()] == [("invalid_step", ("script", 0))]
    assert Config.model_validate(doc).script[0].plugin_key_ == "probe"  # type: ignore[union-attr]
    with pytest.raises(pydantic.ValidationError) as ei:
        Config.model_validate(make_doc([{"probe": "a", "prob": 1}], timeout=None), context={PLUGINS: isolated_registry})
    assert [(e["type"], e["loc"]) for e in ei.value.errors()] == [("extra_forbidden", ("script", 0, "prob"))]


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
    assert schema["$defs"]["pluginStep"]["not"]["anyOf"][-1] == {"required": ["echo"]}
    jsonschema = pytest.importorskip("jsonschema", reason="jsonschema not installed")
    validator = jsonschema.Draft202012Validator(schema)
    assert validator.is_valid(make_doc([{"echo": "hi", "timeout": "5s"}]))
    assert not validator.is_valid(make_doc([{"echo": 1}]))


class BraceStep(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    brace: str

    @pydantic.field_validator("brace")
    @classmethod
    def _check(cls, v: str) -> str:
        raise ValueError("want {name} or ${#arr[@]}, not {}")


class BraceExecutor:
    key = "brace"
    model = BraceStep

    def execute(self, step, ctx, timeout):  # pragma: no cover - never runs
        pass


def test_plugin_error_with_braces_survives_load_time_check(
    isolated_registry: StepRegistry, register_plugin
):
    """#12: a plugin error message is reported verbatim, braces included."""
    register_plugin(BraceExecutor())
    with pytest.raises(pydantic.ValidationError) as ei:
        Config.model_validate(
            {"autobot": "2026-10", "attach": {"spawn": "x"}, "script": [{"brace": "x"}]}
        )
    [err] = ei.value.errors()
    assert err["loc"] == ("script", 0, "brace")
    assert err["type"] == "value_error"
    assert err["msg"] == "Value error, want {name} or ${#arr[@]}, not {}"


# -- P7-08: load-time validation (decision #12) -----------------------------


class EchoStep(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    echo: str


class InProcessEcho:
    key = "echo"
    model = EchoStep

    def execute(self, step, ctx, timeout):  # pragma: no cover - never runs
        raise AssertionError("a script that fails validation must not run")


def cli_nothing_ran(
    tmp_path: Path, script: list, pythonpath: Path | None = None, **kw
) -> subprocess.CompletedProcess[str]:
    """The CLI rejects ``script`` cleanly before prepare or spawn."""
    prepared = tmp_path / "prepared"
    spawned = tmp_path / "spawned"
    doc = make_doc(
        script,
        prepare=f"#!/bin/sh\ntouch {prepared}\n",
        spawn=f"touch {spawned}",
        **kw,
    )
    res = run_cli(doc, tmp_path, pythonpath=pythonpath)
    assert res.returncode == 1, res.stderr
    assert "Validation errors" in res.stderr
    assert "Traceback" not in res.stderr
    assert not prepared.exists(), "prepare ran"
    assert not spawned.exists(), "attach.spawn ran"
    return res


def test_p7_08_plugin_field_typo_fails_at_load(
    isolated_registry: StepRegistry, register_plugin, plugin_path: Path, tmp_path: Path
):
    """SPEC "Common Step Properties": plugin fields are validated at load time.

    A misspelled field is ``extra_forbidden`` at the step's path, and the
    CLI reports it before anything runs.
    """
    register_plugin(InProcessEcho())
    with pytest.raises(pydantic.ValidationError) as ei:
        Config.model_validate(make_doc([{"cmd": "true"}, {"echo": "hi", "ech0": "x"}]))
    [err] = ei.value.errors()
    assert err["type"] == "extra_forbidden"
    assert err["loc"] == ("script", 1, "ech0")

    res = cli_nothing_ran(tmp_path, [{"echo": "hi", "ech0": "x"}], pythonpath=plugin_path)
    assert "extra_forbidden" in res.stderr
    assert "ech0" in res.stderr


def test_p7_08_call_undefined_fn_fails_at_load(tmp_path: Path):
    """SPEC "call": an undefined target is ``undefined_function`` at load time.

    The call is nested in a block so the location shows the full path.
    """
    script = [
        {"cmd": "true"},
        {"block": {"name": "b", "script": [{"call": "nope"}]}},
    ]
    with pytest.raises(pydantic.ValidationError) as ei:
        Config.model_validate(make_doc(script, fn={"f": {"script": []}}))
    [err] = ei.value.errors()
    assert err["type"] == "undefined_function"
    assert err["loc"] == ("script", 1, "block", "script", 0, "call")
    assert err["msg"] == "call to undefined function 'nope'"

    res = cli_nothing_ran(tmp_path, script)
    assert "undefined_function" in res.stderr
    assert "call to undefined function 'nope'" in res.stderr


NOPE = {"call": "nope"}


@pytest.mark.parametrize(
    ("doc_kw", "loc"),
    [
        ({"script": [NOPE]}, ("script", 0, "call")),
        ({"script": [{**NOPE, "when": "false"}]}, ("script", 0, "call")),
        ({"attach_script": [NOPE]}, ("attach", "script", 0, "call")),
        ({"breakout": [NOPE]}, ("attach", "breakout", 0, "call")),
        ({"fn": {"f": {"script": [NOPE]}}}, ("fn", "f", "script", 0, "call")),
        (
            {"script": [{"block": {"name": "b", "enter": [NOPE]}}]},
            ("script", 0, "block", "enter", 0, "call"),
        ),
        (
            {"script": [{"block": {"name": "b", "breakout": [NOPE]}}]},
            ("script", 0, "block", "breakout", 0, "call"),
        ),
    ],
    ids=["script", "when-skipped", "attach-script", "attach-breakout", "fn-body",
         "block-enter", "block-breakout"],
)
def test_p7_08_call_undefined_fn_checked_everywhere(doc_kw: dict, loc: tuple):
    """SPEC "call": every call in the document is checked, even a skipped one."""
    doc_kw = {"script": [], **doc_kw}
    script = doc_kw.pop("script")
    with pytest.raises(pydantic.ValidationError) as ei:
        Config.model_validate(make_doc(script, **doc_kw))
    [err] = ei.value.errors()
    assert (err["type"], err["loc"]) == ("undefined_function", loc)


def test_p7_08_recursive_call_is_accepted():
    """SPEC "call": functions may call themselves; cycles are not detected."""
    Config.model_validate(
        make_doc([{"call": "a"}], fn={"a": {"script": [{"call": "b"}]}, "b": {"script": [{"call": "a"}]}})
    )
