"""P5-84..91: a logout before the session is closed (SPEC "Logging out", "Errors while the script runs").

The device is ``tests/fakes/device.py`` with ``--logout``: a console with a login, a login shell (a real
bash) or a bare prompt loop, and the login prompt again once ``logout`` has been entered. Its log shows
what reached it: ``LOGOUT=`` is the proof of a logout.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import DEVICE, SHELL_PROMPT, FakeDevice, make_doc, make_runner, run_cli

from autobot import cli, signals
from autobot.runner import BreakoutError, Left, left, trail
from autobot.session import PartialLine, PromptHandler, Session, SimpleHandler
from autobot.steps import StepFailure

TYPED = Path(__file__).resolve().parent / "fakes" / "typed.py"
LOGIN = {
    "name": "login",
    "send": {"each": "vars.creds", "fields": [{"match": "login:", "field": "username"}, {"match": "Password:", "field": "password"}]},
}
PROMPTS = [SHELL_PROMPT, LOGIN]
CREDS = {"creds": [{"username": "admin", "password": "secret"}]}
ACCEPT = ("--accept", "admin:secret", "--logout")
# the wait for the login prompt is a step of its own here: a block with nothing in it but its `after`
CONFIRM = {"block": {"name": "logged out"}, "after": "login:", "timeout": "4s"}
# drop what is typed or running, log out, wait for the proof
LOGOUT = [{"control": "c"}, {"line": "logout"}, CONFIRM]
# the documented shape: the same, and the Ctrl-C waits for the far side to read what was sent before it
SHAPE = [{"control": "c", "delay_before": "2s"}, {"line": "logout"}, CONFIRM]
# the same without the control character
BARE = [{"line": "logout"}, CONFIRM]
CONFIRM_TIMEOUT = "timed out after 4.0s waiting for the after pattern 'login:'"
LEFT = "Session may be left logged in: "
SENT = "; the run sent credentials (prompt 'login')"


def console(fake_device: FakeDevice, script: list[dict[str, Any]], breakout: list[dict[str, Any]] | None, *opts: str, **kw: Any) -> tuple[dict[str, Any], Path]:
    spawn, log = fake_device(*ACCEPT, *opts)
    kw.setdefault("prompts", PROMPTS)
    kw.setdefault("vars", CREDS)
    return make_doc(script, spawn=spawn, breakout=breakout, **kw), log


def report(res: subprocess.CompletedProcess[str]) -> list[str]:
    """What the CLI itself says on stderr: not the engine's `>> ` progress lines, not runpy's warning."""
    return [line for line in res.stderr.splitlines() if not line.startswith(">> ") and "RuntimeWarning" not in line]


def run(doc: dict[str, Any]):
    from autobot.models import Config
    from autobot.runner import Runner

    runner = Runner(Config.model_validate(doc), {})
    runner.run()
    return runner


# -- P5-84: the script completes and the breakout doesn't ---------------------------------------------


def test_p5_84_breakout_that_fails_after_the_script_fails_the_run(fake_device: FakeDevice, tmp_path: Path):
    """SPEC "Errors while the script runs": the script did its work and left a program running that takes
    the breakout's `logout`. The login prompt never comes, so the run doesn't count as completed: the CLI
    names the breakout step, says the session may be left logged in and exits with status 4. The
    session's output is what it would be."""
    doc, log = console(fake_device, [{"cmd": "echo configured"}, {"line": "cat"}], BARE)
    res = run_cli(doc, tmp_path)
    path = tmp_path / "script.autobot.yaml"
    assert res.returncode == cli.EXIT_BREAKOUT == 4, res.stderr
    assert report(res) == [
        f"Breakout failed in {path}: {CONFIRM_TIMEOUT}",
        "  at attach.breakout.1 (block: logged out)",
        f"{LEFT}the script completed, but a breakout did not finish{SENT}",
    ]
    progress = [line for line in res.stderr.splitlines() if line.startswith(">> ")]
    assert progress[-2:] == [f">> step failed (TimeoutError): {CONFIRM_TIMEOUT}", f">> breakout error (TimeoutError): {CONFIRM_TIMEOUT}"]
    assert ">> run completed" not in progress and "Run failed" not in res.stderr and "Traceback" not in res.stderr
    assert "\nconfigured\n" in res.stdout and res.stdout.endswith("PROMPT$ cat\nlogout\nlogout\n")  # `cat` took the line
    assert FakeDevice.read(log) == ["LOGIN=admin", "PASSWORD=secret"]


def test_p5_84_run_raises_a_breakout_error_once_the_session_is_closed(fake_device: FakeDevice, children):
    """`Runner.run()` raises `BreakoutError`, a `RunError`, after the close: the error names what ended
    the breakout, and carries it with the step it was in and the prompts answered with credentials."""
    doc, _ = console(fake_device, [{"cmd": "true"}, {"line": "cat"}], BARE)
    with pytest.raises(BreakoutError) as ei:
        run(doc)
    assert str(ei.value) == f"a breakout did not finish (TimeoutError): {CONFIRM_TIMEOUT}"
    assert isinstance(ei.value, RuntimeError)
    state = left(ei.value)
    assert isinstance(state, Left) and state.logins == ("login",)
    assert [type(e) for e in state.breakouts] == [TimeoutError] and str(state.breakouts[0]) == CONFIRM_TIMEOUT
    assert [ref.path for ref in trail(state.breakouts[0])] == ["attach.breakout.1"] == [ref.path for ref in trail(ei.value)]
    assert len(children) == 1 and not children[0].isalive()


def test_p5_84_breakout_that_finishes_is_a_completed_run(fake_device: FakeDevice, tmp_path: Path):
    doc, log = console(fake_device, [{"cmd": "echo configured"}], LOGOUT)
    res = run_cli(doc, tmp_path)
    assert res.returncode == 0, res.stderr
    assert report(res) == [] and res.stderr.splitlines()[-1] == ">> run completed"
    assert FakeDevice.read(log) == ["LOGIN=admin", "PASSWORD=secret", "LOGOUT="]


def test_p5_84_traceback_flag_shows_the_breakouts_error(fake_device: FakeDevice, tmp_path: Path):
    doc, _ = console(fake_device, [{"cmd": "true"}, {"line": "cat"}], BARE)
    res = run_cli(doc, tmp_path, "--traceback")
    lines = report(res)
    assert res.returncode == 4 and lines[0] == "Traceback (most recent call last):"
    assert f"TimeoutError: {CONFIRM_TIMEOUT}" in lines
    assert lines[-3].startswith("Breakout failed in ") and lines[-1].startswith(LEFT)


# -- P5-85: the script fails and the breakout fails too ----------------------------------------------


def test_p5_85_original_failure_stays_the_reason_and_the_breakout_is_reported_as_well(fake_device: FakeDevice, tmp_path: Path):
    """SPEC "Errors while the script runs": a breakout's failure never replaces the script's. The report
    of the run and its status 3 are what they are without it; the breakout's report follows."""
    doc, log = console(fake_device, [{"cmd": "trap '' INT"}, {"cmd": "cat", "timeout": "1s"}], LOGOUT)
    res = run_cli(doc, tmp_path)
    path = tmp_path / "script.autobot.yaml"
    assert res.returncode == cli.EXIT_RUN == 3, res.stderr
    assert report(res) == [
        f"Run failed in {path}: timed out after 1.0s waiting for a shell prompt ('sh')",
        "  at script.1 (cmd: cat)",
        f"Breakout failed in {path}: {CONFIRM_TIMEOUT}",
        "  at attach.breakout.2 (block: logged out)",
        f"{LEFT}a breakout did not finish{SENT}",
    ]
    assert "LOGOUT=" not in FakeDevice.read(log)


def test_p5_85_run_raises_the_scripts_error_with_the_breakouts(fake_device: FakeDevice):
    doc, _ = console(fake_device, [{"cmd": "false"}], [{"cmd": "false", "timeout": "5s"}])
    with pytest.raises(StepFailure, match="^command returned exit code 1$") as ei:
        run(doc)
    state = left(ei.value)
    assert state is not None and state.logins == ("login",)
    assert [ref.path for ref in trail(ei.value)] == ["script.0"]
    assert [[ref.path for ref in trail(e)] for e in state.breakouts] == [["attach.breakout.0"]]
    assert state.breakouts[0] is not ei.value


def test_p5_85_run_whose_breakouts_finish_has_nothing_left(fake_device: FakeDevice):
    doc, log = console(fake_device, [{"cmd": "false"}], LOGOUT)
    with pytest.raises(StepFailure) as ei:
        run(doc)
    assert left(ei.value) is None and FakeDevice.read(log)[-1] == "LOGOUT="


# -- P5-86: a block's breakout ----------------------------------------------------------------------

SUB = {"name": "sub", "script": [{"cmd": "echo in"}], "breakout": [{"cmd": "false"}, {"cmd": "echo never"}]}


def test_p5_86_block_breakout_that_fails_fails_the_run_and_stops_nothing(fake_device: FakeDevice, tmp_path: Path):
    """SPEC "block": a block whose breakout fails may have left what it entered. The script goes on,
    `attach.breakout` runs and logs out, the session is closed, and the run ends with status 4."""
    doc, log = console(fake_device, [{"block": SUB}, {"cmd": "echo after"}], LOGOUT)
    res = run_cli(doc, tmp_path)
    path = tmp_path / "script.autobot.yaml"
    assert res.returncode == 4, res.stderr
    assert report(res) == [
        f"Breakout failed in {path}: command returned exit code 1",
        "  at script.0.block.breakout.0 (cmd: false)",
        f"{LEFT}the script completed, but a breakout did not finish{SENT}",
    ]
    progress = res.stderr.splitlines()
    assert progress.index(">> block breakout error (StepFailure): command returned exit code 1") < progress.index(">> cmd: echo after")
    assert ">> block completed: sub" in progress and ">> breakout: detaching" in progress
    assert FakeDevice.read(log)[-1] == "LOGOUT="


def test_p5_86_every_breakout_that_fails_is_reported_in_order(fake_device: FakeDevice, tmp_path: Path):
    """The block's breakout and `attach.breakout` both fail after a script that failed: three reports."""
    doc, _ = console(fake_device, [{"block": {**SUB, "script": [{"cmd": "false"}]}}], [{"cmd": "false", "timeout": "3s"}])
    res = run_cli(doc, tmp_path)
    path = tmp_path / "script.autobot.yaml"
    assert res.returncode == 3, res.stderr
    assert report(res) == [
        f"Run failed in {path}: command returned exit code 1",
        "  at script.0.block.script.0 (cmd: false)",
        f"Breakout failed in {path}: command returned exit code 1",
        "  at script.0.block.breakout.0 (cmd: false)",
        f"Breakout failed in {path}: command returned exit code 1",
        "  at attach.breakout.0 (cmd: false)",
        f"{LEFT}a breakout did not finish{SENT}",
    ]


def test_p5_86_plugin_runs_a_breakout_through_the_context(attached_runner, capfd):
    """`ctx.run_breakout(steps, what)` is how a breakout runs: the handlers start over, an error is
    logged under `what` and kept, and the call returns."""
    from conftest import steps

    r = attached_runner()
    r.run_breakout(steps([{"cmd": "true", "timeout": "5s"}]))
    assert r._unfinished == []
    r.run_breakout(steps([{"cmd": "false", "timeout": "5s"}, {"cmd": "echo never"}]), "tunnel breakout")
    assert [type(e) for e in r._unfinished] == [StepFailure]
    assert ">> tunnel breakout error (StepFailure): command returned exit code 1" in capfd.readouterr().err
    with pytest.raises(KeyboardInterrupt):  # an interrupt ends the breakout and goes on; it is kept too

        class Interrupting(list):
            def __iter__(self):
                raise KeyboardInterrupt

        r.run_breakout(Interrupting())
    assert [type(e) for e in r._unfinished] == [StepFailure, KeyboardInterrupt]
    r.session.detach()  # while the captured stdout it echoes to is still open


# -- P5-87: the confirmation ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "carrier",
    [CONFIRM, {"control": "c", "after": "login:", "timeout": "4s"}, {"line": "", "after": "login:"}],
    ids=["block", "control", "line"],
)
def test_p5_87_logout_is_confirmed_by_the_after_of_the_next_step(fake_device: FakeDevice, carrier: dict[str, Any], capsys):
    """SPEC "Logging out": `after` waits before its step, so the step after the `logout` line carries the
    wait for the login prompt, whatever the step is."""
    doc, log = console(fake_device, [{"cmd": "echo configured"}], [{"control": "c"}, {"line": "logout"}, carrier])
    run(doc)
    assert FakeDevice.read(log)[:3] == ["LOGIN=admin", "PASSWORD=secret", "LOGOUT="]


def test_p5_87_confirmation_that_never_matches_fails_the_breakout(fake_device: FakeDevice):
    """A logout that went elsewhere, here to a shell whose prompt loop doesn't know the word the script
    uses, is not confirmed: the breakout fails at the step that waits."""
    doc, log = console(fake_device, [{"cmd": "show"}], [{"control": "c"}, {"line": "quit"}, CONFIRM], "--then", "prompt", errors=["^% .*"])
    with pytest.raises(BreakoutError, match="waiting for the after pattern 'login:'"):
        run(doc)
    assert FakeDevice.read(log)[-1] == "LINE=quit"


def test_p5_87_without_a_confirmation_the_same_breakout_passes(fake_device: FakeDevice):
    """The same wrong word with no wait after it: nothing says the console is still logged in."""
    doc, log = console(fake_device, [{"cmd": "show"}], [{"control": "c"}, {"line": "quit"}, {"sleep": "200ms"}], "--then", "prompt", errors=["^% .*"])
    run(doc)
    assert "LOGOUT=" not in FakeDevice.read(log)


def test_p5_87_logout_sent_with_cmd_logs_in_again(fake_device: FakeDevice):
    """SPEC "Logging out": why the logout is a `line`. A `cmd` waits for the next shell prompt, the login
    prompt comes instead, and the `sendEach` prompt answers it: the run is logged in again when it ends."""
    doc, log = console(fake_device, [{"cmd": "true"}], [{"control": "c"}, {"cmd": "logout", "timeout": "5s"}])
    run(doc)
    assert FakeDevice.read(log) == ["LOGIN=admin", "PASSWORD=secret", "LOGOUT=", "LOGIN=admin", "PASSWORD=secret"]


# -- P5-88: part of a line is typed at the far side ----------------------------------------------------


def _typed(tmp_path: Path, breakout: list[dict[str, Any]]) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """A `cmd` of 200,005 characters to a child that stops reading for 2 s after 1000 bytes: the send
    times out with part of the line typed. Then the breakout."""
    log = tmp_path / "typed.log"
    spawn = f"{sys.executable} {TYPED} --stop 1000 --pause 2 --log {log}"
    doc = make_doc([{"cmd": "echo " + "x" * 200_000, "timeout": "1s"}], spawn=spawn, breakout=breakout)
    res = run_cli(doc, tmp_path)
    return res, log.read_text().splitlines() if log.exists() else []


def test_p5_88_breakout_that_starts_with_a_line_is_refused_and_reported(tmp_path: Path):
    """SPEC "The length of a sent line", "Errors while the script runs": after a send that failed partway,
    a breakout that starts with its `logout` line fails at that line. That is a breakout that did not
    finish, and the report says so."""
    res, log = _typed(tmp_path, [{"line": "logout"}, {"sleep": "2500ms"}])
    path = tmp_path / "script.autobot.yaml"
    assert res.returncode == 3, res.stderr
    lines = report(res)
    assert lines[0].startswith(f"Run failed in {path}: timed out after 1.0s while sending a line (")
    assert lines[2:] == [
        f"Breakout failed in {path}: line not sent: part of a line that could not be sent whole is typed at the far "
        "side, and a Return would enter it; send a control character that drops it first (control: c)",
        "  at attach.breakout.0 (line)",
        f"{LEFT}a breakout did not finish",
    ]
    assert log == []  # nothing was entered, and no logout arrived


def test_p5_88_breakout_that_starts_with_a_control_character_delivers_the_logout(tmp_path: Path):
    """The documented shape: the control character drops the part that is typed, and the `logout` line
    arrives as a line of its own."""
    res, log = _typed(tmp_path, [{"control": "c", "timeout": "10s"}, {"line": "logout"}, {"sleep": "500ms"}])
    assert res.returncode == 3, res.stderr
    assert len(report(res)) == 2 and "Breakout failed" not in res.stderr
    assert len(log) == 2 and log[0].startswith("INTERRUPT=") and log[1] == "LINE=logout"


# -- P5-89: the run knows when it sent credentials ----------------------------------------------------

NO_LOGOUT = (
    ">> no logout: the run sent credentials (prompt 'login'), and the script has no attach.breakout to log out "
    "with before the session is closed"
)


def test_p5_89_credentials_and_no_breakout_is_a_warning(fake_device: FakeDevice, tmp_path: Path):
    """SPEC "Logging out": a run that answered a `sendEach` prompt and has no `attach.breakout` says so
    before the session is closed. It is a warning: the run completes with status 0."""
    doc, log = console(fake_device, [{"cmd": "echo configured"}], None)
    res = run_cli(doc, tmp_path)
    assert res.returncode == 0, res.stderr
    assert res.stderr.splitlines()[-2:] == [NO_LOGOUT, ">> run completed"]
    assert "S3CRET" not in res.stderr and "secret" not in res.stderr.replace("--accept admin:secret", "")


def test_p5_89_warning_comes_after_a_failed_script_too(fake_device: FakeDevice, tmp_path: Path):
    doc, _ = console(fake_device, [{"cmd": "false"}], None)
    res = run_cli(doc, tmp_path)
    assert res.returncode == 3, res.stderr
    lines = res.stderr.splitlines()
    assert lines.index(NO_LOGOUT) == lines.index(">> step failed (StepFailure): command returned exit code 1") + 1
    assert report(res)[-1] == "  at script.0 (cmd: false)"


def test_p5_89_credentials_and_a_breakout_that_finishes_say_nothing(fake_device: FakeDevice, tmp_path: Path):
    doc, _ = console(fake_device, [{"cmd": "echo configured"}], LOGOUT)
    res = run_cli(doc, tmp_path)
    assert res.returncode == 0 and "no logout" not in res.stderr and "logged in" not in res.stderr


@pytest.mark.parametrize("breakout", [None, [{"cmd": "false", "timeout": "5s"}]], ids=["no-breakout", "breakout-fails"])
def test_p5_89_answer_that_is_not_a_credential_is_no_login(fake_device: FakeDevice, tmp_path: Path, breakout: list | None):
    """A prompt with a `send` string answers a question (a confirmation here, a pager elsewhere): no
    warning without a breakout, and no mention of credentials when a breakout fails."""
    spawn, log = fake_device("--order", "none", "--ask", "Sure?")
    confirm = {"name": "confirm", "expect": [r"Sure\?"], "send": "y"}
    res = run_cli(make_doc([{"cmd": "echo in"}], spawn=spawn, prompts=[SHELL_PROMPT, confirm], breakout=breakout), tmp_path)
    assert FakeDevice.read(log) == ["ASK=y"]
    assert ">> prompt answered: confirm" in res.stderr and "no logout" not in res.stderr and "credentials" not in res.stderr
    if breakout:
        assert res.returncode == 4 and report(res)[-1] == f"{LEFT}the script completed, but a breakout did not finish"
    else:
        assert res.returncode == 0 and res.stderr.splitlines()[-1] == ">> run completed"


def test_p5_89_several_prompts_are_named_once_each(fake_device: FakeDevice, tmp_path: Path):
    """Two `sendEach` prompts, one for the user name and one for the password: both are named, each once,
    in the order they were first answered."""
    spawn, _ = fake_device("--accept", "admin:secret")
    user = {"name": "user", "expect": ["login:"], "send": {"each": "vars.users"}}
    password = {"name": "pw", "expect": ["Password:"], "send": {"each": "vars.passwords"}}
    doc = make_doc(
        [{"cmd": "echo in"}], spawn=spawn, prompts=[SHELL_PROMPT, user, password],
        vars={"users": ["admin"], "passwords": ["secret"]},
    )
    res = run_cli(doc, tmp_path)
    assert res.returncode == 0, res.stderr
    assert res.stderr.count("no logout") == 1
    assert ">> no logout: the run sent credentials (prompts 'user', 'pw'), and" in res.stderr


def test_p5_89_session_keeps_the_names_until_the_next_spawn(fake_device: FakeDevice):
    """`Session.logins`: empty before any answer, the `sendEach` prompts answered since the spawn, still
    there once the session is closed, and empty again for the next process."""
    spawn, _ = fake_device("--accept", "admin:secret", "--ask", "Sure?")
    handlers = [
        PromptHandler("sh", [r"PROMPT\$ "], [], True),
        PromptHandler("login", ["login:", "Password:"], [["admin", "secret"]], False, [0, 1]),
        SimpleHandler("sure", [r"Sure\?"], "y", str),
    ]
    s = Session(handlers)
    assert s.logins == ()
    s.attach(spawn, timeout=5)
    try:
        s.get_prompt(timeout=5)
        assert s.logins == ("login",)
        s.detach()
        assert s.logins == ("login",)
        s.attach("bash --norc --noprofile -i", timeout=5)
        assert s.logins == ()
    finally:
        s.detach(failing=True)


# -- P5-90: a breakout that is interrupted did not finish ---------------------------------------------


def test_p5_90_interrupt_during_a_breakout_is_a_breakout_that_did_not_finish(fake_device: FakeDevice, tmp_path: Path):
    """SPEC "Errors while the script runs": a breakout that a signal ends has not logged out. The report of
    the signal names the breakout step, and the last line says what may be left."""
    began = tmp_path / "began"
    doc, log = console(fake_device, [{"cmd": "true"}], [{"cmd": f"touch {began}"}, {"cmd": "sleep 30", "timeout": "20s"}, *LOGOUT])
    path = tmp_path / "script.autobot.yaml"
    path.write_text(yaml.safe_dump(doc))
    proc = subprocess.Popen(
        [sys.executable, "-W", "ignore", "-m", "autobot.cli", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        text=True, preexec_fn=lambda: signal.signal(signal.SIGTERM, signal.SIG_DFL),
    )
    try:
        deadline = time.monotonic() + 30
        while not began.exists():
            assert proc.poll() is None and time.monotonic() < deadline
            time.sleep(0.05)
        time.sleep(1)
        proc.send_signal(signal.SIGTERM)
        err = proc.communicate(timeout=30)[1]
    finally:
        proc.kill()
    assert proc.returncode == -signal.SIGTERM, err
    assert [line for line in err.splitlines() if not line.startswith(">> ")] == [
        "Terminated (SIGTERM)",
        "  at attach.breakout.1 (cmd: sleep 30)",
        f"{LEFT}a breakout did not finish{SENT}",
    ]
    assert "LOGOUT=" not in FakeDevice.read(log)


def test_p5_90_left_names_the_interrupt_that_ended_an_inner_breakout():
    """`cli._left` for an interrupt of a block's breakout followed by another error: the interrupted
    breakout is reported by what ended it."""
    import io

    from rich.console import Console

    from autobot import log as log_mod

    first, second = KeyboardInterrupt(), signals.Terminated(signal.SIGTERM)
    ended = RuntimeError("x")
    ended.autobot_left = Left((first, second, ended), ())  # type: ignore[attr-defined]
    out = io.StringIO()
    saved, log_mod.console = log_mod.console, Console(file=out, soft_wrap=True, color_system=None)
    try:
        cli._left(None, ended, "s.yaml")
    finally:
        log_mod.console = saved
    assert out.getvalue().splitlines() == [
        "Breakout failed in s.yaml: interrupted",
        "Breakout failed in s.yaml: interrupted (SIGTERM)",
        f"{LEFT}a breakout did not finish",
    ]


# -- P5-91: the state a breakout starts in --------------------------------------------------------------

IN = {"cmd": "true"}  # the first prompt wait logs in; a `line` would be typed at the login prompt
# a block that enters a sub-shell and leaves it in its breakout
SUBSHELL = {"name": "sub", "enter": [{"line": "bash --norc --noprofile"}], "breakout": [{"control": "c"}, {"line": "exit"}]}
# a command that goes on for a while after Ctrl-C: what is typed meanwhile waits in the terminal, unread
SLOW_TO_STOP = """sh -c 'trap "sleep %s; exit" INT; sleep 30'"""
# case -> (script, make_doc keywords, the type of the script's error)
STATES: dict[str, tuple[list[dict[str, Any]], dict[str, Any], type[BaseException]]] = {
    "completed": ([{"cmd": "echo configured"}], {}, type(None)),
    "step-failure-at-a-prompt": ([{"cmd": "false"}], {}, StepFailure),
    "errors-match": ([{"cmd": "echo '% Invalid input'"}], {"errors": ["^% .*"]}, RuntimeError),
    "timeout-command-running": ([{"cmd": "sleep 30", "timeout": "1s"}], {}, TimeoutError),
    "timeout-program-reading": ([{"cmd": "cat", "timeout": "1s"}], {}, TimeoutError),
    "timeout-half-typed-line": ([IN, {"line": "echo half-typed \\"}, {"cmd": "echo never", "timeout": "1s"}], {}, TimeoutError),
    "after-timeout-after-a-line": ([IN, {"line": "sleep 30"}, {"control": "z", "after": "NEVER", "timeout": "1s"}], {}, TimeoutError),
    "in-a-block-with-its-own-breakout": ([IN, {"block": {**SUBSHELL, "script": [{"cmd": "sleep 30", "timeout": "1s"}]}}], {}, TimeoutError),
    # the command takes half a second to stop, and the sub-shell reads the block's `exit` only then
    "in-a-block-whose-command-stops-slowly": ([IN, {"block": {**SUBSHELL, "script": [{"cmd": SLOW_TO_STOP % "0.5", "timeout": "1s"}]}}], {}, TimeoutError),
}


@pytest.mark.parametrize("case", STATES)
def test_p5_91_recommended_breakout_logs_out_from_every_state(fake_device: FakeDevice, case: str, capsys):
    """SPEC "Logging out": whatever the script left (a prompt, a command that still runs, a program that
    reads the terminal, a line that is typed and not entered, a sub-shell a block just left), the
    breakout that starts with a control character gets its `logout` to the shell, and the login prompt
    confirms it. The device's shell is a real bash."""
    script, kw, error = STATES[case]
    doc, log = console(fake_device, script, SHAPE, **kw)
    if error is type(None):
        run(doc)
    else:
        with pytest.raises(error) as ei:
            run(doc)
        assert left(ei.value) is None, capsys.readouterr().err
    lines = FakeDevice.read(log)
    assert lines == ["LOGIN=admin", "PASSWORD=secret", "LOGOUT="]
    assert "breakout error" not in capsys.readouterr().err


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM, signal.SIGHUP], ids=lambda s: s.name)
def test_p5_91_recommended_breakout_logs_out_after_an_interrupt_or_a_signal(fake_device: FakeDevice, tmp_path: Path, sig: signal.Signals):
    """The same after Ctrl-C, SIGTERM and SIGHUP in the middle of a command, through the CLI."""
    doc, log = console(fake_device, [{"cmd": "sleep 30", "timeout": "20s"}], SHAPE)
    path = tmp_path / "script.autobot.yaml"
    path.write_text(yaml.safe_dump(doc))

    def setup() -> None:
        for s in (signal.SIGTERM, signal.SIGHUP):
            signal.signal(s, signal.SIG_DFL)
        signal.signal(signal.SIGINT, signal.default_int_handler)

    proc = subprocess.Popen(
        [sys.executable, "-W", "ignore", "-m", "autobot.cli", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        text=True, preexec_fn=setup,
    )
    try:
        deadline = time.monotonic() + 30
        while "PASSWORD=secret" not in FakeDevice.read(log):
            assert proc.poll() is None and time.monotonic() < deadline
            time.sleep(0.05)
        time.sleep(1.5)  # the `sleep 30` is running
        proc.send_signal(sig)
        err = proc.communicate(timeout=30)[1]
    finally:
        proc.kill()
    assert proc.returncode == -sig, err
    assert FakeDevice.read(log) == ["LOGIN=admin", "PASSWORD=secret", "LOGOUT="]
    assert "Breakout failed" not in err and "logged in" not in err


def test_p5_91_control_character_discards_a_line_the_far_side_has_not_read(fake_device: FakeDevice):
    """SPEC "control": a terminal throws away the input it holds when Ctrl-C arrives. The block's
    breakout sends Ctrl-C and `exit`; the command takes a second to stop, so `exit` is still unread
    when the Ctrl-C of an `attach.breakout` that doesn't wait arrives and discards it. The `logout`
    then goes to the sub-shell, which is no login shell, and the login prompt never comes."""
    script = [IN, {"block": {**SUBSHELL, "script": [{"cmd": SLOW_TO_STOP % "1", "timeout": "1s"}]}}]
    doc, log = console(fake_device, script, LOGOUT)
    with pytest.raises(TimeoutError, match="waiting for a shell prompt") as ei:
        run(doc)
    state = left(ei.value)
    assert state is not None and str(state.breakouts[0]) == CONFIRM_TIMEOUT
    assert "LOGOUT=" not in FakeDevice.read(log)


def test_p5_91_program_that_ignores_the_control_character_is_caught_by_the_confirmation(fake_device: FakeDevice):
    """What stays the script's job: the control character has to be one the device acts on. A program
    that ignores Ctrl-C (a pager that wants `q`) takes the `logout` line; the confirmation fails."""
    doc, log = console(fake_device, [{"cmd": "trap '' INT"}, {"cmd": "cat", "timeout": "1s"}], LOGOUT)
    with pytest.raises(TimeoutError, match="waiting for a shell prompt") as ei:
        run(doc)
    state = left(ei.value)
    assert state is not None and str(state.breakouts[0]) == CONFIRM_TIMEOUT
    assert "LOGOUT=" not in FakeDevice.read(log)


def test_p5_91_connection_that_is_closed_leaves_the_breakout_nothing_to_do(fake_device: FakeDevice):
    """After the connection has closed the breakout can't confirm anything: it fails, and is reported."""
    doc, _ = console(fake_device, [IN, {"line": "kill -9 $PPID"}, {"cmd": "true"}], LOGOUT)
    with pytest.raises(EOFError) as ei:
        run(doc)
    state = left(ei.value)
    assert state is not None and all(isinstance(e, EOFError) for e in state.breakouts)


def test_p5_91_failed_login_and_the_recommended_breakout(fake_device: FakeDevice):
    """A run that never got in: the login prompt is on the screen without an answer. The breakout's
    Ctrl-C does nothing there, its `logout` is read as a user name, and no Return is pressed by a wait."""
    spawn, log = fake_device("--accept", "admin:other", "--logout")
    doc = make_doc([{"cmd": "true"}], spawn=spawn, prompts=PROMPTS, vars=CREDS, breakout=LOGOUT)
    with pytest.raises(RuntimeError, match="responses exhausted"):
        run(doc)
    assert FakeDevice.read(log) == ["LOGIN=admin", "PASSWORD=secret", "LOGIN=logout"]


@pytest.mark.slow
def test_p5_91_logout_right_after_the_control_character_is_not_lost(fake_device: FakeDevice):
    """The `logout` line follows the Ctrl-C at once, at an idle prompt and with a command running: in
    twenty runs each it arrives every time."""
    for script in ([{"cmd": "true"}], [{"cmd": "sleep 30", "timeout": "500ms"}]):
        for _ in range(20):
            doc, log = console(fake_device, script, LOGOUT)
            try:
                run(doc)
            except TimeoutError as e:
                assert left(e) is None
            assert FakeDevice.read(log)[-1] == "LOGOUT="
