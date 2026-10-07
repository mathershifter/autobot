"""The echo of a sent line, as a line editor writes it when the line wraps (P8-30 to P8-35, P4-44)."""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pexpect
import pytest
from conftest import BASH, FakeDevice, make_runner
from conftest import run_vars as run

from autobot.session import HELD_GRACE, PTY_COLS, PTY_ROWS, CommandError, strip_echo

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
        # less than what is shown, written again: the line stays complete
        ("echo abcd\recho\nout\n", "echo abcd", "out\n"),
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
        # a row that ends after `re` or `ab`: the row after it is written, then written again
        ("reload\rload\nout\n", "reload", "out\n"),
        ("abcd\rcdef\nout\n", "abcdef", "out\n"),
        # each return is read on its own: no one width has a row start at `h` and then at `o` (SPEC says so)
        ("sho\rhow\row\nout\n", "show", "out\n"),
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


# -- P8-33: the cost of reading output that is no echo, or an odd one ---------------------------------


def timed(text: str, sent: str) -> tuple[str, float]:
    started = time.perf_counter()
    out = strip_echo(text, sent)
    return out, time.perf_counter() - started


@pytest.mark.parametrize(
    ("text", "sent"),
    [
        # a line of one character, shown in part and then written again a thousand times, one character each
        ("a" * 500 + "\ra" * 1000 + "\n", "a" * 1000),
        ("a" * 100 + "\ra" * 2000 + "\n", "a" * 200),
        ("ab" * 2000 + "\rab" * 4000 + "\n", "ab" * 4000),
        ("=" * 2000 + ("\r" + "=" * 50) * 500 + "\n", "=" * 4000),
    ],
    ids=["a-1000", "a-200-2000-parts", "ab-4000", "rows-of-50"],
)
def test_p8_33_line_that_repeats_itself_is_read_in_bounded_time(text: str, sent: str):
    """The readings of such a line are many; a few are kept. Seconds before the bound, milliseconds with it."""
    out, seconds = timed(text + "out\n", sent)
    assert seconds < 0.5
    assert out in (text + "out\n", "out\n")


@pytest.mark.parametrize(
    "line",
    [
        "z" * 5_000_000,
        "|\b/\b-\b\\\b" * 625_000,
        "10%\r" * 1_250_000,
        "show " + "\x00" * 5_000_000 + "version",
        "s" + "\rs" * 1_000_000,
        "\n" * 2_000_000,
    ],
    ids=["no-break", "spinner", "progress", "padding", "one-character-parts", "blank-lines"],
)
def test_p8_33_large_output_without_an_echo_is_left_alone_quickly(line: str):
    """5 MB that is not the echo: decided at its first part, or at the bound on a part or on the parts."""
    text = line + "\nout\n"
    out, seconds = timed(text, "show version")
    assert out == text
    assert seconds < 0.2


def test_p8_33_bounds_leave_room_for_an_echo():
    """Padding of a thousand characters at a wrap, a row per character, and a line of one character
    wrapped by a return are echoes within the bounds."""
    cmd = command(200)
    assert strip_echo(echo_of(cmd, " \b" + "\x00" * 1000) + "\nout\n", cmd) == "out\n"
    assert strip_echo("\r".join(cmd) + "\nout\n", cmd) == "out\n"
    assert strip_echo("\n".join(cmd) + "\nout\n", cmd) == "out\n"
    rule = "echo " + "=" * 400
    for wrap, again in WRAPS.values():
        assert strip_echo(echo_of(rule, wrap, again) + "\nout\n", rule) == "out\n"
    # past them, the output is kept
    assert strip_echo("ls" + " " * 2000 + "\r -l\nout\n", "ls -l") == "ls" + " " * 2000 + "\r -l\nout\n"
    assert strip_echo("ls -l" + "\rl" * 20 + "\nout\n", "ls -l") == "ls -l" + "\rl" * 20 + "\nout\n"


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
# a terminal that wraps, and readline as it comes: with `INPUTRC=/dev/null` it reads neither `~/.inputrc`
# nor `/etc/inputrc`, where `set horizontal-scroll-mode on` would replace the wrap by the `<` form
VT100 = "env INPUTRC=/dev/null TERM=vt100"
WIDTHS = [NARROW, COLS]
UTF8 = next((n for n in ("C.UTF-8", "en_US.UTF-8") if n.lower().replace("-", "") in _locales()), None)


def full(cols: int) -> int:
    """The length of the command that fills the first row, after `PROMPT$ `."""
    return cols - len("PROMPT$ ")


def readline(script: list[dict], lc_all: str, cols: int = NARROW) -> dict:
    """Local bash on a terminal that wraps: readline wraps the line at the margin. The pty is 80 columns
    wide, and `stty` makes it narrower. The locale is on the spawn line: readline's wrap depends on it."""
    resize = [{"cmd": f"stty cols {cols}"}] if cols != COLS else []
    return run([*resize, *script], spawn=f"{VT100} LC_ALL={lc_all} {BASH}")


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


# -- P8-34: a line of more rows than a screen of 24 ---------------------------------------------------

# with `PROMPT$ `, 1912 characters fill 24 rows of 80 columns, and 952 fill 24 rows of 40
TALL = [1912, 2000, 4000, 10000]
TALL_NARROW = [952, 2000, 10000]


def tall(script: list[dict], lc_all: str, term: str = "vt100", cols: int = COLS) -> dict:
    """`readline`, on a terminal type of the test's choice."""
    resize = [{"cmd": f"stty cols {cols}"}] if cols != COLS else []
    return run([*resize, *script], spawn=f"env INPUTRC=/dev/null TERM={term} LC_ALL={lc_all} {BASH}")


@pytest.mark.parametrize("term", ["vt100", "xterm"])
@pytest.mark.parametrize("length", TALL)
def test_p8_34_line_of_many_rows_registers_its_output(length: int, term: str):
    """SPEC "The window of the spawned process": the screen is tall enough for the line, so readline
    writes it once. On a screen of 24 rows it clears the screen and writes the prompt first."""
    cmd = command(length)
    out = tall([{"cmd": cmd, "register": "out"}, {"cmd": "echo done", "register": "after"}], "C", term)
    assert out["out"] == cmd[5:]
    assert out["after"] == "done"


@pytest.mark.skipif(UTF8 is None, reason="no UTF-8 locale installed")
@pytest.mark.parametrize("term", ["vt100", "xterm"])
@pytest.mark.parametrize("length", TALL)
def test_p8_34_line_of_many_rows_registers_its_output_in_a_multibyte_locale(length: int, term: str):
    cmd = command(length)
    out = tall([{"cmd": cmd, "register": "out"}, {"cmd": "echo done", "register": "after"}], str(UTF8), term)
    assert out["out"] == cmd[5:]
    assert out["after"] == "done"


@pytest.mark.parametrize("lc_all", ["C", UTF8 or "C"])
@pytest.mark.parametrize("length", TALL_NARROW)
def test_p8_34_line_of_many_rows_on_a_narrow_terminal(length: int, lc_all: str):
    cmd = command(length)
    out = tall([{"cmd": cmd, "register": "out"}, {"cmd": "echo done", "register": "after"}], lc_all, cols=NARROW)
    assert out["out"] == cmd[5:]
    assert out["after"] == "done"


@pytest.mark.parametrize("lc_all", ["C", UTF8 or "C"])
def test_p8_34_longest_line_the_window_shows_at_40_columns(lc_all: str):
    """SPEC "The echo of a sent line": with its prompt, the line is one cell short of the window's
    `PTY_ROWS` rows of 40 columns. (One more is usually answered by clearing the screen, not always, and
    at 80 columns a line this close to the window is past what one write is sure to carry.)"""
    cmd = command(PTY_ROWS * NARROW - len("PROMPT$ ") - 1)
    assert len(cmd) == 19991
    out = tall([{"cmd": cmd, "register": "out"}, {"cmd": "echo done", "register": "after"}], lc_all, cols=NARROW)
    assert out["out"] == cmd[5:]
    assert out["after"] == "done"


def test_p8_34_long_lines_of_a_list_with_errors_and_assert():
    """Each long line of a list loses its own echo, `assert` and `errors` read the output, and the
    command after them gets its own."""
    a, b = command(2500), command(4000, "second ")
    script = [
        {"cmd": [a, "echo two", b], "register": "out"},
        {"cmd": a, "assert": "w498x$","register": "asserted"},
        {"cmd": "echo next", "register": "n"},
    ]
    out = run(script, spawn=f"{VT100} LC_ALL=C {BASH}", errors=["^FATAL"])
    assert out == {"out": f"{a[5:]}\ntwo\n{b[5:]}", "asserted": a[5:], "n": "next"}
    fatal = command(3000, "FATAL ")
    runner = make_runner([{"cmd": fatal, "register": "f"}], spawn=f"{VT100} LC_ALL=C {BASH}", errors=["^FATAL"])
    with pytest.raises(CommandError, match="command error: FATAL"):
        runner.run()


@pytest.mark.parametrize("lc_all", ["C", UTF8 or "C"])
def test_p8_34_screen_the_shell_is_told_of_is_the_one_that_counts(lc_all: str):
    """SPEC "The echo of a sent line": the limit is the screen the line editor works with. Told that it
    has 24 rows, as a device behind a console server may be, readline shows a line that fills them by
    clearing the screen, and the output is not captured; told that it has 100, it shows a longer one."""
    fits, fills, longer = command(1911), command(1912), command(4000)
    script = [
        {"cmd": "stty rows 24"},
        {"cmd": fits, "register": "fits"},
        {"cmd": fills, "register": "fills"},
        {"cmd": "echo next", "register": "next"},
        {"cmd": "stty rows 100"},
        {"cmd": longer, "register": "longer"},
    ]
    assert tall(script, lc_all) == {"fits": fits[5:], "fills": "", "next": "next", "longer": longer[5:]}


@pytest.mark.parametrize("length", [2000, 10000])
def test_p8_34_long_line_on_a_plain_terminal(length: int):
    """A guard: with `TERM=dumb` readline scrolls the line sideways whatever the screen's height."""
    cmd = command(length)
    out = run([{"cmd": cmd, "register": "out"}, {"cmd": "echo done", "register": "after"}])
    assert (out["out"], out["after"]) == (cmd[5:], "done")


SCREENFUL = [
    "echo hi", "true", "", "seq 30", "printf 'a\\nb\\n'", "ls /nonexistent-autobot", "echo $?",
    command(full(COLS) - 1), command(full(COLS)), command(full(COLS) + 1), command(200), command(1000),
]  # fmt: skip


def raw_session(rows: int, term: str, lc_all: str) -> list[str]:
    """What bash writes for each line of `SCREENFUL` on a pty of `rows` rows: every byte, up to the prompt
    that follows a line break."""
    env = {**os.environ, "TERM": term, "NO_COLOR": "1", "LC_ALL": lc_all, "INPUTRC": "/dev/null"}
    cld = pexpect.spawn(BASH, env=env, encoding="utf-8", dimensions=(rows, PTY_COLS), timeout=10)
    try:
        # between the two, readline's bracketed-paste switches and a `\r` (a command without output)
        at_prompt = r"(?:\A|\n)(?:\x1b\[\?2004[hl]|\r)*PROMPT\$ $"
        cld.expect(at_prompt)
        out = []
        for line in SCREENFUL:
            cld.sendline(line)
            cld.expect(at_prompt)
            out.append(str(cld.before) + str(cld.after))
        return out
    finally:
        cld.close(force=True)


@pytest.mark.parametrize("lc_all", ["C", UTF8 or "C"])
@pytest.mark.parametrize("term", ["dumb", "vt100", "xterm"])
def test_p8_34_lines_that_fit_24_rows_are_written_the_same(term: str, lc_all: str):
    """A guard: for a short command, a wrapped one, one that ends at the margin and one of 13 rows, and for
    output of more than 24 lines, the shell writes byte for byte what it writes on a screen of 24 rows."""
    assert raw_session(PTY_ROWS, term, lc_all) == raw_session(24, term, lc_all)


# -- P8-35: the input line of a terminal without a line editor ----------------------------------------

CANON = 4095  # the bytes of a line that the Linux terminal keeps for a program that reads whole lines


@pytest.mark.skipif(sys.platform != "linux", reason="the limit is the Linux terminal's")
@pytest.mark.parametrize("over", [-1, 0, 1, 905, 5905], ids=["under", "at", "over", "5000", "10000"])
def test_p8_35_line_past_the_input_limit_of_the_terminal_is_cut(over: int):
    """SPEC "The length of a sent line": bash without readline leaves the line to the terminal, which
    keeps 4095 bytes of it. The command runs cut off there, without an error."""
    cmd = command(CANON + over)
    out = run(
        [{"cmd": cmd, "register": "out"}, {"cmd": "echo done", "register": "after"}],
        spawn="bash --norc --noprofile --noediting -i",
    )
    assert out["out"] == " ".join(cmd[:CANON].split()[1:])
    assert (out["out"] == cmd[5:]) == (over <= 0)
    assert out["after"] == "done"


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
        spawn=f"{VT100} LC_ALL=C {BASH}",
        errors=["NOPE"],
    )
    assert (out["a"], out["b"], out["c"]) == (first[5:], "second", "third")


@pytest.mark.parametrize("cols", WIDTHS)
def test_p4_44_error_of_a_command_with_a_redrawn_prompt_is_raised_on_it(cols: int):
    fatal = at_margin(cols, "FATAL ")
    script = [{"cmd": f"stty cols {cols}"}, {"cmd": fatal, "register": "a"}, {"cmd": "echo fine", "register": "b"}]
    runner = make_runner(script, spawn=f"{VT100} LC_ALL=C {BASH}", errors=["^FATAL"])
    with pytest.raises(CommandError, match="command error: FATAL"):
        runner.run()
    assert "b" not in runner.config.vars
    # ignored, the error leaves the command its output and the next command its own
    script[1]["ignore_error"] = True
    out = run(script, spawn=f"{VT100} LC_ALL=C {BASH}", errors=["^FATAL"])
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
        spawn=f"{VT100} LC_ALL={UTF8} {BASH}",
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


def test_p4_44_output_that_starts_like_the_command_is_one_poll_late():
    """With the echo off, `printf pr` prints `pr` and no line break, then the prompt: it reads as the start
    of the echo of `printf pr`, so the prompt is held for one poll."""
    started = time.monotonic()
    out = run(
        [{"cmd": "stty -echo"}, {"cmd": "printf pr", "register": "out"}, {"cmd": "printf zz", "register": "zz"}],
        spawn="bash --norc --noprofile --noediting -i",
    )
    assert out == {"out": "", "zz": ""}
    assert HELD_GRACE - 0.2 < time.monotonic() - started < HELD_GRACE + 2
