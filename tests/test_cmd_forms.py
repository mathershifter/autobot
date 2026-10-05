"""P2-01..06, P2-22: ``cmd`` forms (SPEC.md:99, 162, 278, 317)."""

from __future__ import annotations

import pytest
from conftest import SentLog, Timeline, run_vars

from autobot.steps import CmdExecutor


def test_p2_01_multiline_skips_blank_lines(sent: SentLog):
    """SPEC.md:99: a multiline string is split on newlines; blank lines skipped."""
    run_vars([{"cmd": "echo a\n\n   \necho b\n"}])
    assert sent.commands() == ["echo a", "echo b"]


def test_p2_02_each_line_waits_for_prompt(timeline: Timeline):
    """SPEC.md:99: each line waits for a prompt before sending."""
    run_vars([{"cmd": ["echo a", "echo b"]}])
    assert timeline.has_subsequence(
        [
            ("get_prompt",),
            ("sendline", "echo a"),
            ("get_prompt",),
            ("sendline", "echo b"),
            ("get_prompt",),
        ]
    )
    a = timeline.index_of(("sendline", "echo a"))
    b = timeline.index_of(("sendline", "echo b"))
    assert "get_prompt" in [e[0] for e in timeline[a + 1 : b]]


def test_p2_03_list_with_shebang_first_item_sent_verbatim(sent: SentLog):
    """SPEC.md:162: only a string starting with #! is an embedded script."""
    out = run_vars([{"cmd": ["#!/bin/false", "echo x"], "register": "out"}])
    assert out["out"] == "x"
    assert "#!/bin/false" in sent.lines()
    assert not any("/tmp/_autobot_" in line for line in sent.lines())


def test_p2_04_after_skips_initial_get_prompt(timeline: Timeline):
    """SPEC.md:278: with after, the cmd is sent right after the match."""
    run_vars(
        [
            {"line": "printf 'pre%s\\n' READY"},
            {"cmd": "echo x", "after": "preREADY"},
        ]
    )
    i = timeline.index_of(("expect", ["preREADY"]))
    assert timeline[i + 1] == ("sendline", "echo x")


def test_p2_05_multiline_lines_rendered_individually():
    """SPEC.md:99, 317: every line of a multiline cmd is rendered."""
    out = run_vars(
        [{"cmd": "echo {{ vars.a }}\necho {{ vars.b }}", "register": "out"}],
        vars={"a": 1, "b": 2},
    )
    assert out["out"] == "1\n2"


def test_p2_06_multiline_jinja_block_spanning_lines():
    """SPEC.md:99, 317: a Jinja block may span lines of a multiline cmd."""
    out = run_vars(
        [{"cmd": "{% for i in range(2) %}\necho n{{ i }}\n{% endfor %}", "register": "out"}]
    )
    assert out["out"] == "n0\nn1"


NOT_LINE_BREAKS = ["\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"]


@pytest.mark.parametrize("char", NOT_LINE_BREAKS, ids=lambda c: f"U+{ord(c):04X}")
def test_p2_22_only_line_breaks_split_a_command(char: str):
    """SPEC "cmd": a command is split at LF, CRLF and CR, not at the other separators of str.splitlines."""
    text = f"echo a{char}echo b"
    assert CmdExecutor._lines(text) == [text]
    assert CmdExecutor._lines(f"{text}\necho c") == [text, "echo c"]


@pytest.mark.parametrize(
    ("text", "lines"),
    [
        ("a\nb", ["a", "b"]),
        ("a\r\nb\r\n", ["a", "b"]),
        ("a\rb", ["a", "b"]),
        ("a\n\rb", ["a", "b"]),
        ("a\n", ["a"]),
        ("a\r", ["a"]),
        ("\na", ["a"]),
        ("a\n\n \t \nb", ["a", "b"]),
        ("a\n\x0c\u2028\nb", ["a", "b"]),
        ("  a  \n\tb", ["  a  ", "\tb"]),
        ("", [""]),
        ("\n\r\n", [""]),
        (" \x1e ", [""]),
    ],
    ids=repr,
)
def test_p2_22_line_breaks_and_blank_lines(text: str, lines: list[str]):
    """SPEC "cmd": blank lines are skipped, lines are sent as written, and no lines means one empty line."""
    assert CmdExecutor._lines(text) == lines


@pytest.mark.parametrize("char", ["\x85", " ", " "], ids=lambda c: f"U+{ord(c):04X}")
def test_p2_22_separator_inside_a_command_is_sent(sent: SentLog, char: str):
    """SPEC "cmd": a separator that isn't a newline reaches the shell inside its line.

    Only the non-ASCII ones: the terminal acts on a control character such as U+001E itself.
    """
    cmd = f"printf '%s\\n' 'A{char}B' | od -An -tx1"
    out = run_vars([{"cmd": cmd, "register": "out"}])
    assert sent.commands() == [cmd]
    assert bytes.fromhex(out["out"]) == f"A{char}B\n".encode()
