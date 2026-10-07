"""Sending a line to the session: bounded by a timeout, whatever its length (P8-36 to P8-42), and
refused when the terminal it is typed on would cut it (P8-43 to P8-47)."""

from __future__ import annotations

import fcntl
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pexpect
import pytest
from conftest import BASH, SentLog, make_doc, make_runner, run_cli
from conftest import run_vars as run

import autobot.session as session_mod
from autobot.session import LineTooLong, PartialLine, PromptHandler, Session, SimpleHandler, canon_limit

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


PARTIAL = (
    "line not sent: part of a line that could not be sent whole is typed at the far side, and a Return "
    "would enter it; send a control character that drops it first (control: c)"
)


def test_p8_42_line_sent_after_the_cut_line_is_refused(tmp_path: Path, capsys):
    """Without the interrupt, the breakout's line would go after what was sent and enter the two as one
    line: it is not sent, and the child, reading again, gets no line at all."""
    log = tmp_path / "typed.log"
    runner = make_runner(
        [{"cmd": "echo " + "x" * LONG, "timeout": "1s"}],
        spawn=typed("--stop", 1000, "--pause", 2, "--log", log),
        breakout=[{"line": "exit"}, {"sleep": "2500ms"}],
    )
    with pytest.raises(TimeoutError, match="while sending a line"):
        runner.run()
    assert not log.exists()
    assert f">> breakout error (PartialLine): {PARTIAL}\n" in capsys.readouterr().err


def test_p8_42_session_after_a_failed_send(attach):
    """The session is at no prompt and after no line, and its next prompt wait would press no Return."""
    s = attach(typed("--stop", 1000))
    with pytest.raises(TimeoutError):
        s.sendline("x" * LONG, timeout=0.5)
    assert (s._at_prompt, s._sent, s._solicit, s._partial) == (False, None, False, True)


def test_p8_42_send_that_wrote_nothing_leaves_no_partial_line(attach):
    """A control character that the pty didn't take is not part of a line."""
    s = attach(typed("--stop", 0))
    assert s._partial is False
    s.sendline("ls", timeout=5)
    assert s._partial is False


# -- P8-48: while part of a line is typed at the far side ---------------------------------------------


def _partial(attach, tmp_path: Path, *opts: object, handlers=None) -> tuple[Session, Path]:
    """A session whose long line timed out on a child that reads again after 1 s (or what `opts` say)."""
    log = tmp_path / "typed.log"
    s = attach(typed("--stop", 1000, *(opts or ("--pause", 1)), "--log", log), handlers)
    with pytest.raises(TimeoutError, match="while sending a line"):
        s.sendline("rm -rf /important/" + "x" * LONG, timeout=0.5)
    return s, log


@pytest.mark.slow
def test_p8_48_no_prompt_wait_presses_return_however_many_follow(attach, tmp_path: Path, sent: SentLog):
    """SPEC "The length of a sent line": the first wait after the failed send, and the second and the
    third, each go through their idle poll and send nothing: a Return would run the cut command."""
    s, log = _partial(attach, tmp_path)
    sent.clear()
    for _ in range(3):
        with pytest.raises(TimeoutError, match="waiting for a shell prompt"):
            s.get_prompt(timeout=6)
        assert s._partial is True
    assert sent == [] and not log.exists()


@pytest.mark.slow
def test_p8_48_breakouts_that_start_with_a_cmd_enter_nothing(tmp_path: Path, capsys):
    """The run of the report: the failed `cmd` is in a block whose breakout starts with a `cmd`, and so
    does `attach.breakout`. Neither wait presses Return, and the child never gets a line."""
    log = tmp_path / "typed.log"
    block = {
        "name": "b",
        "script": [{"cmd": "rm -rf /important/" + "x" * LONG, "timeout": "1s"}],
        "breakout": [{"cmd": "echo block", "timeout": "6s"}],
    }
    runner = make_runner(
        [{"block": block}],
        spawn=typed("--stop", 1000, "--pause", 4, "--log", log),
        breakout=[{"cmd": "echo attach", "timeout": "6s"}],
    )
    with pytest.raises(TimeoutError, match="while sending a line"):
        runner.run()
    assert not log.exists()
    err = capsys.readouterr().err
    assert err.count("breakout error (TimeoutError): timed out after 6.0s waiting for a shell prompt ('sh')") == 2


def test_p8_48_every_line_is_refused_until_a_control_character(attach, tmp_path: Path, sent: SentLog):
    """A line, an empty one (`return`) and the `$?` check would each end with the Return that enters the
    cut line. They are refused, and the session stays as it is."""
    s, log = _partial(attach, tmp_path)
    sent.clear()
    for send in (lambda: s.sendline("exit", timeout=5), lambda: s.sendline("", solicit=True, timeout=5), lambda: s.check_rc(timeout=5)):
        with pytest.raises(PartialLine) as ei:
            send()
        assert str(ei.value) == PARTIAL
        assert (s._at_prompt, s._sent, s._solicit, s._partial) == (False, None, False, True)
    time.sleep(1.2)  # the child reads again
    assert sent == [] and not log.exists()


def test_p8_48_control_character_ends_it(attach, tmp_path: Path):
    """After a `control: c` the cut line is the far side's to drop, and lines are sent again."""
    s, log = _partial(attach, tmp_path)
    s.sendcontrol("c", timeout=5)
    assert s._partial is False
    s.sendline("exit", timeout=5)
    s.get_prompt(timeout=5)
    lines = log.read_text().splitlines()
    assert lines[0].startswith("INTERRUPT=") and lines[1:] == ["LINE=exit"]


def test_p8_48_control_character_that_is_not_sent_does_not_end_it(attach, tmp_path: Path):
    s, _ = _partial(attach, tmp_path, "--pause", 100)
    with pytest.raises(TimeoutError, match="while sending a control character"):
        s.sendcontrol("c", timeout=0.3)
    assert s._partial is True


def test_p8_48_answer_to_a_prompt_is_not_sent(attach, tmp_path: Path, sent: SentLog):
    """A prompt that shows while the cut line is there (here the echo of the cut line matches one) is not answered:
    the answer would be added to the line. The wait fails and names the prompt."""
    ask = SimpleHandler("ask", [r"x"], "yes", lambda text: text)
    s, log = _partial(attach, tmp_path, handlers=[*SHELL, ask])
    sent.clear()
    with pytest.raises(PartialLine) as ei:
        s.get_prompt(timeout=5)
    assert str(ei.value) == f"prompt 'ask': {PARTIAL}"
    time.sleep(1.2)
    assert sent == [] and not log.exists() and s._partial is True


def test_p8_48_new_child_has_no_partial_line(attach, tmp_path: Path):
    s, _ = _partial(attach, tmp_path)
    s.detach(failing=True)
    assert s._partial is False


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


# -- P8-43: a line the terminal would cut is not sent -------------------------------------------------

NOEDIT = "bash --norc --noprofile --noediting -i"
CANON = 4095  # the bytes of a line, without its line break, that the Linux terminal takes in canonical mode
linux = pytest.mark.skipif(sys.platform != "linux", reason="the limit is the Linux terminal's")
# shells that leave the line to the terminal
CANONICAL = [NOEDIT, pytest.param("dash -i", marks=pytest.mark.skipif(not shutil.which("dash"), reason="no dash"))]
# stands where an ssh or a console server would: its own terminal is raw, and the shell is on another
RELAY = f"{sys.executable} -c \"import pty; pty.spawn(['bash', '--norc', '--noprofile', '--noediting', '-i'])\""


def echo(nbytes: int, lead: str = "") -> str:
    """An `echo` line of `nbytes` bytes, whose output shows where it was cut."""
    words = " ".join(f"w{i:04d}" for i in range(nbytes // 5 + 1))
    return (f"echo {lead}" + words)[:nbytes].rstrip().ljust(nbytes, "x")


def too_long(nbytes: int, takes: int = CANON) -> str:
    return (
        f"line of {nbytes} bytes not sent: the terminal reads whole lines (canonical mode) and takes "
        f"{takes} bytes of one, so the last {nbytes - takes} would be dropped without an error"
    )


def warning(nbytes: int) -> str:
    return (
        f">> long line: {nbytes} bytes; a far side that reads whole lines, with no line editor, keeps only "
        "the first 4095 bytes of one (the usual limit on Linux) and drops the rest without an error\n"
    )


@linux
@pytest.mark.parametrize("spawn", CANONICAL)
@pytest.mark.parametrize("nbytes", [CANON + 1, 5000, 10000])
def test_p8_43_line_past_the_limit_is_refused_and_nothing_is_sent(spawn: str, nbytes: int, sent: SentLog, capsys):
    """SPEC "The length of a sent line": the shell has no line editor, so the terminal would keep 4095
    bytes of the line and run that. The step fails before anything is sent, `ignore_error` or not, and
    the breakout finds the session at its prompt."""
    runner = make_runner(
        [
            {"cmd": "echo first", "register": "first"},
            {"cmd": echo(nbytes), "register": "out", "ignore_error": True},
            {"cmd": "echo never", "register": "never"},
        ],
        spawn=spawn,
        breakout=[{"cmd": "echo clean", "register": "clean"}],
    )
    with pytest.raises(LineTooLong) as ei:
        runner.run()
    assert str(ei.value) == too_long(nbytes)
    assert runner.config.vars == {"first": "first", "clean": "clean"}
    assert sent.commands() == ["echo first", "echo clean"]
    err = capsys.readouterr().err
    assert f">> step failed (LineTooLong): {too_long(nbytes)}\n" in err and "long line" not in err


@linux
@pytest.mark.parametrize("spawn", CANONICAL)
@pytest.mark.parametrize("nbytes", [CANON - 1, CANON])
def test_p8_43_line_up_to_the_limit_is_sent_and_runs_whole(spawn: str, nbytes: int, capsys):
    cmd = echo(nbytes)
    out = run([{"cmd": cmd, "register": "out"}, {"cmd": "echo done", "register": "after"}], spawn=spawn)
    assert out == {"out": cmd[5:], "after": "done"}
    assert "long line" not in capsys.readouterr().err


@linux
def test_p8_43_line_typed_at_a_running_read_is_refused():
    """Under readline too, while a command runs: the shell has given the terminal back, and a line typed
    now waits in it for the `read`."""
    script = [{"cmd": "true"}, {"line": "IFS= read -r x"}, {"sleep": "300ms"}]
    out = run([*script, {"line": "v" * CANON}, {"cmd": "printf %s \"$x\" | wc -c", "register": "n"}])
    assert out == {"n": str(CANON)}
    runner = make_runner(
        [*script, {"line": "v" * (CANON + 1)}],
        breakout=[{"line": "v"}, {"cmd": "printf %s \"$x\" | wc -c", "register": "n"}],
    )
    with pytest.raises(LineTooLong) as ei:
        runner.run()
    assert str(ei.value) == too_long(CANON + 1)
    assert runner.config.vars == {"n": "1"}


@linux
def test_p8_43_python_input_is_a_reader_of_whole_lines():
    script = [{"cmd": "true"}, {"line": "python3 -c 'print(len(input()))'"}, {"sleep": "500ms"}]
    runner = make_runner([*script, {"line": "v" * (CANON + 1)}], breakout=[{"control": "c"}])
    with pytest.raises(LineTooLong):
        runner.run()


@linux
def test_p8_43_answer_to_a_prompt_is_checked_too(attach):
    answer = SimpleHandler("ask", [r"PROMPT\$ "], "y" * 5000, lambda text: text)
    s = attach(NOEDIT, [answer], prompt=False)
    with pytest.raises(LineTooLong) as ei:
        s.get_prompt(timeout=5)
    assert str(ei.value) == too_long(5000)


@linux
def test_p8_43_session_is_where_it_was_after_a_refused_line(attach):
    s = attach(NOEDIT)
    s.sendline("echo before", timeout=5)
    s.get_prompt(timeout=5)
    state = (s._at_prompt, s._sent, s._solicit, s._held)
    assert state[0] is True
    with pytest.raises(LineTooLong):
        s.sendline(echo(5000), timeout=5)
    assert (s._at_prompt, s._sent, s._solicit, s._held) == state
    s.sendline("echo ok", timeout=5)
    assert s.get_prompt(timeout=5) == "ok\n"


@linux
def test_p8_43_only_the_long_line_of_a_multi_line_cmd_is_refused(tmp_path: Path, sent: SentLog):
    """The lines of a `cmd` are sent one by one: those before the long one have run, and it and those
    after it are not sent."""
    a, c = tmp_path / "a", tmp_path / "c"
    runner = make_runner([{"cmd": f"echo a > {a}\n{echo(5000)}\necho c > {c}", "register": "out"}], spawn=NOEDIT)
    with pytest.raises(LineTooLong) as ei:
        runner.run()
    assert str(ei.value) == too_long(5000)
    assert a.read_text() == "a\n" and not c.exists()
    assert sent.commands() == [f"echo a > {a}"]


@linux
def test_p8_43_cli_reports_a_failed_run(tmp_path: Path):
    res = run_cli(make_doc([{"cmd": echo(5000)}], spawn=NOEDIT), tmp_path)
    assert res.returncode == 3, res.stderr
    report = [line for line in res.stderr.splitlines() if not line.startswith(">> ") and "RuntimeWarning" not in line]
    assert report == [
        f"Run failed in {tmp_path / 'script.autobot.yaml'}: {too_long(5000)}",
        f"  at script.0 (cmd: {echo(5000)[:72]}...)",
    ]


# -- P8-44: the bytes of a line -----------------------------------------------------------------------


@linux
def test_p8_44_limit_counts_bytes_not_characters(capsys):
    """2045 two-byte characters after `echo ` are 4095 bytes: sent, and whole. One byte more is refused,
    with the bytes in the message."""
    cmd = "echo " + "é" * 2045
    assert len(cmd) == 2050 and len(cmd.encode()) == CANON
    assert run([{"cmd": cmd, "register": "out"}], spawn=f"env LC_ALL=C.UTF-8 {NOEDIT}") == {"out": "é" * 2045}
    for more, nbytes in (("x", CANON + 1), ("é", CANON + 2)):
        runner = make_runner([{"cmd": cmd + more}], spawn=NOEDIT)
        with pytest.raises(LineTooLong) as ei:
            runner.run()
        assert str(ei.value) == too_long(nbytes)


@linux
@pytest.mark.parametrize("spawn", CANONICAL)
def test_p8_44_upload_lines_of_an_embedded_script_are_short(spawn: str, sent: SentLog, capsys):
    """An embedded script of 30,000 bytes goes up in lines of about 600, on a terminal that would cut a
    long one: none comes near the limit of any platform (1023 on macOS), and nothing is said."""
    script = "#!/bin/sh\n" + "# padding padding padding padding\n" * 900 + "echo uploaded\n"
    assert len(script) > 30000
    out = run([{"cmd": script, "register": "out", "timeout": "30s"}], spawn=spawn)
    assert out == {"out": "uploaded"}
    assert len(sent.lines()) > 70 and max(len(line.encode()) for line in sent.lines()) < 700
    assert "long line" not in capsys.readouterr().err


@linux
def test_p8_44_each_line_of_a_send_with_line_breaks_counts_alone(attach):
    """A `line` may hold several lines: the terminal takes each of them as one."""
    s = attach(NOEDIT)
    s.sendline("\n".join(["echo " + "a" * 4000, "echo " + "b" * 4000, "echo end"]), timeout=5)
    s.expect([r"PROMPT\$ end\r\n"], timeout=10)
    assert s.ctx["before"].count("a") == 4000 * 2 and s.ctx["before"].count("b") == 4000 * 2
    s.get_prompt(timeout=5)
    with pytest.raises(LineTooLong) as ei:
        s.sendline("\n".join(["true", "echo " + "a" * 4091, "true"]), timeout=5)
    assert str(ei.value) == too_long(4096)


# -- P8-45: the mode is that of the terminal when the line is sent ------------------------------------


def test_p8_45_line_editor_reads_a_long_line_itself(capsys):
    """Readline reads the terminal a character at a time: a line of 10,000 characters is sent and runs
    whole, as on a terminal that wraps (P8-34)."""
    cmd = echo(10000)
    out = run([{"cmd": cmd, "register": "out"}, {"cmd": "echo done", "register": "after"}])
    assert out == {"out": cmd[5:], "after": "done"}


@linux
def test_p8_45_terminal_taken_out_of_canonical_mode_by_the_script(capsys):
    """After `stty -icanon` the terminal hands the shell each byte as it comes and cuts nothing: the same
    shell that was refused a long line gets it whole. (That terminal echoes the line break as `^J`, so
    the echo isn't recognized and stays in the capture.)"""
    cmd = echo(6000)
    out = run([{"cmd": "stty -icanon"}, {"cmd": cmd, "register": "out"}, {"cmd": "echo done", "register": "after"}], spawn=NOEDIT)
    assert out == {"out": f"{cmd}^J{cmd[5:]}", "after": "echo done^Jdone"}
    assert capsys.readouterr().err.count(warning(6000)) == 1


@linux
def test_p8_45_mode_as_the_pty_reports_it(attach):
    """What the check goes by: `tcgetattr` on the pty reports the mode the child's terminal is in now."""
    import termios

    def canonical(s: Session) -> bool:
        assert s._cld is not None
        return bool(termios.tcgetattr(s._cld.child_fd)[3] & termios.ICANON)

    s = attach(BASH)
    assert not canonical(s)  # readline, at its prompt
    for _ in range(20):  # and each time the prompt is back, never the mode of the command before it
        s.sendline("true", timeout=5)
        s.get_prompt(timeout=5)
        assert not canonical(s)
    s.sendline("sleep 0.5", timeout=5)
    time.sleep(0.2)
    assert canonical(s)  # while a command runs
    s.get_prompt(timeout=5)
    assert not canonical(s)
    assert canonical(attach(NOEDIT))
    relay = attach(RELAY)
    time.sleep(0.2)
    assert not canonical(relay)  # raw, whatever the shell behind it has


# -- P8-46: a far side Autobot can't see --------------------------------------------------------------


@linux
def test_p8_46_line_cut_behind_a_relay_is_sent_with_one_warning(capsys):
    """The process Autobot spawned keeps its terminal raw and passes every byte on to a shell on a
    terminal of its own, as `ssh` does. Autobot sees only the first, so it sends the lines and says once
    what may happen; here it does happen: the far terminal keeps 4095 bytes, and what is captured is the
    output of the line cut there."""
    first, second = echo(5000), echo(6000, "second ")
    out = run(
        [{"cmd": first, "register": "first"}, {"cmd": second, "register": "second"}, {"cmd": "echo done", "register": "after"}],
        spawn=RELAY,
    )
    assert out == {"first": first[5:CANON], "second": second[5:CANON], "after": "done"}
    err = capsys.readouterr().err
    assert err.count(">> long line: ") == 1 and warning(5000) in err


def test_p8_46_warning_is_printed_once_and_not_for_ordinary_lines(capsys):
    """Also where a line editor reads the line and nothing is cut: Autobot can't tell that from a relay."""
    run([{"cmd": echo(4095)}, {"cmd": "echo short"}])
    assert "long line" not in capsys.readouterr().err
    run([{"cmd": echo(4096)}, {"cmd": echo(9000)}, {"line": "true " + "x" * 5000}, {"cmd": "true"}])
    err = capsys.readouterr().err
    assert err.count(">> long line: ") == 1 and warning(4096) in err
    assert err.index(warning(4096)) < err.index(">> cmd: echo w0000")


# -- P8-47: the limit of the platform -----------------------------------------------------------------


def test_p8_47_limit_by_platform(monkeypatch: pytest.MonkeyPatch):
    """Linux: 4096 with the line break, whatever `fpathconf` says (255 for a pty). macOS: what
    `fpathconf` says, or 1024. Elsewhere it isn't known."""
    asked: list[tuple] = []

    def fpathconf(fd, name):
        asked.append((fd, name))
        return 1024

    def fails(fd, name):
        raise OSError(25, "Inappropriate ioctl for device")

    monkeypatch.setattr(os, "fpathconf", fpathconf)
    monkeypatch.setattr(sys, "platform", "linux")
    assert canon_limit(7) == 4096 and asked == []
    monkeypatch.setattr(sys, "platform", "darwin")
    assert canon_limit(7) == 1024 and asked == [(7, "PC_MAX_CANON")]
    monkeypatch.setattr(os, "fpathconf", fails)
    assert canon_limit(7) == 1024
    for platform in ("freebsd14", "openbsd7", "sunos5"):
        monkeypatch.setattr(sys, "platform", platform)
        assert canon_limit(7) is None


@linux
def test_p8_47_fpathconf_of_a_linux_pty_is_not_its_limit(attach):
    s = attach(NOEDIT)
    assert s._cld is not None and os.fpathconf(s._cld.child_fd, "PC_MAX_CANON") == 255


def test_p8_47_session_goes_by_the_limit_of_the_platform(attach, monkeypatch: pytest.MonkeyPatch, capsys):
    """With the limit of macOS, 1024 with the line break: 1023 bytes are sent and 1024 are not. Where no
    limit is known, nothing is refused, and a line past the Linux limit gets the warning."""
    monkeypatch.setattr(session_mod, "canon_limit", lambda fd: 1024)
    s = attach(NOEDIT)
    s.sendline(echo(1023), timeout=5)
    assert s.get_prompt(timeout=5) == echo(1023)[5:] + "\n"
    with pytest.raises(LineTooLong) as ei:
        s.sendline(echo(1024), timeout=5)
    assert str(ei.value) == too_long(1024, 1023)
    monkeypatch.setattr(session_mod, "canon_limit", lambda fd: None)
    s.sendline(echo(1024), timeout=5)
    s.get_prompt(timeout=5)
    assert "long line" not in capsys.readouterr().err
    s.sendline(echo(5000), timeout=5)
    assert warning(5000) in capsys.readouterr().err
