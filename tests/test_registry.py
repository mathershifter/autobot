"""P7-01..06, P7-13, P7-16..19, P7-24, P7-25: step registry and plugin execution (SPEC.md:286)."""

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
        "plugin autobot_testplugin_clash.ClashExecutor (distribution autobot_testplugin_clash, entry point 'clash'), "
        "step key 'clash': model ClashStep reuses common step property names, which the runner handles and never "
        "passes to the plugin: timeout"
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


class OtherEchoExecutorWithKey(OtherEchoExecutor):
    def __init__(self, key: str) -> None:
        self.key = key


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


# -- P7-24: a model belongs to one step key ------------------------------------


class NapExecutor:
    """The audit's plugin: its own key, the built-in `sleep` step's model."""

    key = "nap"
    model = SleepStep

    def __init__(self) -> None:
        self.ran: list[Any] = []

    def execute(self, step: Any, ctx: Any, timeout: float) -> None:
        self.ran.append(step)


def test_p7_24_plugin_with_a_builtin_model_is_rejected(isolated_registry: StepRegistry):
    """SPEC "Common Step Properties": a plugin can't take over a built-in step by reusing its model."""
    reg = isolated_registry
    before = _registry_state(reg)
    with pytest.raises(PluginError) as ei:
        reg.register(NapExecutor())
    assert str(ei.value) == (
        f"plugin {__name__}.NapExecutor, step key 'nap': model SleepStep is already the model of "
        "the built-in step 'sleep'; a plugin needs a model of its own"
    )
    assert _registry_state(reg) == before
    assert not reg.has("nap")
    # a sleep step is still the built-in's
    assert reg.key_for_step(SleepStep(sleep=0.01)) == "sleep"
    assert type(reg.get("sleep")).__name__ == "SleepExecutor"


def test_p7_24_sleep_step_still_runs_the_builtin(isolated_registry: StepRegistry, timeline: Timeline):
    """The hijack itself: after the registration attempt, `sleep: 10ms` sleeps and the plugin never runs."""
    nap = NapExecutor()
    with pytest.raises(PluginError):
        isolated_registry.register(nap)
    run_script([{"sleep": "10ms"}])
    assert timeline.sleeps() == [0.01]
    assert nap.ran == []


@pytest.mark.parametrize("model", [CmdStep, CallStep, BlockStep, LineStep, ReturnStep, ControlStep])
def test_p7_24_every_builtin_model_is_taken(isolated_registry: StepRegistry, model: type[pydantic.BaseModel]):
    """Every built-in model is refused, and for this reason rather than for its common property fields."""
    reg = isolated_registry
    before = _registry_state(reg)
    with pytest.raises(PluginError, match=rf"model {model.__name__} is already the model of the built-in step '\w+'; "):
        reg.register(_Executor("mine", model))
    assert _registry_state(reg) == before


class RestStep(pydantic.BaseModel):
    """One model for two plugin keys."""

    model_config = pydantic.ConfigDict(extra="forbid")
    nap: str | None = None
    snooze: str | None = None


class RestExecutor:
    model = RestStep

    def __init__(self, key: str) -> None:
        self.key = key
        self.ran: list[RestStep] = []

    def execute(self, step: RestStep, ctx: Any, timeout: float) -> None:
        self.ran.append(step)


def test_p7_26_two_plugins_may_share_a_model(isolated_registry: StepRegistry):
    """SPEC "Common Step Properties": a plugin step is dispatched by its key, so two keys with one model each
    run their own plugin. This was refused for a while, as if the second plugin took the first one's steps."""
    reg = isolated_registry
    nap, snooze = RestExecutor("nap"), RestExecutor("snooze")
    reg.register(nap, origin="distribution pkg-a, entry point 'nap'")
    reg.register(snooze, origin="distribution pkg-b, entry point 'snooze'")
    assert reg.get("nap") is nap and reg.get("snooze") is snooze
    assert [e.key for e in reg.plugin_executors()] == ["nap", "snooze"]
    run_script([{"nap": "a"}, {"snooze": "b"}, {"nap": "c", "when": "true"}, {"sleep": "10ms"}])
    assert [s.nap for s in nap.ran] == ["a", "c"]
    assert [s.snooze for s in snooze.ran] == ["b"]
    # each can register again, and a third key with the same model is as good as the second
    reg.register(RestExecutor("snooze"))
    reg.register(_Executor("doze", RestStep))
    assert reg.has("doze")


def test_p7_26_sharing_a_builtin_model_or_plugin_step_is_still_refused(isolated_registry: StepRegistry):
    """Only plugins may share with each other: a built-in's model and `PluginStep` do decide the dispatch."""
    reg = isolated_registry
    reg.register(RestExecutor("nap"))
    before = _registry_state(reg)
    with pytest.raises(PluginError, match="model SleepStep is already the model of the built-in step 'sleep'"):
        reg.register(NapExecutor())
    with pytest.raises(PluginError, match="model PluginStep is the runner's own model of every plugin step"):
        reg.register(_Executor("all", PluginStep))
    assert _registry_state(reg) == before
    assert reg.key_for_step(SleepStep(sleep=0.01)) == "sleep"


def test_p7_26_a_key_still_belongs_to_one_plugin(isolated_registry: StepRegistry):
    """Sharing a model isn't sharing a key: a second plugin with a registered key is refused as before."""
    reg = isolated_registry
    reg.register(_Executor("echo", EchoStep), origin="distribution pkg-a, entry point 'echo'")
    with pytest.raises(PluginError, match="step key 'echo' is already registered by plugin"):
        reg.register(OtherEchoExecutorWithKey("echo"), origin="distribution pkg-b, entry point 'echo'")
    reg.register(OtherEchoExecutorWithKey("shout"))  # the same model under another key is fine
    assert reg.has("shout")


def test_p7_24_plugin_step_model_is_rejected(isolated_registry: StepRegistry):
    """`PluginStep` is the type of every plugin step, so a plugin with it would take all of them."""
    reg = isolated_registry
    before = _registry_state(reg)
    with pytest.raises(PluginError) as ei:
        reg.register(_Executor("all", PluginStep))
    assert str(ei.value) == (
        f"plugin {__name__}._Executor, step key 'all': model PluginStep is the runner's own model of every "
        "plugin step; a plugin needs a model of its own"
    )
    assert _registry_state(reg) == before


def test_p7_24_own_model_and_reregistration_still_work(isolated_registry: StepRegistry):
    """A model of the plugin's own registers, also a subclass of a built-in's, and the plugin can register again."""

    class MySleep(SleepStep):
        pass

    reg = isolated_registry
    reg.register(_Executor("mysleep", MySleep))
    reg.register(_Executor("mysleep", MySleep))
    assert reg.key_for_step(MySleep(sleep=1)) == "mysleep"
    assert reg.key_for_step(SleepStep(sleep=1)) == "sleep"


NAP_PLUGIN = '''
from autobot.models import SleepStep


class NapExecutor:
    key = "nap"
    model = SleepStep

    def execute(self, step, ctx, timeout):
        print("napped")
'''


def test_p7_24_cli_reports_a_reused_builtin_model(tmp_path: Path):
    """An installed plugin with a built-in's model is a `Plugin error` before anything runs."""
    root = tmp_path / "nap"
    root.mkdir()
    plugin_dist(root, "nap", NAP_PLUGIN, "NapExecutor")
    marker = tmp_path / "prepared"
    res = run_cli(make_doc([{"sleep": "10ms"}], prepare=f"touch {marker}"), tmp_path, pythonpath=root)
    assert _plugin_error(res) == (
        "plugin autobot_testplugin_nap.NapExecutor (distribution autobot_testplugin_nap, entry point 'nap'), "
        "step key 'nap': model SleepStep is already the model of the built-in step 'sleep'; "
        "a plugin needs a model of its own"
    )
    assert "napped" not in res.stdout
    assert not marker.exists()


# -- P7-25: `autobot.registry` is the module --------------------------------------

IMPORT_FORMS = {
    "import-as": "import autobot.registry as reg; print(type(reg).__name__, reg.PluginError.__mro__[1].__name__)",
    "from-import": "from autobot.registry import PluginError; print('module', PluginError.__mro__[1].__name__)",
    "plain-import": "import autobot.registry; print(type(autobot.registry).__name__, "
    "autobot.registry.PluginError.__mro__[1].__name__)",
    "package-first": "import autobot; import autobot.registry as reg; print(type(reg).__name__, "
    "reg.PluginError.__mro__[1].__name__)",
    "from-package": "from autobot import registry as reg; print(type(reg).__name__, reg.PluginError.__mro__[1].__name__)",
}


@pytest.mark.parametrize("form", list(IMPORT_FORMS))
def test_p7_25_plugin_error_is_importable_from_autobot_registry(form: str):
    """SPEC "Common Step Properties": `PluginError` is "from `autobot.registry`", however that is imported.

    `import autobot.registry as reg; reg.PluginError` raised `AttributeError: 'StepRegistry' object has
    no attribute 'PluginError'`: the package bound the registry instance to the submodule's name.
    Each form runs in a fresh interpreter, so nothing this suite imported is in the way.
    """
    res = subprocess.run(
        [sys.executable, "-c", IMPORT_FORMS[form]], check=False, capture_output=True, text=True, timeout=60
    )
    assert (res.returncode, res.stdout.strip(), res.stderr) == (0, "module TypeError", "")


def test_p7_25_autobot_registry_is_the_module():
    """In this process too: the package attribute, `sys.modules` and the import statement agree."""
    import autobot
    import autobot.registry as reg

    assert reg is sys.modules["autobot.registry"] is autobot.registry
    assert reg.PluginError is PluginError
    assert reg.StepRegistry is StepRegistry
    assert isinstance(reg.registry, StepRegistry)
    for name in autobot.__all__:
        assert hasattr(autobot, name), name


def test_p7_25_registry_methods_stay_reachable_on_the_module(isolated_registry: StepRegistry):
    """`from autobot import registry; registry.register(...)` worked on the instance, and still works."""
    from autobot import registry

    for name in ("register", "get", "has", "keys", "key_for_step", "discover", "plugin_executors", "validate_plugin_step"):
        assert getattr(registry, name) == getattr(isolated_registry, name), name
    executor = _Executor("echo", EchoStep)
    registry.register(executor)
    assert registry.has("echo") and registry.get("echo") is executor
    assert isolated_registry.get("echo") is executor
    assert "cmd" in registry.keys()


@pytest.mark.parametrize("name", ["PluginErorr", "nope", "_executors", "__wrapped__"])
def test_p7_25_unknown_module_attribute_is_a_module_error(name: str):
    """A typo is reported for the module, not as a missing attribute of a `StepRegistry` object."""
    import autobot.registry as reg

    with pytest.raises(AttributeError) as ei:
        getattr(reg, name)
    assert str(ei.value) == f"module 'autobot.registry' has no attribute '{name}'"


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
            "plugin autobot_testplugin_reserved.CmdExecutor (distribution autobot_testplugin_reserved, "
            "entry point 'cmd'): step key 'cmd' is reserved (a built-in step or a common step property)"
        ),
    ),
    "common-name-field": (
        [("clash", "clash", CLASH_PLUGIN, "ClashExecutor")],
        (
            "plugin autobot_testplugin_clash.ClashExecutor (distribution autobot_testplugin_clash, "
            "entry point 'clash'), step key 'clash': model ClashStep reuses common step property names, "
            "which the runner handles and never passes to the plugin: timeout"
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


@pytest.mark.parametrize(
    ("name", "key", "source", "target", "message"),
    [
        (
            "p719_reserved",
            "cmd",
            RESERVED_PLUGIN,
            "CmdExecutor",
            ": step key 'cmd' is reserved (a built-in step or a common step property)",
        ),
        (
            "p719_clash",
            "clash",
            CLASH_PLUGIN,
            "ClashExecutor",
            (
                ", step key 'clash': model ClashStep reuses common step property names, "
                "which the runner handles and never passes to the plugin: timeout"
            ),
        ),
    ],
)
def test_p7_19_discovered_rejection_names_origin(
    isolated_registry: StepRegistry,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    name: str,
    key: str,
    source: str,
    target: str,
    message: str,
):
    """A discovered plugin's reserved key or common-name field names its distribution and entry point."""
    plugin_dist(tmp_path, key, source, target, name=name)
    monkeypatch.syspath_prepend(str(tmp_path))
    before = _registry_state(isolated_registry)
    with pytest.raises(PluginError) as ei:
        isolated_registry.discover()
    who = f"autobot_testplugin_{name}.{target} (distribution autobot_testplugin_{name}, entry point {key!r})"
    assert str(ei.value) == f"plugin {who}{message}"
    assert _registry_state(isolated_registry) == before


class _Bare:
    """An executor with only the attributes a test gives it."""

    def __init__(self, **attrs: Any) -> None:
        self.__dict__.update(attrs)


def _run(self: Any, step: Any, ctx: Any, timeout: float) -> None:  # pragma: no cover - never runs
    pass


SHAPES: dict[str, tuple[dict[str, Any], str]] = {
    "no-key": ({"model": EchoStep, "execute": _run}, "executor has no 'key' attribute (a non-empty string)"),
    "empty-key": (
        {"key": "", "model": EchoStep, "execute": _run},
        "executor's 'key' must be a non-empty string, got ''",
    ),
    "int-key": (
        {"key": 5, "model": EchoStep, "execute": _run},
        "executor's 'key' must be a non-empty string, got 5",
    ),
    "no-model": ({"key": "shape", "execute": _run}, "executor has no 'model' attribute (a pydantic model class)"),
    "dict-model": (
        {"key": "shape", "model": dict, "execute": _run},
        "executor's 'model' must be a pydantic model class (a pydantic.BaseModel subclass), got <class 'dict'>",
    ),
    "instance-model": (
        {"key": "shape", "model": EchoStep(echo="x"), "execute": _run},
        "executor's 'model' must be a pydantic model class (a pydantic.BaseModel subclass), got EchoStep(echo='x')",
    ),
    "no-execute": ({"key": "shape", "model": EchoStep}, "executor has no 'execute' method"),
    "execute-not-callable": (
        {"key": "shape", "model": EchoStep, "execute": "run"},
        "executor's 'execute' must be callable, got 'run'",
    ),
}


@pytest.mark.parametrize("shape", list(SHAPES))
def test_p7_19_executor_shape_rejected(isolated_registry: StepRegistry, shape: str):
    """An executor without a usable key, model or execute is a `PluginError`, not an `AttributeError`."""
    attrs, why = SHAPES[shape]
    before = _registry_state(isolated_registry)
    with pytest.raises(PluginError) as ei:
        isolated_registry.register(_Bare(**attrs))
    assert str(ei.value) == f"plugin {__name__}._Bare: {why}"
    assert _registry_state(isolated_registry) == before


def test_p7_19_executor_shape_rejected_with_origin(isolated_registry: StepRegistry):
    """The origin is named the same way as for the other registration errors."""
    with pytest.raises(PluginError) as ei:
        isolated_registry.register(_Bare(key="shape", execute=_run), origin="distribution pkg, entry point 'shape'")
    assert str(ei.value) == (
        f"plugin {__name__}._Bare (distribution pkg, entry point 'shape'): "
        "executor has no 'model' attribute (a pydantic model class)"
    )


def test_p7_19_builtins_skip_the_shape_check(isolated_registry: StepRegistry):
    """Built-ins are registered as they are; only plugins are checked."""
    builtin = _Bare(key="shape", model=EchoStep)
    isolated_registry.register(builtin, builtin=True)
    assert isolated_registry.get("shape") is builtin


NO_MODEL_PLUGIN = """
class BadExecutor:
    key = "bad"

    def execute(self, step, ctx, timeout):
        pass
"""


@pytest.mark.parametrize("form", ["run", "schema"])
def test_p7_19_cli_reports_shape_error_cleanly(tmp_path: Path, form: str):
    """A plugin without a model is one `Plugin error:` line, rc 1, before prepare runs."""
    roots = _install(tmp_path, [("p719_nomodel", "bad", NO_MODEL_PLUGIN, "BadExecutor")])
    marker = tmp_path / "prepared"
    script = tmp_path / "script.autobot.yaml"
    script.write_text(yaml.safe_dump(make_doc([{"cmd": "true"}], prepare=f"touch {marker}")))
    argv = {"run": ["run", str(script)], "schema": ["schema"]}[form]
    res = _autobot(roots, *argv)
    assert _plugin_error(res) == (
        "plugin autobot_testplugin_p719_nomodel.BadExecutor (distribution autobot_testplugin_p719_nomodel, "
        "entry point 'bad'): executor has no 'model' attribute (a pydantic model class)"
    )
    assert not marker.exists()


def _prepend(monkeypatch: pytest.MonkeyPatch, roots: list[Path]) -> None:
    """Put ``roots`` on ``sys.path`` in their order, so discovery finds their entry points in that order."""
    for root in reversed(roots):
        monkeypatch.syspath_prepend(str(root))


def test_p7_20_failed_discovery_raises_again(
    isolated_registry: StepRegistry, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """SPEC.md:495: a broken entry point fails every discovery, not only the first, without loading it again."""
    _prepend(monkeypatch, _install(tmp_path, BAD_PLUGINS["import"][0]))
    reg = isolated_registry
    with pytest.raises(PluginError) as first:
        reg.discover()
    with pytest.raises(PluginError) as second:
        reg.discover()
    assert str(first.value) == str(second.value) == BAD_PLUGINS["import"][1]
    assert second.value.__cause__ is first.value
    assert type(first.value.__cause__) is ModuleNotFoundError
    assert reg.discover_calls == 1  # type: ignore[attr-defined]


def test_p7_20_plugin_after_broken_one_is_not_silently_missing(
    isolated_registry: StepRegistry, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """A plugin listed after the broken one is never reported absent: looking it up raises the discovery error."""
    plugins = [*BAD_PLUGINS["import"][0], ("after", "echo", DUP_PLUGIN, "EchoExecutor")]
    _prepend(monkeypatch, _install(tmp_path, plugins))
    reg = isolated_registry
    with pytest.raises(PluginError):
        reg.discover()
    assert "echo" not in reg._executors
    for lookup in (lambda: reg.has("echo"), lambda: reg.get("echo")):
        with pytest.raises(PluginError) as ei:
            lookup()
        assert str(ei.value) == BAD_PLUGINS["import"][1]
    with pytest.raises(PluginError, match="failed to load"):
        Config.model_validate(make_doc([{"echo": "x"}]))
    assert reg.has("cmd")


def test_p7_20_failed_registration_raises_again(
    isolated_registry: StepRegistry, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """A plugin rejected at registration (here a duplicate key) is remembered the same way."""
    plugins, message = BAD_PLUGINS["duplicate-key"]
    _prepend(monkeypatch, _install(tmp_path, plugins))
    reg = isolated_registry
    for _ in range(2):
        with pytest.raises(PluginError) as ei:
            reg.discover()
        assert str(ei.value) == message
    with pytest.raises(PluginError) as ei:
        reg.has("nope")
    assert str(ei.value) == message
    assert reg.discover_calls == 1  # type: ignore[attr-defined]


def test_p7_20_interrupted_discovery_is_retried(
    isolated_registry: StepRegistry, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """`KeyboardInterrupt` isn't remembered: the next discovery tries the entry points again."""
    plugin_dist(tmp_path, "bad", "raise KeyboardInterrupt\n", "Nothing", name="p720_interrupt")
    monkeypatch.syspath_prepend(str(tmp_path))
    reg = isolated_registry
    for _ in range(2):
        with pytest.raises(KeyboardInterrupt):
            reg.discover()
    assert reg.discover_calls == 2  # type: ignore[attr-defined]


class _Boom(Exception):
    pass


def _raising(attr: str, exc: BaseException) -> Any:
    """An executor whose ``attr`` property raises ``exc``; its other attributes are usable."""

    def boom(self: Any) -> Any:
        raise exc

    attrs: dict[str, Any] = {"key": "shape", "model": EchoStep, "execute": _run, attr: property(boom)}
    return type("Raising", (), attrs)()


@pytest.mark.parametrize("attr", ["key", "model", "execute"])
def test_p7_21_raising_attribute_is_a_plugin_error(isolated_registry: StepRegistry, attr: str):
    """An attribute that raises something other than `AttributeError` is a `PluginError` naming it, chained."""
    cause = RuntimeError(f"no {attr} today")
    before = _registry_state(isolated_registry)
    with pytest.raises(PluginError) as ei:
        isolated_registry.register(_raising(attr, cause), origin="distribution pkg, entry point 'shape'")
    assert str(ei.value) == (
        f"plugin {__name__}.Raising (distribution pkg, entry point 'shape'): "
        f"executor's {attr!r} attribute raised RuntimeError: no {attr} today"
    )
    assert ei.value.__cause__ is cause
    assert _registry_state(isolated_registry) == before


def test_p7_21_raising_attribute_without_message(isolated_registry: StepRegistry):
    """An exception without a message is named by its type alone."""
    with pytest.raises(PluginError) as ei:
        isolated_registry.register(_raising("model", _Boom()))
    assert str(ei.value) == f"plugin {__name__}.Raising: executor's 'model' attribute raised _Boom"
    assert type(ei.value.__cause__) is _Boom


def test_p7_21_raising_attribute_error_is_still_missing(isolated_registry: StepRegistry):
    """A property raising `AttributeError` still counts as a missing attribute."""
    with pytest.raises(PluginError) as ei:
        isolated_registry.register(_raising("execute", AttributeError("gone")))
    assert str(ei.value) == f"plugin {__name__}.Raising: executor has no 'execute' method"


@pytest.mark.parametrize("exc", [KeyboardInterrupt, SystemExit])
def test_p7_21_raising_attribute_does_not_wrap_interrupts(isolated_registry: StepRegistry, exc: type[BaseException]):
    with pytest.raises(exc):
        isolated_registry.register(_raising("key", exc()))


RAISING_KEY_PLUGIN = """
class BadExecutor:
    @property
    def key(self):
        raise RuntimeError("key lookup failed")

    def execute(self, step, ctx, timeout):
        pass
"""

RAISING_KEY_MESSAGE = (
    "plugin autobot_testplugin_p721_key.BadExecutor (distribution autobot_testplugin_p721_key, "
    "entry point 'bad'): executor's 'key' attribute raised RuntimeError: key lookup failed"
)


def test_p7_21_discovered_raising_attribute_is_cached(
    isolated_registry: StepRegistry, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """The error comes out of discovery like any registration error, and every later discovery raises it again."""
    plugin_dist(tmp_path, "bad", RAISING_KEY_PLUGIN, "BadExecutor", name="p721_key")
    monkeypatch.syspath_prepend(str(tmp_path))
    with pytest.raises(PluginError) as first:
        isolated_registry.discover()
    assert str(first.value) == RAISING_KEY_MESSAGE
    assert type(first.value.__cause__) is RuntimeError
    with pytest.raises(PluginError) as second:
        isolated_registry.discover()
    assert str(second.value) == RAISING_KEY_MESSAGE
    assert second.value.__cause__ is first.value


def test_p7_21_cli_reports_raising_attribute_cleanly(tmp_path: Path):
    """A plugin whose `key` property raises is one `Plugin error:` line, rc 1, before prepare runs."""
    roots = _install(tmp_path, [("p721_key", "bad", RAISING_KEY_PLUGIN, "BadExecutor")])
    marker = tmp_path / "prepared"
    script = tmp_path / "script.autobot.yaml"
    script.write_text(yaml.safe_dump(make_doc([{"cmd": "true"}], prepare=f"touch {marker}")))
    res = _autobot(roots, "run", str(script))
    assert _plugin_error(res) == RAISING_KEY_MESSAGE
    assert res.returncode == 1
    assert not marker.exists()
