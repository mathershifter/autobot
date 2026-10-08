"""P6-112..114: SIGTERM and SIGHUP while a run goes on (SPEC "CLI": errors while the script runs, attach).

The CLI runs as a subprocess whose two signals have their default action, whatever the suite inherited.
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
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import BASH, make_doc

from autobot import signals

SIGS = [signal.SIGTERM, signal.SIGHUP]
CLI = [sys.executable, "-W", "ignore", "-m", "autobot.cli"]
# a breakout that a signal ends did not finish: the last line of the report says what that may mean
LEFT = "Session may be left logged in: a breakout did not finish"


def _default() -> None:
    """For a child: the default actions, whatever the suite was started with (`nohup`, an ignored SIGINT).
    Python then sets its own handler for SIGINT."""
    for sig in (*SIGS, signal.SIGINT):
        signal.signal(sig, signal.SIG_DFL)


def _ignore(sig: int) -> Callable[[], None]:
    def setup() -> None:
        _default()
        signal.signal(sig, signal.SIG_IGN)

    return setup


# A command that prints `ready` and then is `sleep`, in the same process: once the word is in the session's
# output the command is running for certain, and a Ctrl-C stops it. The word is in the output only: the
# command as it is sent has it in two pieces. A plain `sleep` is only on its way when the shell echoes it,
# and a Ctrl-C sent then may find the shell between two things, where it takes no interrupt.
RUNNING = """sh -c "echo rea''dy; exec sleep %d\""""


def _ready(echo: Path) -> bool:
    return "\nready\n" in echo.read_text()


def _doc(tmp_path: Path, breakout: list[dict[str, Any]] | None = None, sleep: int = 30) -> tuple[dict[str, Any], Path, str]:
    """A script that blocks in `sleep` inside a block; the block's breakout and `attach.breakout` each
    append a word to the log."""
    log = tmp_path / "log"
    spawn = f"{BASH} -s autobot{os.getpid()}"  # unique, so a leaked shell can be found
    doc = make_doc(
        [
            {
                "block": {
                    "name": "long",
                    "script": [{"cmd": RUNNING % sleep, "timeout": "20s"}, {"cmd": f"echo after >> {log}"}],
                    "breakout": [{"control": "c"}, {"cmd": f"echo block >> {log}", "timeout": "5s"}],
                }
            },
        ],
        spawn=spawn,
        breakout=breakout if breakout is not None else [{"cmd": f"echo attach >> {log}", "timeout": "5s"}],
    )
    return doc, log, spawn


def _wait(what: Callable[[], bool], proc: subprocess.Popen, why: Callable[[], str]) -> None:
    deadline = time.monotonic() + 30
    while not what():
        assert proc.poll() is None and time.monotonic() < deadline, why()
        time.sleep(0.05)


def _signalled(
    tmp_path: Path,
    doc: dict[str, Any],
    sigs: list[int],
    *,
    setup: Callable[[], None] = _default,
    then: Callable[[], bool] | None = None,
    argv: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the CLI on `doc`, send it `sigs[0]` once the command has printed `ready`, and each further signal
    once `then` holds. Its return code, the session's output and its messages."""
    path = tmp_path / "script.autobot.yaml"
    path.write_text(yaml.safe_dump(doc))
    echo, messages = tmp_path / "stdout", tmp_path / "stderr"
    with open(echo, "w") as out, open(messages, "w") as err:
        proc = subprocess.Popen(argv or [*CLI, str(path)], stdout=out, stderr=err, preexec_fn=setup)
        try:
            _wait(lambda: _ready(echo), proc, messages.read_text)
            proc.send_signal(sigs[0])
            for sig in sigs[1:]:
                assert then is not None
                _wait(then, proc, messages.read_text)
                proc.send_signal(sig)
            proc.wait(timeout=60)
        finally:
            proc.kill()
    return subprocess.CompletedProcess(proc.args, proc.returncode, echo.read_text(), messages.read_text())


def _lines(res: subprocess.CompletedProcess[str]) -> list[str]:
    return [line for line in res.stderr.splitlines() if not line.startswith(">> ")]


def _progress(res: subprocess.CompletedProcess[str]) -> list[str]:
    return [line for line in res.stderr.splitlines() if line.startswith(">> ")]


def _pids(cmdline: str) -> list[str]:
    return subprocess.run(["pgrep", "-x", "-f", cmdline], capture_output=True, text=True, check=False).stdout.split()


# -- P6-112: the cleanup runs, and the process ends from the signal ---------------------------------


@pytest.mark.parametrize("sig", SIGS, ids=lambda s: s.name)
def test_p6_112_signal_runs_the_breakouts_and_ends_the_process_from_the_signal(tmp_path: Path, sig: signal.Signals):
    """SPEC "Errors while the script runs": SIGTERM and SIGHUP end a run as an interrupt does. The block's
    breakout and `attach.breakout` run, the session is closed, the CLI says which signal it was and where
    the run was, and the process ends from that signal."""
    doc, log, spawn = _doc(tmp_path)
    res = _signalled(tmp_path, doc, [sig])
    assert res.returncode == -sig, res.stderr
    assert "Traceback" not in res.stderr
    assert _lines(res) == [f"Terminated ({sig.name})", f"  at script.0.block.script.0 (cmd: {RUNNING % 30})"]
    assert log.read_text().split() == ["block", "attach"]
    progress = _progress(res)
    assert progress.index(f">>   step interrupted ({sig.name})") < progress.index(">> block breakout: long")
    assert progress.index(">> block breakout: long") < progress.index(">> breakout: detaching")
    assert _pids(spawn) == []


def test_p6_112_traceback_flag(tmp_path: Path):
    doc, log, _ = _doc(tmp_path)
    path = tmp_path / "script.autobot.yaml"
    res = _signalled(tmp_path, doc, [signal.SIGTERM], argv=[*CLI, str(path), "--traceback"])
    lines = _lines(res)
    assert res.returncode == -signal.SIGTERM, res.stderr
    assert lines[0] == "Traceback (most recent call last):" and "autobot.signals.Terminated: SIGTERM" in lines
    assert lines[-2:] == ["Terminated (SIGTERM)", f"  at script.0.block.script.0 (cmd: {RUNNING % 30})"]
    assert log.read_text().split() == ["block", "attach"]


@pytest.mark.parametrize("sig", SIGS, ids=lambda s: s.name)
def test_p6_112_signal_during_a_step_of_the_breakout_of_a_run_that_completed(tmp_path: Path, sig: signal.Signals):
    """A signal while `attach.breakout` runs after the script ends that breakout, as an interrupt does;
    the session is still closed."""
    log = tmp_path / "log"
    doc, _, spawn = _doc(tmp_path, breakout=[{"cmd": RUNNING % 30, "timeout": "20s"}, {"cmd": f"echo never >> {log}"}], sleep=0)
    doc["script"] = [{"cmd": "true"}]
    res = _signalled(tmp_path, doc, [sig])
    assert res.returncode == -sig, res.stderr
    assert _lines(res) == [f"Terminated ({sig.name})", f"  at attach.breakout.0 (cmd: {RUNNING % 30})", LEFT]
    assert not log.exists() and _pids(spawn) == []


# -- P6-113: the cleanup is bounded, and nothing is caught that the process was started without --------


@pytest.mark.parametrize("sigs", [[signal.SIGTERM, signal.SIGTERM], [signal.SIGHUP, signal.SIGTERM], [signal.SIGTERM, signal.SIGINT]], ids=["term-term", "hup-term", "term-int"])
def test_p6_113_second_signal_ends_the_breakout(tmp_path: Path, sigs: list[signal.Signals]):
    """SPEC "Errors while the script runs": a breakout that hangs doesn't hold the process. A second signal
    ends the breakout that is running, the session is closed, and the process ends from that signal."""
    began, never = tmp_path / "began", tmp_path / "never"
    breakout = [{"cmd": f"touch {began}"}, {"cmd": "sleep 40", "timeout": "30s"}, {"cmd": f"touch {never}"}]
    doc, log, spawn = _doc(tmp_path, breakout=breakout)
    started = time.monotonic()
    res = _signalled(tmp_path, doc, sigs, then=lambda: began.exists() and ">> cmd: sleep 40" in (tmp_path / "stderr").read_text())
    assert time.monotonic() - started < 20
    assert res.returncode == -sigs[1], res.stderr
    head = "Interrupted" if sigs[1] == signal.SIGINT else f"Terminated ({sigs[1].name})"
    assert _lines(res) == [head, "  at attach.breakout.1 (cmd: sleep 40)", LEFT]
    assert log.read_text().split() == ["block"] and not never.exists()
    assert _pids(spawn) == []


@pytest.mark.parametrize("sig", SIGS, ids=lambda s: s.name)
def test_p6_113_ignored_signal_stays_ignored(tmp_path: Path, sig: signal.Signals):
    """A signal the process was started with ignored (`nohup` for SIGHUP) gets no handler: the run goes on
    and completes."""
    doc, log, _ = _doc(tmp_path, sleep=3)
    res = _signalled(tmp_path, doc, [sig], setup=_ignore(sig))
    assert res.returncode == 0, res.stderr
    assert log.read_text().split() == ["after", "block", "attach"]
    assert _lines(res) == [] and _progress(res)[-1] == ">> run completed"


def test_p6_113_only_a_default_action_is_replaced_and_it_is_put_back():
    """`signals.caught` takes a signal that has its default action, leaves a handler of the program's own
    and an ignored signal alone, and puts the default action back."""
    def mine(signum: int, frame: object) -> None:
        pass

    before = {sig: signal.getsignal(sig) for sig in SIGS}
    try:
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        signal.signal(signal.SIGHUP, mine)
        with signals.caught() as caught:
            assert caught == [signal.SIGTERM]
            assert signal.getsignal(signal.SIGTERM) is signals._unwind and signal.getsignal(signal.SIGHUP) is mine
            with signals.caught() as inner:  # a block inside another
                assert inner == []
            assert signal.getsignal(signal.SIGTERM) is signals._unwind
            with pytest.raises(signals.Terminated) as ei:
                signal.raise_signal(signal.SIGTERM)
            assert (ei.value.signum, ei.value.name, str(ei.value)) == (signal.SIGTERM, "SIGTERM", "SIGTERM")
        assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL and signal.getsignal(signal.SIGHUP) is mine
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        with signals.caught() as caught:
            assert signal.SIGHUP not in caught and signal.getsignal(signal.SIGHUP) is signal.SIG_IGN
    finally:
        for sig, handler in before.items():
            signal.signal(sig, handler)


def test_p6_113_no_handler_outside_the_main_thread():
    """In another thread no handler can be set: the block runs without one."""
    before = {sig: signal.getsignal(sig) for sig in SIGS}
    seen: list[Any] = []

    def work() -> None:
        with signals.terminable(), signals.caught() as caught:
            seen.append((list(caught), [signal.getsignal(sig) for sig in SIGS]))

    try:
        for sig in SIGS:
            signal.signal(sig, signal.SIG_DFL)
        thread = threading.Thread(target=work)
        thread.start()
        thread.join()
        assert seen == [([], [signal.SIG_DFL, signal.SIG_DFL])]
    finally:
        for sig, handler in before.items():
            signal.signal(sig, handler)


LIBRARY = """
import sys, yaml
from autobot import Config, Runner
Runner(Config.model_validate(yaml.safe_load(open(sys.argv[1]))), {}).run()
print("returned")
"""


@pytest.mark.parametrize("sig", SIGS, ids=lambda s: s.name)
def test_p6_113_runner_used_as_a_library_cleans_up_and_ends_from_the_signal(tmp_path: Path, sig: signal.Signals):
    """`Runner.run()` without the CLI: the breakouts run, the session is closed, and the process ends
    from the signal, as it would have without Autobot's handler."""
    doc, log, spawn = _doc(tmp_path)
    path = tmp_path / "script.autobot.yaml"
    res = _signalled(tmp_path, doc, [sig], argv=[sys.executable, "-W", "ignore", "-c", LIBRARY, str(path)])
    assert res.returncode == -sig, res.stderr
    assert log.read_text().split() == ["block", "attach"]
    assert "returned" not in res.stdout and _lines(res) == [] and _pids(spawn) == []


@pytest.mark.parametrize("sig", SIGS, ids=lambda s: s.name)
def test_p6_113_signal_while_prepare_runs_goes_the_same_way(tmp_path: Path, sig: signal.Signals):
    """SPEC "attach": the same handler is in place while `attach.prepare` runs. Its temp file is removed,
    nothing was spawned, so no breakout runs, and the process ends from the signal."""
    tdir, started, log = tmp_path / "tmp", tmp_path / "started", tmp_path / "log"
    tdir.mkdir()
    doc = make_doc([], prepare=f"touch {started}\nexec sleep 20 > /dev/null 2>&1\n", breakout=[{"cmd": f"touch {log}"}])
    path = tmp_path / "script.autobot.yaml"
    path.write_text(yaml.safe_dump(doc))
    proc = subprocess.Popen(
        [*CLI, str(path)], env={**os.environ, "TMPDIR": str(tdir)}, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        text=True, preexec_fn=_default,
    )
    try:
        _wait(started.exists, proc, lambda: "prepare never started")
        proc.send_signal(sig)
        err = proc.communicate(timeout=20)[1]
    finally:
        proc.kill()
    assert proc.returncode == -sig, err
    assert list(tdir.iterdir()) == [] and not log.exists()
    assert [line for line in err.splitlines() if not line.startswith(">> ")] == [f"Terminated ({sig.name})"]


# -- P6-114: the terminal Autobot runs on goes away -------------------------------------------------


def _on_a_terminal(tmp_path: Path, doc: dict[str, Any], stderr_too: bool) -> tuple[int, str]:
    """Run the CLI with a pseudo-terminal as its controlling terminal and its stdout (and stderr), and
    close the other side once the command has printed `ready`: the terminal hangs up."""
    path = tmp_path / "script.autobot.yaml"
    path.write_text(yaml.safe_dump(doc))
    messages = tmp_path / "stderr"
    master, slave = os.openpty()

    def setup() -> None:
        _default()
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)

    with open(messages, "w") as err:
        proc = subprocess.Popen(
            [*CLI, str(path)], stdin=slave, stdout=slave, stderr=slave if stderr_too else err,
            start_new_session=True, preexec_fn=setup,
        )
        os.close(slave)
        try:
            seen, deadline = b"", time.monotonic() + 30
            while b"\nready" not in seen:
                assert time.monotonic() < deadline, seen
                seen += os.read(master, 4096)
            time.sleep(0.5)
            os.close(master)
            proc.wait(timeout=60)
        finally:
            proc.kill()
    return proc.returncode, messages.read_text()


@pytest.mark.parametrize("stderr_too", [True, False], ids=["stdout+stderr", "stdout"])
def test_p6_114_terminal_that_hangs_up_does_not_stop_the_breakouts(tmp_path: Path, stderr_too: bool):
    """SPEC "Errors while the script runs": when the terminal goes away (an ssh session that drops, a
    closed window), the process gets SIGHUP and every write to the terminal fails. The breakouts still
    run, although they write the session's output and their progress, and the process ends from SIGHUP:
    a stream nobody reads is pointed to /dev/null at its first failed write (P6-117 to P6-119)."""
    doc, log, spawn = _doc(tmp_path)
    rc, err = _on_a_terminal(tmp_path, doc, stderr_too)
    assert rc == -signal.SIGHUP, err
    assert log.read_text().split() == ["block", "attach"]
    assert _pids(spawn) == []
    if not stderr_too:  # the messages went to a file, which is still there
        assert err.splitlines()[-2:] == ["Terminated (SIGHUP)", f"  at script.0.block.script.0 (cmd: {RUNNING % 30})"]


# -- P6-116: many signals at once ---------------------------------------------------------------------

NOISE = ("Traceback", "Exception ignored", "lost sys.stderr", "Fatal Python error")
# between two signals of a storm, run by run: at once, and spread over the time the run takes to unwind
GAPS = (0, 0.001, 0.005, 0.02)
HEADS = {signal.SIGINT: "Interrupted", signal.SIGTERM: "Terminated (SIGTERM)", signal.SIGHUP: "Terminated (SIGHUP)"}


def _storm(tmp_path: Path, sigs: list[int], runs: int) -> None:
    """Send the CLI all of `sigs`, `GAPS` apart, while a `cmd` waits, `runs` times. Each time the process
    ends from one of them, its report starts with the line for that signal, and nothing of the
    interpreter's shows."""
    doc = make_doc([{"cmd": RUNNING % 30, "timeout": "20s"}], breakout=[{"control": "c"}])
    path = tmp_path / "script.autobot.yaml"
    path.write_text(yaml.safe_dump(doc))
    echo, messages = tmp_path / "stdout", tmp_path / "stderr"
    for run in range(runs):
        with open(echo, "w") as out, open(messages, "w") as err:
            proc = subprocess.Popen([*CLI, str(path)], stdout=out, stderr=err, preexec_fn=_default)
            try:
                _wait(lambda: _ready(echo), proc, messages.read_text)
                for sig in sigs:
                    proc.send_signal(sig)
                    time.sleep(GAPS[run % len(GAPS)])
                proc.wait(timeout=60)
            finally:
                proc.kill()
        said = messages.read_text()
        assert -proc.returncode in set(sigs), (run, proc.returncode, said)
        assert not any(noise in said for noise in NOISE), (run, said)
        report = [line for line in said.splitlines() if not line.startswith(">> ")]
        assert report[:1] == [HEADS[-proc.returncode]], (run, proc.returncode, said)


@pytest.mark.slow
def test_p6_116_storm_of_one_signal_always_ends_from_it_with_its_report(tmp_path: Path):
    """SPEC "Errors while the script runs": twelve SIGTERMs back to back, 200 times. However they fall on
    the unwinding, the report and the end of the interpreter, the process ends from SIGTERM and says so."""
    _storm(tmp_path, [signal.SIGTERM] * 12, 200)


@pytest.mark.slow
@pytest.mark.parametrize(
    "sigs",
    [[signal.SIGINT] * 12, [signal.SIGHUP] * 12, [signal.SIGTERM, signal.SIGHUP, signal.SIGINT] * 4, [signal.SIGINT, signal.SIGTERM] * 6],
    ids=["int", "hup", "term-hup-int", "int-term"],
)
def test_p6_116_mixed_storms(tmp_path: Path, sigs: list[signal.Signals]):
    """The same with Ctrl-C, SIGHUP and the three mixed, 50 times each: the process ends from one of the
    signals it was sent, and its first line is that signal's."""
    _storm(tmp_path, sigs, 50)


@pytest.mark.parametrize(
    "sigs", [[signal.SIGTERM] * 12, [signal.SIGINT] * 12, [signal.SIGTERM, signal.SIGHUP, signal.SIGINT] * 4], ids=["term", "int", "mixed"]
)
def test_p6_116_storm_a_few_times(tmp_path: Path, sigs: list[signal.Signals]):
    """Five runs of each storm, for the selection without the slow tests."""
    _storm(tmp_path, sigs, 5)


def test_p6_116_block_that_a_signal_ends_leaves_the_signals_held():
    """`signals.caught(held=True)`: a signal or an interrupt that ends the block leaves the three signals
    blocked, for the caller's report; any other way out leaves the mask as it was, and so does a block
    that isn't `held`."""
    before = {sig: signal.getsignal(sig) for sig in SIGS}
    mask = signal.pthread_sigmask(signal.SIG_BLOCK, [])
    try:
        for sig in SIGS:
            signal.signal(sig, signal.SIG_DFL)
        with signals.caught(held=True):
            pass
        assert signal.pthread_sigmask(signal.SIG_BLOCK, []) == mask
        with pytest.raises(RuntimeError), signals.caught(held=True):
            raise RuntimeError
        assert signal.pthread_sigmask(signal.SIG_BLOCK, []) == mask
        for error in (signals.Terminated(signal.SIGTERM), KeyboardInterrupt()):
            with pytest.raises(type(error)), signals.caught(held=True):
                raise error
            assert set(signals.HELD) <= signal.pthread_sigmask(signal.SIG_BLOCK, [])
            assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL
            signals.release()
            assert signal.pthread_sigmask(signal.SIG_BLOCK, []) == mask
        with pytest.raises(signals.Terminated), signals.caught():
            raise signals.Terminated(signal.SIGTERM)
        assert signal.pthread_sigmask(signal.SIG_BLOCK, []) == mask
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, mask)
        for sig, handler in before.items():
            signal.signal(sig, handler)


def test_p6_116_signal_that_arrives_while_a_message_is_printed_is_taken_after_it(monkeypatch: pytest.MonkeyPatch, capsys):
    """rich buffers what it prints, and a signal that raised in the middle of a message left the console
    holding that message and every later one: a run could end from its signal with no report at all. The
    signal is taken when the message is out, and the next message is printed as ever."""
    from autobot import log

    before = {sig: signal.getsignal(sig) for sig in SIGS}
    mask = signal.pthread_sigmask(signal.SIG_BLOCK, [])
    printing = log.console.print

    def interrupted(text: object) -> None:
        signal.raise_signal(signal.SIGTERM)  # the handler runs here, in the middle of the message
        printing(text)

    try:
        for sig in SIGS:
            signal.signal(sig, signal.SIG_DFL)
        with signals.caught():
            monkeypatch.setattr(log.console, "print", interrupted)
            with pytest.raises(signals.Terminated):
                log.say("cmd: one")
            assert signals.holding()
            monkeypatch.setattr(log.console, "print", printing)
            log.say("cmd: two")
        assert not signals.holding() and signal.pthread_sigmask(signal.SIG_BLOCK, []) == mask
        assert capsys.readouterr().err == ">> cmd: one\n>> cmd: two\n"
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, mask)
        for sig, handler in before.items():
            signal.signal(sig, handler)


def test_p6_116_one_signal_is_taken_and_the_next_waits_for_a_release():
    """The handler that takes a signal blocks the three; one that arrives then is pending, and is taken
    at `release`. A block that is not `held` leaves them unblocked."""
    before = {sig: signal.getsignal(sig) for sig in SIGS}
    interrupt = signal.getsignal(signal.SIGINT)
    mask = signal.pthread_sigmask(signal.SIG_BLOCK, [])
    try:
        for sig in SIGS:
            signal.signal(sig, signal.SIG_DFL)
        with signals.caught():
            with pytest.raises(signals.Terminated) as first:
                signal.raise_signal(signal.SIGTERM)
            assert first.value.signum == signal.SIGTERM and signals.holding()
            assert set(signals.HELD) <= signal.pthread_sigmask(signal.SIG_BLOCK, [])
            signal.raise_signal(signal.SIGHUP)  # held back: nothing is raised
            assert signal.SIGHUP in signal.sigpending()
            with pytest.raises(signals.Terminated) as second:
                signals.release()
                pass  # the pending one arrives in `release`, or right after it
            assert second.value.signum == signal.SIGHUP and signals.holding()
        assert not signals.holding() and signal.pthread_sigmask(signal.SIG_BLOCK, []) == mask
        assert signal.getsignal(signal.SIGINT) is interrupt  # as it was: Python's handler, or ignored by the suite's parent
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, mask)
        for sig, handler in before.items():
            signal.signal(sig, handler)
