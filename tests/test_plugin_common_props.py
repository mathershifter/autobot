from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pydantic
import pytest

from autobot.models import Config
from autobot.registry import registry
from autobot.runner import Runner
from autobot.session import Session

BASH = "bash --norc --noprofile -i"


class ProbeStep(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    probe: str


class ProbeExecutor:
    key = "probe"
    model = ProbeStep

    def __init__(self) -> None:
        self.calls: list[tuple[str, float]] = []

    def execute(self, step: ProbeStep, ctx: Any, timeout: float) -> None:
        self.calls.append((step.probe, timeout))


@pytest.fixture
def probe() -> Iterator[ProbeExecutor]:
    ex = ProbeExecutor()
    registry.register(ex)
    try:
        yield ex
    finally:
        registry._executors.pop(ex.key, None)
        registry._model_keys.pop(ex.model, None)


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    calls: list[float] = []
    monkeypatch.setattr(Session, "sleep", lambda self, s: calls.append(s))
    return calls


def run(script: list[dict[str, Any]]) -> None:
    cfg = Config.model_validate(
        {
            "autobot": "2026-08",
            "prompts": [{"name": "sh", "expect": [r"PROMPT\$ "]}],
            "attach": {
                "spawn": BASH,
                "timeout": 10,
                "env": {"TERM": "dumb", "PS1": "PROMPT$ ", "PATH": os.environ["PATH"]},
            },
            "script": script,
        }
    )
    Runner(cfg, {}).run()


def test_plugin_delay_after(probe: ProbeExecutor, sleeps: list[float]):
    run([{"probe": "x", "delay_after": "3s"}])
    assert [c[0] for c in probe.calls] == ["x"]
    assert sleeps == [3]


def test_plugin_delay_before(probe: ProbeExecutor, sleeps: list[float]):
    run([{"probe": "x", "delay_before": "500ms"}])
    assert [c[0] for c in probe.calls] == ["x"]
    assert sleeps == [0.5]


def test_plugin_delay_after_real_sleep(probe: ProbeExecutor):
    import time

    start = time.monotonic()
    run([{"probe": "x", "delay_after": "1s"}])
    assert time.monotonic() - start >= 1


def test_plugin_timeout(probe: ProbeExecutor, sleeps: list[float]):
    run([{"probe": "x", "timeout": "7s"}])
    assert probe.calls == [("x", 7)]


@pytest.mark.parametrize(("when", "ran"), [("false", False), ("yes", True)])
def test_plugin_when(probe: ProbeExecutor, sleeps: list[float], when: str, ran: bool):
    run([{"probe": "x", "when": when, "delay_after": "1s"}])
    assert bool(probe.calls) is ran
    assert sleeps == ([1] if ran else [])


def test_plugin_after(probe: ProbeExecutor, sleeps: list[float]):
    run([{"line": "echo mark-ready"}, {"probe": "x", "after": "mark-ready\\r\\n"}])
    assert [c[0] for c in probe.calls] == ["x"]
