"""P6-120..122: a run that a signal is ending can always be ended, and still logs out (SPEC "Errors while
the script runs", "Output").

The CLI runs as a subprocess. The device is the fake console of `test_logout.py`: `LOGOUT=` in its log
is the proof that the breakout got through.
"""

from __future__ import annotations

import fcntl
import os
import signal
import subprocess
import sys
import termios
import threading
import time
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import PS1, SHELL_PROMPT, FakeDevice, default_signals, make_doc

from autobot import signals

CLI = [sys.executable, "-W", "ignore", "-m", "autobot.cli"]
LOGIN = {
    "name": "login",
    "send": {"each": "vars.creds", "fields": [{"match": "[Ll]ogin: ?$", "field": "username"}, {"match": "[Pp]assword: ?$", "field": "password"}]},
}
CREDS = {"creds": [{"username": "admin", "password": "secret"}]}
# the documented breakout
LOGOUT = [{"control": "c", "delay_before": "2s"}, {"line": "logout"}, {"block": {"name": "logged out"}, "after": "[Ll]ogin: ?$", "timeout": "5s"}]
LOGGED_OUT = ["LOGIN=admin", "PASSWORD=secret", "LOGOUT="]
SIGS = [signal.SIGINT, signal.SIGTERM, signal.SIGHUP]
HEADS = {signal.SIGINT: "Interrupted", signal.SIGTERM: "Terminated (SIGTERM)", signal.SIGHUP: "Terminated (SIGHUP)"}
# the three signals as bits of the `SigBlk` mask in /proc/<pid>/status
THREE = sum(1 << (sig - 1) for sig in SIGS)
# a reader that holds its end of the pipe and never reads; the second argument only names the process
STALLED = [sys.executable, "-c", "import time\ntime.sleep(600)\n", "autobot-test-stalled-reader"]
# prints `ready` and is `sleep 30` from then on, in the same process
RUNNING = """sh -c "echo rea''dy; exec sleep 30\""""
linux = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc/<pid>/status, and a pipe of the size Linux gives it")


class Watch(threading.Thread):
    """Samples the blocked-signal mask of a process while it runs, with whether it is waiting. A mask
    seen while the process runs proves nothing: a signal is blocked while its own handler starts, and
    all of them around a fork. A process that waits with one of the three blocked can't be ended."""

    def __init__(self, pid: int):
        super().__init__(daemon=True)
        self._status, self.masks, self._stop_it = Path(f"/proc/{pid}/status"), set(), threading.Event()
        self.start()

    def run(self) -> None:
        while not self._stop_it.wait(0.01):
            try:
                text = self._status.read_text()
            except OSError:
                return
            fields = dict(line.split(":\t", 1) for line in text.splitlines() if ":\t" in line)
            if fields.get("State", "").startswith("S"):  # sleeping: in a call that waits
                self.masks.add(int(fields.get("SigBlk", "0"), 16))

    def blocked(self) -> set[int]:
        self._stop_it.set()
        self.join(5)
        return {mask & THREE for mask in self.masks} - {0}


def document(fake_device: FakeDevice, tmp_path: Path, script: list[dict[str, Any]], breakout: list[dict[str, Any]] | None = None) -> tuple[Path, Path]:
    spawn, devlog = fake_device("--accept", "admin:secret", "--logout")
    doc = make_doc(script, spawn=spawn, prompts=[SHELL_PROMPT, LOGIN], vars=CREDS, breakout=LOGOUT if breakout is None else breakout)
    path = tmp_path / "script.autobot.yaml"
    path.write_text(yaml.safe_dump(doc, width=10000))
    return path, devlog


def wait_for(what: Any, proc: subprocess.Popen, why: Any = lambda: "", seconds: float = 30) -> None:
    deadline = time.monotonic() + seconds
    while not what():
        assert proc.poll() is None and time.monotonic() < deadline, why()
        time.sleep(0.05)


# -- P6-120: a reader that is there and doesn't read ------------------------------------------------------

# more than a pipe holds: the echo of it fills stdout's pipe, and the write of the next piece waits
FLOOD = "seq 1 40000"
# a hundred lines of nine hundred characters each: their `>> cmd:` lines fill stderr's pipe
CHATTER = [": " + "x" * 900 + f" {i}" for i in range(100)]


def stalled_run(fake_device: FakeDevice, tmp_path: Path, stream: str, sig: int, count: int, gap: float = 0.2) -> tuple[int, str, list[str], set[int], float]:
    """Run the CLI with `stream` a pipe that is full and that nobody reads, send it `sig` `count` times,
    `gap` seconds apart, and wait for its end. Its return code, what it wrote to the other stream, the
    device's log, the signal masks seen with one of the three signals blocked, and how long the end took."""
    script = [{"cmd": FLOOD, "timeout": "60s"}] if stream == "stdout" else [{"cmd": CHATTER, "timeout": "60s"}]
    path, devlog = document(fake_device, tmp_path, script)
    other = tmp_path / "other"
    reader = subprocess.Popen(STALLED, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL)
    assert reader.stdin is not None
    with open(other, "w") as f:
        streams = {"stdout": reader.stdin, "stderr": f} if stream == "stdout" else {"stdout": f, "stderr": reader.stdin}
        proc = subprocess.Popen([*CLI, str(path)], preexec_fn=default_signals, **streams)
        watch = Watch(proc.pid)
        try:
            # the login is done and the command is sent; a second later the pipe is full and the write waits
            if stream == "stdout":
                wait_for(lambda: ">> cmd: seq" in other.read_text(), proc)
            else:
                wait_for(lambda: "PASSWORD=secret" in FakeDevice.read(devlog), proc)
            time.sleep(1.5)
            assert proc.poll() is None
            started = time.monotonic()
            for _ in range(count):
                if proc.poll() is None:
                    proc.send_signal(sig)
                time.sleep(gap)
            try:
                proc.wait(timeout=40)
            except subprocess.TimeoutExpired:
                blocked = watch.blocked()
                pytest.fail(f"still running 40 s after {count} signal(s); signal masks seen: {sorted(map(hex, blocked))}")
            took = time.monotonic() - started
        finally:
            proc.kill()
            reader.kill()
    return proc.returncode, other.read_text(), FakeDevice.read(devlog), watch.blocked(), took


@linux
@pytest.mark.parametrize("count", [1, 2, 3, 8])
@pytest.mark.parametrize("sig", SIGS, ids=lambda s: s.name)
def test_p6_120_stalled_stdout_never_holds_a_run_that_a_signal_is_ending(fake_device: FakeDevice, tmp_path: Path, sig: signal.Signals, count: int):
    """SPEC "Output": `autobot script | less`, and the pipe is full. The run is waiting in a write when
    the signal arrives. The write ends, the breakout runs with the output it can't write discarded, the
    device is logged out, and the process ends from the signal, after one signal as after eight. At no
    time is one of the three signals blocked."""
    returncode, err, device, blocked, took = stalled_run(fake_device, tmp_path, "stdout", sig, count)
    assert returncode == -sig, err
    assert device == LOGGED_OUT, err
    assert blocked == set() and took < 30
    report = [line for line in err.splitlines() if not line.startswith(">> ")]
    assert report[:1] == [HEADS[sig]] and "Traceback" not in err


@linux
@pytest.mark.parametrize("count", [1, 3])
@pytest.mark.parametrize("sig", SIGS, ids=lambda s: s.name)
def test_p6_120_stalled_stderr_never_holds_it_either(fake_device: FakeDevice, tmp_path: Path, sig: signal.Signals, count: int):
    """The same with the messages' pipe full: the run is waiting in the write of a `>> cmd:` line."""
    returncode, _, device, blocked, took = stalled_run(fake_device, tmp_path, "stderr", sig, count)
    assert returncode == -sig
    assert device == LOGGED_OUT
    assert blocked == set() and took < 30


@linux
def test_p6_120_slow_reader_is_no_lost_reader_while_no_signal_is_taken(fake_device: FakeDevice, tmp_path: Path):
    """A reader that reads late holds the run up, as it does any program, and loses nothing: only a run
    that a signal is ending gives up on a stream that isn't taking what is written."""
    path, devlog = document(fake_device, tmp_path, [{"cmd": FLOOD, "timeout": "60s"}])
    late = [sys.executable, "-c", "import sys, time\ntime.sleep(3)\nsys.stdout.write(str(sum(1 for _ in sys.stdin)))\n"]
    reader = subprocess.Popen(late, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    proc = subprocess.Popen([*CLI, str(path)], stdout=reader.stdin, stderr=subprocess.DEVNULL, preexec_fn=default_signals)
    assert reader.stdin is not None and reader.stdout is not None
    reader.stdin.close()
    try:
        assert proc.wait(timeout=60) == 0
        lines = int(reader.stdout.read())
    finally:
        proc.kill()
        reader.kill()
    assert lines > 40000 and FakeDevice.read(devlog) == LOGGED_OUT


# -- P6-121: a second signal, and a third ------------------------------------------------------------------


def repeated(fake_device: FakeDevice, tmp_path: Path, sig: int, every: float, breakout: list[dict[str, Any]] | None = None) -> tuple[int, str, list[str], float]:
    """Run the CLI, and once its command runs send it `sig` every `every` seconds until it has ended."""
    path, devlog = document(fake_device, tmp_path, [{"cmd": RUNNING, "timeout": "25s"}], breakout)
    echo, messages = tmp_path / "stdout", tmp_path / "stderr"
    with open(echo, "w") as out, open(messages, "w") as err:
        proc = subprocess.Popen([*CLI, str(path)], stdout=out, stderr=err, preexec_fn=default_signals)
        try:
            wait_for(lambda: "\nready\n" in echo.read_text(), proc, messages.read_text)
            started = time.monotonic()
            while proc.poll() is None and time.monotonic() - started < 60:
                proc.send_signal(sig)
                time.sleep(every)
            took = time.monotonic() - started
        finally:
            proc.kill()
    return proc.returncode, messages.read_text(), FakeDevice.read(devlog), took


@pytest.mark.parametrize("sig", SIGS, ids=lambda s: s.name)
def test_p6_121_signal_that_is_repeated_does_not_skip_the_logout(fake_device: FakeDevice, tmp_path: Path, sig: signal.Signals):
    """SPEC "Errors while the script runs": a supervisor that sends its signal every half second, an
    operator who presses Ctrl-C again. The breakout's first step waits two seconds before its Ctrl-C;
    the signals that arrive meanwhile are dropped, the logout is sent and confirmed, and the process ends
    from the signal."""
    returncode, err, device, took = repeated(fake_device, tmp_path, sig, 0.5)
    assert returncode == -sig, err
    assert device == LOGGED_OUT, err
    report = [line for line in err.splitlines() if not line.startswith(">> ")]
    assert report == [HEADS[sig], f"  at script.0 (cmd: {RUNNING})"]
    assert took < 20


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM], ids=lambda s: s.name)
def test_p6_121_signal_that_is_repeated_ends_a_breakout_that_hangs_after_the_grace(fake_device: FakeDevice, tmp_path: Path, sig: signal.Signals):
    """The same against a breakout that would take a minute more: the logout is sent and confirmed in
    its first seconds, and the signal that arrives after `signals.GRACE` ends the rest of it."""
    never = tmp_path / "never"
    breakout = [*LOGOUT, {"sleep": "60s"}, {"line": f"touch {never}"}]
    returncode, err, device, took = repeated(fake_device, tmp_path, sig, 0.5, breakout)
    assert returncode == -sig, err
    assert device == LOGGED_OUT and not never.exists()
    assert signals.GRACE - 0.5 < took < signals.GRACE + 10
    assert "  at attach.breakout.3 (sleep)" in err and "Session may be left logged in: a breakout did not finish" in err


def hang_up(fake_device: FakeDevice, tmp_path: Path, redirect: str) -> tuple[list[str], str, Path]:
    """An interactive bash on a terminal of its own, with the CLI as its foreground job; the terminal is
    closed once the script's command runs. The device's log 20 s later, and the CLI's messages if
    `redirect` sent them to a file."""
    path, devlog = document(fake_device, tmp_path, [{"cmd": RUNNING, "timeout": "25s"}])
    messages, done = tmp_path / "stderr", tmp_path / "done"
    master, slave = os.openpty()

    def setup() -> None:
        default_signals()
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)

    env = {**os.environ, "PS1": PS1}
    shell = subprocess.Popen(
        ["bash", "--norc", "--noprofile", "-i"], stdin=slave, stdout=slave, stderr=slave, start_new_session=True, preexec_fn=setup, env=env
    )
    os.close(slave)
    try:
        command = f"{' '.join(CLI)} {path} {redirect.format(messages=messages)}; touch {done}\n"
        os.write(master, command.encode())
        deadline = time.monotonic() + 30
        while "PASSWORD=secret" not in FakeDevice.read(devlog):
            assert time.monotonic() < deadline
            time.sleep(0.05)
        time.sleep(1.5)  # the command of the script is running; what the terminal shows is not read here
        os.close(master)  # the window is closed
        master = -1
        deadline = time.monotonic() + 20
        while "LOGOUT=" not in FakeDevice.read(devlog) and time.monotonic() < deadline:
            time.sleep(0.1)
        time.sleep(1)
    finally:
        if master >= 0:
            os.close(master)
        shell.kill()
    return FakeDevice.read(devlog), messages.read_text() if messages.exists() else "", done


@linux
@pytest.mark.parametrize(
    "redirect",
    ["", "2>{messages}", "2>&1 | tee {messages}"],
    ids=["on-the-terminal", "stderr-in-a-file", "through-tee"],
)
def test_p6_121_closing_the_terminal_logs_out(fake_device: FakeDevice, tmp_path: Path, redirect: str):
    """SPEC "Errors while the script runs": the window that `autobot` runs in is closed. The CLI is the
    foreground job of an interactive shell, so it gets SIGHUP twice, from the terminal and from the shell,
    and with `| tee` its reader goes away too. The breakout runs to its end: the device is logged out."""
    device, err, _ = hang_up(fake_device, tmp_path, redirect)
    assert device == LOGGED_OUT, err
    if redirect.startswith("2>{"):
        assert "Terminated (SIGHUP)" in err.splitlines() and "Breakout failed" not in err


# -- P6-122: the output can't be written, and nobody has gone ---------------------------------------------

FULL = "[Errno 28] No space left on device"
TOO_LARGE = "[Errno 27] File too large"


def write_failure(fake_device: FakeDevice, tmp_path: Path, stream: str, how: str) -> tuple[int, str, str, list[str], bool]:
    """Run the CLI with `stream` going to /dev/full, or to a file under a file size limit of 4 blocks
    that the session's output outgrows. Its exit status, both streams and the device's log."""
    never = tmp_path / "never"
    script = [{"cmd": "echo one"}, {"cmd": "seq 1 2000" if how == "limit" else "echo two"}, {"cmd": f"touch {never}"}]
    if stream == "stderr" and how == "limit":
        script[1] = {"cmd": CHATTER[:8]}
    path, devlog = document(fake_device, tmp_path, script)
    out, err = tmp_path / "stdout", tmp_path / "stderr"
    target = {"stdout": out, "stderr": err}[stream] if how == "limit" else "/dev/full"
    redirect = f">{target} 2>{err}" if stream == "stdout" else f"2>{target} >{out}"
    limit = "ulimit -f 4; " if how == "limit" else ""
    command = f"{limit}exec {' '.join(CLI)} {path} {redirect}"
    res = subprocess.run(["bash", "-c", command], capture_output=True, text=True, check=False, timeout=120, preexec_fn=default_signals)
    return res.returncode, out.read_text() if out.exists() else "", (err.read_text() if err.exists() else "") + res.stderr, FakeDevice.read(devlog), never.exists()


@linux
@pytest.mark.parametrize("how", ["limit", "full"])
def test_p6_122_stdout_that_cannot_be_written_fails_the_run_after_the_logout(fake_device: FakeDevice, tmp_path: Path, how: str):
    """SPEC "Output": stdout goes to a file that reaches its size limit, or to a full disk. The run stops
    in the step that was reading, the breakout logs out, and the CLI reports a failed run on stderr and
    exits with status 3: not Python's 120, and with nothing of the interpreter's at its end."""
    returncode, _, err, device, later = write_failure(fake_device, tmp_path, "stdout", how)
    assert returncode == 3, err
    assert not later and "LOGIN=" not in device and "PASSWORD=" not in device
    if how == "limit":  # the limit is reached after the login; a full disk fails the first write, before it
        assert device == LOGGED_OUT
    report = [line for line in err.splitlines() if not line.startswith(">> ")]
    reason = TOO_LARGE if how == "limit" else FULL
    assert report[0].endswith(f"script.autobot.yaml: cannot write stdout: {reason}") and report[0].startswith("Run failed in ")
    # a full disk fails the write of the first output, in the spawn wait: no step, and no breakout
    assert [line[:11] for line in report[1:]] == (["  at script"] if how == "limit" else [])
    assert "Exception ignored" not in err and "Traceback" not in err and "Breakout failed" not in err


@linux
@pytest.mark.parametrize("how", ["limit", "full"])
def test_p6_122_stderr_that_cannot_be_written_fails_the_run_after_the_logout(fake_device: FakeDevice, tmp_path: Path, how: str):
    """The same for the messages: the run stops before its next step, logs out, and exits with status 3.
    The report has nowhere to go."""
    returncode, out, err, device, later = write_failure(fake_device, tmp_path, "stderr", how)
    assert returncode == 3, err
    assert not later and "LOGIN=" not in device and "PASSWORD=" not in device
    if how == "limit":
        assert device == LOGGED_OUT
    assert "Exception ignored" not in err and "Traceback" not in err


def test_p6_122_error_on_a_file_is_a_failure_and_on_a_pipe_a_reader_that_has_gone(tmp_path: Path):
    """`log.lost`: what a failed write means. EPIPE, and EIO on a pipe or a terminal: nobody reads. Any
    other error, and EIO on a regular file: the write failed. An error that says to try again, and an
    error that is none of the operating system's: not lost at all."""
    import errno
    import io

    from autobot import log

    saved = (log.gone, log.failure, log.on_gone)
    told: list[str] = []
    read_end, write_end = os.pipe()
    try:
        cases = [
            ("file", OSError(errno.ENOSPC, "No space left on device"), f"cannot write output: {FULL}"),
            ("file", OSError(errno.EFBIG, "File too large"), f"cannot write output: {TOO_LARGE}"),
            ("file", OSError(errno.EDQUOT, "Disk quota exceeded"), f"cannot write output: [Errno {errno.EDQUOT}] Disk quota exceeded"),
            ("file", OSError(errno.EIO, "Input/output error"), "cannot write output: [Errno 5] Input/output error"),
            ("file", BrokenPipeError(errno.EPIPE, "Broken pipe"), ""),
            ("pipe", OSError(errno.EIO, "Input/output error"), ""),
            ("pipe", BrokenPipeError(errno.EPIPE, "Broken pipe"), ""),
        ]
        for kind, error, failure in cases:
            log.gone, log.failure, log.on_gone = "", "", told.append
            stream = open(tmp_path / "f", "w") if kind == "file" else os.fdopen(os.dup(write_end), "w")
            with stream:
                assert log.lost(stream, error) and (log.gone, log.failure) == ("output", failure), (kind, error)
                assert os.fstat(stream.fileno()).st_rdev == os.stat(os.devnull).st_rdev  # the descriptor is /dev/null's
        assert told == ["output"] * len(cases)
        log.gone, log.failure = "", ""
        with open(tmp_path / "f", "w") as f:
            for error in (BlockingIOError(errno.EAGAIN, "try again"), InterruptedError(errno.EINTR, "interrupted"), ValueError("x"), UnicodeEncodeError("ascii", "x", 0, 1, "x")):
                assert not log.lost(f, error)
            assert log.gone == "" and log.lost(f, OSError(errno.ENOSPC, "full"), tell=False) and told == ["output"] * len(cases)
        assert log.lost(f, ValueError("I/O operation on closed file.")) and log.lost(io.StringIO(), BrokenPipeError(errno.EPIPE, "Broken pipe"))
    finally:
        log.gone, log.failure, log.on_gone = saved
        os.close(read_end)
        os.close(write_end)
