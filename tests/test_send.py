"""Sending a line to the session: bounded by a timeout, whatever its length (P8-36 to P8-42)."""

from __future__ import annotations

import fcntl
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import pexpect
import pytest
from conftest import BASH, SentLog, make_doc, make_runner, run_cli
from conftest import run_vars as run

from autobot.session import PromptHandler, Session, SimpleHandler

TYPED = Path(__file__).resolve().parent / "fakes" / "typed.py"
# more than a pty holds for a child that has stopped reading
LONG = 200_000
SHELL = [PromptHandler("sh", [r"PROMPT\$ "], [], True)]
READLINE = f"env INPUTRC=/dev/null TERM=vt100 LC_ALL=C {BASH}"


def typed(*opts: object) -> str:
    """F8: a child that reads its terminal itself and echoes what it reads."""
    return " ".join([sys.executable, str(TYPED), *map(str, opts)])


@pytest.fixture
def attach():
    """Factory: a session attached to `spawn`, at its first prompt."""
    sessions: list[Session] = []

    def factory(spawn: str, handlers: list[PromptHandler] | None = None, prompt: bool = True) -> Session:
        s = Session(SHELL if handlers is None else handlers)
        sessions.append(s)
        s.attach(spawn, timeout=5)
        if prompt:
            s.get_prompt(timeout=5)
        return s

    yield factory
    for s in sessions:
        s.detach(failing=True)


def blocking(s: Session) -> bool:
    assert s._cld is not None
    return not fcntl.fcntl(s._cld.child_fd, fcntl.F_GETFL) & os.O_NONBLOCK


def sent_bytes(error: BaseException, total: int) -> int:
    m = re.search(rf"\((\d+) of {total} bytes sent\)$", str(error))
    assert m, str(error)
    return int(m.group(1))


# -- P8-36: a send ends at its timeout ----------------------------------------------------------------


def test_p8_36_send_to_a_child_that_stops_reading_times_out(attach):
    """SPEC "The length of a sent line": the child reads 1000 bytes and no more. The pty takes what it
    holds, and the send ends at its timeout with the session's `TimeoutError`."""
    s = attach(typed("--stop", 1000))
    started = time.monotonic()
    with pytest.raises(TimeoutError, match=r"^timed out after 2s while sending a line \(\d+ of 200001 bytes sent\)$") as ei:
        s.sendline("x" * LONG, timeout=2)
    assert 2 <= time.monotonic() - started < 3.5
    assert 1000 <= sent_bytes(ei.value, LONG + 1) < LONG
    assert blocking(s)


def test_p8_36_control_character_is_bounded_too(attach):
    s = attach(typed("--stop", 1000))
    with pytest.raises(TimeoutError):
        s.sendline("x" * LONG, timeout=0.5)
    started = time.monotonic()
    with pytest.raises(TimeoutError, match=r"^timed out after 0\.5s while sending a control character \(0 of 1 bytes sent\)$"):
        s.sendcontrol("c", timeout=0.5)
    assert 0.5 <= time.monotonic() - started < 2


def test_p8_36_answer_to_a_prompt_has_what_is_left_of_the_wait(attach):
    """A prompt's answer is sent inside a prompt wait: it ends when the wait does, and the error names
    the wait's timeout."""
    answer = SimpleHandler("ask", [r"PROMPT\$ "], "y" * LONG, lambda text: text)
    s = attach(typed("--stop", 1000), [answer], prompt=False)
    started = time.monotonic()
    with pytest.raises(TimeoutError, match=r"^timed out after 2s while sending a line \(\d+ of 200001 bytes sent\)$"):
        s.get_prompt(timeout=2)
    assert 2 <= time.monotonic() - started < 3.5


def test_p8_36_cli_reports_a_failed_run_at_the_step(tmp_path: Path):
    """SPEC "Errors while the script runs": a timeout of the session, with the step it happened in."""
    doc = make_doc([{"cmd": "echo " + "x" * LONG, "timeout": "2s"}], spawn=typed("--stop", 1000))
    res = run_cli(doc, tmp_path)
    assert res.returncode == 3, res.stderr
    report = [line for line in res.stderr.splitlines() if not line.startswith(">> ") and "RuntimeWarning" not in line]
    assert re.fullmatch(
        rf"Run failed in {re.escape(str(tmp_path / 'script.autobot.yaml'))}: "
        r"timed out after 2\.0s while sending a line \(\d+ of 200006 bytes sent\)",
        report[0],
    )
    assert report[1] == f"  at script.0 (cmd: echo {'x' * 67}...)"
    assert ">> step failed (TimeoutError): timed out after 2.0s while sending a line" in res.stderr
    assert "Traceback" not in res.stderr


def test_p8_36_one_blocking_write_to_that_child_never_returns():
    """A guard for the tests above: the child they use does stall a writer that writes the line in one
    blocking write and reads nothing, as `pexpect.spawn.sendline` does. Killed from outside."""
    code = (
        "import pexpect, sys\n"
        f"c = pexpect.spawn({typed('--stop', 1000)!r}, timeout=5)\n"
        "c.expect('PROMPT')\n"
        "print('sending', flush=True)\n"
        f"c.sendline('x' * {LONG})\n"
        "print('sent', flush=True)\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            proc.wait(timeout=3)
    finally:
        proc.kill()
    assert proc.communicate()[0] == "sending\n"


# -- P8-37: what is read while a line is sent ---------------------------------------------------------


@pytest.mark.parametrize("gain", [1, 3])
def test_p8_37_output_read_during_a_send_is_read_once_and_in_order(attach, capsys, gain: int):
    """The child writes `ESC[1m` and the byte, `gain` times, for each byte it reads, and stops reading
    while nobody reads that. What is read during the send is the start of what the next wait reads, and of
    the operator echo, with nothing lost, repeated or out of order, and no escape sequence cut in two."""
    s = attach(typed("--gain", gain))
    assert s._cld is not None
    reads: list[int] = []
    expect = s._cld.expect

    def counted(pattern, *args, **kwargs):
        if pattern is pexpect.TIMEOUT:
            reads.append(len(s._cld.buffer))
        return expect(pattern, *args, **kwargs)

    s._cld.expect = counted
    line = "".join(f"{i:05d}," for i in range(10000))
    capsys.readouterr()
    s.sendline(line, timeout=30)
    assert reads, "the send read nothing: the line was no longer than the pty holds"
    assert blocking(s)
    during = s._cld.buffer
    s.expect([r"\r\nDONE (\d+)\r\n"], timeout=30)
    raw = "".join("\x1b[1m" + ch * gain for ch in line)
    assert s.ctx["before"] == raw
    assert raw.startswith(during) and during
    assert s.ctx["match"] == "\r\nDONE 60000\r\n"
    s.detach()
    assert capsys.readouterr().out == "".join(ch * gain for ch in line) + "\r\nDONE 60000\r\nPROMPT$ "


def test_p8_37_prompt_wait_after_a_long_send_captures_the_output(attach):
    """The same through a prompt wait: the echo that was read during the send is read as the echo of
    the line, and the capture is what follows it."""
    s = attach(typed())
    line = "".join(f"{i:05d}," for i in range(10000))
    s.sendline(line, timeout=30)
    assert s.get_prompt(timeout=30) == "DONE 60000\n"


# -- P8-38: a long line to a line editor on a terminal that wraps -------------------------------------


def _long_sends(length: int, runs: int, timeout: float = 20) -> tuple[int, int, float]:
    """Send a line of `length` characters to readline `runs` times, each to a new shell: how many were
    sent, how many timed out, and the longest send."""
    env = {"TERM": "vt100", "LC_ALL": "C", "INPUTRC": "/dev/null", "PS1": "PROMPT$ ", "PATH": os.environ["PATH"]}
    whole = timed_out = 0
    longest = 0.0
    for _ in range(runs):
        s = Session(SHELL)
        s.attach(BASH, env=dict(env), timeout=10)
        try:
            s.get_prompt(timeout=10)
            started = time.monotonic()
            try:
                s.sendline("echo " + "x" * (length - 5), timeout=timeout)
                whole += 1
            except TimeoutError as e:
                assert "while sending a line" in str(e)
                timed_out += 1
            longest = max(longest, time.monotonic() - started)
        finally:
            s.detach(failing=True)
    return whole, timed_out, longest


@pytest.mark.parametrize("length", [60000, 100000])
def test_p8_38_long_line_to_readline_does_not_block(length: int, capsys):
    """SPEC "The length of a sent line": readline echoes while it reads, and one blocking write of a line
    this long sometimes never returned. Each send ends, sent or timed out, within its timeout."""
    whole, timed_out, longest = _long_sends(length, 3)
    assert whole + timed_out == 3 and longest < 22


@pytest.mark.slow
@pytest.mark.parametrize(("length", "runs"), [(60000, 40), (100000, 25), (38000, 25)])
def test_p8_38_long_line_to_readline_does_not_block_in_many_runs(length: int, runs: int, capsys):
    whole, timed_out, longest = _long_sends(length, runs)
    assert whole + timed_out == runs and longest < 22


@pytest.mark.parametrize("length", [30000, 36000, 39000])
def test_p8_38_long_line_within_the_window_registers_its_output(length: int):
    """Lines of 36,000 characters and more were among those that blocked: sent whole, within the window,
    they are echoed once and their output is captured."""
    cmd = "echo " + "".join(f"w{i:05d} " for i in range(length))[: length - 5].rstrip().ljust(length - 5, "x")
    out = run(
        [{"cmd": cmd, "register": "out", "timeout": "60s"}, {"cmd": "echo done", "register": "after"}],
        spawn=READLINE,
    )
    assert out == {"out": cmd[5:], "after": "done"}


def test_p8_38_long_line_on_a_plain_terminal_registers_its_output():
    """With `TERM=dumb` readline shows only the end of the line: one of 100,000 characters is captured."""
    cmd = "echo " + "".join(f"w{i:05d} " for i in range(20000))[:99995].rstrip()
    out = run([{"cmd": cmd, "register": "out", "timeout": "60s"}, {"cmd": "echo done", "register": "after"}])
    assert out == {"out": cmd[5:], "after": "done"}


# -- P8-39: an ordinary send --------------------------------------------------------------------------


def test_p8_39_ordinary_send_is_one_write_after_the_pause(monkeypatch: pytest.MonkeyPatch, sent: SentLog):
    """A line the pty takes at once is written as `pexpect.spawn.sendline` writes it: after the pause of
    `delaybeforesend`, the line and its line break in one write, and nothing is read to get it out. A
    control character is one byte, with no pause."""
    events: list[tuple] = []
    write, sleep, put_line = os.write, time.sleep, Session._put_line
    fds: set[int] = set()

    def put(self, line, *args, **kwargs):
        fds.add(self._cld.child_fd)
        orig = self._cld.expect

        def expect(pattern, *a, **kw):
            if pattern is pexpect.TIMEOUT and kw.get("timeout") == 0:
                events.append(("read",))
            return orig(pattern, *a, **kw)

        self._cld.expect = expect
        try:
            return put_line(self, line, *args, **kwargs)
        finally:
            del self._cld.expect

    def os_write(fd, data):
        n = write(fd, data)
        if fd in fds:
            events.append(("write", bytes(data), n))
        return n

    def time_sleep(seconds):
        if seconds == 0.05:
            events.append(("pause",))
        sleep(seconds)

    monkeypatch.setattr(Session, "_put_line", put)
    monkeypatch.setattr(os, "write", os_write)
    monkeypatch.setattr(time, "sleep", time_sleep)
    script = [
        {"cmd": "echo one", "register": "one"},
        {"cmd": ["echo two", "echo thrée"], "register": "two"},
        {"cmd": "#!/bin/sh\necho script\n", "register": "script"},
        {"line": "sleep 30"},
        {"sleep": "200ms"},
        {"control": "c"},
        {"return": 2},
    ]
    out = run(script)
    assert out == {"one": "one", "two": "two\nthrée", "script": "script"}
    lines = [e for e in sent if e[0] == "line"]
    assert ("read",) not in events
    writes = [e for e in events if e[0] == "write"]
    assert all(n == len(data) for _, data, n in writes)
    expected = []
    for kind, value in sent:
        expected += [("pause",), ("write", value.encode() + b"\n")] if kind == "line" else [("write", b"\x03")]
    assert [e[:2] for e in events] == expected
    assert len(lines) > 10 and b"echo thr\xc3\xa9e\n" in [w[1] for w in writes]


@pytest.mark.parametrize(
    ("key", "byte"),
    [("a", 1), ("C", 3), ("z", 26), ("@", 0), ("`", 0), ("[", 27), ("{", 27), ("\\", 28), ("|", 28),
     ("]", 29), ("}", 29), ("^", 30), ("~", 30), ("_", 31), ("?", 127)],
)  # fmt: skip
def test_p8_39_control_characters_are_those_pexpect_sends(key: str, byte: int):
    """The session writes the control character itself; the keys and their bytes are ptyprocess's."""
    from autobot.session import control_byte

    assert control_byte(key) == bytes([byte])
    r, w = os.pipe()
    try:
        class Proc:
            fileobj = os.fdopen(w, "wb", buffering=0, closefd=False)

            def _writeb(self, b, flush=True):
                return self.fileobj.write(b)

        from ptyprocess import PtyProcess

        PtyProcess.sendcontrol(Proc(), key)  # type: ignore[arg-type]
        assert os.read(r, 8) == bytes([byte])
    finally:
        os.close(r)
        os.close(w)


# -- P8-40: the connection closes while a line is sent ------------------------------------------------


def test_p8_40_child_that_exits_during_a_send_is_a_closed_connection(attach):
    s = attach(typed("--exit", 1000))
    started = time.monotonic()
    with pytest.raises(EOFError, match=r"^connection closed while sending a line$"):
        s.sendline("x" * LONG, timeout=10)
    assert time.monotonic() - started < 5


def test_p8_40_line_the_pty_takes_after_the_child_exited_fails_at_the_wait(attach):
    """A guard: a pty takes a line of ordinary length though nobody is left to read it (on Linux), so
    the send returns as it always did and the wait that follows reports the closed connection."""
    s = attach(typed("--exit", 1))
    s.sendline("x", timeout=5)
    assert s._cld is not None
    s._cld.expect(pexpect.EOF, timeout=5)
    try:
        s.sendline("true", timeout=5)
    except EOFError as e:
        assert str(e) == "connection closed while sending a line"
        return
    with pytest.raises(EOFError, match=r"^connection closed while waiting for a shell prompt \('sh'\)$"):
        s.get_prompt(timeout=5)


def test_p8_40_cli_reports_a_closed_connection(tmp_path: Path):
    doc = make_doc([{"line": "x" * LONG}], spawn=typed("--exit", 1000))
    res = run_cli(doc, tmp_path)
    assert res.returncode == 3, res.stderr
    report = [line for line in res.stderr.splitlines() if not line.startswith(">> ") and "RuntimeWarning" not in line]
    assert report == [
        f"Run failed in {tmp_path / 'script.autobot.yaml'}: connection closed while sending a line",
        "  at script.0 (line)",
    ]


# -- P8-41: an interrupt while a line is sent ---------------------------------------------------------


def test_p8_41_interrupt_during_a_send_ends_it_and_the_breakout_runs(capsys):
    """Ctrl-C while the send waits for the child: the step is interrupted at once and the breakout runs
    (its own send to the child, which still reads nothing, ends at its timeout)."""
    runner = make_runner(
        [{"line": "x" * LONG}],
        spawn=typed("--stop", 1000),
        breakout=[{"control": "c", "timeout": "1s"}],
    )
    # no thread: the spawn forks
    alarm = signal.signal(signal.SIGALRM, lambda *_: signal.raise_signal(signal.SIGINT))
    started = time.monotonic()
    signal.setitimer(signal.ITIMER_REAL, 1.0)
    try:
        with pytest.raises(KeyboardInterrupt):
            runner.run()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, alarm)
    assert 1 <= time.monotonic() - started < 4
    err = capsys.readouterr().err
    assert ">> step interrupted\n" in err
    assert "breakout error (TimeoutError): timed out after 1.0s while sending a control character (0 of 1 bytes sent)" in err
    assert runner.session._cld is None


# -- P8-42: after a send that timed out ---------------------------------------------------------------


def _timed_out_run(tmp_path: Path, breakout: list[dict]) -> tuple[int, list[str]]:
    """A `cmd` whose send times out on a child that reads again 2 s later, then `breakout`: the bytes
    that were sent, and what the child logged."""
    log = tmp_path / "typed.log"
    runner = make_runner(
        [{"cmd": "echo " + "x" * LONG, "timeout": "1s"}],
        spawn=typed("--stop", 1000, "--pause", 2, "--log", log),
        breakout=breakout,
    )
    with pytest.raises(TimeoutError, match="while sending a line") as ei:
        runner.run()
    return sent_bytes(ei.value, LONG + 6), log.read_text().splitlines()


def test_p8_42_breakout_that_starts_with_an_interrupt_drops_the_cut_line(tmp_path: Path):
    """SPEC "The length of a sent line": what was sent is on the child's input line. A `control: c` waits
    until the child reads again and drops it; the line after it is a line of its own."""
    done, log = _timed_out_run(tmp_path, [{"control": "c", "timeout": "10s"}, {"line": "exit"}, {"sleep": "300ms"}])
    assert 1000 <= done < LONG
    assert log == [f"INTERRUPT={done}", "LINE=exit"]


def test_p8_42_line_sent_after_the_cut_line_is_added_to_it(tmp_path: Path):
    """Without the interrupt, the breakout's line goes after what was sent, as one line."""
    done, log = _timed_out_run(tmp_path, [{"line": "exit"}, {"sleep": "300ms"}])
    assert log == [f"LINE={done + 4} bytes ending xxxxexit"]


def test_p8_42_session_after_a_failed_send(attach):
    """The session is at no prompt and after no line, and its next prompt wait would press no Return."""
    s = attach(typed("--stop", 1000))
    with pytest.raises(TimeoutError):
        s.sendline("x" * LONG, timeout=0.5)
    assert (s._at_prompt, s._sent, s._solicit) == (False, None, False)


@pytest.mark.slow
def test_p8_42_prompt_wait_after_a_failed_send_presses_no_return(attach, tmp_path: Path, sent: SentLog):
    """A Return would enter the part of the line that was sent: the wait that follows a failed send goes
    through its idle poll without one, and times out."""
    log = tmp_path / "typed.log"
    s = attach(typed("--stop", 1000, "--pause", 1, "--log", log))
    with pytest.raises(TimeoutError):
        s.sendline("x" * LONG, timeout=0.5)
    sent.clear()
    with pytest.raises(TimeoutError, match="waiting for a shell prompt"):
        s.get_prompt(timeout=7)
    assert sent == [] and not log.exists()
    # anything sent after it is a send like any other
    s.sendcontrol("c", timeout=5)
    s.get_prompt(timeout=5)
    assert log.read_text().startswith("INTERRUPT=")
