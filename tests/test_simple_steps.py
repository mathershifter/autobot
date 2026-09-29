"""P8-06..14: line, return, control, sleep and call steps (SPEC.md:55-66, 174-270)."""

from __future__ import annotations

import time
from collections.abc import Callable

import pytest
from conftest import (
    FakeDevice,
    SentLog,
    SpawnLog,
    SpawnRecorded,
    Timeline,
    make_runner,
    run_script,
    run_vars,
    steps,
)

from autobot.runner import Runner


def test_p8_06_control_sends_in_order(sent: SentLog):
    """SPEC.md:265-270: control characters are sent in list order."""
    run_script([{"control": ["a", "x"]}])
    assert sent.controls() == ["a", "x"]


def test_p8_07_control_bracket_is_ctrl_bracket(fake_device: FakeDevice):
    """SPEC.md:268: control "]" sends Ctrl+] (0x1d)."""
    spawn, log = fake_device("--wait-enter", "--rawdump", "1", "--order", "none", "--then", "prompt")
    run_script(
        [
            {"control": "]", "after": "RAW> "},
            # wait until the device has logged the byte before closing it
            {"return": 1, "after": "RAW=\\w+"},
        ],
        spawn=spawn,
        attach_script=[{"return": 1}],  # the Enter --wait-enter blocks on
    )
    assert "RAW=1d" in FakeDevice.read(log)


def test_p8_08_return_sends_n_newlines(sent: SentLog):
    """SPEC.md:258-263: return: N sends N empty lines."""
    run_script([{"return": 3}], attach_script=[])
    assert sent.lines() == ["", "", ""]


def test_p8_09_line_list_sends_each_without_waiting(timeline: Timeline):
    """SPEC.md:252-256: line sends raw, without waiting for a prompt."""
    run_script([{"line": ["echo a", "echo b"]}], attach_script=[])
    assert timeline.since("attach") == [("sendline", "echo a"), ("sendline", "echo b")]


def test_p8_10_line_and_after_are_rendered(sent: SentLog, timeline: Timeline):
    """README "Templating": line and after are templates (see decision #9)."""
    run_script(
        [
            {"line": "echo {{ vars.x }}"},
            {"line": "printf 'MA%s\\n' RK"},
            {"line": "echo done", "after": "{{ vars.pat }}"},
        ],
        vars={"x": "hi", "pat": "MARK"},
    )
    assert "echo hi" in sent.lines()
    assert ("expect", ["MARK"]) in timeline


def test_p8_11_sleep_step(timeline: Timeline):
    """SPEC.md:174-178: sleep pauses for the duration."""
    run_script([{"sleep": "2s"}])
    assert timeline.sleeps() == [2.0]


def test_p8_11_sleep_step_real(attached_runner: Callable[..., Runner]):
    """SPEC.md:174-178: a real 500ms sleep."""
    r = attached_runner()
    start = time.monotonic()
    r.run_steps(steps([{"sleep": "500ms"}]))
    assert 0.5 <= time.monotonic() - start < 2


def test_p8_12_call_runs_function_steps():
    """SPEC.md:55-66, 180-184: call runs the function's steps."""
    fn = {
        "f": {
            "script": [
                {"cmd": "echo a", "register": "a", "timeout": "5s"},
                {"cmd": "echo b", "register": "b", "timeout": "5s"},
            ]
        }
    }
    out = run_vars([{"call": "f"}], fn=fn)
    assert out["a"] == "a"
    assert out["b"] == "b"


def test_p8_13_call_steps_see_registered_vars():
    """SPEC.md:322: function steps see vars registered earlier."""
    fn = {"f": {"script": [{"cmd": "echo got-{{ vars.v }}", "register": "g", "timeout": "5s"}]}}
    out = run_vars([{"cmd": "echo v", "register": "v"}, {"call": "f"}], fn=fn)
    assert out["g"] == "got-v"


def test_p8_14_attach_env_empty_dict_is_not_omitted(spawned: SpawnLog):
    """SPEC.md:75: an explicit empty env replaces the environment with nothing."""
    spawned.stop = True
    with pytest.raises(SpawnRecorded):
        make_runner([], attach_env={}).run()
    _, kwargs = spawned[0]
    assert kwargs["env"] == {}
