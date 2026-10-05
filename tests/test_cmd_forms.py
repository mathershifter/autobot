"""P2-01..06, P2-22, P2-23: ``cmd`` forms (SPEC.md:99, 162, 278, 317)."""

from __future__ import annotations

import pytest
from conftest import RC_PROBE, SentLog, Timeline, run_vars

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


def test_p2_04_after_is_followed_by_the_first_prompt_wait(timeline: Timeline):
    """SPEC "cmd": `after` is waited for first, then a prompt, and only then is the command sent."""
    run_vars(
        [
            {"line": "printf 'pre%s\\n' READY"},
            {"cmd": "echo x", "after": "preREADY"},
        ]
    )
    i = timeline.index_of(("expect", ["preREADY"]))
    assert list(timeline[i + 1 : i + 3]) == [("get_prompt", None), ("sendline", "echo x")]


# the marker is assembled by printf, so the echoed line can't match it; the sleep keeps the line
# running when `after` matches, so its prompt is still to come
SLOW_LINE = {"line": "printf 'pre%s\\n' READY; sleep 1"}


@pytest.mark.parametrize("cmd", ["echo hi", "#!/bin/sh\necho hi\n"], ids=["plain", "embedded"])
def test_p2_23_cmd_with_after_waits_for_the_pending_prompt(sent: SentLog, cmd: str):
    """SPEC "cmd": an `after` that matches before an earlier prompt doesn't make that prompt the command's own.

    The command used to be sent on the match, while the line before it was still running: `register`
    stored '' and the `$?` check was sent before the command had run.
    """
    script = [SLOW_LINE, {"cmd": cmd, "after": "preREADY", "register": "out"}, {"cmd": "echo two", "register": "two"}]
    out = run_vars(script)
    assert out["out"] == "hi"
    assert out["two"] == "two"
    assert sent.lines().count(RC_PROBE) == 2


def test_p2_23_cmd_with_after_keeps_the_values_of_the_match():
    """SPEC "cmd": the first prompt wait of a `cmd` with `after` leaves `session.before` and `session.match` alone."""
    cmd = "echo got-{{ session.match }}-{{ 'echoed' if session.before | contains('printf') else 'no' }}"
    out = run_vars([SLOW_LINE, {"cmd": cmd, "after": "pre[A-Z]+", "register": "out"}])
    assert out["out"] == "got-preREADY-echoed"


def test_p2_23_cmd_with_after_sends_at_once_at_a_prompt(sent: SentLog):
    """SPEC "Prompt Handling": at a shell prompt the wait returns at once, so nothing is solicited or delayed."""
    out = run_vars(
        [
            {"cmd": "( (sleep 1; printf 'pre%s\\n' READY) & )"},
            {"cmd": "echo x", "after": "preREADY", "register": "out"},
        ]
    )
    assert out["out"] == "x"
    assert "" not in sent.lines()


@pytest.mark.parametrize("step", [{"line": "echo x"}, {"return": 1}, {"control": "a"}], ids=["line", "return", "control"])
def test_p2_23_raw_sends_with_after_wait_for_no_prompt(timeline: Timeline, step: dict):
    """SPEC "line": a raw send goes out on the `after` match; only `cmd` waits for a prompt first."""
    run_vars([SLOW_LINE, {**step, "after": "preREADY"}])
    assert "get_prompt" not in timeline.names()


@pytest.mark.slow
def test_p2_23_after_that_takes_the_prompt_waits_for_the_next_one(sent: SentLog):
    """SPEC "cmd": an `after` that matches the prompt itself takes it, so the step solicits another one."""
    out = run_vars([{"cmd": "echo hi", "after": r"PROMPT\$ ", "register": "out", "timeout": "15s"}])
    assert out["out"] == "hi"
    assert sent.lines() == ["", "echo hi", RC_PROBE]


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
