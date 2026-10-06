"""P8-22..: what Autobot prints, where, and when it is styled (SPEC "CLI", Output)."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pexpect
import pytest
import yaml
from conftest import make_config, make_doc

from autobot import log
from autobot.runner import Runner
from autobot.types import ANSI_ESCAPE_RE

ESC = "\x1b"
# one of each kind of line: a step, a group, an ignored failure, a registered value, a completed block, a failed run
PALETTE = [
    {"block": {"name": "b", "script": [{"cmd": "false", "ignore_error": True, "register": "x"}]}},
    {"cmd": "echo hi", "assert": "nope"},
]
MARKUP = "[bold]x[/bold] [/] [red] :100: :warning:"


def _write(tmp_path: Path, script: list, **kw: Any) -> Path:
    path = tmp_path / "script.autobot.yaml"
    path.write_text(yaml.safe_dump(make_doc(script, **kw)))
    return path


def _env(**extra: str) -> dict[str, str]:
    """The suite's environment without the variables under test, on a terminal type that has color."""
    env = {k: v for k, v in os.environ.items() if k not in ("NO_COLOR", "FORCE_COLOR", "TERM")}
    return {**env, "TERM": "xterm", **extra}


def piped(path: Path, **extra: str) -> subprocess.CompletedProcess[str]:
    """The CLI with stdout and stderr on two pipes."""
    return subprocess.run(
        [sys.executable, "-W", "ignore", "-m", "autobot.cli", str(path)],
        check=False, capture_output=True, text=True, timeout=120, env=_env(**extra),
    )


def on_terminal(path: Path, columns: int = 80, **extra: str) -> tuple[str, int]:
    """The CLI on a pseudo-terminal, as an operator sees it: stdout and stderr in one stream."""
    child = pexpect.spawn(
        sys.executable, ["-W", "ignore", "-m", "autobot.cli", str(path)],
        env=_env(**extra), encoding="utf-8", dimensions=(24, columns), timeout=60,
    )
    out = re.sub(r"\r+\n", "\n", child.read())  # the session's `\r\n` becomes `\r\r\n` on the way through the terminal
    child.close()
    return out, child.exitstatus


def _plain(text: str) -> str:
    return ANSI_ESCAPE_RE.sub("", text)


# -- when the messages are styled ------------------------------------------------


def test_p8_22_no_escape_sequences_when_not_a_terminal(tmp_path: Path):
    """SPEC "Output": a pipe or a file gets plain text, for every kind of line and for a failed run."""
    res = piped(_write(tmp_path, PALETTE))
    assert res.returncode == 3
    assert ESC not in res.stderr and ESC not in res.stdout
    assert ">> block enter: b" in res.stderr.splitlines()


@pytest.mark.parametrize("extra", [{"NO_COLOR": "1"}, {"NO_COLOR": "1", "FORCE_COLOR": "1"}, {"TERM": "dumb"}])
def test_p8_22_no_escape_sequences_under_no_color_on_a_terminal(tmp_path: Path, extra: dict[str, str]):
    """`NO_COLOR` means no escape sequence at all, bold and dim included, and it wins over `FORCE_COLOR`.
    A `dumb` terminal gets none either."""
    out, status = on_terminal(_write(tmp_path, PALETTE), **extra)
    assert status == 3
    assert ESC not in out
    assert ">> block enter: b" in out.splitlines()


def test_p8_22_empty_no_color_is_not_set(tmp_path: Path):
    """As the convention has it, `NO_COLOR` counts only when it isn't empty."""
    out, _ = on_terminal(_write(tmp_path, PALETTE), NO_COLOR="")
    assert f"{ESC}[1;34m>>{ESC}[0m " in out


@pytest.mark.parametrize("extra", [{"FORCE_COLOR": "1"}, {"FORCE_COLOR": "1", "TERM": "dumb"}])
def test_p8_22_color_when_forced_into_a_pipe(tmp_path: Path, extra: dict[str, str]):
    """`FORCE_COLOR` styles the messages where there is no terminal; the session's output stays as it is."""
    path = _write(tmp_path, PALETTE)
    res = piped(path, **extra)
    assert res.returncode == 3
    assert f"{ESC}[1;34m>>{ESC}[0m {ESC}[1mcmd: {ESC}[0mecho hi" in res.stderr.splitlines()
    assert ESC not in res.stdout
    # the styles add nothing but escape sequences
    assert _plain(res.stderr) == piped(path).stderr


def test_p8_22_color_on_a_terminal(tmp_path: Path):
    """On a terminal the messages are styled without any variable set."""
    out, status = on_terminal(_write(tmp_path, PALETTE))
    assert status == 3
    assert f"{ESC}[1;34m>>{ESC}[0m {ESC}[1mcmd: {ESC}[0mecho hi" in out


# -- the palette -------------------------------------------------------------------


def test_p8_23_each_kind_of_line_has_its_style(tmp_path: Path):
    """SPEC "Output": the marker shows what a line is; the text after the label stays readable."""
    path = _write(tmp_path, PALETTE)
    lines = piped(path, FORCE_COLOR="1").stderr.splitlines()
    b, dim, off = f"{ESC}[1m", f"{ESC}[2m", f"{ESC}[0m"
    blue, green, yellow, red = (f"{ESC}[1;3{n}m" for n in (4, 2, 3, 1))
    for line in [
        f"{blue}>>{off} {b}attach: {off}bash --norc --noprofile -i",  # a step: bold blue marker, bold label
        f"{blue}>>{off} {b}block enter: {off}{b}b{off}",  # a group: bold throughout
        f"{blue}>>{off}   {b}cmd: {off}false",  # a step of the block: indented after the marker
        f"{yellow}>>{off}   {ESC}[33merror ignored: {off}command returned exit code 1",  # a warning
        f"{dim}>>{off}   {dim}register: {off}{dim}vars.x{off}",  # a detail
        f"{green}>>{off} {ESC}[32mblock completed: {off}{ESC}[32mb{off}",  # completed
        f"{red}Run failed in {path}{off}: assertion failed: expected ['nope']",  # the verdict
        f"{dim}  at {off}script.1 (cmd: echo hi)",
    ]:
        assert line in lines, line


def test_p8_23_palette_is_small_and_uses_the_terminals_own_colors():
    """Bold, dim and four of the terminal's eight colors: the theme keeps them readable on a dark and on a
    light background. No fixed RGB or 256-color value, no background color."""
    words = {word for styles in log.KINDS.values() for style in styles for word in style.split()}
    words |= set(log.ERROR.split()) | set(log.WARN.split())
    assert words == {"bold", "dim", "blue", "green", "yellow", "red"}


def test_p8_23_load_error_is_styled(tmp_path: Path):
    """A load error's first words are the red verdict, and its message is plain."""
    missing = tmp_path / "missing.yaml"
    res = piped(missing, FORCE_COLOR="1")
    assert res.stderr == f"{ESC}[1;31mCannot read script {missing}{ESC}[0m: No such file or directory\n"


# -- what is never styled ----------------------------------------------------------


@pytest.mark.parametrize("extra", [{}, {"FORCE_COLOR": "1"}])
def test_p8_24_session_output_with_markup_characters_is_printed_literally(tmp_path: Path, extra: dict[str, str]):
    """SPEC "Output": what the device sends reaches stdout as it is: `[bold]`, `[/]` and `:100:` are text,
    and no style is added to it, with or without color."""
    res = piped(_write(tmp_path, [{"cmd": f"echo '{MARKUP}'"}]), **extra)
    assert res.returncode == 0, res.stderr
    echoed = res.stdout.replace("\r\n", "\n").split("\n")
    assert echoed[:3] == [f"PROMPT$ echo '{MARKUP}'", MARKUP, "PROMPT$ echo __AUTOBOT_RC=$?"]
    assert ESC not in res.stdout
    # the message that shows the command is as literal: no markup, no emoji for `:100:`, no highlighting
    assert f">> cmd: echo '{MARKUP}'" in _plain(res.stderr).splitlines()


def test_p8_24_session_output_is_literal_on_a_terminal(tmp_path: Path):
    """On a terminal, with color, the device's line still arrives byte for byte."""
    out, status = on_terminal(_write(tmp_path, [{"cmd": f"echo '{MARKUP}'"}]))
    assert status == 0
    assert MARKUP in out.split("\n")
    assert f"{ESC}[1;34m>>{ESC}[0m {ESC}[1mcmd: {ESC}[0mecho '{MARKUP}'" in out


def test_p8_24_messages_have_no_automatic_highlighting(tmp_path: Path):
    """Numbers, quoted strings and paths in a command get no color of their own: a style always means something."""
    cmd = 'echo "quoted" 12345 /usr/bin 10.0.0.1 True None'
    res = piped(_write(tmp_path, [{"cmd": cmd}]), FORCE_COLOR="1")
    assert f"{ESC}[1;34m>>{ESC}[0m {ESC}[1mcmd: {ESC}[0m{cmd}" in res.stderr.splitlines()


def test_p8_24_long_line_is_not_wrapped_on_a_narrow_terminal(tmp_path: Path):
    """A message is one line whatever the terminal's width: the terminal wraps it, autobot doesn't."""
    cmd = "echo " + " ".join(f"word{i}" for i in range(40))
    out, status = on_terminal(_write(tmp_path, [{"cmd": cmd}]), columns=40, NO_COLOR="1")
    assert status == 0
    assert f">> cmd: {cmd}" in out.split("\n")


# -- nesting ---------------------------------------------------------------------------

NESTED = [
    {"cmd": "echo top"},
    {
        "block": {
            "name": "outer",
            "enter": [{"cmd": "echo enter"}],
            "script": [
                {"call": "f"},
                {"block": {"name": "inner", "script": [{"cmd": "false", "ignore_error": True, "register": "x"}]}},
                {"cmd": "echo back"},
            ],
            "breakout": [{"cmd": "echo out"}],
        }
    },
    {"call": "g"},
    {"cmd": "false"},
]
NESTED_FN = {"f": {"script": [{"call": "g"}, {"cmd": "echo f"}]}, "g": {"script": [{"cmd": "echo g"}]}}


def test_p8_26_progress_lines_are_indented_by_nesting(tmp_path: Path):
    """SPEC "Output": a step of a block or of a called function is indented two spaces a level after the
    marker, which stays in the first column; a block's own lines and a `call:` line are at the level of
    their step. The breakouts and the CLI's report are back at the level they belong to."""
    path = _write(tmp_path, NESTED, fn=NESTED_FN, breakout=[{"cmd": "echo bye", "timeout": "5s"}])
    res = piped(path)
    assert res.returncode == 3, res.stderr
    assert res.stderr.splitlines() == [
        ">> attach: bash --norc --noprofile -i",
        ">> cmd: echo top",
        ">> block enter: outer",
        ">>   cmd: echo enter",
        ">>   call: f",
        ">>     call: g",
        ">>       cmd: echo g",
        ">>     cmd: echo f",
        ">>   block enter: inner",
        ">>     cmd: false",
        ">>     error ignored: command returned exit code 1",
        ">>     register: vars.x",
        ">>   block completed: inner",
        ">>   cmd: echo back",
        ">> block breakout: outer",
        ">>   cmd: echo out",
        ">> block completed: outer",
        ">> call: g",
        ">>   cmd: echo g",
        ">> cmd: false",
        ">> breakout: detaching",
        ">> cmd: echo bye",
        f"Run failed in {path}: command returned exit code 1",
        "  at script.3 (cmd: false)",
    ]
    assert all(line.startswith(">> ") for line in res.stderr.splitlines()[:-2])


def test_p8_26_depth_is_back_at_zero_after_a_failure_deep_in_the_script(tmp_path: Path):
    """A failure inside nested blocks unwinds the indentation with the steps: each block's breakout line is
    at its block's level, and `attach.breakout` at the top."""
    script = [{"block": {"name": "a", "breakout": [{"cmd": "true"}], "script": [
        {"block": {"name": "b", "breakout": [{"cmd": "true"}], "script": [{"call": "f"}]}},
    ]}}]
    path = _write(tmp_path, script, fn={"f": {"script": [{"cmd": "false"}]}}, breakout=[{"cmd": "true", "timeout": "5s"}])
    res = piped(path)
    assert res.returncode == 3, res.stderr
    assert res.stderr.splitlines()[1:-3] == [
        ">> block enter: a",
        ">>   block enter: b",
        ">>     call: f",
        ">>       cmd: false",
        ">>   block breakout: b",
        ">>     cmd: true",
        ">> block breakout: a",
        ">>   cmd: true",
        ">> breakout: detaching",
        ">> cmd: true",
    ]


def test_p8_26_call_line_comes_before_the_functions_steps(capsys: pytest.CaptureFixture[str]):
    """`>> call: <function>` is printed when the call starts, also for a function without steps."""
    runner = Runner(make_config([{"call": "empty"}], fn={"empty": {"script": []}}), {})
    runner.run_steps(runner.config.script)
    assert capsys.readouterr().err == ">> call: empty\n"
    assert log.depth == 0


# -- a message starts on a line of its own -------------------------------------------

STEPS = [{"cmd": "echo hi"}, {"block": {"name": "b", "script": [{"cmd": "echo in"}]}}, {"cmd": "false"}]


def _merged(path: Path, tmp_path: Path, how: str) -> str:
    """The CLI's output with stdout and stderr on one terminal, one pipe or one file."""
    argv = [sys.executable, "-W", "ignore", "-m", "autobot.cli", str(path)]
    if how == "terminal":
        return on_terminal(path, NO_COLOR="1")[0]
    if how == "pipe":
        res = subprocess.run(argv, check=False, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=_env())
        return res.stdout.replace("\r\n", "\n")
    with open(tmp_path / "run.log", "w") as f:
        subprocess.run(argv, check=False, stdout=f, stderr=f, env=_env())
    return (tmp_path / "run.log").read_text().replace("\r\n", "\n")


@pytest.mark.parametrize("how", ["terminal", "pipe", "file"])
def test_p8_25_message_starts_a_new_line_when_the_streams_are_one(tmp_path: Path, how: str):
    """SPEC "Output": where stdout and stderr are the same terminal, pipe or file, a message never continues
    the session's line: the prompt stays on its own line, and the message starts at the first column."""
    path = _write(tmp_path, STEPS)
    lines = _merged(path, tmp_path, how).split("\n")
    assert [line for line in lines if ">> " in line or "Run failed" in line or " at " in line] == [
        ">> attach: bash --norc --noprofile -i",
        ">> cmd: echo hi",
        ">> block enter: b",
        ">>   cmd: echo in",
        ">> block completed: b",
        ">> cmd: false",
        f"Run failed in {path}: command returned exit code 1",
        "  at script.2 (cmd: false)",
    ]
    # the session's lines around a message are whole: the prompt it interrupted, then the echo of the command
    at = lines.index(">> cmd: echo hi")
    assert lines[at - 1 : at + 3] == ["PROMPT$ ", ">> cmd: echo hi", "echo hi", "hi"]
    # no line break is added where the session's output already ended a line
    assert lines[lines.index(">>   cmd: echo in") - 1] == ">> block enter: b"
    assert "" not in lines[:-1]


def test_p8_25_nothing_is_added_when_the_streams_differ(tmp_path: Path):
    """On two pipes stderr gets no extra line break, and stdout is the session's output alone, whichever
    way the streams are set up."""
    res = piped(_write(tmp_path, STEPS))
    assert "" not in res.stderr.split("\n")[:-1]
    out = res.stdout.replace("\r\n", "\n")
    assert out.startswith("PROMPT$ echo hi\nhi\nPROMPT$ echo __AUTOBOT_RC=$?\n__AUTOBOT_RC=0\nPROMPT$ echo in\nin\n")
    assert ">> " not in out and "Run failed" not in out
    # a pipe for the messages and a file for the session: the file is the same transcript
    with open(tmp_path / "device.log", "w") as f:
        subprocess.run(
            [sys.executable, "-W", "ignore", "-m", "autobot.cli", str(tmp_path / "script.autobot.yaml")],
            check=False, stdout=f, stderr=subprocess.PIPE, env=_env(),
        )
    assert (tmp_path / "device.log").read_text() == res.stdout


def test_p8_25_line_break_goes_to_stderr_once(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    """The unit: after an echo that ends mid-line, the next message is preceded by one line break on stderr,
    only where the streams are one; an echo that ends a line needs none."""
    monkeypatch.setattr(log, "_shared", lambda: True)
    log.echoed("out\r\nPROMPT$ ")
    log.say("cmd: a")
    log.say("cmd: b")
    log.echoed("a\r\n")
    log.say("cmd: c")
    log.echoed("")  # nothing written: the line state is unchanged
    log.error("Run failed in x", "boom")
    log.echoed("PROMPT$ ")
    log.note("at", "script.0 (cmd: c)")
    out = capsys.readouterr()
    assert out.err == "\n>> cmd: a\n>> cmd: b\n>> cmd: c\nRun failed in x: boom\n\n  at script.0 (cmd: c)\n"
    assert out.out == ""

    monkeypatch.setattr(log, "_shared", lambda: False)
    log.echoed("PROMPT$ ")
    log.say("cmd: d")
    assert capsys.readouterr().err == ">> cmd: d\n"
    log.echoed("\n")


def test_p8_25_streams_without_a_file_descriptor_are_not_shared(capsys: pytest.CaptureFixture[str]):
    """Captured or replaced streams (no `fileno`) count as different."""
    assert log._shared() is False


# -- log: the unit ------------------------------------------------------------------


def test_p8_23_say_styles_only_the_label(capsys: pytest.CaptureFixture[str]):
    """`log.say` prints the text as it is after `>> `; the label ends at the first `: `."""
    log.say("cmd: echo a: b [/x]")
    log.say("called f", "ok")
    log.error("Run failed in x", "boom [/]")
    log.error("Interrupted", style=log.WARN)
    log.note("at", "script.0 (cmd: true)")
    log.more("  as [it] is")
    assert capsys.readouterr().err.splitlines() == [
        ">> cmd: echo a: b [/x]",
        ">> called f",
        "Run failed in x: boom [/]",
        "Interrupted",
        "  at script.0 (cmd: true)",
        "  as [it] is",
    ]
    assert log.MARK == ">>"
