from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest
from conftest import BASH, make_doc, run_cli
from conftest import run_vars as run

from autobot import screen as screen_mod
from autobot.screen import ANSI_ESCAPE_RE, CleanWriter, strip_echo


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


# -- #20: operator log lines print user and device text verbatim ---------------


@pytest.mark.parametrize("text", ["[/x]", "[bold]x[/bold]"], ids=["closing-tag", "bold-tags"])
def test_p8_15_log_lines_print_markup_like_text_verbatim(tmp_path: Path, text: str):
    """`[...]` in a command, block name or spawn line is logged literally, not as rich markup."""
    doc = make_doc(
        [{"block": {"name": text, "script": [{"cmd": f'echo "{text}"', "register": "out"}]}}],
        spawn=f"env X={text} {BASH}",
    )
    res = run_cli(doc, tmp_path)
    assert res.returncode == 0, res.stderr
    assert "Traceback" not in res.stderr
    lines = res.stderr.splitlines()
    assert f">> attach: env X={text} {BASH}" in lines
    assert f">> block enter: {text}" in lines
    assert f'>>   cmd: echo "{text}"' in lines  # a step of the block
    assert f">> block completed: {text}" in lines


def test_p8_16_log_lines_are_not_wrapped(tmp_path: Path):
    """A `>> cmd:` line longer than 80 columns stays on one line when stderr isn't a terminal."""
    cmd = "echo " + " ".join(f"word{i}" for i in range(40))
    res = run_cli(make_doc([{"cmd": cmd}]), tmp_path)
    assert res.returncode == 0, res.stderr
    assert f">> cmd: {cmd}" in res.stderr.splitlines()


# -- P8-21: the operator echo (screen.CleanWriter) ---------------------------

ESC = "\x1b"
COLORED = f"ab{ESC}[31mcd{ESC}[0m ef{ESC}[?2004h{ESC}M gh{ESC}[1;32;4mij\r\n"
PLAIN = "abcd ef gh" "ij\r\n"


def echoed(*chunks: str, close: bool = False) -> str:
    out = io.StringIO()
    w = CleanWriter(out)
    for chunk in chunks:
        w.write(chunk)
        w.flush()  # pexpect flushes the log after every read
    if close:
        w.close()
    return out.getvalue()


def test_p8_21_echo_removes_sequences():
    """SPEC "ANSI escape sequences": the echo to the operator has the sequences removed."""
    assert echoed(COLORED) == PLAIN


@pytest.mark.parametrize("cut", range(1, len(COLORED)))
def test_p8_21_echo_removes_a_sequence_split_across_two_reads(cut: int):
    """SPEC "ANSI escape sequences": wherever a read ends, the echo never contains a removed sequence."""
    assert echoed(COLORED[:cut], COLORED[cut:]) == PLAIN


def test_p8_21_echo_removes_a_sequence_that_arrives_byte_by_byte():
    assert echoed(*COLORED) == PLAIN


def test_p8_21_echo_split_sequence_from_the_audit():
    """The reported case: `ab ESC[3` then `1mcd ESC[0m` was echoed as `ab ESC[31mcd`."""
    assert echoed(f"ab{ESC}[3", f"1mcd{ESC}[0m") == "abcd"


def test_p8_21_echo_writes_text_before_a_held_start_at_once():
    """Only the unfinished sequence waits; the text before it is on screen after the read."""
    assert echoed(f"login: {ESC}[") == "login: "
    assert echoed("login: ") == "login: "


@pytest.mark.parametrize("tail", [ESC, f"{ESC}[", f"{ESC}[3", f"{ESC}[?2004", f"{ESC}[1;3 "], ids=repr)
def test_p8_21_echo_held_start_is_written_on_close(tail: str):
    """A start that nothing completes isn't lost, and isn't held after the session closes."""
    assert echoed(f"ab{tail}") == "ab"
    assert echoed(f"ab{tail}", close=True) == f"ab{tail}"
    # closing twice, or with nothing held, writes nothing more
    out = io.StringIO()
    w = CleanWriter(out)
    w.write(f"ab{tail}")
    w.close()
    w.close()
    w.write("cd")
    assert out.getvalue() == f"ab{tail}cd"


@pytest.mark.parametrize(
    ("chunks", "expected"),
    [
        ((ESC, "(B", "x"), f"{ESC}(Bx"),  # ESC ( B isn't a removed sequence
        ((ESC, "=x"), f"{ESC}=x"),
        ((ESC, "7"), f"{ESC}7"),
        ((ESC, ESC, "[0m", "x"), f"{ESC}x"),  # the second ESC starts the sequence
        ((f"{ESC}]0;title", "\x07x"), "0;title\x07x"),  # OSC: only its ESC ] is removed
        ((ESC, "Mx"), "x"),
        ((f"{ESC}[", "\r\nx"), f"{ESC}[\r\nx"),  # a control character ends the attempt
    ],
    ids=["charset", "keypad", "save-cursor", "esc-esc", "osc", "two-byte", "newline-in-csi"],
)
def test_p8_21_echo_keeps_what_the_stripping_regex_keeps(chunks: tuple[str, ...], expected: str):
    """Holding a start back changes when text is written, not what: split or whole, the result is the same."""
    assert echoed(*chunks) == expected
    assert echoed("".join(chunks)) == expected
    assert ANSI_ESCAPE_RE.sub("", "".join(chunks)) == expected


def test_p8_21_echo_held_start_is_bounded():
    """Garbage after an ESC can't grow the held text without limit: past the bound it is written out."""
    ESCAPE_HOLD = screen_mod.ESCAPE_HOLD
    assert 16 <= ESCAPE_HOLD <= 256
    out = io.StringIO()
    w = CleanWriter(out)
    for _ in range(1000):
        w.write("0" if w._held or out.getvalue() else f"{ESC}[")
        assert len(w._held) <= ESCAPE_HOLD
    assert len(out.getvalue()) + len(w._held) == 1001
    assert out.getvalue().startswith(f"{ESC}[000")
    # one read of the same garbage isn't held either
    assert echoed(f"{ESC}[" + "0" * 1000) == f"{ESC}[" + "0" * 1000
    # up to the bound the start is held, and a sequence of that length is still removed
    longest = f"{ESC}[" + "1" * (ESCAPE_HOLD - 2)
    assert echoed(longest) == ""
    assert echoed(longest, "m!") == "!"


ECHO_DEVICE = """
import os, sys, time
os.write(1, b"ab\\x1b[3")
time.sleep(0.3)
os.write(1, b"1mcd\\x1b[0m\\nPROMPT$ ")
os.read(0, 4096)
os.write(1, b"bye\\x1b")
os.read(0, 4096)
"""


def test_p8_21_session_echo_on_stdout_has_no_split_sequence(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """End to end: a sequence the device prints in two writes isn't echoed, and stdout gets the echo.

    The lone ESC the device ends with is written when the session is detached.
    """
    device = tmp_path / "echo_device.py"
    device.write_text(ECHO_DEVICE)
    # the first sleep reads both halves before anything is sent, so no tty echo comes between them
    run([{"sleep": "1s"}, {"line": "x"}, {"sleep": "500ms"}], spawn=f"{sys.executable} {device}")
    captured = capsys.readouterr()
    assert captured.out.startswith("abcd\r\nPROMPT$ ")
    assert captured.out.endswith(f"bye{ESC}")
    assert captured.out.count(ESC) == 1
    assert ">> " not in captured.out and ">> attach:" in captured.err


def test_p8_21_session_echo_strips_color_from_a_real_shell(capsys: pytest.CaptureFixture[str]):
    """SPEC "ANSI escape sequences": session output goes to stdout without sequences; `>> ` lines go to stderr."""
    out = run([{"cmd": "printf 'A\\033[1;31mB\\033[0mC\\n'", "register": "out"}])
    assert out["out"] == "ABC"
    captured = capsys.readouterr()
    assert "\nABC\r\n" in captured.out
    assert ESC not in captured.out
    assert ">> cmd: printf" in captured.err and ">> " not in captured.out
