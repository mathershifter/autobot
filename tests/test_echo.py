"""The echo of a sent line, as a line editor writes it when the line wraps (P8-30 to P8-32)."""

from __future__ import annotations

import subprocess
import time

import pytest
from conftest import BASH, FakeDevice, make_runner
from conftest import run_vars as run

from autobot.session import CommandError, strip_echo

EOS_PROMPT = "cmp474(s1)(vrf:MGMT)#"
EOS_PROMPTS = [{"name": "eos", "expect": [r"^cmp474\(s1\)\(vrf:MGMT\)#"], "return": True}]
URL = "https://buildpack.sjc.aristanetworks.com/release/EOS-4.35.2F/final/images/EOS-DPE.swi.checksum"
# the command of the report: with the 21 characters of the prompt, its first 59 end at column 80
REPORTED = f"bash curl -fsSLk {URL}"
COLS = 80
MARGIN = COLS - len(EOS_PROMPT)

# what an editor writes when the cursor reaches the right margin; with `again`, after the next character,
# which it then writes a second time (GNU readline on an auto-margin terminal)
WRAPS = {
    "space-backspace": (" \b", False),
    "space-cr": (" \r", False),
    "cr": ("\r", False),
    "line-break": ("\n", False),
    "space-line-break": (" \n", False),
    "cr-line-break-cr": ("\n\r", False),
    "readline": ("\r", True),
    "bel": ("\x07", False),
    "nul-padding": (" \b\x00\x00\x00", False),
    "space-del": (" \x7f", False),
}


def _locales() -> set[str]:
    try:
        out = subprocess.run(["locale", "-a"], capture_output=True, text=True, check=False).stdout
    except OSError:
        return set()
    return {n.lower().replace("-", "") for n in out.split()}


def command(length: int, lead: str = "") -> str:
    """A command of `length` characters with no repeats near a margin, and blanks inside."""
    words = " ".join(f"w{i:03d}" for i in range(length))
    return (f"echo {lead}" + words)[:length].rstrip().ljust(length, "x")


def echo_of(cmd: str, wrap: str, again: bool = False, cols: int = COLS, prompt: str = EOS_PROMPT) -> str:
    """The echo of `cmd` typed after `prompt`, as captured: `wrap` written each time a row is full."""
    out, col = "", len(prompt)
    for i, ch in enumerate(cmd):
        out += ch
        col += 1
        if col == cols:
            out += (cmd[i + 1 : i + 2] if again else "") + wrap
            col = 0
    return out


@pytest.mark.parametrize("length", [MARGIN - 1, MARGIN, MARGIN + 1, 200], ids=["under", "at", "over", "two-wraps"])
@pytest.mark.parametrize("wrap", WRAPS)
def test_p8_30_wrapped_echo_is_removed(wrap: str, length: int):
    """SPEC "Captured output": every wrap form, for a line that ends one short of the margin, at it, one
    past it, and on the third row."""
    cmd = command(length)
    assert len(cmd) == length
    echo = echo_of(cmd, *WRAPS[wrap])
    assert (echo != cmd) == (length >= MARGIN)
    # a line that ends at the margin of an editor that breaks the line there: the echo is complete at the
    # break, and the empty line after it is kept, as an empty first line of output is
    wrap_text = WRAPS[wrap][0]
    left = wrap_text.partition("\n")[2] + "\n" if "\n" in wrap_text and length == MARGIN else ""
    assert strip_echo(f"{echo}\nout\n", cmd) == f"{left}out\n"


@pytest.mark.parametrize("wrap", WRAPS)
def test_p8_30_reported_command_echo_is_removed(wrap: str):
    echo = echo_of(REPORTED, *WRAPS[wrap])
    assert strip_echo(f"{echo}\n3d9fc4c2\n", REPORTED) == "3d9fc4c2\n"


def test_p8_30_reported_second_command_echo_is_removed():
    """The registered value of the report went out in the next command, backspace and all: the editor
    takes the backspace as an erase, so the line it echoes has neither the blank nor the backspace."""
    sent = "bash echo Image checksum - " + REPORTED[:MARGIN] + " \b" + REPORTED[MARGIN:]
    typed = "bash echo Image checksum - " + REPORTED
    for wrap, again in WRAPS.values():
        assert strip_echo(echo_of(typed, wrap, again) + "\nout\n", sent) == "out\n"
    # the blank echoed, then erased
    erased = typed.replace("/release", "/r \b \belease")
    assert strip_echo(echo_of(erased, " \b") + "\nout\n", sent) == "out\n"


@pytest.mark.parametrize(
    ("text", "sent", "expected"),
    [
        # a blank and a backspace show nothing
        ("ls -l \b\nout\n", "ls -l", "out\n"),
        ("ls \b-l\nout\n", "ls -l", "out\n"),
        # a character written after a backspace replaces the one before it
        ("ls -x\bl\nout\n", "ls -l", "out\n"),
        # erased: backspace, blank, backspace
        ("ls -lx\b \b\nout\n", "ls -l", "out\n"),
        # the cursor moved back and the same text written again
        ("ls -l\b\b\b\b\bls -l\nout\n", "ls -l", "out\n"),
        ("ls -l\b\b-l\nout\n", "ls -l", "out\n"),
        # a backspace at the left edge stays there
        ("\b\bls -l\nout\n", "ls -l", "out\n"),
        # a backspace that makes the terminal show another line: not the echo
        ("ls -\bl\nout\n", "ls -l", "ls -\bl\nout\n"),
        ("ls -l\bx\nout\n", "ls -l", "ls -l\bx\nout\n"),
        ("ls -l\b \b\nout\n", "ls -l", "ls -l\b \b\nout\n"),
        ("ls -ll\b\nout\n", "ls -l", "ls -ll\b\nout\n"),
    ],
)
def test_p8_30_backspace_is_read_as_a_terminal_shows_it(text: str, sent: str, expected: str):
    assert strip_echo(text, sent) == expected


@pytest.mark.parametrize(
    ("text", "sent", "expected"),
    [
        ("\x00ls\x07 -\x7fl\x1b\nout\n", "ls -l", "out\n"),
        ("l\x01\x02s\x0e\x0f -\x1fl\x9b\nout\n", "ls -l", "out\n"),
        ("ls \x00\b\x07-l \r\x00\nout\n", "ls -l", "out\n"),
        # they show nothing, so they hide nothing either
        ("ls -\x07x\nout\n", "ls -l", "ls -\x07x\nout\n"),
        ("ls\x00\x00\nout\n", "ls -l", "ls\x00\x00\nout\n"),
    ],
)
def test_p8_30_other_control_characters_show_nothing(text: str, sent: str, expected: str):
    assert strip_echo(text, sent) == expected


@pytest.mark.parametrize(
    ("text", "sent", "expected"),
    [
        # the character at the margin, a return, the same character again (readline)
        ("echo ab\rbcd\nout\n", "echo abcd", "out\n"),
        ("echo ab\rbc\rcd\rd\nout\n", "echo abcd", "out\n"),
        # the row written again from its start, then continued
        ("echo ab\recho abcd\nout\n", "echo abcd", "out\n"),
        ("echo abcd\rabcd\nout\n", "echo abcd", "out\n"),
        # what is written again differs from what is shown
        ("echo ab\rxcd\nout\n", "echo abcd", "echo ab\rxcd\nout\n"),
        ("echo ab\rout\n", "echo ab", "echo ab\rout\n"),
        # it skips a part of the line
        ("echo a\rcd\nout\n", "echo abcd", "echo a\rcd\nout\n"),
        # a return goes no further back than the start of its captured line
        ("echo ab\n\rbcd\nout\n", "echo abcd", "echo ab\n\rbcd\nout\n"),
        ("echo ab\nbcd\nout\n", "echo abcd", "echo ab\nbcd\nout\n"),
        # the line is never completed
        ("echo ab\rab\rab\nout\n", "echo abcd", "echo ab\rab\rab\nout\n"),
    ],
)
def test_p8_30_carriage_return_continues_or_rewrites(text: str, sent: str, expected: str):
    assert strip_echo(text, sent) == expected


@pytest.mark.parametrize(
    ("text", "sent", "expected"),
    [
        ("\r<9 word10\nout\n", "echo word9 word10", "out\n"),
        ("<9 word10\nout\n", "echo word9 word10", "out\n"),
        # typed up to the margin, then redrawn scrolled, more than once
        ("echo wor\r<rd9 wo\r<9 word10\nout\n", "echo word9 word10", "out\n"),
        ("\r\x07<9 word\x0010 \b\nout\n", "echo word9 word10", "out\n"),
        # not the tail of the line
        ("\r<9 word1\nout\n", "echo word9 word10", "\r<9 word1\nout\n"),
        ("\r<\nout\n", "echo word9 word10", "\r<\nout\n"),
        # only the first line of the echo can be a scrolled one
        ("echo word9\n<word10\nout\n", "echo word9 word10", "echo word9\n<word10\nout\n"),
    ],
)
def test_p8_30_scrolled_echo(text: str, sent: str, expected: str):
    assert strip_echo(text, sent) == expected


@pytest.mark.parametrize(
    ("text", "sent", "expected"),
    [
        # control characters and blanks in the line that was sent
        ("abcd\nout\n", "ab \bcd", "out\n"),
        ("ab \bcd\nout\n", "ab \bcd", "out\n"),
        ("ab\nout\n", "\x07ab\x00", "out\n"),
        ("ls -l\nout\n", "  ls   -l  ", "out\n"),
        ("  ls   -l  \nout\n", "  ls   -l  ", "out\n"),
        ("\tls\t-l\nout\n", "ls -l", "out\n"),
        # a control character echoed as text is no echo of it
        ("ab^Gcd\nout\n", "ab\x07cd", "ab^Gcd\nout\n"),
        # nothing was sent that shows
        ("out\n", "", "out\n"),
        ("\nout\n", "", "\nout\n"),
        (" \b\nout\n", " \b", " \b\nout\n"),
        ("\x07\nout\n", "\x07", "\x07\nout\n"),
        ("x\b\nout\n", "\b", "x\b\nout\n"),
    ],
)
def test_p8_30_sent_line_with_blanks_or_control_characters(text: str, sent: str, expected: str):
    assert strip_echo(text, sent) == expected


@pytest.mark.parametrize("wrap", ["space-backspace", "readline", "line-break"])
def test_p8_30_output_like_the_command_is_kept(wrap: str):
    """Only the echo goes: output that reads as the command, wrapped the same way, stays as it is."""
    cmd = command(200)
    echo = echo_of(cmd, *WRAPS[wrap])
    assert strip_echo(f"{echo}\n{echo}\nout\n", cmd) == f"{echo}\nout\n"
    assert strip_echo(f"{echo}\n{cmd}\n", cmd) == f"{cmd}\n"


@pytest.mark.parametrize(
    "text",
    [
        # no echo, and output that isn't the line
        "show versions\nout\n",
        "show versio\nout\n",
        "how version\nout\n",
        "x\nshow version\n",
        "show\rversion\rshow version!\n",
        "show version\b\b\b\b\b\b\binventory\n",
        "\x07\x00\b\n",
        "",
    ],
)
def test_p8_30_output_that_is_not_the_echo_is_kept(text: str):
    assert strip_echo(text, "show version") == text
    assert strip_echo("\n" + text, "show version") == "\n" + text


def test_p8_30_output_after_the_echo_is_untouched():
    """The comparison reads the echo only: what follows keeps its control characters, blanks and returns."""
    rest = "\x07a \bb\x00\rc\x7f\n  d\t\n\b\n"
    for wrap, again in WRAPS.values():
        assert strip_echo(echo_of(REPORTED, wrap, again) + "\n" + rest, REPORTED) == rest


# -- P8-31: the fake device's line editor --------------------------------------------------------------


def editor(fake_device: FakeDevice, wrap: str, cols: int = COLS):
    return fake_device(
        "--order", "none", "--then", "editor", "--prompt", f"'{EOS_PROMPT}'", "--cols", str(cols),
        "--wrap", wrap.encode().hex(),
    )  # fmt: skip


def eos(script: list[dict], spawn: str) -> dict:
    return run(script, spawn=spawn, prompts=EOS_PROMPTS, errors=["% Invalid input"])


def test_p8_31_reported_run_registers_only_the_output(fake_device: FakeDevice):
    """The report: an EOS-style prompt of 21 characters, a command that passes column 80, and an editor
    that writes a blank and a backspace there. The registered value is the output, and the next command,
    which sends it, is one line."""
    spawn, log = editor(fake_device, " \b")
    first = f"echo curl -fsSLk {URL}"
    assert len(EOS_PROMPT) + len(first) > COLS
    out = eos(
        [
            {"cmd": first, "register": "image_checksum"},
            {"cmd": "echo Image checksum - {{ vars.image_checksum }}", "register": "said"},
        ],
        spawn,
    )
    assert out["image_checksum"] == f"curl -fsSLk {URL}"
    assert out["said"] == f"Image checksum - curl -fsSLk {URL}"
    assert fake_device.read(log) == [f"LINE={first}", f"LINE=echo Image checksum - curl -fsSLk {URL}"]


@pytest.mark.parametrize("length", [MARGIN - 1, MARGIN, MARGIN + 1, 200], ids=["under", "at", "over", "two-wraps"])
@pytest.mark.parametrize("wrap", [" \b", " \r", "\r", "\n", " \b\x00\x07"], ids=repr)
def test_p8_31_wrapped_echo_from_a_device_is_removed(fake_device: FakeDevice, wrap: str, length: int):
    """A blank and a backspace, a blank and a return, a return, a line break (the echo goes on on the next
    line), and padding after the wrap."""
    spawn, log = editor(fake_device, wrap)
    cmd = command(length)
    out = eos([{"cmd": cmd, "register": "out"}], spawn)
    assert out["out"] == cmd[5:]
    assert fake_device.read(log) == [f"LINE={cmd}"]


def test_p8_31_each_line_of_a_list_loses_its_own_echo(fake_device: FakeDevice):
    spawn, _ = editor(fake_device, " \b")
    a, b = command(MARGIN + 30), command(200)
    out = eos([{"cmd": [a, "echo short", b], "register": "out"}, {"cmd": f"{a}\n{b}\n", "register": "two"}], spawn)
    assert out["out"] == f"{a[5:]}\nshort\n{b[5:]}"
    assert out["two"] == f"{a[5:]}\n{b[5:]}"


def test_p8_31_errors_do_not_match_a_wrapped_echo(fake_device: FakeDevice):
    """`errors` are matched against the output without the echo: here the echo breaks at the margin, and
    its second line starts with the text of an error."""
    spawn, _ = editor(fake_device, "\n")
    cmd = command(MARGIN) + "% Invalid input"
    out = run([{"cmd": cmd, "register": "out"}], spawn=spawn, prompts=EOS_PROMPTS, errors=["^% Invalid input"])
    assert out["out"] == cmd[5:]


def test_p8_31_line_then_cmd(fake_device: FakeDevice):
    """A `line` sent at the prompt before a `cmd`: the wait before the command reads the line's echo and
    output, and the command's own echo is removed from its output."""
    spawn, log = editor(fake_device, " \b")
    a, b = command(MARGIN + 10), command(MARGIN + 40)
    out = eos([{"cmd": "echo ready"}, {"line": a}, {"cmd": b, "register": "out"}], spawn)
    assert out["out"] == b[5:]
    assert fake_device.read(log) == ["LINE=echo ready", f"LINE={a}", f"LINE={b}"]


# -- P8-32: GNU readline on a narrow terminal ----------------------------------------------------------

NARROW = 40
WIDTHS = [NARROW, COLS]
UTF8 = next((n for n in ("C.UTF-8", "en_US.UTF-8") if n.lower().replace("-", "") in _locales()), None)


def full(cols: int) -> int:
    """The length of the command that fills the first row, after `PROMPT$ `."""
    return cols - len("PROMPT$ ")


def readline(script: list[dict], lc_all: str, cols: int = NARROW) -> dict:
    """Local bash on a terminal that wraps: readline wraps the line at the margin. The pty is 80 columns
    wide, and `stty` makes it narrower. The locale is on the spawn line: readline's wrap depends on it."""
    resize = [{"cmd": f"stty cols {cols}"}] if cols != COLS else []
    return run([*resize, *script], spawn=f"env TERM=vt100 LC_ALL={lc_all} {BASH}")


@pytest.mark.parametrize("cols", WIDTHS)
@pytest.mark.parametrize("over", [-1, 1, 20, 100, 200], ids=["under", "over", "one-wrap", "two-wraps", "more-wraps"])
def test_p8_32_readline_wrapped_echo_is_removed(over: int, cols: int):
    """In a single-byte locale readline writes the character after the margin, a return, and that
    character again. (A line that ends at the margin makes it write the prompt again: see P4-44.)"""
    cmd = command(full(cols) + over)
    out = readline([{"cmd": cmd, "register": "out"}, {"cmd": "echo done", "register": "after"}], "C", cols)
    assert out["out"] == cmd[5:]
    assert out["after"] == "done"


@pytest.mark.skipif(UTF8 is None, reason="no UTF-8 locale installed")
@pytest.mark.parametrize("cols", WIDTHS)
@pytest.mark.parametrize("over", [-1, 0, 1, 100, 200], ids=["under", "at", "over", "two-wraps", "more-wraps"])
def test_p8_32_readline_wrapped_echo_is_removed_in_a_multibyte_locale(over: int, cols: int):
    """In a multibyte locale readline leaves the wrap to the terminal, and after a line that ends at the
    margin it writes a blank and a return, moves back with escape sequences and writes the last character
    of the line again."""
    cmd = command(full(cols) + over)
    out = readline([{"cmd": cmd, "register": "out"}, {"cmd": "echo done", "register": "after"}], str(UTF8), cols)
    assert out["out"] == cmd[5:]
    assert out["after"] == "done"


def test_p8_32_readline_wrapped_list_and_script():
    """Each line of a list, and the upload lines of an embedded script (about 600 characters each)."""
    a, b = command(NARROW + 10), command(3 * NARROW)
    out = readline(
        [
            {"cmd": [a, b], "register": "out"},
            {"cmd": "#!/bin/sh\n" + "".join(f"echo line{i}\n" for i in range(40)), "register": "script"},
        ],
        "C",
    )
    assert out["out"] == f"{a[5:]}\n{b[5:]}"
    assert out["script"] == "\n".join(f"line{i}" for i in range(40))


# -- P4-44: a prompt that the line editor writes again while it echoes --------------------------------


def at_margin(cols: int, lead: str = "") -> str:
    """A command that, after `PROMPT$ `, ends exactly at the right margin: in a single-byte locale readline
    answers its Return with a blank, a return, cursor-up, the prompt and the line again."""
    return command(full(cols), lead)


@pytest.mark.parametrize("cols", WIDTHS)
def test_p4_44_redrawn_prompt_does_not_end_the_wait(cols: int):
    """With `errors` and no `$?` check: each command registers its own output."""
    first = at_margin(cols)
    out = run(
        [{"cmd": f"stty cols {cols}"}]
        + [{"cmd": c, "register": n} for n, c in (("a", first), ("b", "echo second"), ("c", "echo third"))],
        spawn=f"env TERM=vt100 LC_ALL=C {BASH}",
        errors=["NOPE"],
    )
    assert (out["a"], out["b"], out["c"]) == (first[5:], "second", "third")


@pytest.mark.parametrize("cols", WIDTHS)
def test_p4_44_error_of_a_command_with_a_redrawn_prompt_is_raised_on_it(cols: int):
    fatal = at_margin(cols, "FATAL ")
    script = [{"cmd": f"stty cols {cols}"}, {"cmd": fatal, "register": "a"}, {"cmd": "echo fine", "register": "b"}]
    runner = make_runner(script, spawn=f"env TERM=vt100 LC_ALL=C {BASH}", errors=["^FATAL"])
    with pytest.raises(CommandError, match="command error: FATAL"):
        runner.run()
    assert "b" not in runner.config.vars
    # ignored, the error leaves the command its output and the next command its own
    script[1]["ignore_error"] = True
    out = run(script, spawn=f"env TERM=vt100 LC_ALL=C {BASH}", errors=["^FATAL"])
    assert (out["a"], out["b"]) == (fatal[5:], "fine")


@pytest.mark.parametrize("cols", WIDTHS)
def test_p4_44_assert_sees_the_output_of_a_command_with_a_redrawn_prompt(cols: int):
    cmd = at_margin(cols)
    out = readline([{"cmd": cmd, "assert": "w001", "register": "out"}, {"cmd": "echo next", "register": "n"}], "C", cols)
    assert (out["out"], out["n"]) == (cmd[5:], "next")


@pytest.mark.parametrize("cols", WIDTHS)
def test_p4_44_list_with_redrawn_prompts_and_the_exit_code_check(cols: int):
    a, b = at_margin(cols), at_margin(cols, "second ")
    out = readline([{"cmd": [a, "echo two", b], "register": "out"}, {"cmd": "echo next", "register": "n"}], "C", cols)
    assert out["out"] == f"{a[5:]}\ntwo\n{b[5:]}"
    assert out["n"] == "next"


@pytest.mark.skipif(UTF8 is None, reason="no UTF-8 locale installed")
@pytest.mark.parametrize("cols", WIDTHS)
def test_p4_44_multibyte_locale_writes_no_prompt_again(cols: int):
    """A guard: in a multibyte locale readline moves the cursor instead, and nothing is held back."""
    first = at_margin(cols)
    out = run(
        [{"cmd": f"stty cols {cols}"}]
        + [{"cmd": c, "register": n} for n, c in (("a", first), ("b", "echo second"), ("c", "echo third"))],
        spawn=f"env TERM=vt100 LC_ALL={UTF8} {BASH}",
        errors=["NOPE"],
    )
    assert (out["a"], out["b"], out["c"]) == (first[5:], "second", "third")


@pytest.mark.parametrize("lc_all", ["C", UTF8 or "C"])
def test_p4_44_waits_that_have_no_echo_to_finish_are_as_before(lc_all: str):
    """On the same terminal: a command without output, an empty command, a `line` then a `cmd`, an
    interrupted `line`, and a command sent with the echo off. None is held back for the idle poll."""
    cmd = at_margin(COLS)
    script = [
        {"cmd": "true", "register": "nothing"},
        {"cmd": "", "register": "empty"},
        {"line": "echo from-line"},
        {"cmd": "echo after-line", "register": "after_line"},
        {"line": "sleep 30"},
        {"sleep": "300ms"},
        {"control": "c"},
        {"cmd": "echo after-interrupt", "register": "after_interrupt", "ignore_error": True},
        {"cmd": cmd, "register": "margin"},
        {"cmd": "stty -echo"},
        {"cmd": cmd, "register": "silent"},
        {"cmd": "echo last", "register": "last"},
    ]
    started = time.monotonic()
    out = readline(script, lc_all, COLS)
    assert time.monotonic() - started < 4
    assert out == {
        "nothing": "",
        "empty": "",
        "after_line": "after-line",
        "after_interrupt": "after-interrupt",
        "margin": cmd[5:],
        "silent": cmd[5:],
        "last": "last",
    }
