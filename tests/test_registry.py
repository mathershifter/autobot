"""P7-01..06, P7-13, P7-16..18: step registry and plugin execution (SPEC.md:286)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pydantic
import pytest
import yaml
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
from autobot.registry import PluginError, StepRegistry

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
    with pytest.raises(PluginError) as ei:
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
    with pytest.raises(PluginError) as ei:
        isolated_registry.register(_Executor("alias", AliasStep))
    names = "timeout (field 'limit'), when (field 'gate'), after (field 'wait'), delay_after"
    assert str(ei.value) == _clash("alias", AliasStep, names)
    assert "alias" not in isolated_registry._executors


@pytest.mark.parametrize("key", [*sorted(BUILTINS), *COMMON, "plugin_key_"])
def test_p7_13_reserved_plugin_key_rejected(isolated_registry: StepRegistry, key: str):
    """A built-in or common name as a plugin key would replace a built-in or never be routed: rejected."""
    before = dict(isolated_registry._executors)
    model = pydantic.create_model("KeyStep", value=(str, ...))
    with pytest.raises(PluginError) as ei:
        isolated_registry.register(_Executor(key, model))
    assert str(ei.value) == (
        f"plugin {__name__}._Executor: step key {key!r} is reserved (a built-in step or a common step property)"
    )
    assert isolated_registry._executors == before


def _stderr(res: subprocess.CompletedProcess[str]) -> list[str]:
    # `python -m autobot.cli` warns that the package imported the module first; that line isn't autobot's
    return [line for line in res.stderr.splitlines() if "RuntimeWarning" not in line]


def _plugin_error(res: subprocess.CompletedProcess[str]) -> str:
    """The message of a clean `Plugin error:` exit: rc 1, nothing on stdout, one stderr line, no traceback."""
    assert res.returncode == 1, res.stderr
    assert res.stdout == ""
    assert "Traceback" not in res.stderr
    [line] = _stderr(res)
    assert line.startswith("Plugin error: "), line
    return line.removeprefix("Plugin error: ")


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
    """Discovery rejects the plugin before anything runs, in `run` and `schema`: one clean line, no traceback."""
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
    assert _plugin_error(res) == (
        "plugin autobot_testplugin_clash.ClashExecutor, step key 'clash': model ClashStep reuses "
        "common step property names, which the runner handles and never passes to the plugin: timeout"
    )
    assert not marker.exists()


class EchoStep(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    echo: str


class OtherEchoExecutor:
    key = "echo"
    model = EchoStep

    def execute(self, step: Any, ctx: Any, timeout: float) -> None:  # pragma: no cover - never runs
        pass


def _registry_state(reg: StepRegistry) -> tuple[dict, dict, dict]:
    return dict(reg._executors), dict(reg._model_keys), dict(reg._origins)


def test_p7_16_second_plugin_with_same_key_rejected(isolated_registry: StepRegistry):
    """A key registered by one plugin can't be taken by another: no silent last-one-wins dispatch."""
    reg = isolated_registry
    first = _Executor("echo", EchoStep)
    reg.register(first)
    before = _registry_state(reg)
    # another class, and the same class with another model, are both another plugin
    for other in (OtherEchoExecutor(), _Executor("echo", ProbeStep)):
        with pytest.raises(PluginError) as ei:
            reg.register(other)
        assert str(ei.value) == (
            f"plugin {__name__}.{type(other).__qualname__}: step key 'echo' is already registered by plugin "
            f"{__name__}._Executor"
        )
        assert _registry_state(reg) == before
    assert reg.get("echo") is first


def test_p7_16_same_plugin_registered_again_is_harmless(isolated_registry: StepRegistry):
    """The same instance is a no-op; another instance of the same class and model replaces it."""
    reg = isolated_registry
    first = _Executor("echo", EchoStep)
    reg.register(first)
    reg.register(first)
    assert reg.get("echo") is first
    again = _Executor("echo", EchoStep)
    reg.register(again)
    assert reg.get("echo") is again
    assert [e.key for e in reg.plugin_executors()] == ["echo"]
    assert reg._model_keys[EchoStep] == "echo"


DUP_PLUGIN = '''
import pydantic


class EchoStep(pydantic.BaseModel):
    echo: str


class EchoExecutor:
    key = "echo"
    model = EchoStep

    def execute(self, step, ctx, timeout):
        print("ran", __name__)
'''


@pytest.mark.parametrize("command", ["run-builtin", "run-plugin", "schema"])
def test_p7_16_cli_fails_fast_on_duplicate_plugin_key(tmp_path: Path, command: str):
    """Two installed dists with the same key: discovery fails before anything runs, naming both plugins."""
    roots = []
    for name in ("dup_a", "dup_b"):
        root = tmp_path / name
        root.mkdir()
        plugin_dist(root, "echo", DUP_PLUGIN, "EchoExecutor", name=name)
        roots.append(root)
    marker = tmp_path / "prepared"
    if command == "schema":
        path = os.pathsep.join([*map(str, roots), *filter(None, [os.environ.get("PYTHONPATH")])])
        res = subprocess.run(
            [sys.executable, "-m", "autobot.cli", "schema"],
            check=False,
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": path},
            timeout=60,
        )
    else:
        step = {"cmd": "true"} if command == "run-builtin" else {"echo": "x"}
        res = run_cli(make_doc([step], prepare=f"touch {marker}"), tmp_path, pythonpath=roots)
    assert _plugin_error(res) == (
        "plugin autobot_testplugin_dup_b.EchoExecutor (distribution autobot_testplugin_dup_b, "
        "entry point 'echo'): step key 'echo' is already registered by plugin "
        "autobot_testplugin_dup_a.EchoExecutor (distribution autobot_testplugin_dup_a, entry point 'echo')"
    )
    assert not marker.exists()


BROKEN_PLUGIN = """
import autobot_testplugin_missing_dep
"""

RESERVED_PLUGIN = '''
import pydantic


class CmdStep(pydantic.BaseModel):
    cmd: str


class CmdExecutor:
    key = "cmd"
    model = CmdStep

    def execute(self, step, ctx, timeout):
        pass
'''

# kind -> (plugins as (name, key, source, target), the message after `Plugin error: `)
BAD_PLUGINS: dict[str, tuple[list[tuple[str, str, str, str]], str]] = {
    "import": (
        [("broken", "broken", BROKEN_PLUGIN, "Nothing")],
        (
            "entry point 'broken' (distribution autobot_testplugin_broken) failed to load: "
            "ModuleNotFoundError: No module named 'autobot_testplugin_missing_dep'"
        ),
    ),
    "reserved-key": (
        [("reserved", "cmd", RESERVED_PLUGIN, "CmdExecutor")],
        (
            "plugin autobot_testplugin_reserved.CmdExecutor: step key 'cmd' is reserved "
            "(a built-in step or a common step property)"
        ),
    ),
    "common-name-field": (
        [("clash", "clash", CLASH_PLUGIN, "ClashExecutor")],
        (
            "plugin autobot_testplugin_clash.ClashExecutor, step key 'clash': model ClashStep reuses "
            "common step property names, which the runner handles and never passes to the plugin: timeout"
        ),
    ),
    "duplicate-key": (
        [("dup_a", "echo", DUP_PLUGIN, "EchoExecutor"), ("dup_b", "echo", DUP_PLUGIN, "EchoExecutor")],
        (
            "plugin autobot_testplugin_dup_b.EchoExecutor (distribution autobot_testplugin_dup_b, "
            "entry point 'echo'): step key 'echo' is already registered by plugin "
            "autobot_testplugin_dup_a.EchoExecutor (distribution autobot_testplugin_dup_a, entry point 'echo')"
        ),
    ),
}


def _install(tmp_path: Path, plugins: list[tuple[str, str, str, str]]) -> list[Path]:
    roots = []
    for name, key, source, target in plugins:
        root = tmp_path / name
        root.mkdir()
        plugin_dist(root, key, source, target, name=name)
        roots.append(root)
    return roots


def _autobot(roots: list[Path], *argv: str) -> subprocess.CompletedProcess[str]:
    path = os.pathsep.join([*map(str, roots), *filter(None, [os.environ.get("PYTHONPATH")])])
    return subprocess.run(
        [sys.executable, "-m", "autobot.cli", *argv],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": path},
        timeout=60,
    )


@pytest.mark.parametrize("form", ["default", "run", "schema"])
@pytest.mark.parametrize("kind", list(BAD_PLUGINS))
def test_p7_17_cli_reports_plugin_error_cleanly(tmp_path: Path, kind: str, form: str):
    """Every discovery failure is one `Plugin error:` line, rc 1, before the script is read or prepare runs."""
    plugins, message = BAD_PLUGINS[kind]
    roots = _install(tmp_path, plugins)
    marker = tmp_path / "prepared"
    script = tmp_path / "script.autobot.yaml"
    script.write_text(yaml.safe_dump(make_doc([{"cmd": "true"}], prepare=f"touch {marker}")))
    argv = {"default": [str(script)], "run": ["run", str(script)], "schema": ["schema"]}[form]
    res = _autobot(roots, *argv)
    assert _plugin_error(res) == message
    assert not marker.exists()


def test_p7_17_cli_plugin_error_comes_before_script_errors(tmp_path: Path):
    """Plugins are loaded before the script is read: a broken plugin is reported even for a missing script."""
    plugins, message = BAD_PLUGINS["import"]
    res = _autobot(_install(tmp_path, plugins), str(tmp_path / "missing.yaml"))
    assert _plugin_error(res) == message


LATE_PLUGIN = '''
import pydantic

from autobot.registry import PluginError


class LateStep(pydantic.BaseModel):
    late: str


class LateExecutor:
    key = "late"
    model = LateStep

    def execute(self, step, ctx, timeout):
        raise PluginError("raised while running")
'''


def test_p7_17_cli_plugin_error_at_run_time_is_not_caught(tmp_path: Path):
    """Only discovery is guarded: a `PluginError` from a running step is a traceback, after prepare ran."""
    roots = _install(tmp_path, [("late", "late", LATE_PLUGIN, "LateExecutor")])
    marker = tmp_path / "prepared"
    res = run_cli(make_doc([{"late": "x"}], prepare=f"touch {marker}"), tmp_path, pythonpath=roots)
    assert res.returncode == 1
    assert "Plugin error:" not in res.stderr
    assert "Traceback" in res.stderr
    assert res.stderr.strip().splitlines()[-1] == "autobot.registry.PluginError: raised while running"
    assert marker.exists()


INIT_FAILS_PLUGIN = '''
class Executor:
    def __init__(self):
        raise RuntimeError("bad init")
'''


def test_p7_18_plugin_error_is_a_type_error():
    """Callers that caught the registration `TypeError` still catch it; it's no `ValueError`, which pydantic
    would turn into a validation error."""
    assert issubclass(PluginError, TypeError)
    assert not issubclass(PluginError, ValueError)


@pytest.mark.parametrize(
    ("name", "source", "target", "message", "cause"),
    [
        (
            "p718_missing",
            BROKEN_PLUGIN,
            "Nothing",
            "ModuleNotFoundError: No module named 'autobot_testplugin_missing_dep'",
            ModuleNotFoundError,
        ),
        (
            "p718_attr",
            "X = 1\n",
            "Nothing",
            "AttributeError: module 'autobot_testplugin_p718_attr' has no attribute 'Nothing'",
            AttributeError,
        ),
        ("p718_init", INIT_FAILS_PLUGIN, "Executor", "RuntimeError: bad init", RuntimeError),
        ("p718_bare", "raise RuntimeError\n", "Nothing", "RuntimeError", RuntimeError),
    ],
)
def test_p7_18_discover_wraps_load_failure(
    isolated_registry: StepRegistry,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    name: str,
    source: str,
    target: str,
    message: str,
    cause: type[BaseException],
):
    """A plugin that fails to import or instantiate is a `PluginError` naming its entry point, chained from the cause."""
    plugin_dist(tmp_path, "bad", source, target, name=name)
    monkeypatch.syspath_prepend(str(tmp_path))
    with pytest.raises(PluginError) as ei:
        isolated_registry.discover()
    assert str(ei.value) == f"entry point 'bad' (distribution autobot_testplugin_{name}) failed to load: {message}"
    assert type(ei.value.__cause__) is cause
    assert "bad" not in isolated_registry._executors


@pytest.mark.parametrize("exc", [KeyboardInterrupt, SystemExit])
def test_p7_18_discover_does_not_wrap_interrupts(
    isolated_registry: StepRegistry, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, exc: type[BaseException]
):
    """`KeyboardInterrupt` and `SystemExit` from a plugin's import pass through unchanged."""
    name = f"p718_{exc.__name__.lower()}"
    plugin_dist(tmp_path, "bad", f"raise {exc.__name__}\n", "Nothing", name=name)
    monkeypatch.syspath_prepend(str(tmp_path))
    with pytest.raises(exc):
        isolated_registry.discover()
