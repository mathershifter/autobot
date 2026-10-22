"""The import graph (review findings 3.6 and 3.7): `import autobot` loads no submodule, the models don't
import the registry, the registry is complete without the runner, and the runner doesn't import the steps.

Each case runs in a fresh interpreter, so nothing the suite imported is in the way.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src" / "autobot"
BUILTINS = ["block", "call", "cmd", "control", "line", "return", "set", "sleep"]


def _modules(code: str) -> set[str]:
    """The modules loaded once `code` has run in a fresh interpreter, which must print nothing."""
    code = code + "\nimport sys\nprint('\\n'.join(sorted(sys.modules)))"
    res = subprocess.run([sys.executable, "-c", code], check=False, capture_output=True, text=True, timeout=60)
    assert (res.returncode, res.stderr) == (0, ""), res.stderr
    return set(res.stdout.split())


def test_import_autobot_loads_nothing_else():
    """`import autobot` imports no submodule: not the CLI (argparse, yaml), not the runner (rich)."""
    mods = _modules("import autobot")
    assert not {m for m in mods if m.startswith("autobot.")}
    assert not mods & {"argparse", "yaml", "rich", "jinja2", "pexpect"}


def test_import_models_does_not_import_the_registry():
    """The models ask the registry nothing at import; a validation without a context imports it then."""
    mods = _modules("import autobot.models")
    assert "autobot.models" in mods
    assert not mods & {"autobot.registry", "autobot.steps", "autobot.runner", "autobot.cli"}


def test_registry_has_the_builtins_without_the_runner():
    """The built-ins are registered where the registry is created, not when the runner is imported."""
    mods = _modules(f"import autobot.registry as reg\nassert sorted(reg.registry.keys()) == {BUILTINS!r}")
    assert not mods & {"autobot.runner", "autobot.session", "autobot.cli", "rich"}


def test_runner_imports_registry_not_steps():
    """The runner dispatches through the registry alone."""
    assert "autobot.steps" not in _imports(SRC / "runner.py"), "runner.py imports the steps"
    assert "autobot.registry" in _imports(SRC / "runner.py")


def test_models_import_the_registry_only_as_the_default():
    """At module level the models import keys and types; the registry only inside `_default_plugins`."""
    tree = ast.parse((SRC / "models.py").read_text())
    top = {m for node in tree.body for m in _names(node)}
    assert "autobot.registry" not in top
    [default] = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_default_plugins"]
    assert "autobot.registry" in {m for node in ast.walk(default) for m in _names(node)}


def _imports(path: Path) -> set[str]:
    return {m for node in ast.walk(ast.parse(path.read_text())) for m in _names(node)}


def _names(node: ast.AST) -> set[str]:
    if isinstance(node, ast.ImportFrom) and node.level:
        base = "autobot" + ("." + node.module if node.module else "")
        if node.module:
            return {base}
        return {f"{base}.{alias.name}" for alias in node.names}
    if isinstance(node, ast.ImportFrom) and node.module:
        return {node.module}
    if isinstance(node, ast.Import):
        return {alias.name for alias in node.names}
    return set()


def test_package_names_resolve_on_first_use():
    """`from autobot import ...` gives each name of `__all__`; the registry attribute is the module; `main`
    is the CLI's (`autobot.cli:main`), not the package's."""
    code = """
import autobot
import sys
assert not {m for m in sys.modules if m.startswith("autobot.")}
from autobot import Config, PluginLookup, Reporter, Runner, RunnerContext, SessionLike, StepExecutor, registry
assert registry is sys.modules["autobot.registry"] is autobot.registry
assert all(hasattr(autobot, name) for name in autobot.__all__)
assert Config.__module__ == "autobot.models" and Runner.__module__ == "autobot.runner"
try:
    autobot.main
except AttributeError as e:
    assert str(e) == "module 'autobot' has no attribute 'main'", e
else:
    raise AssertionError("autobot.main")
from autobot.cli import main
assert callable(main)
"""
    assert "autobot.cli" in _modules(code)


@pytest.mark.parametrize("command", ["module", "script"])
def test_cli_help_runs(command: str):
    """`python -m autobot.cli --help` and the installed console script `autobot --help` both run."""
    if command == "module":
        argv = [sys.executable, "-m", "autobot.cli", "--help"]
    else:
        script = Path(sys.executable).parent / "autobot"
        if not script.exists():
            pytest.skip("no installed console script beside the interpreter")
        argv = [str(script), "--help"]
    res = subprocess.run(argv, check=False, capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr
    assert res.stdout.startswith("usage: ") and "{run,validate,schema}" in res.stdout
