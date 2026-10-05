"""Attach and block lifecycles (SPEC.md:68-85, 186-204).

Tests prefixed ``test_p5_NN_`` map to test plan rows P5-NN.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pexpect
import pytest
from conftest import (
    BASH,
    RC_PROBE,
    SHELL_ENV,
    AttachRecorded,
    FakeDevice,
    ProbeExecutor,
    SentLog,
    Timeline,
    handler_names,
    make_doc,
    make_runner,
    run_cli,
    run_script,
    steps,
)

from autobot.runner import Runner
from autobot.session import PromptHandler, Session

TOP_PROMPTS = [{"name": "top", "expect": [r"PROMPT\$ "], "return": True}]
BLOCK_PROMPTS = [{"name": "blk", "expect": [r"PROMPT\$ "], "return": True}]
STUCK = {"cmd": "true", "after": "NEVER_APPEARS", "timeout": 1}


def top_runner(script: list, breakout: list | None = None, **kw: Any) -> Runner:
    return make_runner(script, prompts=TOP_PROMPTS, breakout=breakout, **kw)


@pytest.fixture
def attached(attached_runner: Callable[..., Runner]) -> Callable[[], Runner]:
    return lambda: attached_runner(prompts=TOP_PROMPTS)


def block(**kw: Any) -> dict:
    return {"block": {"name": "b", "prompts": BLOCK_PROMPTS, **kw}}


# -- Session translates pexpect exceptions ----------------------------------


def test_expect_timeout_is_builtin_timeout():
    s = Session([])
    s.attach(BASH, env=SHELL_ENV, timeout=5)
    try:
        with pytest.raises(TimeoutError) as ei:
            s.expect(["NEVER_APPEARS"], timeout=0.5)
        assert isinstance(ei.value.__cause__, pexpect.TIMEOUT)
    finally:
        s.detach()


def test_p5_46_attach_timeout_message_names_spawn():
    """SPEC attach "If the spawn wait fails": the message names the spawn command, not the internal pattern."""
    s = Session([])
    with pytest.raises(TimeoutError) as ei:
        s.attach("sleep 30", env=SHELL_ENV, timeout=0.5)
    assert str(ei.value) == "timed out after 0.5s waiting for the first output from 'sleep 30' (attach.timeout)"
    assert isinstance(ei.value.__cause__, pexpect.TIMEOUT)


def test_p5_47_check_rc_timeout_message(shell_session: Session):
    """SPEC "cmd" ignore_error: the `$?` wait times out with a message naming the exit code check."""
    s = shell_session
    s.get_prompt(timeout=5)
    s.sendline("sleep 30")
    with pytest.raises(TimeoutError, match=r"^timed out after 1s waiting for the exit code of the command \(echo \$\?\)$"):
        s.check_rc(timeout=1)


@pytest.mark.parametrize(
    ("spawn", "status"),
    [
        ("true", " (exit status 0)"),
        ("false", " (exit status 1)"),
        ("sh -c 'exit 255'", " (exit status 255)"),
        ("sh -c 'kill -KILL $$'", " (killed by SIGKILL)"),
        ("sh -c 'kill -HUP $$'", ""),  # closing the pty sends SIGHUP too, so it isn't reported
    ],
    ids=["true", "false", "exit-255", "sigkill", "sighup"],
)
def test_p5_49_attach_eof_message(spawn: str, status: str):
    """SPEC attach "If the spawn wait fails": the message names the spawn command and how the process ended."""
    s = Session([])
    with pytest.raises(EOFError) as ei:
        s.attach(spawn, env=SHELL_ENV, timeout=5)
    assert str(ei.value) == f"connection closed before any output from '{spawn}'{status}"
    assert isinstance(ei.value.__cause__, pexpect.EOF)
    assert s._cld is None


def test_p5_50_check_rc_eof_message(shell_session: Session):
    """SPEC "cmd" ignore_error: the connection closing during the `$?` wait names the exit code check."""
    s = shell_session
    s.get_prompt(timeout=5)
    s.sendline("sleep 0.5; exit")  # the shell is still running when the `$?` check is sent
    with pytest.raises(EOFError) as ei:
        s.check_rc(timeout=5)
    assert str(ei.value) == "connection closed while waiting for the exit code of the command (echo $?)"


def test_p5_51_sleep_eof_message():
    """SPEC "sleep": the connection closing during a sleep raises EOFError, not a silent wait."""
    with pytest.raises(EOFError, match=r"^connection closed while waiting for the end of a sleep$"):
        run_script([{"line": "exit"}, {"sleep": "5s"}])


@pytest.mark.parametrize(
    ("what", "message"),
    [
        (None, "waiting for 'NEVER' or 'NOPE'"),
        ("the after pattern 'NEVER'", "waiting for the after pattern 'NEVER'"),
    ],
    ids=["patterns", "what"],
)
def test_p5_48_expect_timeout_message(shell_session: Session, what: str | None, message: str):
    """Session.expect quotes its patterns as written (no list repr), or uses the caller's description."""
    with pytest.raises(TimeoutError) as ei:
        shell_session.expect(["NEVER", "NOPE"] if what is None else ["NEVER"], timeout=0.5, what=what)
    assert str(ei.value) == f"timed out after 0.5s {message}"


def test_sleep_and_check_rc_eof_is_builtin_eof():
    s = Session([])
    s.attach(BASH, env=SHELL_ENV, timeout=5)
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


def test_block_breakout_after_timeout_restores_handlers(attached, capsys):
    r = attached()
    try:
        r.run_steps(steps([block(script=[{"cmd": "true"}], breakout={"script": [STUCK]})]))
        assert handler_names(r) == ["top"]
        # session still usable with the restored top-level prompts
        r.run_steps(steps([{"cmd": "true"}]))
    finally:
        r.session.detach()
    assert "block breakout error (TimeoutError)" in capsys.readouterr().err


def test_block_failing_enter_runs_breakout_and_restores(attached, capsys):
    r = attached()
    try:
        with pytest.raises(RuntimeError, match="exit code 1"):
            r.run_steps(
                steps([block(
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


def test_block_original_error_preserved_when_breakout_fails(attached):
    r = attached()
    try:
        with pytest.raises(RuntimeError, match="exit code 1"):
            r.run_steps(steps([block(script=[{"cmd": "false"}], breakout={"script": [STUCK]})]))
        assert handler_names(r) == ["top"]
    finally:
        r.session.detach()


def test_block_breakout_template_error_is_best_effort(attached, capsys):
    r = attached()
    try:
        r.run_steps(
            steps([block(script=[{"cmd": "true"}], breakout={"script": [{"line": "{{ oops("}]})])
        )
        assert handler_names(r) == ["top"]
    finally:
        r.session.detach()
    assert "block breakout error (ValueError): template error: " in capsys.readouterr().err


# -- attach lifecycle -------------------------------------------------------


def test_attach_breakout_failure_still_detaches(children, capsys):
    r = top_runner([{"cmd": "true"}], breakout=[STUCK])
    r.run()
    assert len(children) == 1
    assert not children[0].isalive()
    assert r.session._cld is None
    assert "breakout error (TimeoutError)" in capsys.readouterr().err


def test_attach_original_error_preserved_when_breakout_fails(children):
    r = top_runner([{"cmd": "false"}], breakout=[STUCK])
    with pytest.raises(RuntimeError, match="exit code 1"):
        r.run()
    assert len(children) == 1
    assert not children[0].isalive()


# -- P5-56: a close that fails (attach lifecycle step 6) -----------------------

CLOSE_FAILED = "Could not terminate the child."
CLOSE_LOG = f">> close error (ExceptionPexpect): {CLOSE_FAILED}"


@pytest.fixture
def unkillable(monkeypatch: pytest.MonkeyPatch) -> list[pexpect.spawn]:
    """`pexpect.spawn.close` raises what it raises for a child that survives SIGHUP, SIGINT and SIGKILL.

    The child is really closed first, so the test leaves nothing behind.
    """
    closed: list[pexpect.spawn] = []
    orig = pexpect.spawn.close

    def close(self, force=True):
        orig(self, force)
        closed.append(self)
        raise pexpect.ExceptionPexpect(CLOSE_FAILED)

    monkeypatch.setattr(pexpect.spawn, "close", close)
    return closed


def detached(r: Runner) -> bool:
    return r.session._cld is None and r.session._echo is None


@pytest.mark.parametrize(
    ("script", "breakout", "error", "message"),
    [
        ([{"cmd": "false"}], None, RuntimeError, r"^command returned exit code 1$"),
        ([STUCK], None, TimeoutError, r"^timed out after 1(\.0)?s waiting for the after pattern 'NEVER_APPEARS'$"),
        ([{"cmd": "echo {{ vars.nope }}"}], None, ValueError, r"^template error: "),
        ([{"cmd": "false"}], [STUCK], RuntimeError, r"^command returned exit code 1$"),
    ],
    ids=["step-failure", "timeout", "template-error", "with-failing-breakout"],
)
def test_p5_56_close_failure_does_not_replace_the_script_error(
    unkillable: list, capsys, script: list, breakout: list | None, error: type[Exception], message: str
):
    """SPEC "attach": a session that can't be closed never replaces the error the script raised.

    `detach` ran in a `finally`, so pexpect's `Could not terminate the child.` took the place of the
    script's own error, which was left only as its `__context__`.
    """
    r = top_runner(script, breakout=breakout)
    with pytest.raises(error, match=message):
        r.run()
    assert len(unkillable) == 1
    assert detached(r)
    assert CLOSE_LOG in capsys.readouterr().err


@pytest.mark.parametrize(
    ("spawn", "error", "message"),
    [
        ("sleep 30", TimeoutError, r"^timed out after 0\.5s waiting for the first output from 'sleep 30' \(attach\.timeout\)$"),
        ("true", EOFError, r"^connection closed before any output from 'true' \(exit status 0\)$"),
    ],
    ids=["timeout", "eof"],
)
def test_p5_56_close_failure_does_not_replace_a_spawn_wait_error(
    unkillable: list, capsys, spawn: str, error: type[Exception], message: str
):
    """The same for the close that follows a failed spawn wait, inside `Session.attach`."""
    r = top_runner([], spawn=spawn, timeout=0.5)
    with pytest.raises(error, match=message):
        r.run()
    assert len(unkillable) == 1  # closed once: the runner's own detach finds nothing left
    assert detached(r)
    assert CLOSE_LOG in capsys.readouterr().err


def test_p5_56_close_failure_alone_is_the_runs_error(unkillable: list, capsys):
    """With no other error, the close failure is raised, not swallowed, and the session still forgets the child."""
    r = top_runner([{"cmd": "true"}], breakout=[{"line": "exit"}])
    with pytest.raises(pexpect.ExceptionPexpect, match=f"^{CLOSE_FAILED}$"):
        r.run()
    assert len(unkillable) == 1
    assert detached(r)
    assert ">> close error" not in capsys.readouterr().err
    r.session.detach()  # nothing left to close: no second attempt, no error
    assert len(unkillable) == 1


def test_p5_56_detach_forgets_the_child_when_close_fails(unkillable: list, capsys):
    """`Session.detach`: the state is cleared whether or not the close fails, and `failing` only logs it."""
    for failing in (False, True):
        s = Session([])
        s.attach(BASH, env=SHELL_ENV, timeout=5)
        if failing:
            s.detach(failing=True)
        else:
            with pytest.raises(pexpect.ExceptionPexpect, match=f"^{CLOSE_FAILED}$"):
                s.detach()
        assert s._cld is None and s._echo is None
        assert (CLOSE_LOG in capsys.readouterr().err) is failing
        with pytest.raises(RuntimeError, match="^not attached$"):
            s.sendline("x")
    assert len(unkillable) == 2


def test_p5_56_interrupt_during_close_is_not_swallowed(monkeypatch: pytest.MonkeyPatch):
    """A `KeyboardInterrupt` while closing propagates even when another error is in flight."""
    orig = pexpect.spawn.close

    def close(self, force=True):
        orig(self, force)
        raise KeyboardInterrupt

    monkeypatch.setattr(pexpect.spawn, "close", close)
    r = top_runner([{"cmd": "false"}])
    with pytest.raises(KeyboardInterrupt):
        r.run()
    assert detached(r)


def test_p5_56_echo_is_flushed_when_close_fails(unkillable: list, capsys):
    """What the operator echo still holds is written out even though the close fails."""
    s = Session([])
    s.attach(BASH, env=SHELL_ENV, timeout=5)
    assert s._echo is not None
    s._echo.write("tail\x1b[")
    s.detach(failing=True)
    assert capsys.readouterr().out.endswith("tail\x1b[")


# -- P5-58: nothing can be done with a detached session -------------------------


def test_p5_58_get_prompt_after_detach_is_not_attached():
    """A session that was at a shell prompt when it was detached isn't at one any more.

    `get_prompt` returned `""` at once for it, as if a prompt were waiting, instead of raising `not attached`.
    """
    s = Session([PromptHandler("sh", [r"PROMPT\$ "], [], True)])
    s.attach(BASH, env=SHELL_ENV, timeout=5)
    s.get_prompt(timeout=5)
    assert s.get_prompt(timeout=5) == ""  # at the prompt: returns at once
    s.detach()
    assert s._at_prompt is False
    with pytest.raises(RuntimeError, match="^not attached$"):
        s.get_prompt(timeout=5)
    with pytest.raises(RuntimeError, match="^not attached$"):
        s.check_rc(timeout=5)


def test_p5_58_session_that_never_attached_is_not_attached():
    """A session that was never attached raises the same way from every wait and send."""
    s = Session([PromptHandler("sh", [r"PROMPT\$ "], [], True)])
    for use in (s.get_prompt, s.check_rc, lambda: s.sendline("x"), lambda: s.sendcontrol("c"), lambda: s.expect(["x"])):
        with pytest.raises(RuntimeError, match="^not attached$"):
            use()


# a child whose first output is a line that reads like the echo of a command, then a line, then a shell
ECHO_LIKE = "bash -c \"echo 'echo stale-cmd'; echo real; PS1='PROMPT$ ' exec bash --norc --noprofile -i\""


def test_p5_59_reattached_session_strips_no_stale_echo():
    """SPEC "Captured output": the echo that is removed is the echo of a line sent to this child.

    A line sent to a child that was detached before its prompt stayed remembered: the first wait on the
    next child took that child's own output `echo stale-cmd` for the echo and removed it.
    """
    s = Session([PromptHandler("sh", [r"PROMPT\$ "], [], True)])
    try:
        s.attach(BASH, env=SHELL_ENV, timeout=5)
        s.get_prompt(timeout=5)
        s.sendline("echo stale-cmd")
        s.detach()  # without waiting for the command's prompt
        s.attach(ECHO_LIKE, env=SHELL_ENV, timeout=5)
        assert s.ctx == {"before": "", "match": ""}  # nothing of the first child's output
        assert s.get_prompt(timeout=5) == "echo stale-cmd\nreal\n"
        assert s.ctx["before"] == "echo stale-cmd\nreal\n"
    finally:
        s.detach()


@pytest.mark.slow
def test_p5_59_first_wait_of_a_reattached_session_solicits(fake_device: FakeDevice, sent: SentLog):
    """SPEC "Prompt Handling": the first wait after a spawn follows no command, so it presses Return for an idle console.

    After a command sent to the child before, the session still counted the wait as following that command.
    """
    s = Session([PromptHandler("sh", [r"PROMPT\$ "], [], True)])
    try:
        s.attach(BASH, env=SHELL_ENV, timeout=5)
        s.get_prompt(timeout=5)
        s.sendline("echo stale-cmd")
        s.detach()
        sent.clear()
        spawn, _ = fake_device("--wait-enter", "--order", "none", "--then", "prompt")
        s.attach(spawn, env=dict(SHELL_ENV), timeout=5)
        s.get_prompt(timeout=15)
        assert sent.lines() == [""]
    finally:
        s.detach()


def test_p5_58_reattached_session_waits_for_its_own_prompt(sent: SentLog):
    """A session attached again starts away from a prompt: its first wait reads the new child's prompt."""
    s = Session([PromptHandler("sh", [r"PROMPT\$ "], [], True)])
    try:
        for word in ("one", "two"):
            s.attach(BASH, env=SHELL_ENV, timeout=5)
            s.get_prompt(timeout=5)
            s.sendline(f"echo {word}")
            assert s.get_prompt(timeout=5) == f"{word}\n"
            s.detach()
    finally:
        s.detach()
    assert sent.lines() == ["echo one", "echo two"]


# -- P5: spawn failure (attach lifecycle step 2) ------------------------------

# case -> (spawn, error, message); the spawn wait fails before any output
SPAWN_FAILURES: dict[str, tuple[str, type[BaseException], str]] = {
    "timeout": (
        "sleep 30",
        TimeoutError,
        r"^timed out after 1(\.0)?s waiting for the first output from 'sleep 30' \(attach\.timeout\)$",
    ),
    "exits": ("true", EOFError, r"^connection closed before any output from 'true' \(exit status 0\)$"),
    "not-found": (
        "autobot_no_such_cmd",
        pexpect.ExceptionPexpect,
        r"^The command was not found or was not executable: autobot_no_such_cmd",
    ),
}


def spawn_runner(tmp_path: Path, spawn: str) -> tuple[Runner, Path]:
    """Every phase appends a tag to ``log``; the breakout would also send ``exit``."""
    log = tmp_path / "log"
    runner = make_runner(
        [{"cmd": f"echo main >> {log}"}],
        spawn=spawn,
        timeout=1,
        prepare=f"#!/bin/sh\necho prepare >> {log}\n",
        attach_script=[{"cmd": f"echo attach >> {log}", "timeout": "2s"}],
        breakout=[{"line": "exit"}, {"cmd": f"echo breakout >> {log}", "timeout": "2s"}],
    )
    return runner, log


def progress(err: str) -> list[str]:
    return [line for line in err.splitlines() if line.startswith(">> ")]


@pytest.mark.parametrize("case", SPAWN_FAILURES)
def test_p5_32_spawn_failure_skips_scripts_and_breakout(
    case: str, tmp_path: Path, children, sent: SentLog, capsys
):
    """SPEC attach "If the spawn wait fails": prepare ran; no script, no breakout; closed."""
    spawn, error, message = SPAWN_FAILURES[case]
    r, log = spawn_runner(tmp_path, spawn)
    with pytest.raises(error, match=message):
        r.run()
    assert log.read_text().split() == ["prepare"]
    assert list(sent) == []  # nothing reached the process, breakout included
    assert r.session._cld is None
    if case == "not-found":
        assert children == []  # pexpect refuses before it forks
    else:
        assert len(children) == 1
        assert not children[0].isalive()
        assert children[0].closed  # pty released
    assert progress(capsys.readouterr().err) == [
        ">> prepare: running local script",
        ">> prepare: done",
        f">> attach: {spawn}",
    ]


def test_p5_33_banner_then_exit_runs_breakout(
    tmp_path: Path, fake_device: FakeDevice, children, capsys
):
    """SPEC attach: output ends the spawn wait; the first wait then hits EOF and breakout runs."""
    spawn, _ = fake_device("--exit-after-banner")
    r, log = spawn_runner(tmp_path, spawn)
    with pytest.raises(EOFError, match=r"^connection closed while waiting for a shell prompt \('sh'\)$"):
        r.run()
    assert log.read_text().split() == ["prepare"]
    assert len(children) == 1
    assert not children[0].isalive()
    assert children[0].closed
    assert r.session._cld is None
    assert progress(capsys.readouterr().err) == [
        ">> prepare: running local script",
        ">> prepare: done",
        f">> attach: {spawn}",
        ">> breakout: detaching",
        ">> breakout error (EOFError): connection closed while waiting for a shell prompt ('sh')",
    ]


def _pids(cmdline: str) -> list[str]:
    return subprocess.run(["pgrep", "-x", "-f", cmdline], capture_output=True, text=True, check=False).stdout.split()


@pytest.mark.parametrize("case", [*SPAWN_FAILURES, "banner"])
def test_p5_34_spawn_failure_cli_exit_status(case: str, tmp_path: Path, fake_device: FakeDevice):
    """SPEC CLI: a spawn failure is a run-time error: traceback, status 1, no child left."""
    if case == "banner":
        spawn, _ = fake_device("--exit-after-banner")
        error = "EOFError"
    else:
        spawn, exc, _ = SPAWN_FAILURES[case]
        error = f"{exc.__module__}.{exc.__qualname__}" if exc.__module__ != "builtins" else exc.__name__
    if case == "timeout":
        spawn = f"sleep 9{os.getpid()}"  # unique, so a leaked child can be found
    log = tmp_path / "log"
    doc = make_doc(
        [{"cmd": f"echo main >> {log}"}],
        spawn=spawn,
        timeout=1,
        prepare=f"#!/bin/sh\necho prepare >> {log}\n",
        breakout=[{"cmd": f"echo breakout >> {log}", "timeout": "2s"}],
    )
    try:
        res = run_cli(doc, tmp_path)
        leaked = _pids(spawn) if case == "timeout" else []
    finally:
        for pid in _pids(spawn) if case == "timeout" else []:
            os.kill(int(pid), signal.SIGKILL)
    assert res.returncode == 1, res.stderr
    assert "Traceback" in res.stderr
    assert res.stderr.rstrip().splitlines()[-1].startswith(f"{error}: ")
    assert (">> breakout: detaching" in res.stderr) == (case == "banner")
    assert log.read_text().split() == ["prepare"]
    assert leaked == []


# -- P5: attach lifecycle (SPEC.md:68-85) -----------------------------------


def test_p5_01_attach_lifecycle_order(
    tmp_path: Path, timeline: Timeline, children, monkeypatch: pytest.MonkeyPatch
):
    """SPEC.md:79-85: prepare -> spawn -> attach.script -> script -> breakout -> close."""
    log = tmp_path / "log"
    at_attach: list[str] = []
    recorded_attach = Session.attach

    def attach(self, *a, **k):
        at_attach.append(log.read_text() if log.exists() else "")
        return recorded_attach(self, *a, **k)

    monkeypatch.setattr(Session, "attach", attach)
    run_script(
        [{"cmd": f"echo main >> {log}"}],
        prepare=f"#!/bin/sh\necho prepare >> {log}\n",
        attach_script=[{"cmd": f"echo attach >> {log}", "timeout": "5s"}],
        breakout=[{"cmd": f"echo breakout >> {log}", "timeout": "5s"}],
    )
    assert log.read_text().split() == ["prepare", "attach", "main", "breakout"]
    assert at_attach == ["prepare\n"]
    assert timeline.names().count("attach") == 1
    assert len(children) == 1
    assert not children[0].isalive()


def test_p5_02_prepare_nonzero_aborts_before_spawn(timeline: Timeline, children):
    """SPEC.md:72, 80: prepare failing aborts before spawn."""
    r = make_runner([{"cmd": "true"}], prepare="#!/bin/sh\nexit 3\n")
    with pytest.raises(RuntimeError, match="prepare script failed with exit code 3"):
        r.run()
    assert "attach" not in timeline.names()
    assert children == []


@pytest.mark.parametrize("rc", [0, 3], ids=["success", "failure"])
def test_p5_03_prepare_temp_file_removed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rc: int):
    """SPEC.md:72: the local prepare script leaves no temp file behind."""
    import tempfile

    tdir = tmp_path / "tmp"
    tdir.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tdir))
    r = make_runner([], prepare=f"#!/bin/sh\nexit {rc}\n")
    if rc:
        with pytest.raises(RuntimeError):
            r.run()
    else:
        r.run()
    assert list(tdir.glob("_autobot_*")) == []


@pytest.fixture
def prep_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Temp dir for the prepare script, so a leftover ``_autobot_*`` file is seen."""
    import tempfile

    tdir = tmp_path / "tmp"
    tdir.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tdir))
    return tdir


@pytest.mark.parametrize("text", ["\ud800", "\udcff"], ids=["lone-surrogate", "non-utf8-arg"])
def test_p5_55_prepare_temp_file_removed_when_the_write_fails(
    timeline: Timeline, prep_tmp: Path, capsys: pytest.CaptureFixture[str], text: str
):
    """SPEC "attach": the temp file is removed in every case, also when the script can't be written.

    A byte of an `--arg` that isn't UTF-8 reaches the script as a lone surrogate (`\\udcff`),
    which UTF-8 can't encode; the file used to be left behind.
    """
    r = make_runner([], prepare="#!/bin/sh\necho {{ args.x }}\n", args={"x": text})
    with pytest.raises(UnicodeEncodeError):
        r.run()
    assert list(prep_tmp.iterdir()) == []
    assert "attach" not in timeline.names()
    assert ">> prepare: done" not in capsys.readouterr().err


def test_p5_55_run_prepare_removes_the_file_when_the_write_fails(prep_tmp: Path):
    """The audit's reproduction: `Runner._run_prepare("echo \\ud800")` left `_autobot_*.sh` in the temp dir."""
    with pytest.raises(UnicodeEncodeError):
        Runner._run_prepare("echo \ud800")
    assert list(prep_tmp.iterdir()) == []


def test_p5_55_prepare_temp_file_removed_when_chmod_fails(prep_tmp: Path, monkeypatch: pytest.MonkeyPatch):
    """Every later failure removes it too."""

    def chmod(path: Any, mode: int) -> None:
        raise PermissionError(1, "Operation not permitted", str(path))

    monkeypatch.setattr(os, "chmod", chmod)
    with pytest.raises(PermissionError):
        Runner._run_prepare("#!/bin/sh\ntrue\n")
    assert list(prep_tmp.iterdir()) == []


def test_p5_04_prepare_without_shebang_runs_under_sh(
    tmp_path: Path, prep_tmp: Path, timeline: Timeline, capsys
):
    """SPEC attach table: without a shebang, prepare runs under ``/bin/sh``."""
    out = tmp_path / "out"
    make_runner([], prepare=f"\n  echo \"sh:$0\" > {out}\n").run()
    assert out.read_text().startswith(f"sh:{prep_tmp}/_autobot_")
    assert progress(capsys.readouterr().err)[:2] == [
        ">> prepare: running local script (no shebang, using /bin/sh)",
        ">> prepare: done",
    ]
    assert "attach" in timeline.names()
    assert list(prep_tmp.glob("_autobot_*")) == []


JUNK = {
    "blank-lines": "\n\n\r\n",
    "spaces": " \t ",
    "bom": "\ufeff",
    "mixed": "\ufeff\n \u200b\t\u2060\n",
}
PY_ONLY = "import pathlib; pathlib.Path({out!r}).write_text(str(sum(range(4))))\n"


@pytest.mark.parametrize("junk", JUNK.values(), ids=JUNK.keys())
@pytest.mark.parametrize("interp", ["sh", "python3"])
def test_p5_42_prepare_shebang_after_leading_junk(
    junk: str, interp: str, tmp_path: Path, prep_tmp: Path, capsys
):
    """SPEC attach table: leading blank lines, whitespace, a BOM and zero-width characters are ignored."""
    out = tmp_path / "out"
    if interp == "sh":
        body = f"#!/bin/sh\necho sh > {out}\n"
        want = "sh\n"
    else:
        body = "#!/usr/bin/env python3\n" + PY_ONLY.format(out=str(out))
        want = "6"
    make_runner([], prepare=junk + body).run()
    assert out.read_text() == want
    assert ">> prepare: running local script" in progress(capsys.readouterr().err)
    assert list(prep_tmp.glob("_autobot_*")) == []


def test_p5_43_prepare_shebang_crlf(tmp_path: Path, prep_tmp: Path):
    """SPEC attach table: a CR ending the shebang line is dropped (was ENOENT for ``/bin/sh\\r``)."""
    out = tmp_path / "out"
    make_runner([], prepare=f"#!/bin/sh\r\necho ok > {out}\n").run()
    assert out.read_text() == "ok\n"
    assert list(prep_tmp.glob("_autobot_*")) == []


@pytest.mark.parametrize(
    "prepare", ["echo hi\nexit 4\n", "\ufeff\n  #!/bin/sh\nexit 4\n"], ids=["no-shebang", "junk-shebang"]
)
def test_p5_44_prepare_fallback_nonzero_aborts(
    prepare: str, prep_tmp: Path, timeline: Timeline, children
):
    """SPEC attach table: the exit status check is the same with or without a shebang."""
    with pytest.raises(RuntimeError, match="^prepare script failed with exit code 4$"):
        make_runner([{"cmd": "true"}], prepare=prepare).run()
    assert list(prep_tmp.glob("_autobot_*")) == []
    assert "attach" not in timeline.names()
    assert children == []


@pytest.mark.parametrize("case", ["missing", "not-executable"])
def test_p5_45_prepare_interpreter_cannot_run(
    case: str, tmp_path: Path, prep_tmp: Path, timeline: Timeline, children
):
    """SPEC attach table: an interpreter that can't run is a ``RuntimeError`` naming the shebang."""
    if case == "missing":
        interp = tmp_path / "no_such_interp"
        exc, err = FileNotFoundError, "[Errno 2] No such file or directory"
    else:
        interp = tmp_path / "interp"
        interp.write_text("not a program\n")
        interp.chmod(0o600)
        exc, err = PermissionError, "[Errno 13] Permission denied"
    first = f"#!{interp}"
    with pytest.raises(RuntimeError) as ei:
        make_runner([{"cmd": "true"}], prepare=f"\n{first}\r\necho hi\n").run()
    assert str(ei.value) == f"prepare script could not run ({first!r}): {err}"
    assert isinstance(ei.value.__cause__, exc)
    assert list(prep_tmp.glob("_autobot_*")) == []
    assert "attach" not in timeline.names()
    assert children == []


def test_p5_52_prepare_written_as_utf8(
    tmp_path: Path, prep_tmp: Path, monkeypatch: pytest.MonkeyPatch
):
    """SPEC attach table: the prepare script is written as UTF-8 whatever the locale's encoding."""
    import tempfile

    real = tempfile._io

    class Latin1Default:
        """``tempfile``'s ``_io``, but the locale's encoding is Latin-1 (an ISO-8859-1 locale)."""

        def __getattr__(self, name: str) -> Any:
            return getattr(real, name)

        @staticmethod
        def open(file, mode="r", *args, encoding=None, **kw):
            # tempfile passes io.text_encoding(None), which is "locale"
            if "b" not in mode and encoding in (None, "locale"):
                encoding = "latin-1"
            return real.open(file, mode, *args, encoding=encoding, **kw)

    monkeypatch.setattr(tempfile, "_io", Latin1Default())
    out = tmp_path / "out"
    make_runner([], prepare=f"#!/bin/sh\necho '\u00e9 \u2713 \u65e5\u672c' > {out}\n").run()
    assert out.read_bytes() == b"\xc3\xa9 \xe2\x9c\x93 \xe6\x97\xa5\xe6\x9c\xac\n"
    assert list(prep_tmp.glob("_autobot_*")) == []


def test_p5_05_prepare_is_templated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """SPEC attach table and "Jinja2 Templating" (decision #9): prepare is rendered."""
    out = tmp_path / "out"
    monkeypatch.setenv("AUTOBOT_T_X", "from-os")
    run_script(
        [],
        env={"AUTOBOT_T_X": "default", "Y": "{{ env.AUTOBOT_T_X }}-y"},
        vars={"v": "V"},
        args={"a": "A"},
        prepare=(
            "#!/bin/sh\n"
            f"echo '{{{{ env.Y }}}} {{{{ vars.v }}}} {{{{ args.a }}}}' > {out}\n"
        ),
    )
    assert out.read_text() == "from-os-y V A\n"


BASH_ARRAY = '#!/bin/bash\narr=(a b c)\n{body}\n'


def test_p5_05_prepare_bash_array_length_needs_raw(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, timeline: Timeline, children
):
    """SPEC "Jinja2 Templating" (decision #9): ``{#`` in prepare opens a Jinja comment.

    Bare ``${#arr[@]}`` fails to render before anything runs: no prepare
    process, no spawn, no temp file. Wrapped in ``{% raw %}`` it runs.
    """
    import tempfile

    tdir = tmp_path / "tmp"
    tdir.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tdir))
    out = tmp_path / "out"

    bare = BASH_ARRAY.format(body=f'echo "${{#arr[@]}}" > {out}')
    with pytest.raises(ValueError, match="^template error: "):
        make_runner([], prepare=bare).run()
    assert not out.exists()
    assert list(tdir.glob("_autobot_*")) == []
    assert "attach" not in timeline.names()
    assert children == []

    raw = BASH_ARRAY.format(body=f'{{% raw %}}echo "${{#arr[@]}}"{{% endraw %}} > {out}')
    run_script([], prepare=raw)
    assert out.read_text() == "3\n"
    assert list(tdir.glob("_autobot_*")) == []


def test_p5_06_attach_breakout_runs_after_script_failure(tmp_path: Path):
    """SPEC.md:77, 84: breakout runs in finally after the main script fails."""
    bo = tmp_path / "bo"
    with pytest.raises(RuntimeError, match="exit code 1"):
        run_script([{"cmd": "false"}], breakout=[{"cmd": f"touch {bo}", "timeout": "5s"}])
    assert bo.exists()


def test_p5_07_attach_breakout_runs_after_attach_script_failure(tmp_path: Path, sent: SentLog):
    """SPEC.md:76-84: attach.script failure skips the main script, runs breakout."""
    bo = tmp_path / "bo"
    with pytest.raises(RuntimeError, match="exit code 1"):
        run_script(
            [{"cmd": "echo MAIN_STEP"}],
            attach_script=[{"cmd": "false", "timeout": "5s"}],
            breakout=[{"cmd": f"touch {bo}", "timeout": "5s"}],
        )
    assert "echo MAIN_STEP" not in sent.lines()
    assert bo.exists()


def test_p5_08_attach_breakout_resets_handlers_first(timeline: Timeline):
    """SPEC.md:84: handlers are reset before the attach breakout runs."""
    run_script([{"cmd": "echo main"}], breakout=[{"cmd": "echo bo", "timeout": "5s"}])
    main = timeline.index_of(("sendline", "echo main"))
    reset = timeline.index_of(("reset_handlers",), main)
    assert timeline.index_of(("sendline", "echo bo"), main) > reset


def test_p5_09_attach_breakout_error_logged(capsys):
    """SPEC.md:84: breakout is best-effort; errors are logged to stderr."""
    run_script([{"cmd": "true"}], breakout=[{"cmd": "false", "timeout": "5s"}])
    assert "breakout error (StepFailure)" in capsys.readouterr().err


# -- P5: block lifecycle (SPEC.md:186-204) ----------------------------------


def test_p5_10_block_prompts_active_inside_block(probe: ProbeExecutor):
    """SPEC.md:193, 199: block prompts replace top-level prompts inside the block."""
    runner = run_script(
        [
            {
                "block": {
                    "name": "b",
                    "prompts": [{"name": "blk", "expect": [r"BLK\$ "], "return": True}],
                    # quoted so the echoed command itself never matches a prompt
                    "enter": [{"line": "PS1='BL''K$ '"}],
                    "script": [
                        {"probe": "inside"},
                        {"cmd": "echo x", "register": "x", "timeout": "5s"},
                    ],
                    "breakout": {"script": [{"line": "PS1='PROM''PT$ '"}]},
                },
                "timeout": "5s",
            },
            {"probe": "after"},
            {"cmd": "echo y", "register": "y"},
        ]
    )
    assert probe.by_name("inside")["handlers"] == ["blk"]
    assert runner.config.vars["x"] == "x"
    assert probe.by_name("after")["handlers"] == ["sh"]
    assert runner.config.vars["y"] == "y"


@pytest.mark.parametrize("with_breakout", [False, True], ids=["no-breakout", "breakout"])
@pytest.mark.parametrize("phase", ["enter", "script"])
def test_p5_11_block_prompts_restored_on_failure(attached, phase: str, with_breakout: bool):
    """SPEC.md:203, README:304: previous handlers restored on every failure path."""
    blk: dict[str, Any] = {"name": "b", "prompts": BLOCK_PROMPTS}
    fail = [{"cmd": "false", "timeout": "5s"}]
    if phase == "enter":
        blk["enter"] = fail
        blk["script"] = [{"line": "echo SHOULD_NOT_RUN"}]
    else:
        blk["script"] = fail
    if with_breakout:
        blk["breakout"] = {"script": [{"line": "true"}]}
    r = attached()
    with pytest.raises(RuntimeError, match="exit code 1"):
        r.run_steps(steps([{"block": blk}]))
    assert handler_names(r) == ["top"]


@pytest.mark.parametrize("inner_fails", [False, True], ids=["ok", "inner-fails"])
def test_p5_12_nested_block_restore(probe: ProbeExecutor, inner_fails: bool):
    """SPEC.md:193, 203: nested blocks restore their parent's handlers."""
    shell = [r"PROMPT\$ "]
    inner_script: list[dict[str, Any]] = [{"probe": "in_inner"}]
    if inner_fails:
        inner_script.append({"cmd": "false", "timeout": "5s"})
    inner = {
        "block": {
            "name": "inner",
            "prompts": [{"name": "i", "expect": shell, "return": True}],
            "script": inner_script,
        }
    }
    outer = {
        "block": {
            "name": "outer",
            "prompts": [{"name": "o", "expect": shell, "return": True}],
            "script": [inner, {"probe": "after_inner"}],
            "breakout": {"script": [{"probe": "outer_breakout"}]},
        }
    }
    runner = make_runner([outer, {"probe": "top"}])
    if inner_fails:
        with pytest.raises(RuntimeError, match="exit code 1"):
            runner.run()
    else:
        runner.run()
    assert probe.by_name("in_inner")["handlers"] == ["i"]
    assert probe.by_name("outer_breakout")["handlers"] == ["o"]
    assert handler_names(runner) == ["sh"]
    ran = [r["step"].probe for r in probe.records]
    if inner_fails:
        assert ran == ["in_inner", "outer_breakout"]
    else:
        assert ran == ["in_inner", "after_inner", "outer_breakout", "top"]
        assert probe.by_name("after_inner")["handlers"] == ["o"]
        assert probe.by_name("top")["handlers"] == ["sh"]


def test_p5_13_block_without_prompts_leaves_handlers_untouched(
    probe: ProbeExecutor, timeline: Timeline
):
    """SPEC.md:199, 203: without prompts the block never swaps handlers."""
    run_script([{"probe": "top"}, {"block": {"name": "b", "script": [{"probe": "inner"}]}}])
    assert probe.by_name("inner")["handlers_id"] == probe.by_name("top")["handlers_id"]
    assert "restore_handlers" not in [e[0] for e in timeline.since("attach")]


def test_p5_14_block_breakout_resets_handlers_first(timeline: Timeline):
    """SPEC.md:202: handlers are reset before the block breakout runs."""
    run_script(
        [
            {
                "block": {
                    "name": "b",
                    "script": [{"line": "echo s"}],
                    "breakout": {"script": [{"line": "echo bo"}]},
                }
            }
        ]
    )
    assert timeline.has_subsequence(
        [("sendline", "echo s"), ("reset_handlers",), ("sendline", "echo bo")]
    )


def test_p5_15_block_step_order(tmp_path: Path):
    """SPEC.md:198-203: enter -> script -> breakout."""
    log = tmp_path / "log"

    def tag(t: str) -> dict:
        return {"cmd": f"echo {t} >> {log}", "timeout": "5s"}

    run_script(
        [
            {
                "block": {
                    "name": "b",
                    "enter": [tag("enter")],
                    "script": [tag("script")],
                    "breakout": {"script": [tag("breakout")]},
                }
            }
        ]
    )
    assert log.read_text().split() == ["enter", "script", "breakout"]


def test_p5_16_block_breakout_error_does_not_restore_early(attached, capsys):
    """SPEC.md:203, README:304: a failing block breakout is logged; handlers restored."""
    r = attached()
    try:
        r.run_steps(
            steps([block(script=[{"cmd": "true"}], breakout={"script": [{"cmd": "false"}]})])
        )
        assert handler_names(r) == ["top"]
    finally:
        r.session.detach()
    assert "block breakout error (StepFailure)" in capsys.readouterr().err


CREDS = [{"username": "admin", "password": "pw"}]


def _send_each_block(each: str, log: Path) -> dict:
    def tag(t: str) -> dict:
        return {"cmd": f"echo {t} >> {log}"}

    fields = [{"match": "login:", "field": "username"}, {"match": "Password:", "field": "password"}]
    login = {"name": "login", "send": {"each": each, "fields": fields}}
    return {
        "block": {
            "name": "b",
            "prompts": [*BLOCK_PROMPTS, login],
            "enter": [tag("enter")],
            "script": [tag("script")],
            "breakout": {"script": [tag("block-breakout")]},
        }
    }


@pytest.mark.parametrize(
    ("before", "each", "problem"),
    [
        ([], "vars.nope", "no key 'nope' in 'vars'"),
        ([{"cmd": "echo hi", "register": "creds"}], "vars.creds", "'vars.creds' is a string, not a list"),
    ],
    ids=["missing-key", "overwritten-by-register"],
)
def test_p5_25_block_send_each_error_aborts_at_entry(tmp_path: Path, before: list, each: str, problem: str):
    """SPEC sendEach: a block's collection is resolved on entry, with `vars` as registered so far.

    An error stops the run before `enter`; the block's breakout doesn't run, the attach breakout does,
    and the handlers were never swapped.
    """
    log = tmp_path / "log"
    runner = top_runner(
        [*before, _send_each_block(each, log), {"cmd": f"echo after >> {log}"}],
        breakout=[{"cmd": f"echo attach-breakout >> {log}"}],
        vars={"creds": CREDS},
    )
    with pytest.raises(ValueError) as ei:
        runner.run()
    assert str(ei.value) == f"prompt 'login': sendEach '{each}': {problem}"
    assert log.read_text().split() == ["attach-breakout"]
    assert handler_names(runner) == ["top"]


def test_p5_26_block_send_each_valid(tmp_path: Path, probe: ProbeExecutor):
    """SPEC sendEach: a block's resolvable collection loads on entry and the block runs."""
    log = tmp_path / "log"
    blk = _send_each_block("vars.creds", log)
    blk["block"]["script"].append({"probe": "inside"})
    runner = run_script([blk], prompts=TOP_PROMPTS, vars={"creds": CREDS})
    assert log.read_text().split() == ["enter", "script", "block-breakout"]
    assert probe.by_name("inside")["handlers"] == ["blk", "login"]
    assert handler_names(runner) == ["top"]


# -- P5: prompt state across a block swap (SPEC "Prompt state across a swap") --

# a step that took one 5 s idle wait before the fix can't pass these bounds
FAST = 3.0
BLK_ONLY = [{"name": "blk", "expect": [r"BLK\$ "], "return": True}]
# quoted so the echoed commands never match a prompt
TO_BLK = {"line": "PS1='BL''K$ '"}
TO_TOP = {"line": "PS1='PROM''PT$ '"}
ASK = "read -p 'conti''nue? ' a; echo got=$a"
YN = {"name": "yn", "expect": [r"continue\? "], "send": "y"}


def reg(cmd: str, name: str) -> dict:
    return {"cmd": cmd, "register": name, "timeout": "5s"}


def timed(runner: Runner) -> float:
    start = time.monotonic()
    try:
        runner.run()
    finally:
        elapsed = time.monotonic() - start
    return elapsed


# case -> block prompts that recognize the top-level prompt `PROMPT$ `
RECOGNIZING = {
    "same": BLOCK_PROMPTS,
    "broader": [{"name": "blk", "expect": [r"[A-Z]+\$ "], "return": True}],
    "added-question": [*BLOCK_PROMPTS, YN],
}


@pytest.mark.parametrize("case", RECOGNIZING)
def test_p5_35_block_swap_keeps_recognized_prompt(sent: SentLog, case: str):
    """SPEC "Prompt state across a swap": entry and exit at a recognized prompt cost no wait and no newline.

    Before the fix each swap reset the prompt state: about 5 s and one solicit newline on entry and again on exit.
    """
    runner = top_runner([
        reg("echo before", "b"),
        {"block": {"name": "b", "prompts": RECOGNIZING[case], "script": [reg("echo in1", "i1"), reg("echo in2", "i2")]}},
        reg("echo after", "a"),
    ])
    assert timed(runner) < FAST
    assert runner.config.vars == {"b": "before", "i1": "in1", "i2": "in2", "a": "after"}
    assert "" not in sent.lines()
    assert sent.commands() == ["echo before", "echo in1", "echo in2", "echo after"]
    assert runner.session.ctx == {"before": "after\n", "match": "PROMPT$ "}


def test_p5_36_block_question_answered_without_solicit(sent: SentLog):
    """SPEC "Prompt state across a swap": a block that only adds a question handler starts at the prompt."""
    runner = top_runner([
        reg("echo before", "b"),
        {"block": {"name": "b", "prompts": RECOGNIZING["added-question"], "script": [reg(ASK, "ans")]}},
        reg("echo after", "a"),
    ])
    assert timed(runner) < FAST
    assert sent.lines() == ["echo before", RC_PROBE, ASK, "y", RC_PROBE, "echo after", RC_PROBE]
    assert runner.config.vars == {"b": "before", "ans": "y\ngot=y", "a": "after"}


def test_p5_37_block_prompt_change_waits_for_new_prompt(sent: SentLog):
    """SPEC "Prompt state across a swap": a sub-CLI entered with `line` and left in the breakout.

    The swap ends the prompt state (`BLK$ ` patterns don't match `PROMPT$ `), but `line` sends anyway;
    each wait then matches the prompt the shell prints, with no solicit newline.
    """
    runner = top_runner([
        reg("echo before", "b"),
        {"block": {"name": "b", "prompts": BLK_ONLY, "enter": [TO_BLK], "script": [reg("echo in1", "i1")],
                   "breakout": {"script": [TO_TOP]}}},
        reg("echo after", "a"),
    ])
    assert timed(runner) < FAST
    assert "" not in sent.lines()
    assert runner.config.vars == {"b": "before", "i1": "in1", "a": "after"}


def test_p5_38_nested_blocks_keep_recognized_prompt(sent: SentLog, probe: ProbeExecutor):
    """SPEC "Prompt state across a swap": every nested entry and exit is checked; all recognize `PROMPT$ `."""
    inner = {"block": {"name": "inner", "prompts": [{"name": "i", "expect": [r"[A-Z]+\$ "], "return": True}],
                       "script": [{"probe": "in_inner"}, reg("echo i1", "i1")]}}
    outer = {"block": {"name": "outer", "prompts": [{"name": "o", "expect": [r"PROMPT\$ "], "return": True}],
                       "script": [reg("echo o1", "o1"), inner, {"probe": "after_inner"}, reg("echo o2", "o2")]}}
    runner = top_runner([reg("echo before", "b"), outer, reg("echo after", "a")])
    assert timed(runner) < FAST
    assert "" not in sent.lines()
    assert runner.config.vars == {"b": "before", "o1": "o1", "i1": "i1", "o2": "o2", "a": "after"}
    assert probe.by_name("in_inner")["ctx"]["before"] == "o1\n"
    assert probe.by_name("after_inner")["ctx"]["before"] == "i1\n"


def test_p5_39_unrecognized_prompt_not_used_on_entry(sent: SentLog):
    """SPEC "Prompt state across a swap": a `cmd` can't enter a sub-CLI whose prompt the block expects.

    The block's prompts don't match `PROMPT$ `, so the cmd waits; at its 1 s deadline it solicits once and
    times out without sending. Restored, the top-level prompts match the prompt the newline brought.
    """
    enter = [{"cmd": "PS1='BL''K$ '", "timeout": 1}]
    runner = top_runner(
        [reg("echo before", "b"), {"block": {"name": "b", "prompts": BLK_ONLY, "enter": enter}}],
        breakout=[reg("echo bo", "bo")],
    )
    with pytest.raises(TimeoutError, match=r"^timed out after 1(\.0)?s waiting for a shell prompt \('blk'\)$"):
        runner.run()
    assert sent.lines() == ["echo before", RC_PROBE, "", "echo bo", RC_PROBE]
    assert runner.config.vars == {"b": "before", "bo": "bo"}


def test_p5_40_unrecognized_prompt_not_used_on_exit(sent: SentLog):
    """SPEC "Prompt state across a swap": a block left at its own prompt; the restored prompts don't match it."""
    runner = top_runner([
        {"block": {"name": "b", "prompts": BLK_ONLY, "enter": [TO_BLK], "script": [reg("echo in1", "i1")]}},
        {"cmd": "echo after", "timeout": 1},
    ])
    with pytest.raises(TimeoutError, match=r"^timed out after 1(\.0)?s waiting for a shell prompt \('top'\)$"):
        runner.run()
    assert sent.lines() == [TO_BLK["line"], "echo in1", RC_PROBE, ""]
    assert runner.config.vars == {"i1": "in1"}


def test_p5_41_restore_after_error_keeps_recognized_prompt(sent: SentLog):
    """SPEC "Prompt state across a swap": a failure at a prompt leaves the session at it for both breakouts."""
    runner = top_runner(
        [reg("echo before", "b"),
         block(script=[{"cmd": "false", "timeout": "5s"}], breakout={"script": [reg("echo bbo", "bbo")]})],
        breakout=[reg("echo bo", "bo")],
    )
    start = time.monotonic()
    with pytest.raises(RuntimeError, match="exit code 1"):
        runner.run()
    assert time.monotonic() - start < FAST
    assert "" not in sent.lines()
    assert runner.config.vars == {"b": "before", "bbo": "bbo", "bo": "bo"}


# -- P5: attach spawn arguments ---------------------------------------------


@pytest.mark.parametrize(("value", "expected"), [("500ms", 0.5), ("1.5s", 1.5)])
def test_p5_23_attach_timeout_subsecond(timeline: Timeline, value: str, expected: float):
    """SPEC.md:74, 307-313: attach.timeout is a duration and is not truncated."""
    timeline.stop_attach = True
    with pytest.raises(AttachRecorded):
        make_runner([], timeout=value).run()
    _, _, kwargs = next(c for c in timeline.calls if c[0] == "attach")
    assert kwargs["timeout"] == expected


@pytest.mark.parametrize("value", ["", "  ", "\n"], ids=["empty", "blank", "newline"])
def test_p5_53_spawn_rendering_empty_stops_before_prepare(timeline: Timeline, tmp_path: Path, value: str):
    """SPEC "attach": a spawn that renders to nothing is a ValueError; prepare doesn't run and nothing is spawned."""
    prepared = tmp_path / "prepared"
    runner = make_runner([], spawn="{{ args.cmd }}", args={"cmd": value}, prepare=f"#!/bin/sh\ntouch {prepared}\n")
    with pytest.raises(ValueError, match=r"^attach\.spawn rendered to an empty command: '\{\{ args\.cmd \}\}'$"):
        runner.run()
    assert not prepared.exists()
    assert "attach" not in timeline.names()


NO_COMMAND = ["''", '""', "\\", "'", "''\"\"", "'' ''", "'' ls", '"" --version']


@pytest.mark.parametrize("value", NO_COMMAND)
def test_p5_54_spawn_rendering_to_quotes_stops_before_prepare(timeline: Timeline, tmp_path: Path, value: str):
    """SPEC "attach": a spawn that renders to quotes or a backslash names no command; nothing runs.

    `pexpect.spawn("''")` raised `IndexError: list index out of range`, after `prepare` had run.
    """
    prepared = tmp_path / "prepared"
    runner = make_runner([], spawn="{{ args.cmd }}", args={"cmd": value}, prepare=f"#!/bin/sh\ntouch {prepared}\n")
    with pytest.raises(ValueError, match=r"^attach\.spawn rendered to an empty command: '\{\{ args\.cmd \}\}'$"):
        runner.run()
    assert not prepared.exists()
    assert "attach" not in timeline.names()


@pytest.mark.parametrize("value", ["'true'", '"true"', "tr''ue", "\\true", "'/bin/true' ''"])
def test_p5_54_quoted_command_still_spawns(value: str):
    """A command that is quoted or escaped is still a command: it gets as far as the spawn wait."""
    runner = make_runner([], spawn="{{ args.cmd }}", args={"cmd": value})
    with pytest.raises(EOFError, match=r"^connection closed before any output from .* \(exit status 0\)$"):
        runner.run()


@pytest.mark.parametrize(
    ("spawn", "args"),
    [
        (f" {BASH}", {}),
        (f"\t \n{BASH}", {}),
        ("{{ args.wrapper | default('') }} " + BASH, {}),
        ("{{ args.wrapper | default('') }} bash --norc --noprofile -i", {"wrapper": "env"}),
    ],
    ids=["space", "tab-newline", "empty-wrapper", "wrapper"],
)
def test_p5_57_spawn_with_leading_whitespace_runs(timeline: Timeline, spawn: str, args: dict[str, str]):
    """SPEC "attach": leading whitespace is removed from the command line before it is spawned.

    `pexpect.spawn(' bash')` looks up an empty command name (`The command was not found or was not
    executable`), and a wrapper template that renders to nothing leaves exactly that space.
    """
    r = run_script([{"cmd": "echo hi", "register": "out"}], spawn=spawn, args=args)
    assert r.config.vars["out"] == "hi"
    [(_, call, _)] = [c for c in timeline.calls if c[0] == "attach"]
    assert call[0] == ("env " if args else "") + BASH


def test_p5_57_messages_name_the_command_without_the_whitespace():
    """The spawn-wait errors show the command line that was spawned."""
    with pytest.raises(EOFError, match=r"^connection closed before any output from 'true' \(exit status 0\)$"):
        make_runner([], spawn="  true").run()


@pytest.mark.parametrize("value", [" ''", "  '' ls", " \\"])
def test_p5_57_rendered_quotes_behind_whitespace_still_stop_the_run(timeline: Timeline, value: str):
    """A rendered `spawn` that is whitespace and then quotes still names no command."""
    runner = make_runner([], spawn="{{ args.cmd }}", args={"cmd": value})
    with pytest.raises(ValueError, match=r"^attach\.spawn rendered to an empty command: '\{\{ args\.cmd \}\}'$"):
        runner.run()
    assert "attach" not in timeline.names()


def test_p5_24_attach_timeout_default_and_spawn_templated(
    timeline: Timeline, monkeypatch: pytest.MonkeyPatch
):
    """SPEC.md:73-74, 317: spawn is rendered; omitted timeout uses the 300s default."""
    monkeypatch.delenv("AB_SPAWN_SH", raising=False)
    timeline.stop_attach = True
    runner = make_runner(
        [], spawn="{{ env.AB_SPAWN_SH }} -i", env={"AB_SPAWN_SH": "bash --norc"}, timeout=None
    )
    with pytest.raises(AttachRecorded):
        runner.run()
    _, args, kwargs = next(c for c in timeline.calls if c[0] == "attach")
    assert args[0] == "bash --norc -i"
    assert kwargs["timeout"] == 300
