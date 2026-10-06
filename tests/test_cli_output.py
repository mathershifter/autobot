"""P8-22..29: what Autobot prints, where, and when it is styled (SPEC "CLI", Output)."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pexpect
import pydantic
import pytest
import yaml
from conftest import make_config, make_doc, plugin_dist

from autobot import cli, log
from autobot.models import Config
from autobot.runner import Runner
from autobot.steps import CmdExecutor, StepFailure
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


def test_p8_22_nothing_but_the_documented_variables_decides(tmp_path: Path):
    """SPEC "Output": whether stderr is a terminal, `TERM`, `NO_COLOR` and `FORCE_COLOR` decide, and nothing
    else: rich's own `TTY_COMPATIBLE` neither styles a pipe nor unstyles a terminal."""
    path = _write(tmp_path, PALETTE)
    assert ESC not in piped(path, TTY_COMPATIBLE="1").stderr
    out, _ = on_terminal(path, TTY_COMPATIBLE="0")
    assert f"{ESC}[1;34m>>{ESC}[0m {ESC}[1mcmd: {ESC}[0mecho hi" in out
    out, _ = on_terminal(path, TTY_COMPATIBLE="1", NO_COLOR="1")
    assert ESC not in out


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
        f"{red}>>{off} {ESC}[31mstep failed (StepFailure): {off}assertion failed: expected ['nope']",  # a failure
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


ARROW = "printf 'a \\342\\206\\222 b\\n'"  # the bytes of U+2192, which ASCII and Latin-1 can't write


@pytest.mark.parametrize(
    ("encoding", "shown"),
    [("ascii", b"a \\u2192 b"), ("latin-1", b"a \\u2192 b"), ("utf-8", "a \u2192 b".encode()), ("utf-8:strict", "a \u2192 b".encode())],
)
def test_p8_24_character_the_stdout_encoding_cannot_write_is_escaped(tmp_path: Path, encoding: str, shown: bytes):
    """SPEC "Output": a character of the session's output that stdout's encoding can't represent is written as
    its backslash escape, and the run goes on; a stdout that can represent it gets it unchanged."""
    res = subprocess.run(
        [sys.executable, "-W", "ignore", "-m", "autobot.cli", str(_write(tmp_path, [{"cmd": ARROW, "register": "out"}, {"cmd": "echo next"}]))],
        check=False, capture_output=True, timeout=120, env=_env(PYTHONIOENCODING=encoding),
    )
    assert res.returncode == 0, res.stderr
    assert shown + b"\r\n" in res.stdout
    assert b">> cmd: echo next" in res.stderr and b"step failed" not in res.stderr


def test_p8_24_messages_have_no_automatic_highlighting(tmp_path: Path):
    """Numbers, quoted strings and paths in a command get no color of their own: a style always means something."""
    cmd = 'echo "quoted" 12345 /usr/bin 10.0.0.1 True None'
    res = piped(_write(tmp_path, [{"cmd": cmd}]), FORCE_COLOR="1")
    assert f"{ESC}[1;34m>>{ESC}[0m {ESC}[1mcmd: {ESC}[0m{cmd}" in res.stderr.splitlines()


def test_p8_24_control_characters_in_a_message(capsys: pytest.CaptureFixture[str]):
    """SPEC "Output": in a message a tab becomes spaces up to the next multiple of eight columns, and a BEL,
    backspace, vertical tab, form feed or carriage return is left out. A line break in it stays."""
    log.say("cmd: a\tb\x07c\x08d\x0be\x0cf\rg")
    log.say("error ignored: two\nlines", "warn")
    assert capsys.readouterr().err == ">> cmd: a       bcdefg\n>> error ignored: two\nlines\n"


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
        ">> step failed (StepFailure): command returned exit code 1",
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
        ">>       step failed (StepFailure): command returned exit code 1",
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


# -- what is sent without a `cmd` ----------------------------------------------------------

SECRET = "s3cret-hunter2"
# the question is printed in two pieces, so the echo of the command doesn't show it
ASK_PIN = "printf 'P'; printf 'IN: '; read -s pin; echo; test \"$pin\" = s3cret-hunter2 && echo PIN-OK"
ASK_OK = "printf 'con'; printf 'tinue? '; read answer; echo got-$answer"


def test_p8_27_line_and_return_say_that_they_sent_not_what(tmp_path: Path):
    """SPEC "Output": a `line` prints `>> line sent` for each line and a `return` `>> return sent` for each
    newline. The text of a line is never printed by Autobot: it may be a password."""
    script = [
        {"cmd": "stty -echo"},
        {"line": [f"echo {SECRET} > /dev/null", "true"]},
        {"return": 2},
        {"block": {"name": "b", "script": [{"line": "true"}]}},
    ]
    res = piped(_write(tmp_path, script))
    assert res.returncode == 0, res.stderr
    assert res.stderr.splitlines() == [
        ">> attach: bash --norc --noprofile -i",
        ">> cmd: stty -echo",
        ">> line sent",
        ">> line sent",
        ">> return sent",
        ">> return sent",
        ">> block enter: b",
        ">>   line sent",
        ">> block completed: b",
        ">> run completed",
    ]
    assert SECRET not in res.stderr and SECRET not in res.stdout


def test_p8_27_prompt_answer_names_the_prompt_and_never_the_response(tmp_path: Path):
    """SPEC "Output": each answer to a prompt prints `>> prompt answered: <name>`; the response, a `sendEach`
    value or a `send` string, is never printed."""
    prompts = [
        {"name": "sh", "expect": [r"^PROMPT\$ $"], "return": True},
        {"name": "pin", "expect": ["PIN: $"], "send": {"each": "vars.pins"}},
        {"name": "confirm", "expect": [r"continue\? $"], "send": "{{ vars.word }}"},
    ]
    doc_vars = {"pins": [SECRET], "word": "yes-" + SECRET}
    res = piped(_write(tmp_path, [{"cmd": ASK_PIN}, {"block": {"name": "b", "script": [{"cmd": ASK_OK}]}}], prompts=prompts, vars=doc_vars))
    assert res.returncode == 0, res.stderr
    assert "PIN-OK" in res.stdout.split() and f"got-yes-{SECRET}" in res.stdout.split()
    lines = res.stderr.splitlines()
    assert lines[2] == ">> prompt answered: pin"
    assert ">>   prompt answered: confirm" in lines  # at the level of the step that waits
    # the device echoes the confirmation; autobot prints neither response
    assert SECRET not in res.stderr.replace(ASK_PIN, "")


def test_p8_27_completed_run_ends_with_run_completed(tmp_path: Path):
    """SPEC "CLI": a run that completes prints `>> run completed` last, after the breakout; a failed run and
    a script that can't be loaded don't."""
    ok = piped(_write(tmp_path, [{"cmd": "true"}], breakout=[{"line": "exit"}]), FORCE_COLOR="1")
    assert ok.returncode == 0, ok.stderr
    assert ok.stderr.splitlines()[-3:] == [
        f"{ESC}[1;34m>>{ESC}[0m {ESC}[1mbreakout: {ESC}[0m{ESC}[1mdetaching{ESC}[0m",
        f"{ESC}[1;34m>>{ESC}[0m {ESC}[1mline sent{ESC}[0m",
        f"{ESC}[1;32m>>{ESC}[0m {ESC}[32mrun completed{ESC}[0m",
    ]
    assert "run completed" not in ok.stdout
    assert "run completed" not in piped(_write(tmp_path, [{"cmd": "false"}])).stderr
    assert "run completed" not in piped(_write(tmp_path, [{"cmdd": "true"}])).stderr


def test_p8_27_run_completed_starts_a_line_after_the_last_prompt(tmp_path: Path):
    """With the streams on one pipe, the run doesn't end on the session's open prompt line."""
    lines = _merged(_write(tmp_path, [{"cmd": "true"}]), tmp_path, "pipe").split("\n")
    assert lines[-3:] == ["PROMPT$ ", ">> run completed", ""]


# -- where a step fails -------------------------------------------------------------------


def test_p8_29_failed_step_says_so_before_the_breakouts(tmp_path: Path):
    """SPEC "Output": the step that fails prints `>> step failed (<type>): <message>` at its own level as it
    fails, so the log shows why the breakouts start; the CLI's report follows after them. An ignored failure
    and a step that completes print no such line, and a breakout's failing step prints its own."""
    script = [
        {"cmd": "false", "ignore_error": True},
        {"block": {"name": "b", "script": [{"cmd": "echo hi", "assert": "nope"}, {"cmd": "echo never"}],
                   "breakout": [{"cmd": "false"}, {"cmd": "echo never"}]}},
    ]
    path = _write(tmp_path, script)
    res = piped(path)
    assert res.returncode == 3, res.stderr
    assert res.stderr.splitlines() == [
        ">> attach: bash --norc --noprofile -i",
        ">> cmd: false",
        ">> error ignored: command returned exit code 1",
        ">> block enter: b",
        ">>   cmd: echo hi",
        ">>   step failed (StepFailure): assertion failed: expected ['nope']",
        ">> block breakout: b",
        ">>   cmd: false",
        ">>   step failed (StepFailure): command returned exit code 1",
        ">> block breakout error (StepFailure): command returned exit code 1",
        f"Run failed in {path}: assertion failed: expected ['nope']",
        "  at script.1.block.script.0 (cmd: echo hi)",
    ]


@pytest.mark.parametrize(
    ("script", "line"),
    [
        ([{"cmd": "sleep 30", "timeout": "1s"}], ">> step failed (TimeoutError): timed out after 1.0s waiting for a shell prompt ('sh')"),
        ([{"cmd": "echo {{ nope }}"}], ">> step failed (ScriptError): template error: 'nope' is undefined"),
        ([{"line": "exit"}, {"sleep": "5s"}], ">> step failed (EOFError): connection closed while waiting for the end of a sleep"),
    ],
    ids=["timeout", "template", "closed"],
)
def test_p8_29_failed_step_names_the_error_type(tmp_path: Path, script: list, line: str):
    """The line names the exception's class, as the breakout and close error lines do."""
    res = piped(_write(tmp_path, script))
    assert res.returncode == 3, res.stderr
    assert res.stderr.splitlines().count(line) == 1


def test_p8_29_failed_step_is_printed_once_by_the_innermost_step(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch):
    """A failure deep in blocks and calls is one line, at the failing step's level, in red on a terminal."""
    def execute(self: CmdExecutor, step: object, ctx: object, timeout: float) -> None:
        raise StepFailure("stop [/here]")

    monkeypatch.setattr(CmdExecutor, "execute", execute)
    script = [{"block": {"name": "b", "script": [{"call": "f"}]}}]
    runner = Runner(make_config(script, fn={"f": {"script": [{"cmd": "true"}]}}), {})
    with pytest.raises(StepFailure):
        runner.run_steps(runner.config.script)
    assert capsys.readouterr().err.splitlines() == [
        ">> block enter: b",
        ">>   call: f",
        ">>     step failed (StepFailure): stop [/here]",
    ]
    assert log.KINDS["fail"] == ("bold red", "red", "")


def test_p8_29_unexpected_error_and_interrupt_in_a_step(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch):
    """An unexpected error is named the same way; an interrupt prints `>> step interrupted`. `SystemExit`
    prints nothing."""
    errors: list[BaseException] = [KeyError("x"), KeyboardInterrupt(), SystemExit(3)]

    def execute(self: CmdExecutor, step: object, ctx: object, timeout: float) -> None:
        raise errors.pop(0)

    monkeypatch.setattr(CmdExecutor, "execute", execute)
    runner = Runner(make_config([{"cmd": "true"}]), {})
    for error in (KeyError, KeyboardInterrupt, SystemExit):
        with pytest.raises(error):
            runner.run_steps(runner.config.script)
    assert capsys.readouterr().err.splitlines() == [">> step failed (KeyError): 'x'", ">> step interrupted"]


CATCHING_PLUGIN = '''
import pydantic

from autobot.models import CmdStep
from autobot.steps import StepFailure


class TryStep(pydantic.BaseModel):
    attempt: str


class TryExecutor:
    key = "attempt"
    model = TryStep

    def execute(self, step, ctx, timeout):
        try:
            ctx.run_steps([CmdStep(cmd=step.attempt, timeout=5)])
        except StepFailure:
            pass
'''


def test_p8_29_step_failed_is_printed_even_if_a_plugin_then_catches_the_error(tmp_path: Path):
    """SPEC "Output": the line is printed when the error leaves the step it was raised in, at once. A plugin
    step that catches the error from `ctx.run_steps` can't take the line back: the run goes on and completes."""
    root = tmp_path / "plugin"
    root.mkdir()
    plugin_dist(root, "attempt", CATCHING_PLUGIN, "TryExecutor")
    res = piped(_write(tmp_path, [{"attempt": "false"}, {"cmd": "true"}]), PYTHONPATH=str(root))
    assert res.returncode == 0, res.stderr
    assert res.stderr.splitlines() == [
        ">> attach: bash --norc --noprofile -i",
        ">>   cmd: false",
        ">>   step failed (StepFailure): command returned exit code 1",
        ">> cmd: true",
        ">> run completed",
    ]


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
        ">> step failed (StepFailure): command returned exit code 1",
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


# -- the validation report ----------------------------------------------------------------

INVALID = """\
autobot: "2026-08"
prompts:
  - name: sh
    expect: ['^PROMPT\\$ $', '(']
    return: true
    send: 'x'
  - name: confirm
    expect: 'continue\\?'
    send: yes
errors: ['']
attach:
  spawn: ''
  timeout: 5 minutes
  env: {DEBUG: true}
script:
  - cmd: echo start
    timout: 5s
  - cmdd: oops
  - block:
      name: B
      script:
        - control: ab
        - return: 0
  - sleep:
  - line: [a, b]
    after: {x: 1, y: 2}
"""
REPORT = [
    "Validation errors:",
    "  autobot: autobot 2026-08 is no longer supported; use 2026-10 [unsupported_version]",
    "  prompts.0.send: a return prompt is a shell prompt and sends nothing; remove send or return [return_with_send]",
    "  prompts.0.expect.1: invalid regex '(': missing ), unterminated subpattern at position 0 [invalid_regex]",
    "  prompts.1.send: send must be a string; quote it, e.g. send: 'yes' or send: '1234' (unquoted, YAML reads yes, no, "
    "on, off, true, false and numbers as booleans or numbers) [send_type]",
    "  errors.0: an errors pattern must not be empty: an empty regex matches any output, so every command would fail "
    "[string_too_short]",
    "  attach.spawn: spawn must be a command, not an empty or blank string (got '') [empty_command]",
    "  attach.timeout: invalid duration: 5 minutes [value_error]",
    "  attach.env.DEBUG: an environment value is a string, and unquoted this one is a boolean (true); quote it to set "
    "it as written, e.g. 'true' or 'yes' [string_type]",
    "  script.0.cmd.timout: Extra inputs are not permitted [extra_forbidden]",
    "  script.1: cannot determine step type; expected one of cmd, sleep, call, block, line, return, control or a "
    "registered plugin step (got a mapping with the key cmdd) [invalid_step]",
    "  script.2.block.block.script.0.control.control: a control value is one character, a letter or one of "
    "@ ` [ { \\ | ] } ^ ~ _ ?, got 'ab' [control_char]",
    "  script.2.block.block.script.1.return.return: Input should be greater than or equal to 1 (got 0) [greater_than_equal]",
    "  script.3.sleep.sleep: invalid duration: null [value_error]",
    "  script.4.line.after: Input should be a valid string (got a mapping with the keys x, y) [string_type]",
]


def _invalid(tmp_path: Path, raw: str = INVALID) -> Path:
    path = tmp_path / "script.autobot.yaml"
    path.write_text(raw)
    return path


def test_p8_28_validation_report_is_one_line_an_error(tmp_path: Path):
    """SPEC "CLI": after `Validation errors:` comes one line for each error: where it is, what it is, the
    value where the message doesn't show it, and the error's type."""
    res = piped(_invalid(tmp_path))
    assert (res.returncode, res.stdout) == (1, "")
    assert res.stderr.splitlines() == REPORT


def test_p8_28_validation_report_locations_are_the_models(tmp_path: Path):
    """The location is the error's `loc` as the models report it, joined with dots, and the lines keep the
    models' order."""
    try:
        Config.model_validate(yaml.safe_load(INVALID))
    except pydantic.ValidationError as e:
        errors = e.errors()
    assert [line.split(": ")[0] for line in REPORT[1:]] == ["  " + ".".join(map(str, err["loc"])) for err in errors]
    assert [line.rsplit(" [", 1)[1] for line in REPORT[1:]] == [err["type"] + "]" for err in errors]


def test_p8_28_validation_report_styles(tmp_path: Path):
    """The location is bold and the type dim; the message is plain, and `[...]` in it is text."""
    lines = piped(_invalid(tmp_path), FORCE_COLOR="1").stderr.splitlines()
    assert lines[0] == f"{ESC}[1;31mValidation errors:{ESC}[0m"
    assert lines[1] == (
        f"  {ESC}[1mautobot{ESC}[0m: autobot 2026-08 is no longer supported; use 2026-10 {ESC}[2m[unsupported_version]{ESC}[0m"
    )
    assert [_plain(line) for line in lines] == REPORT


@pytest.mark.parametrize(
    ("raw", "line"),
    [
        ("- a\n- b\n", "  (document): Input should be a valid dictionary or instance of Config (got a list of 2 items) [model_type]"),
        ("", "  (document): Input should be a valid dictionary or instance of Config (got null) [model_type]"),
        ("autobot: '2026-10'\nattach: {spawn: sh}\n", "  script: Field required [missing]"),
        (
            "autobot: '2026-10'\nattach: {spawn: sh}\nscript: '[bold]x[/bold]'\n",
            "  script: Input should be a valid list (got '[bold]x[/bold]') [list_type]",
        ),
    ],
    ids=["list", "empty", "missing", "markup"],
)
def test_p8_28_validation_report_of_a_document_error(tmp_path: Path, raw: str, line: str):
    """An error of the document as a whole is at `(document)`; a missing key shows no value."""
    res = piped(_invalid(tmp_path, raw))
    assert res.returncode == 1
    assert res.stderr.splitlines() == ["Validation errors:", line]


SH = {"name": "sh", "expect": "x"}
SECRETS = {
    "send-on-a-return-prompt": {"prompts": [{**SH, "return": True, "send": "hunter2-secret"}]},
    "send-number": {"prompts": [SH, {"name": "pin", "expect": "PIN:", "send": 20240917}]},
    "send-each-part": {"prompts": [SH, {"name": "pin", "expect": "PIN:", "send": {"each": 20240917}}]},
    "send-each-field": {
        "prompts": [SH, {"name": "l", "send": {"each": "vars.c", "fields": [{"match": "x", "field": 20240917}]}}]
    },
    "block-prompt-send": {
        "script": [{"block": {"name": "b", "prompts": [{**SH, "return": True, "send": "hunter2-secret"}]}}]
    },
    "line-number": {"script": [{"line": 20240917}]},
    "line-list-item": {"script": [{"line": ["enable", 20240917]}]},
    "line-in-a-block": {"script": [{"block": {"name": "b", "enter": [{"line": 20240917}]}}]},
    "env-value": {"env": {"PASSWORD": 20240917}},
    "attach-env-value": {"attach": {"spawn": "sh", "env": {"PASSWORD": 20240917}}},
    "env-not-a-mapping": {"env": "hunter2-secret"},
}


@pytest.mark.parametrize("case", SECRETS)
def test_p8_28_value_that_would_be_sent_is_never_in_the_report(tmp_path: Path, case: str):
    """SPEC "CLI": the report shows no value for an error at a prompt's `send` (the parts of a `sendEach`
    included), at a `line` or at an `env` or `attach.env` value: it may be a password."""
    path = tmp_path / "script.autobot.yaml"
    path.write_text(yaml.safe_dump(make_doc([]) | SECRETS[case]))
    res = piped(path)
    lines = res.stderr.splitlines()
    assert res.returncode == 1 and lines[0] == "Validation errors:" and len(lines) > 1
    assert "hunter2" not in res.stderr and "20240917" not in res.stderr and "(got" not in res.stderr


def test_p8_28_other_values_are_still_shown(tmp_path: Path):
    """The rule is about those locations only: a `cmd`, a prompt's `expect` or a `fn` named `line` keep their value."""
    doc = {
        "prompts": [{"name": "sh", "expect": 5}],
        "fn": {"line": {"script": 7}},
        "script": [{"cmd": 20240917}, {"return": "many"}],
    }
    path = tmp_path / "script.autobot.yaml"
    path.write_text(yaml.safe_dump(make_doc([]) | doc))
    got = [line.rsplit(" [", 1)[0].rsplit(" (got ", 1)[-1] for line in piped(path).stderr.splitlines()[1:]]
    assert got == ["5)", "7)", "20240917)", "'many')"]


CONTROL_MSG = "a control value is one character, a letter or one of @ ` [ { \\ | ] } ^ ~ _ ?, got "
VALIDATOR_PLUGIN = '''
import pydantic


class ShoutStep(pydantic.BaseModel):
    shout: str

    @pydantic.field_validator("shout")
    @classmethod
    def _check(cls, v):
        raise ValueError("bad\\nvalue \\x1b[31mred\\ttab\\rend \\x07")


class ShoutExecutor:
    key = "shout"
    model = ShoutStep

    def execute(self, step, ctx, timeout):
        pass
'''


@pytest.mark.parametrize(
    ("doc", "line"),
    [
        ({"script": [{"control": "a\nb"}]}, "  script.0.control.control: " + CONTROL_MSG + "'a\\nb' [control_char]"),
        ({"script": [{"control": "a\rb"}]}, "  script.0.control.control: " + CONTROL_MSG + "'a\\rb' [control_char]"),
        ({"script": [{"control": "\tb"}]}, "  script.0.control.control: " + CONTROL_MSG + "'\\tb' [control_char]"),
        (
            {"script": [{"control": "\x1b[31mred"}]},
            "  script.0.control.control: " + CONTROL_MSG + "'\\x1b[31mred' [control_char]",
        ),
        (
            {"script": [{"cmd": "true", "timeout": "5 min\nutes"}]},
            "  script.0.cmd.timeout: invalid duration: 5 min\\nutes [value_error]",
        ),
        (
            {"script": [], "fn": {"a\nb\x1b": {"script": 5}}},
            "  fn.a\\nb\\x1b.script: Input should be a valid list (got 5) [list_type]",
        ),
        (
            {"script": [{"cmd": "true", "after": {"k\ney": 1}}]},
            "  script.0.cmd.after: Input should be a valid string (got a mapping with the key k\\ney) [string_type]",
        ),
    ],
    ids=["newline", "carriage-return", "tab", "escape", "duration", "location", "mapping-key"],
)
def test_p8_28_control_characters_in_a_validation_error_are_escaped(tmp_path: Path, doc: dict, line: str):
    """SPEC "CLI": an error is one line whatever its message, location or value holds: a control character is
    written as its escape (`\\n`, `\\x1b`), so no line break and no ESC from the script reaches the terminal."""
    path = tmp_path / "script.autobot.yaml"
    path.write_text(yaml.safe_dump(make_doc([]) | doc))
    res = piped(path, FORCE_COLOR="1")
    assert res.returncode == 1
    assert _plain(res.stderr).split("\n") == ["Validation errors:", line, ""]
    # the only escape sequences are the report's own styles
    assert ESC not in ANSI_ESCAPE_RE.sub("", res.stderr) and "\r" not in res.stderr and "\t" not in res.stderr
    assert piped(path).stderr.split("\n") == ["Validation errors:", line, ""]


def test_p8_28_control_characters_in_a_plugin_models_message_are_escaped(tmp_path: Path):
    """The rule holds for a message the CLI doesn't write itself: a plugin model's validator."""
    root = tmp_path / "plugin"
    root.mkdir()
    plugin_dist(root, "shout", VALIDATOR_PLUGIN, "ShoutExecutor")
    res = piped(_write(tmp_path, [{"shout": "x"}]), PYTHONPATH=str(root))
    assert res.returncode == 1
    assert res.stderr.split("\n") == [
        "Validation errors:",
        "  script.0.shout: bad\\nvalue \\x1b[31mred\\ttab\\rend \\x07 [value_error]",
        "",
    ]


@pytest.mark.parametrize(
    ("type_", "msg", "value", "got"),
    [
        ("string_type", "Input should be a valid string", 5, " (got 5)"),
        ("string_type", "Input should be a valid string", 2.5, " (got 2.5)"),
        ("string_type", "Input should be a valid string", True, " (got true)"),
        ("string_type", "Input should be a valid string", None, " (got null)"),
        ("string_type", "Input should be a valid string", {}, " (got an empty mapping)"),
        ("string_type", "Input should be a valid string", {"a": 1, 2: 3}, " (got a mapping with the keys a, 2)"),
        ("string_type", "Input should be a valid string", ["a"], " (got a list of 1 item)"),
        ("string_type", "Input should be a valid string", [], " (got a list of 0 items)"),
        ("string_type", "Input should be a valid string", b"hi", " (got binary data)"),
        ("list_type", "Input should be a valid list", "x" * 80, " (got '" + "x" * 59 + "...)"),
        # a value that happens to be a word of the message is still shown: the type decides, not the text
        ("list_type", "Input should be a valid list", "list", " (got 'list')"),
        ("string_type", "Input should be a valid string", "string", " (got 'string')"),
        ("int_type", "Input should be a valid integer", "valid", " (got 'valid')"),
        ("some_type", "a is not allowed", "a", " (got 'a')"),
        ("nul_character", "spawn must not contain a NUL character", "a\0b", " (got 'a\\x00b')"),
        ("empty_command", "spawn must be a command, not an empty or blank string", "  ", " (got '  ')"),
        # these types have a message that shows the value, or a name that says what it is
        ("empty_command", "spawn must name a command: the first word of \"'' ls\" is empty", "'' ls", ""),
        ("string_too_short", "a regex must not be empty", "", ""),
        ("too_short", "expect must be a regex or a non-empty list of regexes", [], ""),
        ("value_error", "invalid duration: 5 minutes", "5 minutes", ""),
        ("value_error", "invalid duration: null", None, ""),
        ("value_error", "invalid duration: true", True, ""),
        ("value_error", "invalid duration: nan (not a finite number)", float("nan"), ""),
        ("value_error", "invalid duration: b'hi'", b"hi", ""),
        ("control_char", "one character, got 'ab'", "ab", ""),
        ("invalid_regex", "invalid regex '(': missing )", "(", ""),
        ("unsupported_version", "autobot 2026-08 is no longer supported", "2026-08", ""),
        ("unsupported_version", "unsupported autobot version '2026'; expected 2026-10", 2026, ""),
        ("undefined_function", "call to undefined function 'nope'", "nope", ""),
        ("missing", "Field required", {"a": 1}, ""),
        ("extra_forbidden", "Extra inputs are not permitted", "5s", ""),
    ],
)
def test_p8_28_offending_value_is_shown_unless_the_message_shows_it(type_: str, msg: str, value: object, got: str):
    """The value is written as YAML writes it, cut at 60 characters; a mapping shows its keys and a list its
    length. Whether it is shown depends on the error's type: not for the types whose message shows the value
    or whose name says what it is, and not for a missing or an unknown key."""
    assert cli._got(type_, msg, value) == got


@pytest.mark.parametrize(
    ("doc", "lines"),
    [
        ({"autobot": 2026}, ["  autobot: unsupported autobot version '2026'; expected 2026-10 [unsupported_version]"]),
        (
            {"script": [{"sleep": float("nan")}]},
            ["  script.0.sleep.sleep: invalid duration: nan (not a finite number) [value_error]"],
        ),
        ({"script": [{"sleep": b"hi"}]}, ["  script.0.sleep.sleep: invalid duration: b'hi' [value_error]"]),
        ({"script": "list"}, ["  script: Input should be a valid list (got 'list') [list_type]"]),
        # a value that is neither a string nor a list of strings is one error, not one for each of the two
        (
            {"script": [{"cmd": 5}]},
            ["  script.0.cmd.cmd: Input should be a string or a list of strings (got 5) [string_type]"],
        ),
        (
            {"script": [{"cmd": {"a": 1}}]},
            ["  script.0.cmd.cmd: Input should be a string or a list of strings (got a mapping with the key a) [string_type]"],
        ),
        ({"script": [{"line": 5}]}, ["  script.0.line.line: Input should be a string or a list of strings [string_type]"]),
        (
            {"script": [{"control": 5}, {"cmd": "true", "assert": 7}]},
            [
                "  script.0.control.control: Input should be a string or a list of strings (got 5) [string_type]",
                "  script.1.cmd.assert: Input should be a string or a list of strings (got 7) [string_type]",
            ],
        ),
        # an entry of `expect` takes a string only: a list in it is `grouped_expect`
        (
            {"prompts": [{"name": "sh", "expect": ["x", 5]}]},
            ["  prompts.0.expect.1: Input should be a valid string (got 5) [string_type]"],
        ),
        # a list with a bad item: the item is the error, at its index
        (
            {"script": [{"cmd": ["ok", 5, None]}]},
            [
                "  script.0.cmd.cmd.1: Input should be a valid string (got 5) [string_type]",
                "  script.0.cmd.cmd.2: Input should be a valid string (got null) [string_type]",
            ],
        ),
        ({"script": [{"line": ["ok", 5]}]}, ["  script.0.line.line.1: Input should be a valid string [string_type]"]),
        # a mapping that only has keys named like the members of the union is left alone
        ({"env": {"str": 5, "list[str]": 6}}, [
            "  env.list[str]: Input should be a valid string [string_type]",
            "  env.str: Input should be a valid string [string_type]",
        ]),
    ],
    ids=["version", "nan", "binary", "word", "cmd", "cmd-mapping", "line", "control-assert", "expect-entry", "cmd-item", "line-item", "env-keys"],
)
def test_p8_28_one_line_for_one_mistake(tmp_path: Path, doc: dict, lines: list[str]):
    """SPEC "CLI": the value is not repeated after a message that shows it, and a value that is neither a
    string nor a list of strings is reported once, at the field, with what the field accepts."""
    path = tmp_path / "script.autobot.yaml"
    path.write_text(yaml.dump(make_doc([]) | doc))
    res = piped(path)
    assert res.stderr.splitlines() == ["Validation errors:", *lines]


def test_p8_26_indentation_stops_at_eight_levels(tmp_path: Path):
    """SPEC "Output": the indentation grows for eight levels and no further, so a deep recursion stays readable."""
    fn = {"f": {"script": [{"call": "f"}]}}
    res = piped(_write(tmp_path, [{"call": "f"}], fn=fn))
    assert res.returncode == 3
    calls = [line for line in res.stderr.splitlines() if line.endswith("call: f")]
    assert len(calls) > 100
    assert calls[:10] == [">> " + "  " * min(n, 8) + "call: f" for n in range(10)]
    assert set(calls[8:]) == {">> " + "  " * 8 + "call: f"}
    assert max(map(len, res.stderr.splitlines()[:-12])) < 120


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
