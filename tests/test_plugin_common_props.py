from __future__ import annotations

import time

import pytest
from conftest import ProbeExecutor, Timeline, run_script


def test_plugin_delay_after(probe: ProbeExecutor, timeline: Timeline):
    run_script([{"probe": "x", "delay_after": "3s"}])
    assert [c[0] for c in probe.calls] == ["x"]
    assert timeline.sleeps() == [3]


def test_plugin_delay_before(probe: ProbeExecutor, timeline: Timeline):
    run_script([{"probe": "x", "delay_before": "500ms"}])
    assert [c[0] for c in probe.calls] == ["x"]
    assert timeline.sleeps() == [0.5]


def test_plugin_delay_after_real_sleep(probe: ProbeExecutor):
    start = time.monotonic()
    run_script([{"probe": "x", "delay_after": "1s"}])
    assert time.monotonic() - start >= 1


def test_plugin_timeout(probe: ProbeExecutor, timeline: Timeline):
    run_script([{"probe": "x", "timeout": "7s"}])
    assert probe.calls == [("x", 7)]


@pytest.mark.parametrize(("when", "ran"), [("false", False), ("yes", True)])
def test_plugin_when(probe: ProbeExecutor, timeline: Timeline, when: str, ran: bool):
    run_script([{"probe": "x", "when": when, "delay_after": "1s"}])
    assert bool(probe.calls) is ran
    assert timeline.sleeps() == ([1] if ran else [])


def test_plugin_after(probe: ProbeExecutor, timeline: Timeline):
    run_script([{"line": "echo mark-ready"}, {"probe": "x", "after": "mark-ready\\r\\n"}])
    assert [c[0] for c in probe.calls] == ["x"]
