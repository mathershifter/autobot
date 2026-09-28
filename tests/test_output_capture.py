from __future__ import annotations

import pytest
from conftest import run_vars as run

from autobot.session import strip_echo


def test_newlines_preserved():
    out = run([{"cmd": "printf 'one\\ntwo\\n'", "register": "out"}])
    assert out["out"] == "one\ntwo"


def test_echo_removed():
    out = run([{"cmd": "echo hello", "register": "out"}])
    assert out["out"] == "hello"


def test_long_command_echo_removed():
    cmd = "echo " + " ".join(f"word{i}" for i in range(40))
    out = run([{"cmd": cmd, "register": "out"}])
    assert out["out"] == " ".join(f"word{i}" for i in range(40))


@pytest.mark.parametrize(
    "cmd", ["false-cmd-xyz; echo stopped", "false-cmd-xyz; echo stopped # is-system-running"]
)
def test_assert_does_not_match_echo(cmd: str):
    with pytest.raises(RuntimeError, match="assertion failed"):
        run([{"cmd": cmd, "assert": "running"}])


def test_assert_matches_output():
    out = run([{"cmd": "echo running", "assert": "running", "register": "out"}])
    assert out["out"] == "running"


def test_errors_ignore_echo():
    out = run(
        [{"cmd": "printf 'x\\n' # % comment", "register": "out"}],
        errors=["% .*"],
    )
    assert out["out"] == "x"


def test_errors_fire_on_output():
    with pytest.raises(RuntimeError, match="command error: % bad input"):
        run([{"cmd": "echo '% bad input'"}], errors=["% .*"])


def test_list_registers_all_lines():
    out = run([{"cmd": ["echo a", "echo b", "echo c"], "register": "out"}])
    assert out["out"] == "a\nb\nc"


def test_multiline_string_registers_all_lines():
    out = run([{"cmd": "echo a\n# ! a comment\necho b\n", "register": "out"}], errors=["! .*"])
    assert out["out"] == "a\nb"


def test_list_error_on_middle_line_raises():
    with pytest.raises(RuntimeError, match="command error: % middle"):
        run(
            [{"cmd": ["echo a", "echo '% middle'", "touch /nonexistent-autobot-marker"]}],
            errors=["% .*"],
        )


def test_list_error_on_middle_line_stops_remaining_lines():
    out = run(
        [
            {"cmd": ["echo a", "echo '% middle'", "echo c"], "ignore_error": True, "register": "out"},
            {"cmd": "echo after", "register": "after"},
        ],
        errors=["% .*"],
    )
    assert out["out"] == "a\n% middle"
    assert out["after"] == "after"


def test_ignored_error_registers_output():
    out = run(
        [
            {"cmd": "echo before; echo '% oops'", "ignore_error": True, "register": "out"},
            {"cmd": "echo next", "register": "next"},
        ],
        errors=["% .*"],
    )
    assert out["out"] == "before\n% oops"
    assert out["next"] == "next"


def test_ignored_rc_failure_registers_output():
    out = run([{"cmd": "echo partial; false", "ignore_error": True, "register": "out"}])
    assert out["out"] == "partial"


def test_echo_off_keeps_output():
    out = run(
        [
            {"cmd": "stty -echo"},
            {"cmd": "printf 'real\\n'", "register": "out"},
        ],
        spawn="bash --norc --noprofile --noediting -i",
    )
    assert out["out"] == "real"


def test_embedded_script_register():
    out = run([{"cmd": "#!/bin/sh\necho one\necho two\n", "register": "out"}])
    assert out["out"] == "one\ntwo"


def test_no_trailing_newline_output_dropped():
    out = run([{"cmd": "printf 'x\\ny'", "register": "out"}])
    assert out["out"] == "x"


@pytest.mark.parametrize(
    ("text", "sent", "expected"),
    [
        ("echo hi\nhi\n", "echo hi", "hi\n"),
        ("echo a very\r long line\nout\n", "echo a very long line", "out\n"),
        ("echo a\n b\nout\n", "echo a b", "out\n"),
        ("\r<9 word10\nout\n", "echo word9 word10", "out\n"),
        ("real\n", "printf 'real\\n'", "real\n"),
        ("hi\n", "", "hi\n"),
        # P8-05 (SPEC.md:112): echo wrapped mid-word
        ("echo ab\ncd\nout\n", "echo abcd", "out\n"),
        # P8-05: a mismatched echo leaves the output unchanged
        ("echo xyz\nout\n", "echo abc", "echo xyz\nout\n"),
        # P8-05: only a prefix of the echo, then nothing: unchanged
        ("echo a\n", "echo abc", "echo a\n"),
        # P8-05: scroll form whose tail doesn't match: unchanged
        ("\r<zzz\nout\n", "echo word9 word10", "\r<zzz\nout\n"),
        # P8-05: output identical to the command keeps the second copy
        ("echo hi\necho hi\n", "echo hi", "echo hi\n"),
        # P8-05: whitespace-only sent: unchanged
        ("  hi\n", "   ", "  hi\n"),
    ],
)
def test_strip_echo(text: str, sent: str, expected: str):
    """SPEC.md:112 (P8-05 extends the table)."""
    assert strip_echo(text, sent) == expected
