from __future__ import annotations

import os
from typing import Any

import pexpect
import pytest

from autobot.models import Config
from autobot.runner import Runner
from autobot.session import Session

SHELL = "bash --norc --noprofile -i"
ENV = {"PS1": "PROMPT$ ", "TERM": "dumb", "NO_COLOR": "1", "PATH": os.environ["PATH"]}
TOP_PROMPTS = [{"name": "top", "expect": [r"PROMPT\$ "], "return": True}]
BLOCK_PROMPTS = [{"name": "blk", "expect": [r"PROMPT\$ "], "return": True}]
STUCK = {"cmd": "true", "after": "NEVER_APPEARS", "timeout": 1}


def make_runner(script: list, breakout: list | None = None, **attach: Any) -> Runner:
    cfg: dict[str, Any] = {
        "autobot": "2026-08",
        "prompts": TOP_PROMPTS,
        "attach": {"spawn": SHELL, "env": ENV, "timeout": 5, **attach},
        "script": script,
    }
    if breakout is not None:
        cfg["attach"]["breakout"] = {"script": breakout}
    return Runner(Config.model_validate(cfg), {})


@pytest.fixture
def children(monkeypatch) -> list[pexpect.spawn]:
    """Record every child the session detaches, so tests can check it is dead."""
    seen: list[pexpect.spawn] = []
    orig = Session.detach

    def detach(self):
        if self._cld is not None:
            seen.append(self._cld)
        orig(self)

    monkeypatch.setattr(Session, "detach", detach)
    return seen


def handler_names(runner: Runner) -> list[str]:
    return [h.name for h in runner.session.save_handlers()]


def attached(runner: Runner) -> Runner:
    runner.session.attach(SHELL, env=ENV, timeout=5)
    return runner


def _steps(steps: list) -> list:
    return Config.model_validate(
        {"autobot": "2026-08", "attach": {"spawn": SHELL}, "script": steps}
    ).script


def block(**kw: Any) -> dict:
    return {"block": {"name": "b", "prompts": BLOCK_PROMPTS, **kw}}


# -- Session translates pexpect exceptions ----------------------------------


def test_expect_timeout_is_builtin_timeout():
    s = Session([])
    s.attach(SHELL, env=ENV, timeout=5)
    try:
        with pytest.raises(TimeoutError) as ei:
            s.expect(["NEVER_APPEARS"], timeout=0.5)
        assert isinstance(ei.value.__cause__, pexpect.TIMEOUT)
    finally:
        s.detach()


def test_sleep_and_check_rc_eof_is_builtin_eof():
    s = Session([])
    s.attach(SHELL, env=ENV, timeout=5)
    try:
        s.sendline("exit")
        with pytest.raises(EOFError) as ei:
            s.sleep(5)
        assert isinstance(ei.value.__cause__, pexpect.EOF)
        with pytest.raises((EOFError, OSError)):
            s.check_rc(timeout=1)
    finally:
        s.detach()


# -- block lifecycle --------------------------------------------------------


def test_block_breakout_after_timeout_restores_handlers(capsys):
    r = attached(make_runner([]))
    try:
        r.run_steps(_steps([block(script=[{"cmd": "true"}], breakout={"script": [STUCK]})]))
        assert handler_names(r) == ["top"]
        # session still usable with the restored top-level prompts
        r.run_steps(_steps([{"cmd": "true"}]))
    finally:
        r.session.detach()
    assert "block breakout error (TimeoutError)" in capsys.readouterr().err


def test_block_failing_enter_runs_breakout_and_restores(capsys):
    r = attached(make_runner([]))
    try:
        with pytest.raises(RuntimeError, match="exit code 1"):
            r.run_steps(
                _steps([block(
                    enter=[{"cmd": "false"}],
                    script=[{"cmd": "echo SHOULD_NOT_RUN"}],
                    breakout={"script": [{"cmd": "echo BREAKOUT_RAN"}]},
                )])
            )
        assert handler_names(r) == ["top"]
    finally:
        r.session.detach()
    out = capsys.readouterr()
    assert ">> block breakout: b" in out.err
    assert "cmd: echo BREAKOUT_RAN" in out.err
    assert "SHOULD_NOT_RUN" not in out.err


def test_block_original_error_preserved_when_breakout_fails():
    r = attached(make_runner([]))
    try:
        with pytest.raises(RuntimeError, match="exit code 1"):
            r.run_steps(_steps([block(script=[{"cmd": "false"}], breakout={"script": [STUCK]})]))
        assert handler_names(r) == ["top"]
    finally:
        r.session.detach()


def test_block_breakout_template_error_is_best_effort(capsys):
    r = attached(make_runner([]))
    try:
        r.run_steps(
            _steps([block(script=[{"cmd": "true"}], breakout={"script": [{"line": "{{ oops("}]})])
        )
        assert handler_names(r) == ["top"]
    finally:
        r.session.detach()
    assert "block breakout error (TemplateSyntaxError)" in capsys.readouterr().err


# -- attach lifecycle -------------------------------------------------------


def test_attach_breakout_failure_still_detaches(children, capsys):
    r = make_runner([{"cmd": "true"}], breakout=[STUCK])
    r.run()
    assert len(children) == 1
    assert not children[0].isalive()
    assert r.session._cld is None
    assert "breakout error (TimeoutError)" in capsys.readouterr().err


def test_attach_original_error_preserved_when_breakout_fails(children):
    r = make_runner([{"cmd": "false"}], breakout=[STUCK])
    with pytest.raises(RuntimeError, match="exit code 1"):
        r.run()
    assert len(children) == 1
    assert not children[0].isalive()


def test_attach_initial_timeout_closes_child(children):
    r = make_runner([{"cmd": "true"}], spawn="sleep 30", timeout=1)
    with pytest.raises(TimeoutError):
        r.run()
    assert len(children) == 1
    assert not children[0].isalive()
