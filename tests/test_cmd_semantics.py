"""P1: ``cmd`` success semantics, ``register`` and ``ignore_error``.

SPEC.md:101-138 and 324. Tests are named ``test_p1_NN_*`` after the
test plan rows (tests/TEST_PLAN.md).
"""

from __future__ import annotations

import re
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import (
    RC_PROBE,
    SHELL_PROMPT,
    ProbeExecutor,
    SentLog,
    Timeline,
    make_runner,
    run_vars,
    steps,
)

from autobot.runner import Runner
from autobot.session import CommandError, PromptHandler, Session
from autobot.steps import StepFailure

PCT_ERR = ["% .*"]


def test_p1_01_assert_replaces_rc_check(sent: SentLog):
    """SPEC.md:104-107: with assert, no $? check is made."""
    run_vars([{"cmd": "echo running; false", "assert": "running"}])
    assert RC_PROBE not in sent.lines()


def test_p1_02_errors_without_assert_replace_rc_check(sent: SentLog):
    """SPEC.md:105, 107: top-level errors replace the $? check."""
    run_vars([{"cmd": "false"}], errors=PCT_ERR)
    assert RC_PROBE not in sent.lines()


def test_p1_03_no_assert_no_errors_nonzero_rc_raises(sent: SentLog):
    """SPEC.md:105: without assert/errors, a nonzero $? raises."""
    with pytest.raises(RuntimeError, match="exit code 7"):
        run_vars([{"cmd": "(exit 7)"}])
    assert sent.lines().count(RC_PROBE) == 1


def test_p1_04_rc_check_uses_last_line_only():
    """SPEC.md:105: only the last line's return code is checked."""
    run_vars([{"cmd": ["false", "true"]}])


def test_p1_05_assert_and_errors_errors_still_raise():
    """SPEC.md:101, 107: errors are checked per line even when assert is set."""
    with pytest.raises(CommandError, match="command error: % bad"):
        run_vars([{"cmd": "echo '% bad'; echo running", "assert": "running"}], errors=PCT_ERR)


def test_p1_06_assert_is_rendered():
    """SPEC.md:317: assert is a Jinja2 template."""
    step = {"cmd": "echo running", "assert": "{{ vars.want }}"}
    run_vars([step], vars={"want": "running"})
    with pytest.raises(RuntimeError, match="assertion failed"):
        run_vars([step], vars={"want": "nope"})


def test_p1_07_assert_any_of_list():
    """SPEC.md:104: any matching assert pattern passes."""
    run_vars([{"cmd": "echo running", "assert": ["absent", "running"]}])
    with pytest.raises(RuntimeError, match="assertion failed"):
        run_vars([{"cmd": "echo running", "assert": ["absent", "other"]}])


def test_p1_08_assert_checks_output_of_all_lines():
    """SPEC.md:104: assert sees the captured output of all lines."""
    run_vars([{"cmd": ["echo alpha", "echo beta"], "assert": "alpha"}])


def test_p1_09_errors_match_multiline_anchor():
    """SPEC.md:116: errors use re.MULTILINE, so ^ matches any line."""
    with pytest.raises(CommandError, match="command error: ERR x"):
        run_vars([{"cmd": "printf 'ok\\nERR x\\n'"}], errors=["^ERR.*"])


def test_p1_10_errors_dot_does_not_cross_lines():
    """SPEC.md:116: '.' does not cross line boundaries."""
    with pytest.raises(CommandError) as ei:
        run_vars([{"cmd": "printf '%% a\\nb\\n'"}], errors=PCT_ERR)
    assert str(ei.value) == "command error: % a"


def test_p1_11_errors_leave_session_at_prompt(
    attached_runner: Callable[..., Runner], sent: SentLog
):
    """SPEC.md:116: after an errors match the session is left at the prompt."""
    r = attached_runner(errors=PCT_ERR)
    with pytest.raises(CommandError):
        r.run_steps(steps([{"cmd": "echo '% oops'", "timeout": "5s"}]))
    sent.clear()
    r.run_steps(steps([{"cmd": "echo next", "register": "n", "timeout": "5s"}]))
    assert r.config.vars["n"] == "next"
    assert sent.lines() == ["echo next"]


def test_p1_12_register_value_is_stripped():
    """SPEC.md:127: registered output is whitespace-stripped."""
    out = run_vars([{"cmd": "printf '\\n  x  \\n\\n'", "register": "out"}])
    assert out["out"] == "x"


def test_p1_13_register_with_errors_config_on_success():
    """SPEC.md:136: register works when errors are configured."""
    out = run_vars([{"cmd": "echo fine", "register": "out"}], errors=PCT_ERR)
    assert out["out"] == "fine"


def test_p1_14_unignored_failure_registers_nothing(attached_runner: Callable[..., Runner]):
    """SPEC.md:136: a failing, non-ignored step stores nothing."""
    r = attached_runner(vars={"out": "old"})
    with pytest.raises(RuntimeError, match="exit code 1"):
        r.run_steps(steps([{"cmd": "echo x; false", "register": "out", "timeout": "5s"}]))
    assert r.config.vars["out"] == "old"


def test_p1_15_ignored_assert_failure_registers_output():
    """SPEC.md:138: an ignored assert failure still registers the output."""
    out = run_vars(
        [
            {"cmd": "echo nope", "assert": "yes", "ignore_error": True, "register": "out"},
            {"cmd": "echo next", "register": "next"},
        ]
    )
    assert out["out"] == "nope"
    assert out["next"] == "next"


def test_p1_16_ignored_error_in_list_stops_at_failing_line(sent: SentLog):
    """SPEC.md:101, 138: output through the failing line; later lines not sent."""
    out = run_vars(
        [{"cmd": ["echo a", "echo '% b'", "echo c"], "ignore_error": True, "register": "out"}],
        errors=PCT_ERR,
    )
    assert out["out"] == "a\n% b"
    assert "echo c" not in sent.lines()


def test_p1_17_session_before_not_clobbered_by_rc_probe():
    """SPEC.md:324: the internal $? probe does not replace session.before."""
    out = run_vars(
        [
            {"cmd": "echo MARKX"},
            {
                "cmd": "echo ran",
                "register": "r",
                "when": "{{ session.before | contains('MARKX') }}",
            },
        ]
    )
    assert out["r"] == "ran"


@pytest.mark.slow
def test_p1_18_output_spanning_idle_poll_not_duplicated():
    """SPEC.md:111-114: output printed across a 5s idle poll is captured once.

    The session prints ``abc``, then, more than one poll later, ``def``; the captured text is
    exactly what was printed. No solicit newline is sent while the command runs (P4-37), so
    its echo no longer splits the output into ``abc\\ndef``.
    """
    out = run_vars([{"cmd": "printf abc; sleep 6; echo def", "register": "out", "timeout": "15s"}])
    assert out["out"] == "abcdef"


def test_p1_19_session_before_cleared_by_empty_output(attached_runner: Callable[..., Runner]):
    """SPEC.md:324: session.before is the last command's (empty) output."""
    r = attached_runner(errors=PCT_ERR)
    r.run_steps(steps([{"cmd": "echo MARKX", "timeout": "5s"}, {"cmd": "true", "timeout": "5s"}]))
    assert r.session.ctx["before"] == ""
    skipped = steps(
        [
            {
                "cmd": "echo ran",
                "register": "r",
                "when": "{{ session.before | contains('MARKX') }}",
                "timeout": "5s",
            }
        ]
    )
    r.run_steps(skipped)
    assert "r" not in r.config.vars


# -- P1-20: ignore_error covers command failures only (decision #10) --------

# `LOG%s: ` keeps the echoed command from matching the `LOGIN: ` pattern
TWO_LOGINS = "printf 'LOG%s: ' IN; read a; printf 'LOG%s: ' IN; read b"
ONE_SHOT_LOGIN = {"name": "login", "expect": ["LOGIN: "], "send": {"each": "vars.logins"}}


def aborting_runner(step: dict, **kw) -> Runner:
    """``step`` with ignore_error and register r (preset), then a probe step."""
    return make_runner(
        [{**step, "ignore_error": True, "register": "r"}, {"probe": "next"}],
        vars={"r": "preset", "logins": ["x"]},
        **kw,
    )


def assert_aborted(runner: Runner, probe: ProbeExecutor) -> None:
    assert probe.calls == [], "the step after the aborting step ran"
    assert runner.config.vars["r"] == "preset"


def test_p1_20_ignore_error_does_not_swallow_timeout(probe: ProbeExecutor):
    """SPEC "cmd" ignore_error: a prompt timeout aborts despite ignore_error."""
    r = aborting_runner({"cmd": "sleep 30", "timeout": 1})
    with pytest.raises(TimeoutError):
        r.run()
    assert_aborted(r, probe)


def test_p1_20_ignore_error_does_not_swallow_eof(probe: ProbeExecutor):
    """SPEC "cmd" ignore_error: a closed connection (EOFError) aborts.

    Uses the real shell rather than F3: ``exit`` closes the session before
    the next prompt.
    """
    r = aborting_runner({"cmd": "exit"})
    with pytest.raises(EOFError):
        r.run()
    assert_aborted(r, probe)


def test_p1_20_ignore_error_does_not_swallow_template_error(probe: ProbeExecutor, sent: SentLog):
    """SPEC "cmd" ignore_error: an undefined template variable aborts."""
    r = aborting_runner({"cmd": "echo {{ vars.nope }}"})
    with pytest.raises(ValueError, match="^template error: "):
        r.run()
    assert_aborted(r, probe)
    assert sent.commands() == []


def test_p1_20_ignore_error_does_not_swallow_invalid_regex(probe: ProbeExecutor):
    """SPEC "cmd" ignore_error: an assert that renders to an invalid regular expression aborts.

    It is a ``ValueError`` naming the pattern, not a raw ``re.error``; one that isn't a template is
    rejected when the script is loaded (P6-65).
    """
    r = aborting_runner({"cmd": "echo hi", "assert": "{{ '(' }}"})
    with pytest.raises(ValueError, match=r"^assert: invalid regex '\(': missing \), unterminated subpattern") as ei:
        r.run()
    assert isinstance(ei.value.__cause__, re.error)
    assert_aborted(r, probe)


def test_p1_21_assert_rendering_to_an_empty_regex_aborts(probe: ProbeExecutor):
    """SPEC "cmd": an assert pattern that renders to nothing would match any output; it aborts instead."""
    r = aborting_runner({"cmd": "echo hi", "assert": ["hi", "{{ vars.empty }}"]})
    r.config.vars["empty"] = ""
    with pytest.raises(ValueError, match="^assert: a pattern rendered to an empty regex, which matches any output$"):
        r.run()
    assert_aborted(r, probe)


def test_p1_20_ignore_error_does_not_swallow_responses_exhausted(probe: ProbeExecutor):
    """SPEC "cmd" ignore_error: a prompt-response failure inside the command aborts.

    The command prompts ``LOGIN: `` twice; the prompt's sendEach has one item, so
    the second match raises ``responses exhausted``.
    """
    r = aborting_runner({"cmd": TWO_LOGINS}, prompts=[SHELL_PROMPT, ONE_SHOT_LOGIN])
    with pytest.raises(RuntimeError, match=r"^prompt 'login': responses exhausted$"):
        r.run()
    assert_aborted(r, probe)


def test_p1_22_empty_cmd_list_sends_no_exit_code_check(sent: SentLog):
    """SPEC "cmd": `cmd: []` sends nothing, so it can't fail on an earlier command's exit code."""
    out = run_vars([{"cmd": "echo hi; false", "assert": "hi"}, {"cmd": [], "register": "r"}])
    assert sent.lines() == ["echo hi; false"]
    assert out["r"] == ""


@pytest.mark.parametrize("errors", [None, PCT_ERR], ids=["rc", "errors"])
def test_p1_22_empty_cmd_list_waits_for_no_prompt(sent: SentLog, timeline: Timeline, errors):
    """SPEC "cmd": a prompt wait may press Return or answer a prompt, so `cmd: []` makes none."""
    out = run_vars([{"cmd": [], "register": "r"}], vars={"r": "old"}, errors=errors)
    assert sent == []
    assert "get_prompt" not in timeline.names()
    assert out["r"] == ""


def test_p1_22_empty_cmd_list_keeps_after_and_delays(sent: SentLog, timeline: Timeline):
    """SPEC "cmd": the common step properties of a `cmd: []` step still apply."""
    run_vars(
        [
            {"line": "printf 'pre%s\\n' READY"},
            {"cmd": [], "after": "preREADY", "delay_before": "1s", "delay_after": "2s"},
        ]
    )
    i = timeline.index_of(("expect", ["preREADY"]))
    assert list(timeline[i + 1 :]) == [("sleep", 1.0), ("sleep", 2.0)]
    assert len(sent.lines()) == 1


def test_p1_22_empty_cmd_list_assert_checks_empty_output(sent: SentLog, probe: ProbeExecutor):
    """SPEC "cmd": the output of no lines is empty, so an assert fails unless it matches the empty string."""
    with pytest.raises(StepFailure, match="^assertion failed: expected "):
        run_vars([{"cmd": [], "assert": "hi", "register": "r"}, {"probe": "next"}])
    assert probe.calls == []
    out = run_vars(
        [
            {"cmd": [], "assert": "hi", "ignore_error": True, "register": "ignored"},
            {"cmd": [], "assert": "^$", "register": "matched"},
        ]
    )
    assert out == {"ignored": "", "matched": ""}
    assert sent == []


@pytest.mark.parametrize("cmd", ["", [""], "{{ '' }}", "\n  \n", ["", "{{ '' }}"]], ids=repr)
def test_p1_22_empty_line_is_still_sent_and_checked(sent: SentLog, cmd):
    """SPEC "cmd": only `cmd: []` has no lines; an empty or blank string sends one empty line per item."""
    run_vars([{"cmd": cmd}])
    n = len(cmd) if isinstance(cmd, list) else 1
    assert sent.lines() == [""] * n + [RC_PROBE]


# -- P1-23: the `$?` marker arriving in pieces --------------------------------

SPLIT_RC_DEVICE = """
import os, sys, time
tail = b"" if sys.argv[1] == "-" else sys.argv[1].encode().decode("unicode_escape").encode()
os.write(1, b"PROMPT$ ")
while data := os.read(0, 4096):
    if b"__AUTOBOT_RC" in data:
        os.write(1, b"__AUTOBOT_RC=1")
        time.sleep(0.5)
        os.write(1, b"27" + tail)
    else:
        os.write(1, b"out\\r\\n")
    os.write(1, b"PROMPT$ ")
"""


@pytest.fixture
def split_rc_device(tmp_path: Path) -> Callable[[str], str]:
    """A device whose exit code is 127, printed as `1`, a 0.5s pause, then `27` and `tail`."""
    path = tmp_path / "split_rc.py"
    path.write_text(SPLIT_RC_DEVICE)
    return lambda tail: f"{sys.executable} {path} '{tail or '-'}'"


@pytest.mark.parametrize("tail", [r"\r\n", r"\n", r"\x1b[0m\r\n", ""], ids=["crlf", "lf", "ansi", "prompt"])
def test_p1_23_exit_code_split_across_reads_is_read_whole(split_rc_device: Callable[[str], str], tail: str):
    """SPEC "cmd": the `$?` check reads the whole exit code, also when its digits arrive in two reads."""
    s = Session([PromptHandler("sh", [r"PROMPT\$ "], [], True)])
    s.attach(split_rc_device(tail), env={"PATH": "/usr/bin:/bin"}, timeout=5)
    try:
        s.get_prompt(timeout=5)
        s.sendline("x")
        assert s.get_prompt(timeout=5) == "out\n"
        assert s.check_rc(timeout=5) == 127
        # the rest of the marker's line is consumed: the next command's output starts clean
        s.sendline("y")
        assert s.get_prompt(timeout=5) == "out\n"
    finally:
        s.detach()


def test_p1_23_split_exit_code_fails_the_step_with_the_real_code(split_rc_device: Callable[[str], str]):
    """SPEC "cmd": the step reports the command's exit code, not its first digit."""
    with pytest.raises(StepFailure, match=r"^command returned exit code 127$"):
        run_vars([{"cmd": "x"}], spawn=split_rc_device(r"\r\n"), attach_env={"PATH": "/usr/bin:/bin"})


def test_p1_23_echoed_probe_is_not_taken_for_the_marker(shell_session: Session):
    """The echo of `echo __AUTOBOT_RC=$?` has no digits, so only the printed marker matches."""
    s = shell_session
    s.get_prompt(timeout=5)
    for cmd, rc in [("(exit 3)", 3), ("true", 0), ("(exit 127)", 127)]:
        s.sendline(cmd)
        s.get_prompt(timeout=5)
        assert s.check_rc(timeout=5) == rc
        s.sendline("echo next")
        assert s.get_prompt(timeout=5) == "next\n"
