"""Shared fixtures for the Autobot suite (test plan fixtures F1, F2, F4 to F7).

Import helpers with ``from conftest import ...``; pytest puts ``tests/`` on
``sys.path`` because the directory has no ``__init__.py``.
"""

from __future__ import annotations

import copy
import importlib.metadata
import json
import os
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pexpect
import pydantic
import pytest
import yaml

import autobot.registry as registry_mod
import autobot.runner as runner_mod
from autobot.cli import load_schema
from autobot.models import Config
from autobot.registry import StepRegistry
from autobot.runner import Runner
from autobot.session import LineTooLong, PromptHandler, Session
from autobot.steps import register_builtins


ROOT = Path(__file__).resolve().parent.parent
DEVICE = Path(__file__).resolve().parent / "fakes" / "device.py"

# -- F1: real local shell ----------------------------------------------------


@pytest.fixture(autouse=True)
def _plain_shell_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The spawned shell inherits the suite's environment, and `env` in templates holds all of it: drop
    what would change how bash behaves, the suite's own `AB_*` names, and names of mapping methods, and
    give the shell the prompt that `SHELL_PROMPT` matches."""
    for name in ("PROMPT_COMMAND", "BASH_ENV", "ENV", "PS0", *dir({}), *(k for k in os.environ if k.startswith("AB_"))):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PS1", PS1)


BASH = "bash --norc --noprofile -i"
PS1 = "PROMPT$ "
# for a `Session.attach` of its own; a run's shell inherits its `PS1` from `_plain_shell_env`
SHELL_ENV = {"TERM": "dumb", "PS1": PS1, "PATH": os.environ["PATH"]}
SHELL_PROMPT = {"name": "sh", "expect": [r"PROMPT\$ "], "return": True}
RC_PROBE = "echo __AUTOBOT_RC=$?"

_TIMEOUT_STEPS = ("cmd", "call", "block", "control")
_DEFAULT = object()


def make_doc(
    script: list[dict[str, Any]],
    *,
    prompts: Any = _DEFAULT,
    errors: list[str] | None = None,
    vars: dict[str, Any] | None = None,
    env: dict[str, str] | None = None,
    fn: dict[str, Any] | None = None,
    spawn: str = BASH,
    attach_script: list[dict[str, Any]] | None = None,
    breakout: list[dict[str, Any]] | None = None,
    prepare: str | None = None,
    timeout: Any = 5,
    step_timeout: str | None = "5s",
) -> dict[str, Any]:
    """Build a raw YAML-shaped document for a local-shell run."""
    script = copy.deepcopy(script)
    if step_timeout is not None:
        for step in script:
            if any(k in step for k in _TIMEOUT_STEPS):
                step.setdefault("timeout", step_timeout)
    attach: dict[str, Any] = {"spawn": spawn}
    if timeout is not None:
        attach["timeout"] = timeout
    if attach_script:
        attach["script"] = copy.deepcopy(attach_script)
    if breakout is not None:
        attach["breakout"] = breakout
    if prepare is not None:
        attach["prepare"] = prepare
    doc: dict[str, Any] = {
        "autobot": "2026-10",
        "prompts": [SHELL_PROMPT] if prompts is _DEFAULT else prompts,
        "attach": attach,
        "script": script,
    }
    if errors:
        doc["errors"] = errors
    if vars is not None:
        doc["vars"] = vars
    if env is not None:
        doc["env"] = env
    if fn is not None:
        doc["fn"] = fn
    return doc


def make_config(script: list[dict[str, Any]], **kw: Any) -> Config:
    return Config.model_validate(make_doc(script, **kw))


def make_runner(script: list[dict[str, Any]], *, args: dict[str, str] | None = None, **kw: Any) -> Runner:
    return Runner(make_config(script, **kw), args or {})


def run_script(script: list[dict[str, Any]], *, args: dict[str, str] | None = None, **kw: Any) -> Runner:
    """Build a config, run it end to end, and return the runner.

    Registered values are in ``runner.config.vars``.
    """
    runner = make_runner(script, args=args, **kw)
    runner.run()
    return runner


def run_vars(script: list[dict[str, Any]], **kw: Any) -> dict[str, Any]:
    """``run_script`` and return the registered ``vars``."""
    return run_script(script, **kw).config.vars


def steps(items: list[dict[str, Any]]) -> list:
    """Validate a list of step dicts through ``Config``."""
    return Config.model_validate(
        {"autobot": "2026-10", "attach": {"spawn": BASH}, "script": items}
    ).script


def handler_names(runner_or_session: Runner | Session) -> list[str]:
    session = runner_or_session.session if isinstance(runner_or_session, Runner) else runner_or_session
    return [h.name for h in session.save_handlers()]


@pytest.fixture
def attached_runner() -> Iterator[Callable[..., Runner]]:
    """Factory: a Runner attached to local bash, its first prompt pending."""
    runners: list[Runner] = []

    def factory(**kw: Any) -> Runner:
        runner = make_runner([], **kw)
        runners.append(runner)
        runner.session.attach(BASH, env=dict(SHELL_ENV), timeout=5)
        return runner

    yield factory
    for r in runners:
        r.session.detach()


@pytest.fixture
def shell_session() -> Iterator[Session]:
    s = Session([PromptHandler("sh", [r"PROMPT\$ "], [], True)])
    s.attach(BASH, env=dict(SHELL_ENV), timeout=5)
    try:
        yield s
    finally:
        s.detach()


@pytest.fixture
def children(monkeypatch: pytest.MonkeyPatch) -> list[pexpect.spawn]:
    """Record every child the session detaches, so tests can check it is dead."""
    seen: list[pexpect.spawn] = []
    orig = Session.detach

    def detach(self, *args, **kwargs):
        if self._cld is not None:
            seen.append(self._cld)
        orig(self, *args, **kwargs)

    monkeypatch.setattr(Session, "detach", detach)
    return seen


# -- F2: scriptable fake device ---------------------------------------------


class FakeDevice:
    def __init__(self, tmp_path: Path):
        self._tmp = tmp_path
        self._n = 0
        self.log_path: Path | None = None

    def __call__(self, *opts: str) -> tuple[str, Path]:
        self._n += 1
        self.log_path = self._tmp / f"device{self._n}.log"
        cmd = " ".join([sys.executable, str(DEVICE), "--log", str(self.log_path), *opts])
        return cmd, self.log_path

    @staticmethod
    def read(log_path: Path) -> list[str]:
        return log_path.read_text().splitlines() if log_path.exists() else []


@pytest.fixture
def fake_device(tmp_path: Path) -> FakeDevice:
    """Factory: ``fake_device(*opts) -> (spawn_cmd, log_path)``."""
    return FakeDevice(tmp_path)


# -- F4: send and spawn recorders -------------------------------------------


class SentLog(list):
    def lines(self) -> list[str]:
        return [v for k, v in self if k == "line"]

    def commands(self) -> list[str]:
        return [v for v in self.lines() if v and v != RC_PROBE]

    def controls(self) -> list[str]:
        return [v for k, v in self if k == "ctrl"]


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> SentLog:
    log = SentLog()
    orig_line = Session._put_line
    orig_ctrl = Session._put_control

    def put_line(self, line, *args, **kwargs):
        log.append(("line", line))
        try:
            return orig_line(self, line, *args, **kwargs)
        except LineTooLong:
            log.pop()  # refused: nothing of it was sent
            raise

    def put_control(self, char, *args, **kwargs):
        log.append(("ctrl", char))
        return orig_ctrl(self, char, *args, **kwargs)

    monkeypatch.setattr(Session, "_put_line", put_line)
    monkeypatch.setattr(Session, "_put_control", put_control)
    return log


class SpawnRecorded(Exception):
    """Raised by ``spawned`` in stop mode instead of starting a child."""


class SpawnLog(list):
    stop = False


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> SpawnLog:
    log = SpawnLog()
    orig = pexpect.spawn.__init__

    def init(self, command, *args, **kwargs):
        log.append((command, kwargs))
        if log.stop:
            raise SpawnRecorded(command)
        orig(self, command, *args, **kwargs)

    monkeypatch.setattr(pexpect.spawn, "__init__", init)
    return log


# -- F5: timeline recorder ---------------------------------------------------


class AttachRecorded(Exception):
    """Raised by ``timeline`` when ``stop_attach`` is set."""


class Timeline(list):
    """Ordered ``(name, principal_arg)`` events; full calls in ``calls``."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, tuple, dict]] = []
        self.stop_attach = False

    def names(self) -> list[str]:
        return [e[0] for e in self]

    def sleeps(self) -> list[float]:
        return [e[1] for e in self if e[0] == "sleep"]

    def since(self, name: str) -> list[tuple]:
        """Events after the first ``name`` event (exclusive)."""
        names = self.names()
        return list(self[names.index(name) + 1 :]) if name in names else []

    def index_of(self, event: tuple, start: int = 0) -> int:
        for i in range(start, len(self)):
            if _event_matches(self[i], event):
                return i
        raise ValueError(f"{event!r} not in timeline after {start}: {list(self)!r}")

    def has_subsequence(self, expected: list[tuple]) -> bool:
        pos = 0
        for event in expected:
            try:
                pos = self.index_of(event, pos) + 1
            except ValueError:
                return False
        return True


def _event_matches(actual: tuple, expected: tuple) -> bool:
    return actual[0] == expected[0] and (len(expected) == 1 or actual[1] == expected[1])


def _principal(name: str, args: tuple, kwargs: dict) -> Any:
    if name == "restore_handlers":
        return [h.name for h in args[0]]
    if name in ("get_prompt", "reset_handlers"):
        return None
    if args:
        return args[0]
    if name == "sendline":
        return kwargs.get("line", "")
    return next(iter(kwargs.values()), None)


@pytest.fixture
def timeline(monkeypatch: pytest.MonkeyPatch) -> Timeline:
    tl = Timeline()

    def sleep(self, seconds):
        tl.append(("sleep", seconds))
        tl.calls.append(("sleep", (seconds,), {}))

    monkeypatch.setattr(Session, "sleep", sleep)

    def wrap(name: str) -> None:
        orig = getattr(Session, name)

        def method(self, *args, **kwargs):
            tl.append((name, _principal(name, args, kwargs)))
            tl.calls.append((name, args, kwargs))
            if name == "attach" and tl.stop_attach:
                raise AttachRecorded(args[0] if args else kwargs.get("spawn"))
            return orig(self, *args, **kwargs)

        monkeypatch.setattr(Session, name, method)

    for name in ("expect", "get_prompt", "sendline", "reset_handlers", "restore_handlers", "attach"):
        wrap(name)
    return tl


# -- F6: registry isolation and plugins -------------------------------------


class ProbeStep(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    probe: str


class ProbeExecutor:
    """Plugin that records what it saw of the session when executed."""

    key = "probe"
    model = ProbeStep

    def __init__(self) -> None:
        self.calls: list[tuple[str, float]] = []
        self.records: list[dict[str, Any]] = []

    def execute(self, step: ProbeStep, ctx: Any, timeout: float) -> None:
        self.calls.append((step.probe, timeout))
        handlers = ctx.session.save_handlers()
        self.records.append(
            {
                "step": step,
                "timeout": timeout,
                "handlers": [h.name for h in handlers],
                "handlers_id": id(handlers),
                "ctx": dict(ctx.session.ctx),
            }
        )

    def by_name(self, probe: str) -> dict[str, Any]:
        return next(r for r in self.records if r["step"].probe == probe)


@pytest.fixture
def isolated_registry(monkeypatch: pytest.MonkeyPatch) -> StepRegistry:
    """A fresh registry with builtins, and a counted ``entry_points``."""
    reg = StepRegistry()
    register_builtins(reg)
    reg.discover_calls = 0  # type: ignore[attr-defined]
    monkeypatch.setattr(registry_mod, "registry", reg)
    monkeypatch.setattr(runner_mod, "registry", reg)
    real = importlib.metadata.entry_points

    def entry_points(**kw):
        if kw.get("group") == "autobot.steps":
            reg.discover_calls += 1  # type: ignore[attr-defined]
        return real(**kw)

    monkeypatch.setattr(registry_mod.importlib.metadata, "entry_points", entry_points)
    return reg


@pytest.fixture
def register_plugin() -> Iterator[Callable[[Any], Any]]:
    """Register executors on the current registry; removed in teardown."""
    added: list[tuple[StepRegistry, Any]] = []

    def register(executor: Any) -> Any:
        reg = registry_mod.registry
        reg.register(executor)
        added.append((reg, executor))
        return executor

    yield register
    for reg, ex in added:
        reg._executors.pop(ex.key, None)


@pytest.fixture
def probe(register_plugin) -> ProbeExecutor:
    return register_plugin(ProbeExecutor())


def plugin_dist(root: Path, key: str, source: str, target: str, *, name: str | None = None) -> Path:
    """Write ``<mod>.py`` plus a dist-info with an ``autobot.steps`` entry.

    ``target`` is the attribute in the module the entry point loads.
    ``name`` (default ``key``) names the module and distribution,
    ``autobot_testplugin_<name>``, so two dists can share a step key.
    Put ``root`` on ``sys.path`` (``monkeypatch.syspath_prepend``) or on
    ``PYTHONPATH`` for CLI subprocesses. Never call ``discover()`` on the
    global registry while it is on ``sys.path``.
    """
    mod = f"autobot_testplugin_{name or key}"
    (root / f"{mod}.py").write_text(source)
    dist = root / f"{mod}-0.1.dist-info"
    dist.mkdir()
    (dist / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {mod}\nVersion: 0.1\n")
    (dist / "entry_points.txt").write_text(f"[autobot.steps]\n{key} = {mod}:{target}\n")
    return root


def run_cli(
    doc: Any,
    tmp_path: Path,
    *cli_args: str,
    pythonpath: Path | list[Path] | None = None,
    raw: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``python -m autobot.cli`` on ``doc`` (or ``raw`` file text).

    ``pythonpath`` is prepended to ``PYTHONPATH``; a list keeps its order.
    """
    path = tmp_path / "script.autobot.yaml"
    path.write_text(raw if raw is not None else yaml.safe_dump(doc))
    env = dict(os.environ)
    if pythonpath:
        paths = [pythonpath] if isinstance(pythonpath, Path) else pythonpath
        env["PYTHONPATH"] = os.pathsep.join([*map(str, paths), *filter(None, [env.get("PYTHONPATH")])])
    return subprocess.run(
        [sys.executable, "-m", "autobot.cli", str(path), *cli_args],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


# -- F7: JSON schema validator ----------------------------------------------


@pytest.fixture(scope="session")
def schema() -> dict[str, Any]:
    return load_schema()


@pytest.fixture(scope="session")
def schema_validator(schema: dict[str, Any]) -> Any:
    jsonschema = pytest.importorskip("jsonschema", reason="jsonschema not installed; parity tests skipped")
    return jsonschema.Draft202012Validator(schema)


def model_ok(doc: Any) -> bool:
    try:
        Config.model_validate(doc)
    except pydantic.ValidationError:
        return False
    return True


@pytest.fixture
def both_validate(schema_validator: Any) -> Callable[[Any], tuple[bool, bool]]:
    """``both_validate(doc) -> (model_ok, schema_ok)``."""

    def check(doc: Any) -> tuple[bool, bool]:
        return model_ok(doc), schema_validator.is_valid(doc)

    return check
