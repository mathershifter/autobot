"""P6-114..116: how a run ends when it isn't the script that ends it (SPEC "Errors while the script runs",
"Output"): SIGTERM and SIGHUP, output that can't be written, and Ctrl-C.

The CLI runs as a process of its own against the console of ``test_logout``: a login, a real bash, and the
login prompt again after ``logout``. Its log shows what reached it: ``LOGOUT=`` is a breakout that ran.
Every wait is for something the device did (a file a command touched, a line in the device's log), and a
signal is sent to a command that is certainly running: the process that touched the file is the one that
becomes ``sleep``.
"""

from __future__ import annotations

import errno
import fcntl
import io
import os
import resource
import select
import signal
import subprocess
import sys
import termios
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import BASH, FakeDevice, SentLog, default_signals, make_doc, make_runner
from test_logout import LEFT, LOGOUT, SENT, console, report

from autobot import cli, log
from autobot.screen import CleanWriter
from autobot.session import PromptHandler, Session

SIGNALS = [signal.SIGTERM, signal.SIGHUP]
IN = ["LOGIN=admin", "PASSWORD=secret"]  # the device's log of a run that logged in and did not log out
END = 5.0  # seconds a process has to end in, once it is told to
NOISE = ("Traceback", "Exception ignored", "BrokenPipeError", "Error in sys.excepthook")
LIBRARY = (
    "import sys, yaml\n"
    "from autobot.models import Config\n"
    "from autobot.runner import Runner\n"
    "Runner(Config.model_validate(yaml.safe_load(open(sys.argv[1]))), {}).run()\n"
)


def running(mark: Path, seconds: int = 30) -> dict[str, Any]:
    """A command that is running for certain once `mark` exists."""
    return {"cmd": f'sh -c "touch {mark}; exec sleep {seconds}"', "timeout": "60s"}


def interrupted(sig: signal.Signals) -> str:
    return f"Interrupted ({sig.name}): {cli.UNCLEAN}"


def lost(path: Path, stream: str, why: str) -> str:
    return f"Run failed in {path}: cannot write {stream}: {why}; {cli.UNCLEAN}"


PIPE = "[Errno 32] Broken pipe"


class Cli:
    """A run of the CLI on `doc` as a process of its own. stdout and stderr go to files unless given."""

    def __init__(self, doc: dict[str, Any], tmp_path: Path, stdout: Any = None, stderr: Any = None, pre: Callable[[], None] | None = None, argv: list[str] | None = None, **kw: Any):
        self.path = tmp_path / "script.autobot.yaml"
        self.path.write_text(yaml.safe_dump(doc))
        self._out, self._err = tmp_path / "stdout", tmp_path / "stderr"
        files = []
        if stdout is None:
            stdout = open(self._out, "w")
            files.append(stdout)
        if stderr is None:
            stderr = open(self._err, "w")
            files.append(stderr)

        def preexec() -> None:
            default_signals()
            if pre:
                pre()

        argv = argv or [sys.executable, "-W", "ignore", "-m", "autobot.cli"]
        self.proc = subprocess.Popen([*argv, str(self.path)], stdout=stdout, stderr=stderr, preexec_fn=preexec, **kw)
        for f in files:
            f.close()

    def wait_for(self, what: Callable[[], Any], seconds: float = 30) -> None:
        deadline = time.monotonic() + seconds
        while not what():
            assert self.proc.poll() is None, f"the run ended first ({self.proc.returncode}): {self.err}"
            assert time.monotonic() < deadline, self.err
            time.sleep(0.02)

    def ends(self, seconds: float = END) -> int:
        """The return code of the process, which ends within `seconds`."""
        try:
            return self.proc.wait(timeout=seconds)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
            raise AssertionError(f"still running after {seconds}s: {self.err}") from None

    def signal(self, sig: int) -> int:
        self.proc.send_signal(sig)
        return self.ends()

    @property
    def out(self) -> str:
        return self._out.read_text() if self._out.exists() else ""

    @property
    def err(self) -> str:
        return self._err.read_text() if self._err.exists() else ""

    def report(self) -> list[str]:
        """What is on stderr that is no `>> ` progress line."""
        return [line for line in self.err.splitlines() if not line.startswith(">> ")]

    def quiet(self) -> None:
        """Nothing of the interpreter's is in the output: no traceback, no complaint at exit."""
        for text in (self.err, self.out):
            assert not any(word in text for word in NOISE), text

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()


@pytest.fixture
def cli_run(tmp_path: Path):
    """Factory for `Cli` runs; whatever is still running when the test ends is killed."""
    runs: list[Cli] = []

    def start(doc: dict[str, Any], **kw: Any) -> Cli:
        runs.append(Cli(doc, tmp_path, **kw))
        return runs[-1]

    yield start
    for run in runs:
        run.kill()


def unread(fd: int) -> int:
    """How many bytes wait in the pipe that `fd` is the read end of."""
    return int.from_bytes(fcntl.ioctl(fd, termios.FIONREAD, b"\0\0\0\0"), sys.byteorder)


def full(fd: int, least: int) -> Callable[[], bool]:
    """Whether the pipe holds at least `least` bytes and has stopped growing: its writer waits."""
    seen = [-1, 0.0]

    def stalled() -> bool:
        now, size = time.monotonic(), unread(fd)
        if size != seen[0]:
            seen[:] = [size, now]
        return size >= least and now - seen[1] > 0.3

    return stalled


def read_until(run: Cli, fd: int, what: Callable[[], Any]) -> None:
    """Read the pipe as its reader does, until `what` is so."""
    deadline = time.monotonic() + 30
    while not what():
        assert run.proc.poll() is None and time.monotonic() < deadline, run.err
        if select.select([fd], [], [], 0.02)[0]:
            os.read(fd, 65536)


# -- P6-114: SIGTERM and SIGHUP ---------------------------------------------------------------------------


@pytest.mark.parametrize("sig", SIGNALS, ids=lambda s: s.name)
def test_p6_114_signal_during_a_command_ends_the_run_at_once_without_the_breakouts(fake_device: FakeDevice, cli_run, tmp_path: Path, sig: signal.Signals):
    """SPEC "Errors while the script runs": SIGTERM and SIGHUP end the process at once, from the signal,
    after one line on stderr. No breakout step is sent: the device is still logged in."""
    ready = tmp_path / "ready"
    doc, device = console(fake_device, [running(ready)], LOGOUT)
    run = cli_run(doc)
    run.wait_for(ready.exists)
    assert run.signal(sig) == -sig
    lines = run.err.splitlines()
    assert lines[-1] == interrupted(sig) and lines[-2].startswith(">> cmd: sh -c ")
    assert not any(line.startswith(">> breakout") or "step interrupted" in line for line in lines)
    assert FakeDevice.read(device) == IN
    run.quiet()


@pytest.mark.parametrize("sig", SIGNALS, ids=lambda s: s.name)
def test_p6_114_signal_during_a_breakout_ends_it_there(fake_device: FakeDevice, cli_run, tmp_path: Path, sig: signal.Signals):
    """The same while a breakout runs: its remaining steps, the logout among them, are not sent."""
    ready = tmp_path / "ready"
    doc, device = console(fake_device, [{"cmd": "true"}], [running(ready), *LOGOUT])
    run = cli_run(doc)
    run.wait_for(ready.exists)
    assert run.signal(sig) == -sig
    lines = run.err.splitlines()
    assert lines[-1] == interrupted(sig) and ">> breakout: detaching" in lines
    assert ">> control sent: ^C" not in lines and not any(line.startswith("Breakout failed") or line.startswith(LEFT) for line in lines)
    assert FakeDevice.read(device) == IN
    run.quiet()


@pytest.mark.parametrize("sig", SIGNALS, ids=lambda s: s.name)
def test_p6_114_signal_ends_a_run_whose_stdout_nobody_reads(fake_device: FakeDevice, cli_run, tmp_path: Path, sig: signal.Signals):
    """A reader that holds the pipe and doesn't read: the run waits in a write to stdout, with the pipe
    full. One signal ends it all the same, with its line on stderr, and nothing more is sent."""
    ready = tmp_path / "ready"
    r, w = os.pipe()
    try:
        doc, device = console(fake_device, [{"cmd": "true"}, {"cmd": f"touch {ready}; yes | head -n 100000", "timeout": "60s"}], LOGOUT)
        run = cli_run(doc, stdout=w)
        os.close(w)
        run.wait_for(ready.exists)
        run.wait_for(full(r, fcntl.fcntl(r, fcntl.F_GETPIPE_SZ) - 8192))
        assert run.signal(sig) == -sig
        assert run.err.splitlines()[-1] == interrupted(sig)
        assert FakeDevice.read(device) == IN
        run.quiet()
    finally:
        os.close(r)


@pytest.mark.parametrize("sig", SIGNALS, ids=lambda s: s.name)
def test_p6_114_signal_ends_a_run_whose_stderr_nobody_reads(fake_device: FakeDevice, cli_run, tmp_path: Path, sig: signal.Signals):
    """The same with stderr: the run waits in the write of a message. The handler writes its line only
    if stderr takes it at once, so there is none here, and the process ends from the signal."""
    r, w = os.pipe()
    try:
        size = fcntl.fcntl(w, fcntl.F_SETPIPE_SZ, 4096)
        lines = [f": {n:03d} {'x' * 600}" for n in range(3 * size // 600)]  # each is logged as `>> cmd: ...`
        doc, device = console(fake_device, [{"cmd": "true"}, {"cmd": lines, "timeout": "60s"}], LOGOUT)
        run = cli_run(doc, stderr=w)
        os.close(w)
        run.wait_for(lambda: FakeDevice.read(device) == IN)
        run.wait_for(full(r, size - 1024))
        assert run.signal(sig) == -sig
        assert FakeDevice.read(device) == IN
        assert b"Interrupted" not in os.read(r, 65536)
        run.quiet()
    finally:
        os.close(r)


def handled(monkeypatch: pytest.MonkeyPatch, stderr: Any, sig: signal.Signals = signal.SIGTERM) -> list[tuple]:
    """What the handler of `cli._ended_by_signal` does when it is called, with `stderr` as `sys.stderr`: its
    calls of `os.write`, `os.kill` (with the signal's action at that time) and `os._exit`, in order."""
    calls: list[tuple] = []
    saved = signal.getsignal(sig)
    monkeypatch.setattr(sys, "stderr", stderr)
    try:
        signal.signal(sig, signal.SIG_DFL)
        with cli._ended_by_signal():
            handler = signal.getsignal(sig)
            monkeypatch.setattr(os, "write", lambda fd, data: calls.append(("write", fd, data)) or len(data))
            monkeypatch.setattr(os, "kill", lambda pid, signum: calls.append(("kill", pid, signum, signal.getsignal(signum))))
            monkeypatch.setattr(os, "_exit", lambda status: calls.append(("exit", status)))
            handler(sig, None)  # type: ignore[operator, misc]
    finally:
        monkeypatch.undo()
        signal.signal(sig, saved)
    return calls


@pytest.mark.parametrize("sig", SIGNALS, ids=lambda s: s.name)
def test_p6_114_handler_writes_one_line_and_ends_from_the_signal(monkeypatch: pytest.MonkeyPatch, sig: signal.Signals):
    """The handler: one write of the line to stderr's file descriptor, the signal's default action put
    back, the signal sent to the process itself, and an exit with 128 plus its number should that not end it."""
    r, w = os.pipe()
    with os.fdopen(r, "rb"), os.fdopen(w, "w") as stderr:
        assert handled(monkeypatch, stderr, sig) == [
            ("write", w, f"{interrupted(sig)}\n".encode()),
            ("kill", os.getpid(), sig, signal.SIG_DFL),
            ("exit", 128 + sig),
        ]


def test_p6_114_handler_writes_nowhere_but_to_a_stderr_that_takes_it(monkeypatch: pytest.MonkeyPatch):
    """No line without a stderr (`2>&-`: file descriptor 2 is then whatever was opened next, the session's
    pty perhaps), with a closed one, or with one that would make the write wait: a pipe that is full."""
    end = [("kill", os.getpid(), signal.SIGTERM, signal.SIG_DFL), ("exit", 143)]
    assert handled(monkeypatch, None) == end
    closed = io.StringIO()
    closed.close()
    assert handled(monkeypatch, closed) == end
    r, w = os.pipe()
    with os.fdopen(r, "rb"), os.fdopen(w, "w") as stderr:
        os.set_blocking(w, False)
        try:
            while True:
                os.write(w, b"x" * 65536)
        except BlockingIOError:
            pass
        os.set_blocking(w, True)
        assert handled(monkeypatch, stderr) == end


@pytest.mark.parametrize("sig", SIGNALS, ids=lambda s: s.name)
def test_p6_114_ignored_signal_stays_ignored(fake_device: FakeDevice, cli_run, tmp_path: Path, sig: signal.Signals):
    """A signal that is ignored when the run starts (`nohup` ignores SIGHUP) gets no handler: the run goes
    on, completes, and logs out."""
    ready = tmp_path / "ready"
    doc, device = console(fake_device, [running(ready, 2)], LOGOUT)
    run = cli_run(doc, pre=lambda: signal.signal(sig, signal.SIG_IGN))
    run.wait_for(ready.exists)
    run.proc.send_signal(sig)
    assert run.ends(30) == 0, run.err
    assert "Interrupted" not in run.err and run.err.splitlines()[-1] == ">> run completed"
    assert FakeDevice.read(device) == [*IN, "LOGOUT="]


def test_p6_114_only_a_default_action_is_replaced_and_it_is_put_back():
    """`cli._ended_by_signal`: a signal with its default action gets the handler for the block, and its
    default action back after it. One that is ignored, or has a handler of somebody else's, is left alone."""

    def theirs(signum, frame):
        raise AssertionError("not called")

    saved = {sig: signal.getsignal(sig) for sig in SIGNALS}
    try:
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        with cli._ended_by_signal():
            assert callable(signal.getsignal(signal.SIGTERM)) and signal.getsignal(signal.SIGHUP) is signal.SIG_IGN
        assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL and signal.getsignal(signal.SIGHUP) is signal.SIG_IGN
        signal.signal(signal.SIGTERM, theirs)
        with cli._ended_by_signal():
            assert signal.getsignal(signal.SIGTERM) is theirs
        assert signal.getsignal(signal.SIGTERM) is theirs
    finally:
        for sig, handler in saved.items():
            signal.signal(sig, handler)


def test_p6_114_no_handler_outside_the_main_thread():
    """A handler can be set only in the main thread: in another one the block sets none, and raises nothing."""
    saved = {sig: signal.getsignal(sig) for sig in SIGNALS}
    seen: list[Any] = []

    def block() -> None:
        try:
            with cli._ended_by_signal():
                seen.append([signal.getsignal(sig) for sig in SIGNALS])
        except BaseException as e:  # noqa: BLE001 - whatever it is, the test reports it
            seen.append(e)

    try:
        for sig in SIGNALS:
            signal.signal(sig, signal.SIG_DFL)
        thread = threading.Thread(target=block)
        thread.start()
        thread.join()
        assert seen == [[signal.SIG_DFL, signal.SIG_DFL]]
    finally:
        for sig, handler in saved.items():
            signal.signal(sig, handler)


@pytest.mark.parametrize("sig", SIGNALS, ids=lambda s: s.name)
def test_p6_114_runner_used_as_a_library_installs_nothing(fake_device: FakeDevice, cli_run, tmp_path: Path, sig: signal.Signals):
    """`Runner.run()` by itself sets no handler: the signal does what it does to the program that uses it,
    here its default action, and nothing is printed."""
    ready = tmp_path / "ready"
    doc, device = console(fake_device, [running(ready)], LOGOUT)
    run = cli_run(doc, argv=[sys.executable, "-W", "ignore", "-c", LIBRARY])
    run.wait_for(ready.exists)
    assert run.signal(sig) == -sig
    assert "Interrupted" not in run.err
    assert make_runner([{"cmd": "true"}]).before_attach is None
    run.quiet()


@pytest.mark.parametrize("sig", SIGNALS, ids=lambda s: s.name)
def test_p6_114_prepare_deals_with_a_signal_itself(cli_run, tmp_path: Path, sig: signal.Signals):
    """SPEC "attach": the handler is set from the spawn on. While `attach.prepare` runs, SIGTERM kills the
    script's shell and removes its temp file, and SIGHUP has its default action; neither prints the line."""
    tdir, started = tmp_path / "tmp", tmp_path / "started"
    tdir.mkdir()
    doc = make_doc([], prepare=f"touch {started}\nexec sleep 20 > /dev/null 2>&1\n")
    run = cli_run(doc, env={**os.environ, "TMPDIR": str(tdir)})
    run.wait_for(started.exists)
    assert run.signal(sig) == -sig
    assert "Interrupted" not in run.err and ">> attach:" not in run.err
    if sig == signal.SIGTERM:
        assert list(tdir.iterdir()) == []
    run.quiet()


# -- P6-115: output that can't be written -----------------------------------------------------------------


def test_p6_115_head_1_ends_the_run_without_the_breakouts(fake_device: FakeDevice, cli_run, tmp_path: Path):
    """SPEC "Output": `autobot script | head -1`. The reader leaves at the first line, the next write of
    the session's output fails, and the run ends there: status 3, one report, no breakout, nothing sent
    after the failed write."""
    doc, device = console(fake_device, [{"cmd": "echo in"}, {"cmd": "echo never"}], LOGOUT)
    run = cli_run(doc, stdout=subprocess.PIPE)
    head = subprocess.Popen(["head", "-n", "1"], stdin=run.proc.stdout, stdout=subprocess.DEVNULL)
    run.proc.stdout.close()
    try:
        assert run.ends(30) == cli.EXIT_RUN == 3, run.err
    finally:
        head.kill()
        head.wait()
    lines = run.report()
    assert lines[0] == lost(run.path, "stdout", PIPE) and all(line.startswith("  at ") for line in lines[1:]) and len(lines) <= 2
    assert not any(line.startswith(">> breakout") for line in run.err.splitlines())
    assert FakeDevice.read(device) in ([], IN[:1], IN)  # where the login was when the reader left; no logout, and no more
    run.quiet()


def _reader_leaves(cli_run, fake_device: FakeDevice, tmp_path: Path, streams: str) -> tuple[Cli, list[str], Path]:
    """A run whose reader of `streams` (`stdout`, `stderr` or `both`) leaves once the run is logged in:
    the reader closes the pipe when the command that touches a file has run. The command after it takes
    a second, so the script is still running. The run, the device's log and the file of the last step."""
    inside, went_on = tmp_path / "inside", tmp_path / "went-on"
    doc, device = console(fake_device, [{"cmd": f"touch {inside}"}, {"cmd": "sleep 1; echo more"}, {"cmd": f"touch {went_on}"}], LOGOUT)
    r, w = os.pipe()
    try:
        run = cli_run(doc, **{"stdout": {"stdout": w}, "stderr": {"stderr": w}, "both": {"stdout": w, "stderr": w}}[streams])
        os.close(w)
        read_until(run, r, inside.exists)
    finally:
        os.close(r)
    assert run.ends(30) == 3, run.err
    return run, FakeDevice.read(device), went_on


def test_p6_115_reader_that_leaves_after_the_login_ends_the_run_there(fake_device: FakeDevice, cli_run, tmp_path: Path):
    """The reader leaves in the middle of the script: the step that was reading ends, the steps after it
    are not sent, the breakout doesn't run, and the device stays logged in."""
    run, device, went_on = _reader_leaves(cli_run, fake_device, tmp_path, "stdout")
    lines = run.report()
    assert lines[0] == lost(run.path, "stdout", PIPE) and lines[1].startswith("  at script.") and len(lines) == 2
    assert device == IN and not went_on.exists()
    assert not any(line.startswith(">> breakout") or "step failed" in line for line in run.err.splitlines())
    run.quiet()


@pytest.mark.parametrize("streams", ["stderr", "both"])
def test_p6_115_stderr_that_is_gone_ends_the_run_too(fake_device: FakeDevice, cli_run, tmp_path: Path, streams: str):
    """A message that can't be written ends the run like the session's output: status 3, no breakout, and
    nothing more is sent. There is nowhere to report it."""
    run, device, went_on = _reader_leaves(cli_run, fake_device, tmp_path, streams)
    assert device == IN and not went_on.exists()
    run.quiet()


@pytest.mark.skipif(not os.path.exists("/dev/full"), reason="no /dev/full")
def test_p6_115_full_disk_ends_the_run(fake_device: FakeDevice, cli_run):
    """`autobot script > /dev/full`: the first write of the session's output fails. Status 3, never
    Python's 120 for a stream it could not flush at exit."""
    doc, device = console(fake_device, [{"cmd": "echo in"}], LOGOUT)
    with open("/dev/full", "w") as full_disk:
        run = cli_run(doc, stdout=full_disk)
        assert run.ends(30) == 3, run.err
    assert run.report() == [lost(run.path, "stdout", "[Errno 28] No space left on device")]
    assert FakeDevice.read(device) == []  # the banner could not be written: nothing was answered
    run.quiet()


def test_p6_115_file_size_limit_ends_the_run(fake_device: FakeDevice, cli_run, tmp_path: Path):
    """`ulimit -f`: stdout is a file that may not grow past 300 bytes. The write that would is the end."""
    doc, device = console(fake_device, [{"cmd": "echo in"}, {"cmd": "yes | head -n 400"}, {"cmd": "echo never"}], LOGOUT)
    run = cli_run(doc, stderr=subprocess.PIPE, text=True, pre=lambda: resource.setrlimit(resource.RLIMIT_FSIZE, (300, 300)))
    err = run.proc.communicate(timeout=60)[1]
    assert run.proc.returncode == 3, err
    lines = [line for line in err.splitlines() if not line.startswith(">> ")]
    assert lines[0] == lost(run.path, "stdout", "[Errno 27] File too large") and len(lines) <= 2
    assert ">> breakout" not in err and "LOGOUT=" not in FakeDevice.read(device)
    assert not any(word in err for word in NOISE) and (tmp_path / "stdout").stat().st_size <= 300


def test_p6_115_reader_gone_at_ctrl_c_ends_the_run_at_the_breakouts_first_failed_write(fake_device: FakeDevice, cli_run, tmp_path: Path):
    """`autobot script | tee log` and Ctrl-C, which ends `tee` too. The interrupt ends the step and the
    breakout starts, as it does; its first write of the session's output fails, and that is the end:
    status 3, and no step of the breakout is sent after it."""
    ready, broke = tmp_path / "ready", tmp_path / "broke-out"
    doc, device = console(fake_device, [running(ready)], [{"control": "c"}, {"cmd": f"touch {broke}", "timeout": "5s"}, *LOGOUT])
    r, w = os.pipe()
    try:
        run = cli_run(doc, stdout=w)
        os.close(w)
        read_until(run, r, ready.exists)
    finally:
        os.close(r)
    assert run.signal(signal.SIGINT) == 3, run.err
    lines = run.err.splitlines()
    assert lines[-5:-2] == [">> step interrupted", ">> breakout: detaching", ">> control sent: ^C"]
    assert lines[-2] == lost(run.path, "stdout", PIPE) and lines[-1].startswith("  at attach.breakout.1 (cmd: touch ")
    assert not broke.exists() and FakeDevice.read(device) == IN
    run.quiet()


@pytest.mark.skipif(not os.path.exists("/dev/full"), reason="no /dev/full")
def test_p6_115_message_that_cannot_be_written_ends_any_command(tmp_path: Path):
    """`autobot validate` with a stderr that takes nothing: status 3, and nothing of the interpreter's."""
    path = tmp_path / "script.autobot.yaml"
    path.write_text(yaml.safe_dump(make_doc([{"cmd": "true"}])))
    with open("/dev/full", "w") as full_disk:
        res = subprocess.run([sys.executable, "-m", "autobot.cli", "validate", str(path)], stdout=subprocess.PIPE, stderr=full_disk, text=True, check=False, timeout=60)
    assert res.returncode == 3 and res.stdout == ""


class Breaking(io.StringIO):
    """A stream that fails from the write that has `mark` in it."""

    def __init__(self, error: BaseException | None, mark: str = "NOW"):
        super().__init__()
        self.error, self.mark, self.broken, self.calls = error, mark, False, 0

    def write(self, data: str) -> int:
        self.broken = self.broken or self.mark in data
        if self.broken:
            self.calls += 1
            if self.error is None:
                self.close()
            else:
                raise self.error
        return super().write(data)


NOW = {"cmd": "echo N''OW"}
BLOCK = {"block": {"name": "b", "script": [NOW], "breakout": [{"cmd": "echo block-bye"}]}}
# case -> the script, and the message on stderr whose write fails when stderr is the stream that fails
LOST = {
    "script": ([NOW, {"cmd": "echo never"}], ">> cmd: echo N"),
    "block": ([BLOCK, {"cmd": "echo never"}], ">>   cmd: echo N"),
    "block-in-a-block": ([{"block": {"name": "outer", "script": [BLOCK], "breakout": [{"cmd": "echo outer-bye"}]}}], ">>     cmd: echo N"),
    "embedded-script": ([{"cmd": "#!/bin/sh\necho N''OW\n"}], ">> script: executing"),
}


@pytest.mark.parametrize("case", LOST)
@pytest.mark.parametrize("stream", ["stdout", "stderr"])
@pytest.mark.parametrize("error", [BrokenPipeError(errno.EPIPE, "Broken pipe"), OSError(errno.EIO, "Input/output error"), None], ids=["EPIPE", "EIO", "closed"])
def test_p6_115_run_raises_output_lost_and_sends_nothing_more(sent: SentLog, children, monkeypatch: pytest.MonkeyPatch, case: str, stream: str, error: BaseException | None):
    """`Runner.run()` raises `log.OutputLost`, no `Exception`, once the session is closed, whether it is
    the session's output or a message that could not be written. Whatever would run on the way out sends
    nothing: not `attach.breakout`, not a block's breakout, not the cleanup of an embedded script."""
    script, message = LOST[case]
    monkeypatch.setattr(sys, stream, Breaking(error, "NOW" if stream == "stdout" else message))
    why = r"I/O operation on closed file\.?" if error is None else rf"\[Errno {error.errno}\] {error.strerror}"
    runner = make_runner([{"cmd": "true"}, *script], breakout=[{"cmd": "echo bye"}])
    with pytest.raises(log.OutputLost, match=f"^cannot write {stream}: {why}$") as ei:
        runner.run()
    monkeypatch.undo()
    assert not isinstance(ei.value, Exception)
    assert not any("bye" in line or "never" in line or line.startswith("rm -f") for line in sent.lines()), sent
    assert sent.controls() == []
    assert len(children) == 1 and not children[0].isalive() and runner.session._cld is None


def test_p6_115_writer_fails_once_and_stays_failed():
    """`CleanWriter`: after a write that failed, no write is tried again, a later one raises the same
    `OutputLost`, and the close at the end of the session raises nothing."""
    stream = Breaking(BrokenPipeError(errno.EPIPE, "Broken pipe"))
    writer = CleanWriter(stream)
    writer.write("fine\n")
    with pytest.raises(log.OutputLost) as first:
        writer.write("NOW\n")
    with pytest.raises(log.OutputLost) as second:
        writer.write("more\n")
    with pytest.raises(log.OutputLost):
        writer.flush()
    writer.close()
    assert second.value is first.value and stream.calls == 1 and stream.getvalue() == "fine\n"


def test_p6_115_last_write_of_a_session_that_fails(monkeypatch: pytest.MonkeyPatch, capfd, tmp_path: Path):
    """`Session.detach` writes what the echo still holds. If that fails, the close raises `OutputLost`;
    with `failing`, while another error is on its way, it is logged like any close that fails."""
    for failing in (False, True):
        out = open(tmp_path / "out", "w")
        monkeypatch.setattr(sys, "stdout", out)
        s = Session([PromptHandler("sh", [r"PROMPT\$ "], [], True)])
        s.attach(BASH, timeout=5)
        cld = s._cld
        out.close()
        if failing:
            s.detach(failing=True)
            assert ">> close error (OutputLost): cannot write stdout: I/O operation on closed file" in capfd.readouterr().err
        else:
            with pytest.raises(log.OutputLost, match="^cannot write stdout: I/O operation on closed file"):
                s.detach()
        assert s._cld is None and cld is not None and not cld.isalive()
        monkeypatch.undo()


def test_p6_115_what_a_failed_write_is(monkeypatch: pytest.MonkeyPatch):
    """`log.writing`: any error of the operating system and a closed stream are `OutputLost`, named for
    the stream; a `ValueError` of an open stream is a bug and stays what it is."""
    stream = io.StringIO()
    for error, text in [(OSError(errno.ENOSPC, "No space left on device"), "[Errno 28] No space left on device"), (BlockingIOError(errno.EAGAIN, "x"), "[Errno 11] x")]:
        with pytest.raises(log.OutputLost, match="^cannot write the output: " + text.replace("[", r"\[").replace("]", r"\]")) as ei, log.writing(stream):
            raise error
        assert ei.value.__cause__ is error
    with pytest.raises(ValueError, match="^a bug$"), log.writing(stream):
        raise ValueError("a bug")
    stream.close()
    with pytest.raises(log.OutputLost, match="^cannot write the output: I/O operation on closed file"), log.writing(stream):
        stream.write("x")
    monkeypatch.setattr(sys, "stderr", stream)
    with pytest.raises(log.OutputLost, match="^cannot write stderr: "):
        log.say("x")


@pytest.mark.parametrize("error", [BrokenPipeError(errno.EPIPE, "Broken pipe"), OSError(errno.ENOSPC, "No space left on device")], ids=["EPIPE", "ENOSPC"])
def test_p6_115_message_that_cannot_be_written_is_output_lost(monkeypatch: pytest.MonkeyPatch, error: OSError):
    """A message is rendered by rich and written by `log`: a failed write is `OutputLost`, never the
    `SystemExit` rich raises for a broken pipe, and the message is not kept for the next one."""

    class Stderr(io.StringIO):
        def write(self, data: str) -> int:
            raise error

    monkeypatch.setattr(sys, "stderr", Stderr())
    with pytest.raises(log.OutputLost, match=r"^cannot write stderr: \[Errno "):
        log.say("one")
    out = io.StringIO()
    monkeypatch.setattr(sys, "stderr", out)
    log.say("two")
    assert out.getvalue() == ">> two\n"


def test_p6_115_no_stderr_is_no_error(monkeypatch: pytest.MonkeyPatch):
    """With file descriptor 2 closed there is no `sys.stderr`: a message goes nowhere, and nothing fails."""
    monkeypatch.setattr(sys, "stderr", None)
    log.say("x")
    log.error("Run failed", "y")


# -- P6-116: Ctrl-C ---------------------------------------------------------------------------------------


def test_p6_116_ctrl_c_runs_the_breakouts_and_ends_from_sigint(fake_device: FakeDevice, cli_run, tmp_path: Path):
    """SPEC "Errors while the script runs": one Ctrl-C ends the step, the breakout logs out, the session
    is closed, `Interrupted` is printed with the step, and the process ends from SIGINT."""
    ready = tmp_path / "ready"
    doc, device = console(fake_device, [running(ready)], LOGOUT)
    run = cli_run(doc)
    run.wait_for(ready.exists)
    assert run.signal(signal.SIGINT) == -signal.SIGINT, run.err
    lines = run.report()
    assert lines[0] == "Interrupted" and lines[1].startswith("  at script.0 (cmd: sh -c ") and len(lines) == 2
    progress = [line for line in run.err.splitlines() if line.startswith(">> ")]
    at = progress.index(">> step interrupted")
    assert progress[at + 1 : at + 4] == [">> breakout: detaching", ">> control sent: ^C", ">> line sent"]
    assert FakeDevice.read(device) == [*IN, "LOGOUT="]
    run.quiet()


def test_p6_116_second_ctrl_c_ends_the_breakout(fake_device: FakeDevice, cli_run, tmp_path: Path):
    """A second Ctrl-C while the breakout runs ends that breakout: the session is closed, the report
    names the breakout's step, and says that the session may be left logged in."""
    ready, began = tmp_path / "ready", tmp_path / "began"
    doc, device = console(fake_device, [running(ready)], [{"control": "c"}, running(began), *LOGOUT])
    run = cli_run(doc)
    run.wait_for(ready.exists)
    run.proc.send_signal(signal.SIGINT)
    run.wait_for(began.exists)
    assert run.signal(signal.SIGINT) == -signal.SIGINT, run.err
    lines = run.report()
    assert lines[0] == "Interrupted" and lines[1].startswith("  at attach.breakout.1 (cmd: sh -c ")
    assert lines[2:] == [f"{LEFT}a breakout did not finish{SENT}"]
    assert FakeDevice.read(device) == IN
    run.quiet()


def test_p6_116_third_ctrl_c_ends_the_next_breakout(fake_device: FakeDevice, cli_run, tmp_path: Path):
    """Three: in the script, in a block's breakout, in `attach.breakout`. Each ends what is running, the
    session is closed, the process ends from SIGINT, and both breakouts are reported."""
    ready, inner, outer = tmp_path / "ready", tmp_path / "inner", tmp_path / "outer"
    block = {"name": "b", "script": [running(ready)], "breakout": [{"control": "c"}, running(inner)]}
    doc, device = console(fake_device, [{"block": block}], [{"control": "c"}, running(outer), *LOGOUT])
    run = cli_run(doc)
    for mark in (ready, inner):
        run.wait_for(mark.exists)
        run.proc.send_signal(signal.SIGINT)
    run.wait_for(outer.exists)
    assert run.signal(signal.SIGINT) == -signal.SIGINT, run.err
    lines = run.report()
    assert lines[0] == "Interrupted" and lines[1].startswith("  at attach.breakout.1 (cmd: sh -c ")
    assert lines[2] == f"Breakout failed in {run.path}: interrupted" and lines[3].startswith("  at script.0.block.breakout.1 (cmd: sh -c ")
    assert lines[4:] == [f"{LEFT}a breakout did not finish{SENT}"]
    assert FakeDevice.read(device) == IN
    run.quiet()


def test_p6_116_ctrl_c_always_ends_a_run_whose_stdout_nobody_reads(fake_device: FakeDevice, cli_run, tmp_path: Path):
    """A reader that holds the pipe and doesn't read. Each Ctrl-C ends the write the run waits in, and
    what follows waits in the next one: Ctrl-C pressed again and again ends the process, from SIGINT.
    No signal is ever held back."""
    ready = tmp_path / "ready"
    r, w = os.pipe()
    try:
        doc, _ = console(fake_device, [{"cmd": "true"}, {"cmd": f"touch {ready}; yes | head -n 100000", "timeout": "60s"}], LOGOUT)
        run = cli_run(doc, stdout=w)
        os.close(w)
        run.wait_for(ready.exists)
        run.wait_for(full(r, fcntl.fcntl(r, fcntl.F_GETPIPE_SZ) - 8192))
        for _ in range(40):
            run.proc.send_signal(signal.SIGINT)
            try:
                run.proc.wait(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                pass
        assert run.proc.poll() == -signal.SIGINT, run.err
        assert "Interrupted" in run.report()
    finally:
        os.close(r)
