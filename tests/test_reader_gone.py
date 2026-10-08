"""P6-117..119: nobody reads the run's output any more (SPEC "Output", "Errors while the script runs").

The CLI runs as a subprocess whose stdout is a pipe to a reader that exits: `head -1`, or a reader that
exits at the first line with a word in it, so that the test decides how far the run has come. The device
is the fake console of `test_logout.py`: `LOGOUT=` in its log is the proof that the breakout got through.
"""

from __future__ import annotations

import errno
import io
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import SHELL_PROMPT, FakeDevice, make_doc

from autobot import log, signals
from autobot.session import Session

CLI = [sys.executable, "-W", "ignore", "-m", "autobot.cli"]
LOGIN = {
    "name": "login",
    "send": {"each": "vars.creds", "fields": [{"match": "login:", "field": "username"}, {"match": "Password:", "field": "password"}]},
}
CREDS = {"creds": [{"username": "admin", "password": "secret"}]}
# the documented breakout: its Ctrl-C waits, so that a command the shell has only just been sent is running
LOGOUT = [{"control": "c", "delay_before": "2s"}, {"line": "logout"}, {"block": {"name": "logged out"}, "after": "[Ll]ogin: ?$", "timeout": "5s"}]
LOGGED_OUT = ["LOGIN=admin", "PASSWORD=secret", "LOGOUT="]
# exits at the first line that has the word in it, and reads nothing more
UNTIL = "import sys\nfor line in sys.stdin:\n    if sys.argv[1] in line:\n        break\n"
SIGS = [signal.SIGINT, signal.SIGTERM, signal.SIGHUP]
# prints `ready` and is silent from then on. The word is in the session's output only: the command as it
# is sent, and as the `>> cmd:` line shows it, has it in two pieces
SILENT = """sh -c "echo rea''dy; exec sleep 30\""""
HEADS = {signal.SIGINT: "Interrupted", signal.SIGTERM: "Terminated (SIGTERM)", signal.SIGHUP: "Terminated (SIGHUP)"}


def _default() -> None:
    for sig in SIGS:
        signal.signal(sig, signal.SIG_DFL)


def until(word: str) -> list[str]:
    return [sys.executable, "-c", UNTIL, word]


class Run:
    def __init__(self, returncode: int, err: str, device: list[str]):
        self.returncode, self.err, self.device = returncode, err, device
        self.report = [line for line in err.splitlines() if not line.startswith(">> ")]
        self.progress = [line for line in err.splitlines() if line.startswith(">> ")]


def piped(
    fake_device: FakeDevice,
    tmp_path: Path,
    script: list[dict[str, Any]],
    reader: list[str],
    *,
    both: bool = False,
    sig: int | None = None,
    err_reader: list[str] | None = None,
    breakout: list[dict[str, Any]] | None = None,
) -> Run:
    """Run the CLI on `script` with stdout piped to `reader`. stderr is a file, the same pipe (`both`), or
    a pipe to `err_reader`. Once the reader has exited, send `sig`."""
    spawn, devlog = fake_device("--accept", "admin:secret", "--logout")
    doc = make_doc(script, spawn=spawn, prompts=[SHELL_PROMPT, LOGIN], vars=CREDS, breakout=LOGOUT if breakout is None else breakout)
    path = tmp_path / "script.autobot.yaml"
    path.write_text(yaml.safe_dump(doc))
    messages = tmp_path / "stderr"
    out = subprocess.Popen(reader, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL)
    other = subprocess.Popen(err_reader, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL) if err_reader else None
    assert out.stdin is not None
    with open(messages, "w") as err:
        stderr = out.stdin if both else other.stdin if other else err
        proc = subprocess.Popen([*CLI, str(path)], stdout=out.stdin, stderr=stderr, preexec_fn=_default)
        out.stdin.close()
        if other and other.stdin:
            other.stdin.close()
        try:
            out.wait(timeout=60)
            if sig is not None:
                time.sleep(0.3)  # nothing is written meanwhile: the run is in a silent command
                assert proc.poll() is None, messages.read_text()
                proc.send_signal(sig)
            proc.wait(timeout=60)
        finally:
            proc.kill()
            for reader_proc in (out, other):
                if reader_proc:
                    reader_proc.kill()
    return Run(proc.returncode, messages.read_text(), FakeDevice.read(devlog))


# -- P6-117: the reader goes away, and nothing else happens -------------------------------------------


def test_p6_117_head_1_stops_the_run_and_the_breakout_runs(fake_device: FakeDevice, tmp_path: Path):
    """SPEC "Output": `autobot script | head -1`. The first write after `head` has exited stops the run
    as an interrupt would, the breakout runs, no later step is sent, and the process ends from SIGPIPE,
    which a shell reports as 141. stderr is still there and gets the report."""
    never = tmp_path / "never"
    run = piped(fake_device, tmp_path, [{"cmd": "echo one"}, {"cmd": f"touch {never}"}], ["head", "-1"])
    assert run.returncode == -signal.SIGPIPE, run.err
    assert run.report == ["Terminated (SIGPIPE): nobody reads stdout any more", "  at script.0 (cmd: echo one)"]
    assert ">> step interrupted (SIGPIPE)" in run.progress and ">> breakout: detaching" in run.progress
    assert not never.exists() and ">> cmd: touch" not in run.err
    # `head` left at the banner, so the run was stopped in the middle of the login: the user name was
    # sent, and the breakout's `logout` line is what the device read as the password. Nobody is logged
    # in, nothing empty was entered, and the device has one failed login for the user in its log (SPEC
    # "Logging out" says so)
    assert run.device == ["LOGIN=admin", "PASSWORD=logout"]
    assert "Traceback" not in run.err and "Broken pipe" not in run.err


def test_p6_117_shell_pipeline_reports_141(fake_device: FakeDevice, tmp_path: Path):
    """The same through a shell: the pipeline's status for autobot is 141."""
    spawn, devlog = fake_device("--accept", "admin:secret", "--logout")
    never = tmp_path / "never"
    doc = make_doc([{"cmd": "echo one"}, {"cmd": f"touch {never}"}], spawn=spawn, prompts=[SHELL_PROMPT, LOGIN], vars=CREDS, breakout=LOGOUT)
    path = tmp_path / "script.autobot.yaml"
    path.write_text(yaml.safe_dump(doc))
    command = f"{' '.join(CLI)} {path} 2>{tmp_path / 'stderr'} | head -1; echo status=${{PIPESTATUS[0]}}"
    res = subprocess.run(["bash", "-c", command], capture_output=True, text=True, check=False, timeout=120, preexec_fn=_default)
    assert res.stdout.splitlines() == ["Welcome", "status=141"], (res.stdout, (tmp_path / "stderr").read_text())
    assert not never.exists()


@pytest.mark.parametrize("both", [False, True], ids=["stderr-alive", "both-gone"])
def test_p6_117_reader_gone_after_the_login_logs_out(fake_device: FakeDevice, tmp_path: Path, both: bool):
    """The reader exits while the first command runs, after the login. The run stops in that step, the
    breakout logs out and its wait for the login prompt is met, and the steps after it are never sent.
    With stderr alive the report is there; with stdout and stderr the same dead pipe nothing can be
    said, and the process ends the same way."""
    never = tmp_path / "never"
    script = [{"cmd": "echo marker-one"}, {"cmd": "sleep 2"}, {"cmd": f"touch {never}"}]
    run = piped(fake_device, tmp_path, script, until("marker-one"), both=both)
    assert run.returncode == -signal.SIGPIPE, run.err
    assert run.device == LOGGED_OUT and not never.exists()
    if both:
        assert run.err == ""
    else:
        assert run.report == ["Terminated (SIGPIPE): nobody reads stdout any more", "  at script.0 (cmd: echo marker-one)"]
        assert run.progress[-2:] == [">> block enter: logged out", ">> block completed: logged out"]
        assert ">> cmd: sleep 2" not in run.progress


def test_p6_117_stderr_that_goes_away_stops_the_run_too(fake_device: FakeDevice, tmp_path: Path):
    """The messages' reader exits and the session's output is still read: the same stop, named after
    stderr, which can't be told."""
    never = tmp_path / "never"
    script = [{"cmd": "echo marker-one"}, {"cmd": "sleep 2"}, {"cmd": f"touch {never}"}]
    run = piped(fake_device, tmp_path, script, [sys.executable, "-c", "import sys; sys.stdin.read()"], err_reader=until("marker-one"))
    assert run.returncode == -signal.SIGPIPE
    assert run.device == LOGGED_OUT and not never.exists()


def test_p6_117_second_stream_that_goes_away_does_not_interrupt_the_breakout(fake_device: FakeDevice, tmp_path: Path):
    """stdout's reader exits in the script, and stderr's once the breakout has started: the breakout
    goes on to its end, and the device is logged out."""
    script = [{"cmd": "echo marker-one"}, {"cmd": "sleep 2"}]
    breakout = [{"cmd": "sleep 1", "timeout": "5s"}, *LOGOUT]
    run = piped(fake_device, tmp_path, script, until("marker-one"), err_reader=until("breakout: detaching"), breakout=breakout)
    assert run.returncode == -signal.SIGPIPE
    assert run.device == LOGGED_OUT


# -- P6-118: the reader is gone, and a signal ends the run --------------------------------------------


@pytest.mark.parametrize("both", [False, True], ids=["stderr-alive", "both-gone"])
@pytest.mark.parametrize("sig", SIGS, ids=lambda s: s.name)
def test_p6_118_signal_with_the_reader_gone_still_logs_out_and_ends_from_the_signal(
    fake_device: FakeDevice, tmp_path: Path, sig: signal.Signals, both: bool
):
    """SPEC "Errors while the script runs": `autobot script | tee log` and Ctrl-C, which ends `tee` as
    well. The reader exits at the command's one line of output, and the run waits in a command that is
    silent from then on, so nothing has failed yet when the signal arrives. The breakout's own writes then fail, and it runs all the same: the device is
    logged out, and the process ends from the signal, not from the broken pipe."""
    run = piped(fake_device, tmp_path, [{"cmd": SILENT, "timeout": "20s"}], until("ready"), both=both, sig=sig)
    assert run.returncode == -sig, run.err
    assert run.device == LOGGED_OUT
    if not both:
        assert run.report == [HEADS[sig], f"  at script.0 (cmd: {SILENT})"]
        assert "Broken pipe" not in run.err and "Traceback" not in run.err


# -- P6-119: when it is noticed, and what it is --------------------------------------------------------


def test_p6_119_reader_gone_during_the_attach_breakout_changes_nothing(fake_device: FakeDevice, tmp_path: Path):
    """The script has completed and `attach.breakout` runs when the reader exits. The breakout runs to
    its end with its output discarded, and the run completes with status 0."""
    breakout = [{"cmd": "echo in-the-breakout", "timeout": "5s"}, {"cmd": "sleep 1", "timeout": "5s"}, *LOGOUT]
    run = piped(fake_device, tmp_path, [{"cmd": "echo done"}], until("in-the-breakout"), breakout=breakout)
    assert run.returncode == 0, run.err
    assert run.device == LOGGED_OUT and run.report == [] and run.progress[-1] == ">> run completed"


def test_p6_119_reader_gone_during_a_block_breakout_stops_the_script_after_it(fake_device: FakeDevice, tmp_path: Path):
    """The reader exits while a block's breakout runs. That breakout is not interrupted; the script's
    next step is not sent, and the run ends as stopped."""
    finished, never = tmp_path / "finished", tmp_path / "never"
    block = {
        "name": "sub",
        "script": [{"cmd": "true"}],
        "breakout": [{"cmd": "echo in-the-breakout", "timeout": "5s"}, {"cmd": "sleep 1", "timeout": "5s"}, {"cmd": f"touch {finished}", "timeout": "5s"}],
    }
    run = piped(fake_device, tmp_path, [{"block": block}, {"cmd": f"touch {never}"}], until("in-the-breakout"))
    assert run.returncode == -signal.SIGPIPE, run.err
    assert finished.exists() and not never.exists()
    assert run.device == LOGGED_OUT
    assert run.report == ["Terminated (SIGPIPE): nobody reads stdout any more"]  # between two steps: no `at` line
    assert ">> block completed: sub" in run.progress


def test_p6_119_lost_points_the_stream_away_and_tells_the_run(tmp_path: Path):
    """`log.lost`: a stream that can't be written is pointed to /dev/null and the run is told, once per
    failed write; an error that is none of the operating system's is no lost stream. (What each error
    means: P6-122.)"""
    told: list[str] = []
    saved = (log.on_gone, log.gone, log.failure)
    log.on_gone, log.gone, log.failure = told.append, "", ""
    try:
        with open(tmp_path / "f", "w") as f:
            assert not log.lost(f, ValueError("x"))
            assert not log.lost(f, UnicodeEncodeError("ascii", "x", 0, 1, "x"))
            assert told == []
            assert log.lost(f, BrokenPipeError(errno.EPIPE, "Broken pipe"))
            assert os.fstat(f.fileno()).st_rdev == os.stat(os.devnull).st_rdev  # the descriptor is /dev/null's
        assert log.lost(f, ValueError("I/O operation on closed file."))
        assert log.lost(io.StringIO(), BrokenPipeError(errno.EPIPE, "Broken pipe"))  # no descriptor: nothing to point
        assert told == ["output"] * 3 and log.failure == ""
    finally:
        log.on_gone, log.gone, log.failure = saved


LIBRARY = """
import os, sys, yaml
from autobot import Config, Runner
from autobot.signals import ReaderGone, Terminated
runner = Runner(Config.model_validate(yaml.safe_load(open(sys.argv[1]))), {})
try:
    runner.run()
except ReaderGone as e:
    null = os.stat(os.devnull)
    same = os.fstat(1).st_rdev == null.st_rdev
    open(sys.argv[2], "w").write(f"{type(e).__name__} {e.stream} {e.signum} {isinstance(e, Terminated)} {isinstance(e, Exception)} {same}")
    sys.exit(7)
"""


def test_p6_119_runner_used_as_a_library_raises_reader_gone_after_the_cleanup(fake_device: FakeDevice, tmp_path: Path):
    """`Runner.run()` without the CLI: `autobot.signals.ReaderGone`, a `Terminated`, is raised once the
    breakouts have run and the session is closed. The process is not ended, and its stdout stays
    pointed to /dev/null."""
    spawn, devlog = fake_device("--accept", "admin:secret", "--logout")
    doc = make_doc([{"cmd": "echo marker-one"}, {"cmd": "sleep 2"}], spawn=spawn, prompts=[SHELL_PROMPT, LOGIN], vars=CREDS, breakout=LOGOUT)
    path, caught = tmp_path / "script.autobot.yaml", tmp_path / "caught"
    path.write_text(yaml.safe_dump(doc))
    reader = subprocess.Popen(until("marker-one"), stdin=subprocess.PIPE, stdout=subprocess.DEVNULL)
    proc = subprocess.Popen(
        [sys.executable, "-W", "ignore", "-c", LIBRARY, str(path), str(caught)], stdout=reader.stdin, stderr=subprocess.DEVNULL, preexec_fn=_default
    )
    assert reader.stdin is not None
    reader.stdin.close()
    try:
        assert proc.wait(timeout=60) == 7
    finally:
        proc.kill()
        reader.kill()
    assert caught.read_text() == f"ReaderGone stdout {int(signal.SIGPIPE)} True False True"
    assert FakeDevice.read(devlog) == LOGGED_OUT


def test_p6_119_reader_gone_is_a_terminated_for_sigpipe():
    e = signals.ReaderGone("stdout")
    assert (e.signum, e.name, e.stream, str(e)) == (signal.SIGPIPE, "SIGPIPE", "stdout", "SIGPIPE")


def test_p6_119_stop_raised_while_a_piece_is_echoed_does_not_cost_the_session_the_piece(shell_session: Session):
    """The stop for a reader that is gone is raised where the echo is written, which is before pexpect
    keeps what it has read. The piece is put back: the prompt that came with it is there for the next
    wait, as a breakout that starts with a `cmd` needs."""
    s = shell_session
    s.get_prompt(timeout=5)
    assert s._echo is not None
    write, raised = s._echo.write, []

    def stop(data: str) -> None:
        if not raised and "kept" in data and "echo" not in data:
            raised.append(data)
            raise signals.ReaderGone("stdout")
        write(data)

    s._echo.write = stop  # type: ignore[method-assign]
    s.sendline("stty -echo", timeout=5)
    s.get_prompt(timeout=5)
    s.sendline("echo kept", timeout=5)
    with pytest.raises(signals.ReaderGone):
        s.get_prompt(timeout=5)
    assert len(raised) == 1
    started = time.monotonic()
    s._solicit = False  # as after the command: the wait must find the prompt, not ask for a new one
    assert s.get_prompt(timeout=4) == "kept\n"
    assert time.monotonic() - started < 2



def test_p6_119_pexpect_keeps_a_piece_where_the_session_puts_one_back():
    """`session._Echo` puts a piece back into the two buffers pexpect keeps what it has read in. They are
    private to pexpect, whose version is capped for that: a version that names them otherwise fails here."""
    import io

    import pexpect

    child = pexpect.spawn("true", encoding="utf-8")
    try:
        assert isinstance(child._buffer, io.StringIO) and isinstance(child._before, io.StringIO)
        child._buffer.write("kept")
        assert child.buffer == "kept"
    finally:
        child.close()
    assert tuple(int(n) for n in pexpect.__version__.split(".")[:2]) == (4, 9)


def test_p6_117_prepare_script_that_loses_its_reader_ends_the_run_the_same_way(tmp_path: Path):
    """SPEC "Output": `attach.prepare` writes to the run's own stdout. With `| head -1` the script is
    what gets SIGPIPE; the run ends as one whose reader has gone, from SIGPIPE, with nothing spawned."""
    spawned = tmp_path / "spawned"
    doc = make_doc([{"cmd": "true"}], spawn=f"sh -c 'touch {spawned}; exec bash --norc --noprofile -i'",
                   prepare="#!/bin/sh\nwhile :; do echo line; done\n")
    path, messages = tmp_path / "script.autobot.yaml", tmp_path / "stderr"
    path.write_text(yaml.safe_dump(doc))
    reader = subprocess.Popen(["head", "-1"], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL)
    with open(messages, "w") as err:
        proc = subprocess.Popen([*CLI, str(path)], stdout=reader.stdin, stderr=err, preexec_fn=_default)
        assert reader.stdin is not None
        reader.stdin.close()
        try:
            assert proc.wait(timeout=60) == -signal.SIGPIPE, messages.read_text()
        finally:
            proc.kill()
            reader.kill()
    report = [line for line in messages.read_text().splitlines() if not line.startswith(">> ")]
    assert report == ["Terminated (SIGPIPE): nobody reads the output of prepare any more"] and not spawned.exists()
