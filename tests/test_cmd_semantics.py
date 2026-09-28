"""P1: ``cmd`` success semantics, ``register`` and ``ignore_error``.

SPEC.md:101-138 and 324. Tests are named ``test_p1_NN_*`` after the
test plan rows (tests/TEST_PLAN.md).
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from conftest import RC_PROBE, SentLog, run_vars, steps

from autobot.runner import Runner
from autobot.session import CommandError

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

    The session prints ``abc``, then (after the solicit newline's echo)
    ``def``; the captured text is exactly what was printed.
    """
    out = run_vars([{"cmd": "printf abc; sleep 6; echo def", "register": "out", "timeout": "15s"}])
    assert out["out"] == "abc\ndef"


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
