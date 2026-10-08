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
from conftest import BASH, DEVICE, SHELL_PROMPT, FakeDevice, default_signals, make_doc, make_runner, plugin_dist, run_cli

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
PATTERN = "[Ll]ogin: ?$"  # the login prompt at the end of the output
CONFIRM = {"block": {"name": "logged out"}, "after": PATTERN, "timeout": "4s"}
# drop what is typed or running, log out, wait for the proof
LOGOUT = [{"control": "c"}, {"line": "logout"}, CONFIRM]
# the documented shape: the same, and the Ctrl-C waits for the far side to read what was sent before it
SHAPE = [{"control": "c", "delay_before": "2s"}, {"line": "logout"}, CONFIRM]
# the same without the control character
BARE = [{"line": "logout"}, CONFIRM]
CONFIRM_TIMEOUT = f"timed out after 4.0s waiting for the after pattern '{PATTERN}'"
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


defaults = default_signals


def wait_for(what: Any, proc: subprocess.Popen, seconds: float = 30) -> None:
    deadline = time.monotonic() + seconds
    while not what():
        assert proc.poll() is None and time.monotonic() < deadline
        time.sleep(0.05)


def signalled(doc: dict[str, Any], tmp_path: Path, sig: int, running: str) -> tuple[int, str]:
    """Run the CLI on `doc` and send it `sig` once the shell has echoed the command `running`: from then
    on that command is what runs. Its return code and its messages."""
    path = tmp_path / "script.autobot.yaml"
    path.write_text(yaml.safe_dump(doc))
    echo, messages = tmp_path / "stdout", tmp_path / "stderr"
    with open(echo, "w") as out, open(messages, "w") as err:
        proc = subprocess.Popen([sys.executable, "-W", "ignore", "-m", "autobot.cli", str(path)], stdout=out, stderr=err, preexec_fn=defaults)
        try:
            wait_for(lambda: f"\n{running}\n" in echo.read_text().replace("PROMPT$ ", "\n"), proc)
            proc.send_signal(sig)
            proc.wait(timeout=60)
        finally:
            proc.kill()
    return proc.returncode, messages.read_text()


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
    [CONFIRM, {"control": "c", "after": PATTERN, "timeout": "4s"}, {"line": "", "after": PATTERN}],
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
    with pytest.raises(BreakoutError, match="waiting for the after pattern"):
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
    ">> no logout: the run sent credentials (prompt 'login') outside a block with a breakout, and the script has no "
    "attach.breakout to log out with before the session is closed"
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
    assert ">> no logout: the run sent credentials (prompts 'user', 'pw') outside a block" in res.stderr


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
    returncode, err = signalled(doc, tmp_path, signal.SIGTERM, "sleep 30")
    assert began.exists() and returncode == -signal.SIGTERM, err
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
    returncode, err = signalled(doc, tmp_path, sig, "sleep 30")
    assert returncode == -sig, err
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
    twenty runs each it arrives every time. The command is running for certain: the process that
    printed `ready` is the one that becomes `sleep`."""
    running = [IN, {"line": """sh -c "echo rea''dy; exec sleep 30\""""}, {**READY, "after": "ready\r\n"}]
    # the wait is long: what is counted is whether the logout arrives, not how soon on a busy machine
    breakout = [{"control": "c"}, {"line": "logout"}, {**CONFIRM, "timeout": "20s"}]
    for script in ([{"cmd": "true"}], running):
        for _ in range(20):
            doc, log = console(fake_device, script, breakout)
            run(doc)
            assert FakeDevice.read(log)[-1] == "LOGOUT="


# -- P5-92: what confirms a logout ----------------------------------------------------------------------

WORD = {"block": {"name": "logged out"}, "after": "login:", "timeout": "4s"}  # the word, wherever it is
READY = {"block": {"name": "the program runs"}, "timeout": "10s"}  # carries the wait for a program's first output


def confirmed_by(confirm: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"control": "c", "delay_before": "2s"}, {"line": "logout"}, confirm]


@pytest.mark.parametrize("confirm", [CONFIRM, WORD], ids=["prompt-at-the-end", "the-word"])
def test_p5_92_last_login_banner_is_no_login_prompt(fake_device: FakeDevice, tmp_path: Path, confirm: dict[str, Any]):
    """SPEC "Logging out": SIGTERM arrives after the password was accepted and before the shell is there.
    The device prints `Last login: ...` three seconds later, while the breakout runs, and starts its
    shell four seconds after that. The pattern for the prompt at the end of the output is not met by the
    banner: the breakout waits until the shell has taken the `logout` and the device asks for a login
    again. The bare word is met by the banner, and the session is closed with the device still logged
    in, and no word of it in the report."""
    # the login prompt of this script is one at the end of the output, too: `login:` alone would take
    # the banner for a second login prompt and answer it
    login = {**LOGIN, "send": {"each": "vars.creds", "fields": [{"match": "login: $", "field": "username"}, {"match": "Password:", "field": "password"}]}}
    breakout = confirmed_by({**confirm, "timeout": "15s"})
    doc, log = console(
        fake_device, [{"cmd": "sleep 30", "timeout": "20s"}], breakout,
        "--last-login", "--banner-delay", "3", "--post-auth-delay", "4", prompts=[SHELL_PROMPT, login],
    )
    path = tmp_path / "script.autobot.yaml"
    path.write_text(yaml.safe_dump(doc))
    proc = subprocess.Popen(
        [sys.executable, "-W", "ignore", "-m", "autobot.cli", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        text=True, preexec_fn=defaults,
    )
    try:
        wait_for(lambda: "PASSWORD=secret" in FakeDevice.read(log), proc)
        proc.send_signal(signal.SIGTERM)
        err = proc.communicate(timeout=60)[1]
    finally:
        proc.kill()
    assert proc.returncode == -signal.SIGTERM, err
    assert "Breakout failed" not in err, err
    assert ("LOGOUT=" in FakeDevice.read(log)) == (confirm is CONFIRM)


@pytest.mark.parametrize("confirm", [CONFIRM, WORD], ids=["prompt-at-the-end", "the-word"])
def test_p5_92_login_prompt_with_a_capital_is_a_login_prompt(fake_device: FakeDevice, confirm: dict[str, Any]):
    """A device that asks `Login: `. The pattern takes either case; `login:` alone waits out its timeout
    on a device that is logged out, and the run reports a session that may be left logged in."""
    login = {**LOGIN, "send": {"each": "vars.creds", "fields": [{"match": "[Ll]ogin:", "field": "username"}, {"match": "Password:", "field": "password"}]}}
    doc, log = console(fake_device, [{"cmd": "true"}], confirmed_by(confirm), "--capital", prompts=[SHELL_PROMPT, login])
    if confirm is CONFIRM:
        run(doc)
    else:
        with pytest.raises(BreakoutError):
            run(doc)
    assert FakeDevice.read(log) == ["LOGIN=admin", "PASSWORD=secret", "LOGOUT="]


@pytest.mark.parametrize("confirm", [CONFIRM, WORD], ids=["prompt-at-the-end", "the-word"])
def test_p5_92_unread_output_with_the_word_in_it_is_no_login_prompt(fake_device: FakeDevice, confirm: dict[str, Any]):
    """Output from before the logout that nothing has read, with `login:` in it, and a program that
    takes the `logout` line. The pattern for the end of the output is not met and the breakout fails,
    as it should; the bare word is met by the old text, and the run completes with the device logged in."""
    # the wait for the first line of output tells that the shell has read the line; the second stays unread
    script = [IN, {"cmd": "trap '' INT"}, {"line": "echo rea''dy; echo 3 failed login: attempts; cat"}, {**READY, "after": "ready\r\n"}]
    doc, log = console(fake_device, script, confirmed_by(confirm))
    if confirm is CONFIRM:
        with pytest.raises(BreakoutError, match="waiting for the after pattern"):
            run(doc)
    else:
        run(doc)
    assert "LOGOUT=" not in FakeDevice.read(log)


def test_p5_92_dollar_is_the_end_of_what_has_arrived(shell_session: Session):
    """What `$` means in an `after` pattern, and where the pattern stops telling a prompt from a banner:
    the end of the unread output at the time the pattern is tried. Text that ends in `login: ` and is
    followed by more in the same write doesn't meet it; the same text meets it while it is the last
    thing that has arrived, whether it came before the wait or stops there in the middle of a line."""
    s = shell_session
    s.get_prompt(timeout=5)
    s.sendline("stty -echo", timeout=5)  # the echo of the lines below has the word in it, and comes in pieces
    s.get_prompt(timeout=5)
    s.sendline("printf 'Last login: Tue Oct 7\\n'; sleep 1; printf 'hostname login: '; sleep 2; echo", timeout=5)
    started = time.monotonic()
    s.expect([PATTERN], timeout=10)
    assert time.monotonic() - started > 0.8 and s.ctx["before"].endswith("hostname ")  # the banner was passed over
    s.get_prompt(timeout=10)
    # a banner that arrives in two pieces, with a pause after `Last login: `: met at the pause
    s.sendline("printf 'Last login: '; sleep 2; printf 'Tue Oct 7\\n'", timeout=5)
    started = time.monotonic()
    s.expect([PATTERN], timeout=10)
    assert time.monotonic() - started < 1.5 and s.ctx["before"].endswith("Last ")


# -- P5-93: where the credentials were sent, what the report says, and a runner that runs again -------


def in_a_block(fake_device: FakeDevice, breakout: bool, inner: bool = False) -> tuple[dict[str, Any], Path]:
    """A script that reaches the console from a shell, inside a block: the block's `enter` starts the
    device, its first `cmd` logs in. With `breakout`, the block logs out and leaves the device with
    Ctrl-]. With `inner`, the login happens in a block of its own, without a breakout, inside that one."""
    device, log = fake_device(*ACCEPT, "--escape")
    leave = [{"control": "c"}, {"line": "logout"}, {"control": "]", "after": PATTERN, "timeout": "5s"}, {"cmd": "true", "timeout": "5s"}]
    login = [{"line": device}, {"cmd": "echo in", "timeout": "5s"}]
    block: dict[str, Any] = {"name": "console", "enter": [{"block": {"name": "login", "script": login}}] if inner else login}
    if breakout:
        block["breakout"] = leave
    return make_doc([IN, {"block": block}], spawn=BASH, prompts=PROMPTS, vars=CREDS), log


@pytest.mark.parametrize("inner", [False, True], ids=["in-the-block", "in-a-block-inside-it"])
def test_p5_93_credentials_sent_in_a_block_with_a_breakout_are_that_breakouts(fake_device: FakeDevice, capfd, inner: bool):
    """SPEC "A breakout that doesn't finish": the login happens inside a block whose breakout logs out.
    The script has no `attach.breakout` and needs none: there is no `no logout` warning."""
    doc, log = in_a_block(fake_device, breakout=True, inner=inner)
    runner = run(doc)
    assert FakeDevice.read(log) == ["LOGIN=admin", "PASSWORD=secret", "LOGOUT=", "DETACH="]
    assert runner.session.logins == ("login",) and runner.session.logins_open == ()
    assert "no logout" not in capfd.readouterr().err


def test_p5_93_credentials_sent_in_a_block_without_a_breakout_are_warned_of(fake_device: FakeDevice, capfd):
    """The same block without a breakout: nothing logs out, and the warning says so."""
    doc, _ = in_a_block(fake_device, breakout=False)
    runner = run(doc)
    assert runner.session.logins_open == ("login",)
    assert NO_LOGOUT in capfd.readouterr().err.splitlines()


def test_p5_93_runner_that_runs_again_starts_with_nothing_left(fake_device: FakeDevice, tmp_path: Path):
    """A breakout that fails in the first run and not in the second: the second run completes. What the
    first run left is not carried over."""
    once = tmp_path / "once"
    spawn, _ = fake_device("--order", "none")
    runner = make_runner([{"cmd": "true"}], spawn=spawn, breakout=[{"cmd": f"test -e {once} || {{ touch {once}; false; }}", "timeout": "5s"}])
    with pytest.raises(BreakoutError):
        runner.run()
    runner.run()
    assert runner._unfinished == []


def test_p5_93_failed_login_is_reported_as_credentials_that_reached_no_prompt(fake_device: FakeDevice, tmp_path: Path):
    """SPEC "Errors while the script runs": the device refuses the credentials. The breakout's `logout` is
    read as a user name, its wait is not met, and the last line of the report says what is known: the
    credentials were sent, and no shell prompt came after them."""
    spawn, log = fake_device("--accept", "admin:other", "--logout")
    res = run_cli(make_doc([{"cmd": "true"}], spawn=spawn, prompts=PROMPTS, vars=CREDS, breakout=LOGOUT), tmp_path)
    assert res.returncode == 3, res.stderr
    lines = report(res)
    assert lines[0].endswith("prompt 'login': responses exhausted")
    assert lines[-1] == f"{LEFT}a breakout did not finish; credentials were sent (prompt 'login'), and no shell prompt was reached after them"
    assert FakeDevice.read(log) == ["LOGIN=admin", "PASSWORD=secret", "LOGIN=logout"]


def test_p5_93_login_pending_ends_at_a_shell_prompt(fake_device: FakeDevice):
    """`Session.login_pending`: false before any answer, true once credentials are sent, false again
    when a prompt wait ends at a shell prompt."""
    spawn, _ = fake_device("--accept", "admin:secret", "--post-auth-delay", "1")
    s = Session([PromptHandler("sh", [r"PROMPT\$ "], [], True), PromptHandler("login", ["login:", "Password:"], [["admin", "secret"]], False, [0, 1])])
    s.attach(spawn, timeout=5)
    try:
        assert s.login_pending is False
        with pytest.raises(TimeoutError):
            s.get_prompt(timeout=0.5)
        assert s.login_pending is True and s.logins_open == ("login",)
        s.get_prompt(timeout=5)
        assert s.login_pending is False and s.logins == ("login",)
    finally:
        s.detach(failing=True)


def test_p5_93_closed_connection_is_reported_as_one(fake_device: FakeDevice, tmp_path: Path):
    """The connection closes in the script, so the breakout has nobody to talk to. The last line says
    that, and what it may mean behind a console server, instead of a session that may be left logged in."""
    doc, _ = console(fake_device, [IN, {"line": "kill -9 $PPID"}, {"cmd": "true"}], LOGOUT)
    res = run_cli(doc, tmp_path)
    assert res.returncode == 3, res.stderr
    lines = report(res)
    assert lines[0].endswith("connection closed while waiting for a shell prompt ('sh')")
    assert lines[-1] == (
        "Connection closed before a breakout finished: a console behind a console server may still be logged in; "
        "the run sent credentials (prompt 'login')"
    )
    assert not any(line.startswith(LEFT) for line in lines)


BREAKOUT_BUG = """
import pydantic


class OopsStep(pydantic.BaseModel):
    oops: str


class OopsExecutor:
    key = "oops"
    model = OopsStep

    def execute(self, step, ctx, timeout):
        if step.oops == "os":
            raise OSError(28, "No space left on device")
        raise ValueError("plugin bug " + step.oops)
"""


def test_p5_93_bug_in_a_breakout_step_is_reported_as_a_bug(fake_device: FakeDevice, tmp_path: Path):
    """An error in a breakout that no device explains, here a plugin's `ValueError`: the breakout's
    report says it is unexpected, names the plugin and prints the traceback. The script completed, so
    the status is 4, and the last line still says what may be left."""
    root = tmp_path / "plugin"
    root.mkdir()
    plugin_dist(root, "oops", BREAKOUT_BUG, "OopsExecutor")
    doc, _ = console(fake_device, [{"cmd": "true"}], [{"oops": "value"}, *LOGOUT])
    res = run_cli(doc, tmp_path, pythonpath=root)
    path = tmp_path / "script.autobot.yaml"
    assert res.returncode == 4, res.stderr
    lines = report(res)
    assert lines[:4] == [
        f"Breakout failed in {path}: unexpected error (ValueError): plugin bug value",
        "Unexpected error in plugin 'oops': this is a bug in the plugin, not in the script. "
        "Please report it to the plugin's author with the traceback below.",
        "  at attach.breakout.0 (oops)",
        "Traceback (most recent call last):",
    ]
    assert lines[-2] == "ValueError: plugin bug value"
    assert lines[-1] == f"{LEFT}the script completed, but a breakout did not finish{SENT}"


def test_p5_93_operating_system_error_in_a_breakout_points_to_the_traceback(fake_device: FakeDevice, tmp_path: Path):
    """An `OSError` from the engine's side of a breakout step is a failed breakout whose report ends with
    the pointer to `--traceback`, as the report of a failed run does; from a plugin's own code it is the
    plugin's bug."""
    root = tmp_path / "plugin"
    root.mkdir()
    plugin_dist(root, "oops", BREAKOUT_BUG, "OopsExecutor")
    doc, _ = console(fake_device, [{"cmd": "true"}], [{"oops": "os"}])
    res = run_cli(doc, tmp_path, pythonpath=root)
    lines = report(res)
    assert res.returncode == 4 and lines[1].startswith("Unexpected error in plugin 'oops'") and "OSError: [Errno 28] No space left on device" in lines

    import io

    from rich.console import Console

    from autobot import log as log_mod

    error = OSError(28, "No space left on device")
    ended = BreakoutError("x")
    ended.autobot_left = Left((error,), ())  # type: ignore[attr-defined]
    out = io.StringIO()
    saved, log_mod.console = log_mod.console, Console(file=out, soft_wrap=True, color_system=None)
    try:
        cli._left(None, ended, "s.yaml")
    finally:
        log_mod.console = saved
    assert out.getvalue().splitlines() == [
        "Breakout failed in s.yaml: [Errno 28] No space left on device",
        "  (run with --traceback for details)",
        f"{LEFT}the script completed, but a breakout did not finish",
    ]
