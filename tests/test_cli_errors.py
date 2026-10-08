"""P6-83..89: how the CLI ends a run that fails (SPEC "CLI": errors while the script runs, exit status)."""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pexpect
import pydantic
import pytest
import yaml
from conftest import BASH, PS1, SHELL_PROMPT, make_config, make_doc, plugin_dist, run_cli

from autobot import cli
from autobot.registry import PluginError
from autobot.runner import Runner, StepRef, trail
from autobot.session import CommandError
from autobot.steps import CmdExecutor, StepFailure
from autobot.types import EnvError, RunError, ScriptError

PIN = {"name": "pin", "expect": ["PIN:"], "send": {"each": "vars.pins"}}
BOOLEAN = (
    "template error: an expression gave a boolean (true), which is never written as text; quote the value in "
    "the script, or say which text is meant, e.g. {{ value | string }} (True) or {{ value | tojson }} (true)"
)

# case -> (script, make_doc keywords, the reason, the `at` line or None for a failure outside any step)
RUN_FAILURES: dict[str, tuple[list, dict[str, Any], str, str | None]] = {
    "exit-code": ([{"cmd": "false"}], {}, "command returned exit code 1", "script.0 (cmd: false)"),
    "assert": (
        [{"cmd": "echo a", "assert": "zzz"}], {}, "assertion failed: expected ['zzz']", "script.0 (cmd: echo a)",
    ),
    "errors-pattern": (
        [{"cmd": "echo '% Invalid input'"}],
        {"errors": ["^% .*"]},
        "command error: % Invalid input",
        "script.0 (cmd: echo '% Invalid input')",
    ),
    "prompt-timeout": (
        [{"cmd": "sleep 30", "timeout": "1s"}],
        {},
        "timed out after 1.0s waiting for a shell prompt ('sh')",
        "script.0 (cmd: sleep 30)",
    ),
    "after-timeout": (
        [{"control": "c", "after": "NEVER", "timeout": "1s"}],
        {},
        "timed out after 1.0s waiting for the after pattern 'NEVER'",
        "script.0 (control)",
    ),
    "connection-closed": (
        [{"line": "exit"}, {"cmd": "true"}],
        {},
        "connection closed while waiting for a shell prompt ('sh')",
        "script.1 (cmd: true)",
    ),
    "template-undefined": (
        [{"cmd": "echo {{ vars.nope }}"}],
        {"vars": {}},
        "template error: 'dict object' has no attribute 'nope'",
        "script.0 (cmd: echo {{ vars.nope }})",
    ),
    "template-boolean": (
        [{"cmd": "echo {{ vars.debug }}"}], {"vars": {"debug": True}}, BOOLEAN, "script.0 (cmd: echo {{ vars.debug }})",
    ),
    "assert-renders-invalid": (
        [{"cmd": "true", "assert": "{{ vars.p }}"}],
        {"vars": {"p": "("}},
        "assert: invalid regex '(': missing ), unterminated subpattern at position 0",
        "script.0 (cmd: true)",
    ),
    "after-renders-empty": (
        [{"cmd": "true", "after": "{{ vars.p }}"}],
        {"vars": {"p": ""}},
        "after: the pattern rendered to an empty regex, which matches at once",
        "script.0 (cmd: true)",
    ),
    "spawn-renders-empty": (
        [{"cmd": "true"}],
        {"vars": {"s": ""}, "spawn": "{{ vars.s }}"},
        "attach.spawn rendered to an empty command: '{{ vars.s }}'",
        None,
    ),
    "spawn-renders-a-nul": (
        [{"cmd": "true"}],
        {"vars": {"s": "a\0b"}, "spawn": "echo {{ vars.s }}", "prepare": "#!/bin/sh\nexit 9\n"},
        "attach.spawn rendered to a command line with a NUL character: 'echo {{ vars.s }}'",
        None,
    ),
    "prepare-fails": ([{"cmd": "true"}], {"prepare": "#!/bin/sh\nexit 3\n"}, "prepare script failed with exit code 3", None),
    "prepare-removes-itself": (
        # the script deletes its own temp file: the report is still its exit code
        [{"cmd": "true"}],
        {"prepare": '#!/bin/sh\nrm -- "$0"\nexit 4\n'},
        "prepare script failed with exit code 4",
        None,
    ),
    "prepare-cannot-run": (
        [{"cmd": "true"}],
        {"prepare": "#!/autobot/no/such/interpreter\n"},
        "prepare script could not run ('#!/autobot/no/such/interpreter'): [Errno 2] No such file or directory",
        None,
    ),
    "spawn-not-found": (
        [{"cmd": "true"}],
        {"spawn": "autobot_no_such_cmd"},
        "The command was not found or was not executable: autobot_no_such_cmd.",
        None,
    ),
    "spawn-timeout": (
        [{"cmd": "true"}],
        {"spawn": "sleep 30", "timeout": 1},
        "timed out after 1.0s waiting for the first output from 'sleep 30' (attach.timeout)",
        None,
    ),
    "spawn-exits": (
        [{"cmd": "true"}], {"spawn": "true"}, "connection closed before any output from 'true' (exit status 0)", None,
    ),
    "responses-exhausted": (
        # the shell asks for a PIN twice, and there is one to answer with. The question is printed in two
        # pieces, so the echo of the command doesn't show it: only the shell's own questions are answered
        [{"cmd": "printf 'PI'; printf 'N:'; read a; printf 'PI'; printf 'N:'; read b"}],
        {"prompts": [SHELL_PROMPT, PIN], "vars": {"pins": ["1234"]}},
        "prompt 'pin': responses exhausted",
        "script.0 (cmd: printf 'PI'; printf 'N:'; read a; printf 'PI'; printf 'N:'; read b)",
    ),
    "block-send-each": (
        [{"block": {"name": "b", "prompts": [SHELL_PROMPT, {**PIN, "send": {"each": "vars.nope"}}]}}],
        {"vars": {}},
        "prompt 'pin': sendEach 'vars.nope': no key 'nope' in 'vars'",
        "script.0 (block: b)",
    ),
}


def _lines(res: subprocess.CompletedProcess[str]) -> list[str]:
    """What the CLI itself says on stderr: not the engine's `>> ` progress lines, not runpy's warning."""
    return [line for line in res.stderr.splitlines() if not line.startswith(">> ") and "RuntimeWarning" not in line]


@pytest.mark.parametrize("case", RUN_FAILURES)
def test_p6_83_cli_expected_run_failure_has_no_traceback(case: str, tmp_path: Path):
    """SPEC "CLI": a failure the script, the device or the environment explains is `Run failed in <path>: <reason>`
    and the step it happened in, with status 3 and no traceback."""
    script, kw, reason, at = RUN_FAILURES[case]
    log = tmp_path / "broke-out"
    touch = f"touch {log}"
    doc = make_doc(script, breakout=[{"control": "c"}, {"cmd": touch, "timeout": "3s"}], **kw)
    res = run_cli(doc, tmp_path)
    path = tmp_path / "script.autobot.yaml"
    assert res.returncode == 3, res.stderr
    assert "Traceback" not in res.stderr
    report = [f"Run failed in {path}: {reason}", *([f"  at {at}"] if at else [])]
    if case == "connection-closed":  # the breakout has nobody to talk to: its failure is reported as well (P6-115)
        report += [
            f"Breakout failed in {path}: connection closed while waiting for a shell prompt ('sh')",
            f"  at attach.breakout.1 (cmd: {touch if len(touch) <= 72 else touch[:72] + '...'})",
            "Connection closed before a breakout finished: a console behind a console server may still be logged in",
        ]
    assert _lines(res) == report
    assert "Run failed" not in res.stdout
    # a failure after the spawn wait still breaks out; one before it has nothing to break out of
    assert (">> breakout: detaching" in res.stderr.splitlines()) == (at is not None)
    # and its steps get through, also from a shell that is still waiting at a question (the breakout's
    # Ctrl-C ends that); only where the shell is gone do they fail
    assert log.exists() == (at is not None and case != "connection-closed")


def _main(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[Any, str, str]:
    """`cli.main()` in this process: its exit status, stdout and stderr."""
    monkeypatch.setattr(sys, "argv", ["autobot", *argv])
    with pytest.raises(SystemExit) as ei:
        cli.main()
    out = capsys.readouterr()
    return ei.value.code, out.out, out.err


def _script(tmp_path: Path, script: list | None = None, **kw: Any) -> Path:
    path = tmp_path / "script.autobot.yaml"
    path.write_text(yaml.safe_dump(make_doc(script or [{"cmd": "true"}], **kw)))
    return path


def _validation_error() -> Exception:
    try:
        make_config([{"cmd": 1}])
    except pydantic.ValidationError as e:
        return e
    raise AssertionError("the document validated")


HINT = "  (run with --traceback for details)\n"
# (the error, the reason reported, whether it is expected only for its broad built-in class)
EXPECTED_ERRORS: list[tuple[BaseException, str, bool]] = [
    (StepFailure("command returned exit code 2"), "command returned exit code 2", False),
    (CommandError("command error: % bad", "out"), "command error: % bad", False),
    (RunError("prompt 'p': no response available"), "prompt 'p': no response available", False),
    (ScriptError("template error: boom"), "template error: boom", False),
    (EnvError("env cycle: A -> A"), "env cycle: A -> A", False),
    (TimeoutError("timed out after 5s waiting for x"), "timed out after 5s waiting for x", False),
    (EOFError("connection closed while waiting for x"), "connection closed while waiting for x", False),
    (EOFError(), "EOFError", False),
    (pexpect.ExceptionPexpect("Could not terminate the child."), "Could not terminate the child.", False),
    (OSError(5, "Input/output error"), "[Errno 5] Input/output error", True),
    (BrokenPipeError(32, "Broken pipe"), "[Errno 32] Broken pipe", True),
    (
        UnicodeEncodeError("utf-8", "\udcff", 0, 1, "surrogates not allowed"),
        "'utf-8' codec can't encode character '\\udcff' in position 0: surrogates not allowed",
        True,
    ),
    # outside any `call`, the reason is the error's own
    (RecursionError("maximum recursion depth exceeded"), "maximum recursion depth exceeded", True),
]


@pytest.mark.parametrize(
    ("error", "reason", "hint"), EXPECTED_ERRORS, ids=[type(e).__name__ for e, _, _ in EXPECTED_ERRORS]
)
def test_p6_84_expected_error_classes_exit_3_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    error: BaseException,
    reason: str,
    hint: bool,
):
    """SPEC "CLI": each class of expected failure is one `Run failed` line, status 3, whatever raises it.
    One that is expected only for its broad built-in class (`OSError`, `UnicodeError`, `RecursionError`) could
    also be a bug, so its report ends with where to find the traceback; Autobot's own errors, a timeout and a
    closed connection don't need that."""
    def run(self: Runner) -> None:
        raise error

    monkeypatch.setattr(Runner, "run", run)
    path = _script(tmp_path)
    code, out, err = _main(monkeypatch, capsys, str(path))
    assert (code, out, err) == (3, "", f"Run failed in {path}: {reason}\n" + (HINT if hint else ""))


def test_p6_84_hint_is_left_out_when_the_traceback_is_shown(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
):
    """With `--traceback` the report doesn't point to the flag."""
    def run(self: Runner) -> None:
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(Runner, "run", run)
    path = _script(tmp_path)
    code, _, err = _main(monkeypatch, capsys, str(path), "--traceback")
    assert code == 3
    assert err.startswith("Traceback (most recent call last):\n")
    assert err.endswith(f"OSError: [Errno 5] Input/output error\nRun failed in {path}: [Errno 5] Input/output error\n")


UNEXPECTED_ERRORS: list[BaseException] = [
    KeyError("x"),
    ValueError("unknown step type: x"),
    RuntimeError("not attached"),
    TypeError("can't"),
    AssertionError(),
    PluginError("raised while running"),
    _validation_error(),
]


@pytest.mark.parametrize("error", UNEXPECTED_ERRORS, ids=[type(e).__name__ for e in UNEXPECTED_ERRORS])
def test_p6_84_any_other_error_is_unexpected_and_keeps_its_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path, error: BaseException
):
    """SPEC "CLI": what isn't an expected failure is a bug: one line that says so, the traceback, status 70.

    A plain `ValueError` or `RuntimeError` is one of them, and so is a pydantic `ValidationError`, which is a
    `ValueError`: only the engine's own `ScriptError` and `RunError` are expected.
    """
    def run(self: Runner) -> None:
        raise error

    monkeypatch.setattr(Runner, "run", run)
    code, out, err = _main(monkeypatch, capsys, str(_script(tmp_path)))
    lines = err.splitlines()
    assert (code, out) == (70, "")
    assert lines[0] == (
        "Unexpected error in Autobot: this is a bug, not a problem with the script. "
        "Please report it with the traceback below."
    )
    assert lines[1] == "Traceback (most recent call last):"
    assert type(error).__name__ in lines[-1] or isinstance(error, pydantic.ValidationError)
    assert "Run failed" not in err


def test_p6_84_unexpected_error_outside_a_run_is_reported_the_same_way(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
):
    """A bug while the script is loaded, or in `autobot schema`, gets the same line, traceback and status."""
    def boom(*_args: object, **_kwargs: object) -> None:
        raise KeyError("loading")

    monkeypatch.setattr(cli.Config, "model_validate", boom)
    code, _, err = _main(monkeypatch, capsys, str(_script(tmp_path)))
    assert code == 70
    assert err.startswith("Unexpected error in Autobot: ") and err.rstrip().endswith("KeyError: 'loading'")

    monkeypatch.setattr(cli, "load_schema", boom)
    code, out, err = _main(monkeypatch, capsys, "schema")
    assert (code, out) == (70, "")
    assert err.startswith("Unexpected error in Autobot: ") and "Traceback (most recent call last):" in err


def test_p6_84_system_exit_is_not_caught(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path):
    """`SystemExit` from a run passes through with its own status and no message."""
    def run(self: Runner) -> None:
        raise SystemExit(9)

    monkeypatch.setattr(Runner, "run", run)
    assert _main(monkeypatch, capsys, str(_script(tmp_path))) == (9, "", "")


def test_p6_84_expected_classes_keep_their_base_classes():
    """The engine's own classes add nothing to what an `except` clause catches: they are still a `ValueError`
    or a `RuntimeError`, and the CLI's set of expected failures holds neither of those."""
    assert issubclass(ScriptError, ValueError) and issubclass(EnvError, ScriptError)
    assert issubclass(RunError, RuntimeError)
    assert issubclass(StepFailure, RunError) and issubclass(CommandError, RunError)
    assert ValueError not in cli.EXPECTED and RuntimeError not in cli.EXPECTED and Exception not in cli.EXPECTED
    assert not issubclass(pydantic.ValidationError, cli.EXPECTED)
    assert not issubclass(PluginError, cli.EXPECTED)
    assert not issubclass(KeyboardInterrupt, cli.EXPECTED)


# -- where it failed ------------------------------------------------------------

NESTED = [
    {"cmd": "true"},
    {
        "block": {
            "name": "outer",
            "enter": [{"cmd": "true"}],
            "script": [
                {"cmd": "true"},
                {"block": {"name": "inner", "script": [{"call": "check"}], "breakout": [{"cmd": "false"}]}},
            ],
        }
    },
]
FN = {
    "check": {"script": [{"cmd": "true"}, {"call": "deep"}]},
    "deep": {"script": [{"cmd": ["true", "false"]}]},
}


def test_p6_85_failure_names_the_step_and_the_calls_that_led_to_it(tmp_path: Path):
    """SPEC "CLI": `at` is the failing step's own path in the script; a step of a function also lists its callers,
    nearest first. A block is part of the path and isn't listed. A breakout's own failure doesn't change it."""
    res = run_cli(make_doc(NESTED, fn=FN), tmp_path)
    assert res.returncode == 3, res.stderr
    assert _lines(res) == [
        f"Run failed in {tmp_path / 'script.autobot.yaml'}: command returned exit code 1",
        "  at fn.deep.script.0 (cmd: true (+1 more))",
        "  called from fn.check.script.1 (call: deep)",
        "  called from script.1.block.script.1.block.script.0 (call: check)",
        f"Breakout failed in {tmp_path / 'script.autobot.yaml'}: command returned exit code 1",
        "  at script.1.block.script.1.block.breakout.0 (cmd: false)",
        "Session may be left logged in: a breakout did not finish",
    ]
    assert ">>   block breakout error (StepFailure): command returned exit code 1" in res.stderr.splitlines()


@pytest.mark.parametrize(
    ("part", "at"),
    [
        ("attach_script", "attach.script.0 (cmd: false)"),
        ("enter", "script.0.block.enter.0 (cmd: false)"),
    ],
)
def test_p6_85_paths_of_attach_script_and_block_enter(tmp_path: Path, part: str, at: str):
    """The path names the list the step is in: `attach.script`, a block's `enter`."""
    bad = [{"cmd": "false", "timeout": "5s"}]
    if part == "enter":
        doc = make_doc([{"block": {"name": "b", "enter": bad}}])
    else:
        doc = make_doc([], attach_script=bad)
    res = run_cli(doc, tmp_path)
    assert res.returncode == 3, res.stderr
    assert _lines(res)[1:] == [f"  at {at}"]


FAILS = {"when": "{{ nope }}"}  # an undefined variable: the step fails before it does anything


@pytest.mark.parametrize(
    ("step", "what"),
    [
        ({"cmd": "echo " + "x" * 100, **FAILS}, "cmd: echo " + "x" * 67 + "..."),
        ({"cmd": "\n\n  echo first\necho second\n", **FAILS}, "cmd: echo first"),
        ({"cmd": "#!/bin/sh\necho hi\n", **FAILS}, "cmd: #!/bin/sh"),
        ({"cmd": [], **FAILS}, "cmd"),
        ({"cmd": ["echo {{ vars.v }}"], **FAILS}, "cmd: echo {{ vars.v }}"),
        ({"cmd": ["echo a", "echo b", "echo c"], **FAILS}, "cmd: echo a (+2 more)"),
        ({"line": "hunter2", **FAILS}, "line"),
        ({"line": ["a", "b"], **FAILS}, "line"),
        ({"return": 2, **FAILS}, "return"),
        ({"control": "c", **FAILS}, "control"),
        ({"sleep": "5s"}, "sleep"),
        ({"call": "f", **FAILS}, "call: f"),
        ({"block": {"name": "Host Console"}, **FAILS}, "block: Host Console"),
    ],
    ids=[
        "long", "first-line", "embedded", "empty-list", "template", "list", "line", "lines", "return", "control",
        "sleep", "call", "block",
    ],
)
def test_p6_85_what_a_step_is_called_in_a_report(tmp_path: Path, step: dict, what: str):
    """SPEC "CLI": in the `at` line a `cmd` shows its first line as written, not rendered, cut at 72 characters;
    `call` and `block` their name. A `line` shows nothing of what it sends: it may be a password, which the
    session never echoes."""
    # a `sleep` has no `when` to fail it: it fails because the step before it has closed the connection
    script = [{"line": "exit"}, step] if "sleep" in step else [step]
    res = run_cli(make_doc(script, fn={"f": {"script": []}}, step_timeout=None), tmp_path)
    assert res.returncode == 3, res.stderr
    assert _lines(res)[1:] == [f"  at script.{len(script) - 1} ({what})"]
    assert "hunter2" not in res.stderr


def test_p6_85_trail_is_set_once_by_the_innermost_step(monkeypatch: pytest.MonkeyPatch):
    """The exception carries the steps that were running, outermost first; an outer step doesn't overwrite it,
    and an exception raised outside any step has an empty trail."""
    def execute(self: CmdExecutor, step: object, ctx: object, timeout: float) -> None:
        raise StepFailure("stop")

    monkeypatch.setattr(CmdExecutor, "execute", execute)
    script = [{"block": {"name": "b", "script": [{"call": "f"}]}}]
    runner = Runner(make_config(script, fn={"f": {"script": [{"cmd": "true"}]}}), {})
    with pytest.raises(StepFailure) as ei:
        runner.run_steps(runner.config.script)
    assert trail(ei.value) == (
        StepRef("script.0", "block", "block: b"),
        StepRef("script.0.block.script.0", "call", "call: f"),
        StepRef("fn.f.script.0", "cmd", "cmd: true"),
    )
    assert runner._stack == []
    assert trail(ValueError("elsewhere")) == ()


def test_p6_85_endless_recursion_is_a_script_problem(tmp_path: Path):
    """SPEC "call": functions may call themselves, and one that never stops fails the run; only the nearest
    callers are listed."""
    res = run_cli(make_doc([{"call": "f"}], fn={"f": {"script": [{"call": "f"}]}}), tmp_path)
    assert res.returncode == 3, res.stderr
    assert "Traceback" not in res.stderr
    lines = _lines(res)
    assert lines[0].startswith(
        f"Run failed in {tmp_path / 'script.autobot.yaml'}: functions call each other too deeply (maximum recursion"
    )
    assert lines[1] == "  at fn.f.script.0 (call: f)"
    assert lines[2:7] == ["  called from fn.f.script.0 (call: f)"] * 5
    assert len(lines) == 9 and lines[7].startswith("  ... and ") and lines[7].endswith(" more callers")
    assert lines[8] == "  (run with --traceback for details)"


def test_p6_83_prepare_that_removes_its_own_file_and_succeeds_runs_the_script(tmp_path: Path):
    """SPEC "attach": the temp file of `prepare` is removed afterwards, and it is no error if the script has
    removed it already."""
    res = run_cli(make_doc([{"cmd": "echo ran-$((40+2))"}], prepare='#!/bin/sh\nrm -- "$0"\n'), tmp_path)
    assert res.returncode == 0, res.stderr
    assert "ran-42" in res.stdout


# -- unexpected errors ------------------------------------------------------------

BUGGY_PLUGIN = '''
import pydantic


class BoomStep(pydantic.BaseModel):
    boom: str


class NestStep(pydantic.BaseModel):
    nest: str


class BoomExecutor:
    key = "boom"
    model = BoomStep

    def execute(self, step, ctx, timeout):
        return {"a": 1}[step.boom]


class NestExecutor:
    """Runs steps it builds itself: they aren't in the script, so their path is the plugin step's."""

    key = "nest"
    model = NestStep

    def execute(self, step, ctx, timeout):
        from autobot.models import CallStep, CmdStep
        if step.nest == "call":
            ctx.run_steps([CallStep(call="no_such_function")])
        ctx.run_steps([CmdStep(cmd="true", timeout=5), CmdStep(cmd=step.nest, timeout=5)])
'''


@pytest.fixture
def buggy(tmp_path: Path) -> list[Path]:
    roots = []
    for key, target in (("boom", "BoomExecutor"), ("nest", "NestExecutor")):
        root = tmp_path / f"plugin_{key}"
        root.mkdir()
        roots.append(plugin_dist(root, key, BUGGY_PLUGIN, target))
    return roots


def test_p6_86_plugin_bug_keeps_its_traceback_and_names_the_plugin(tmp_path: Path, buggy: list[Path]):
    """SPEC "CLI": an error a plugin's `execute` raises that isn't an expected failure is the plugin's bug: a
    line naming the plugin, the step, the traceback, status 70. The breakout still runs first."""
    log = tmp_path / "broke-out"
    doc = make_doc([{"cmd": "true"}, {"boom": "x"}], breakout=[{"cmd": f"touch {log}", "timeout": "3s"}])
    res = run_cli(doc, tmp_path, pythonpath=buggy)
    lines = _lines(res)
    assert res.returncode == 70, res.stderr
    assert lines[:3] == [
        "Unexpected error in plugin 'boom': this is a bug in the plugin, not in the script. "
        "Please report it to the plugin's author with the traceback below.",
        "  at script.1 (boom)",
        "Traceback (most recent call last):",
    ]
    assert lines[-1] == "KeyError: 'x'"
    assert any("autobot_testplugin_boom.py" in line for line in lines)
    assert log.exists()


OWN_PLUGIN = '''
import pydantic


class OwnStep(pydantic.BaseModel):
    own: str


def forever():
    return forever()


class OwnExecutor:
    key = "own"
    model = OwnStep

    def execute(self, step, ctx, timeout):
        if step.own == "oserror":
            open("/autobot/no/such/file")
        if step.own == "unicode":
            "\\udcff".encode()
        if step.own == "recursion":
            forever()
        if step.own == "timeout":
            raise TimeoutError("the plugin gave up after 1s")
        if step.own == "session":
            # the session's own write fails: its pty is gone
            ctx.session._cld.close()
            ctx.session.sendline("true")
'''


def _own(tmp_path: Path, mode: str, *cli_args: str) -> subprocess.CompletedProcess[str]:
    root = tmp_path / "plugin_own"
    if not root.exists():
        root.mkdir()
        plugin_dist(root, "own", OWN_PLUGIN, "OwnExecutor")
    return run_cli(make_doc([{"own": mode}]), tmp_path, *cli_args, pythonpath=root)


@pytest.mark.parametrize(
    ("mode", "last"),
    [
        ("oserror", "FileNotFoundError: [Errno 2] No such file or directory: '/autobot/no/such/file'"),
        ("unicode", "UnicodeEncodeError: 'utf-8' codec can't encode character '\\udcff' in position 0: surrogates not allowed"),
        ("recursion", "RecursionError: maximum recursion depth exceeded"),
    ],
)
def test_p6_86_broad_error_of_a_plugins_own_code_is_the_plugins_bug(tmp_path: Path, mode: str, last: str):
    """SPEC "CLI": an `OSError`, `UnicodeError` or `RecursionError` is a failed run when the engine raises it.
    Raised by a plugin's own code, with no frame of Autobot below its `execute`, it is the plugin's bug like
    any other exception: the plugin is named, the traceback is kept, status 70."""
    res = _own(tmp_path, mode)
    lines = _lines(res)
    assert res.returncode == 70, res.stderr
    assert lines[0].startswith("Unexpected error in plugin 'own': this is a bug in the plugin, not in the script.")
    assert lines[1:3] == ["  at script.0 (own)", "Traceback (most recent call last):"]
    assert lines[-1] == last
    assert "Run failed" not in res.stderr and "--traceback for details" not in res.stderr


def test_p6_86_broad_error_of_the_session_under_a_plugin_is_a_failed_run(tmp_path: Path):
    """The same class raised by Autobot's session, which the plugin only called, is a failed run, reported
    with the hint; `--traceback` shows that the session raised it."""
    res = _own(tmp_path, "session")
    assert res.returncode == 3, res.stderr
    assert _lines(res) == [
        f"Run failed in {tmp_path / 'script.autobot.yaml'}: [Errno 9] Bad file descriptor",
        "  at script.0 (own)",
        "  (run with --traceback for details)",
    ]
    res = _own(tmp_path, "session", "--traceback")
    lines = _lines(res)
    assert res.returncode == 3
    assert any("autobot/session.py" in line and "in sendline" in line for line in lines)
    assert lines[-2:] == [
        f"Run failed in {tmp_path / 'script.autobot.yaml'}: [Errno 9] Bad file descriptor",
        "  at script.0 (own)",
    ]


def test_p6_86_timeout_raised_by_a_plugin_is_a_failed_run_without_the_hint(tmp_path: Path):
    """A plugin may raise the errors the session raises: a `TimeoutError` of its own is a failed run."""
    res = _own(tmp_path, "timeout")
    assert res.returncode == 3, res.stderr
    assert _lines(res) == [
        f"Run failed in {tmp_path / 'script.autobot.yaml'}: the plugin gave up after 1s",
        "  at script.0 (own)",
    ]


def test_p6_86_expected_failure_inside_a_plugin_is_still_expected(tmp_path: Path, buggy: list[Path]):
    """A plugin's step that fails as a built-in would (here an exit code, in steps the plugin builds) is a run
    failure, not a bug. Steps that aren't in the script are named after the step that runs them."""
    res = run_cli(make_doc([{"nest": "false"}]), tmp_path, pythonpath=buggy)
    assert res.returncode == 3, res.stderr
    assert _lines(res) == [
        f"Run failed in {tmp_path / 'script.autobot.yaml'}: command returned exit code 1",
        "  at script.0.nest.1 (cmd: false)",
        "  called from script.0 (nest)",
    ]


def test_p6_86_unexpected_error_under_a_plugin_names_the_plugin(tmp_path: Path, buggy: list[Path]):
    """An unexpected error in a built-in step that a plugin runs is the plugin's doing: here a `call` the
    plugin builds to a function that doesn't exist, which no script can contain. The report names the plugin."""
    res = run_cli(make_doc([{"nest": "call"}]), tmp_path, pythonpath=buggy)
    lines = _lines(res)
    assert res.returncode == 70, res.stderr
    assert lines[:4] == [
        "Unexpected error in plugin 'nest': this is a bug in the plugin, not in the script. "
        "Please report it to the plugin's author with the traceback below.",
        "  at script.0.nest.0 (call: no_such_function)",
        "  called from script.0 (nest)",
        "Traceback (most recent call last):",
    ]
    assert lines[-1] == "ValueError: undefined function: no_such_function"


def test_p6_86_bug_in_a_builtin_step_is_autobots(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
):
    """An unexpected error in a built-in step is reported as Autobot's, with the step, and the session is closed."""
    def execute(self: CmdExecutor, step: object, ctx: object, timeout: float) -> None:
        raise ZeroDivisionError("division by zero")

    monkeypatch.setattr(CmdExecutor, "execute", execute)
    path = _script(tmp_path, [{"block": {"name": "b", "script": [{"cmd": "echo hi"}]}}])
    code, _, err = _main(monkeypatch, capsys, str(path))
    lines = [line for line in err.splitlines() if not line.startswith(">> ")]
    assert code == 70
    assert lines[0].startswith("Unexpected error in Autobot: this is a bug, not a problem with the script.")
    assert lines[1:3] == ["  at script.0.block.script.0 (cmd: echo hi)", "Traceback (most recent call last):"]
    assert lines[-1] == "ZeroDivisionError: division by zero"


# -- --traceback -------------------------------------------------------------------


def test_p6_87_traceback_flag_shows_the_traceback_of_an_expected_failure(tmp_path: Path):
    """SPEC "CLI": with `--traceback` an expected failure prints its traceback, cause included, and then the
    same report and status as without it."""
    doc = make_doc([{"cmd": "echo {{ vars.nope }}"}], vars={})
    plain = run_cli(doc, tmp_path)
    for res in (run_cli(doc, tmp_path, "--traceback"), _flag_first(tmp_path)):
        lines = _lines(res)
        assert res.returncode == 3, res.stderr
        assert lines[0] == "Traceback (most recent call last):"
        assert "jinja2.exceptions.UndefinedError: 'dict object' has no attribute 'nope'" in lines
        assert "The above exception was the direct cause of the following exception:" in lines
        assert "autobot.types.ScriptError: template error: 'dict object' has no attribute 'nope'" in lines
        assert lines[-2:] == _lines(plain)
    assert "Traceback" not in plain.stderr


def _flag_first(tmp_path: Path) -> subprocess.CompletedProcess[str]:
    """`autobot --traceback <script>`: an option before the script, without the `run` subcommand."""
    return subprocess.run(
        [sys.executable, "-m", "autobot.cli", "--traceback", str(tmp_path / "script.autobot.yaml")],
        check=False, capture_output=True, text=True, timeout=120,
    )


def test_p6_87_traceback_flag_for_a_script_error_and_a_plugin_error(tmp_path: Path):
    """The flag also covers the load errors that come from an exception: `Script error` and `Plugin error`."""
    res = run_cli(make_doc([{"cmd": "true"}], env={"A": "{{ 1/0 }}"}), tmp_path, "--traceback")
    lines = _lines(res)
    assert res.returncode == 1
    assert lines[0] == "Traceback (most recent call last):" and "ZeroDivisionError: division by zero" in lines
    assert lines[-1] == (
        f"Script error in {tmp_path / 'script.autobot.yaml'}: env.A: template error: ZeroDivisionError: division by zero"
    )

    root = tmp_path / "broken"
    root.mkdir()
    plugin_dist(root, "broken", "raise ImportError('no such lib')\n", "X")
    for argv in (["run", str(tmp_path / "script.autobot.yaml"), "--traceback"], ["schema", "--traceback"]):
        res = subprocess.run(
            [sys.executable, "-m", "autobot.cli", *argv],
            check=False, capture_output=True, text=True, timeout=120,
            env={**os.environ, "PYTHONPATH": str(root)},
        )
        lines = _lines(res)
        assert (res.returncode, res.stdout) == (1, "")
        assert lines[0] == "Traceback (most recent call last):" and "ImportError: no such lib" in lines
        assert lines[-1].startswith("Plugin error: entry point 'broken' ")


def test_p6_87_traceback_flag_changes_nothing_for_a_run_that_succeeds(tmp_path: Path):
    res = run_cli(make_doc([{"cmd": "true"}]), tmp_path, "--traceback")
    assert res.returncode == 0, res.stderr
    assert _lines(res) == []


# -- Ctrl-C ------------------------------------------------------------------------


def _interrupt(tmp_path: Path, *cli_args: str) -> tuple[subprocess.CompletedProcess[str], Path, str]:
    """Run a script that blocks in `sleep 30` inside a block, and interrupt the CLI once it is there."""
    started, log = tmp_path / "started", tmp_path / "log"
    spawn = f"{BASH} -s autobot{os.getpid()}"  # unique, so a leaked shell can be found
    doc = make_doc(
        [
            {"cmd": f"touch {started}"},
            {
                "block": {
                    "name": "long",
                    "script": [{"cmd": "sleep 30", "timeout": "20s"}, {"cmd": f"echo never >> {log}"}],
                    "breakout": [{"control": "c"}, {"cmd": f"echo block >> {log}", "timeout": "5s"}],
                }
            },
        ],
        spawn=spawn,
        breakout=[{"cmd": f"echo attach >> {log}", "timeout": "5s"}],
    )
    path = tmp_path / "script.autobot.yaml"
    path.write_text(yaml.safe_dump(doc))
    echo, messages = tmp_path / "stdout", tmp_path / "stderr"
    with open(echo, "w") as out, open(messages, "w") as err:
        proc = subprocess.Popen([sys.executable, "-m", "autobot.cli", str(path), *cli_args], stdout=out, stderr=err)
        try:
            # the session echoes the command once the shell has it: from then on `sleep 30` is what runs
            deadline = time.monotonic() + 30
            while "\nsleep 30" not in echo.read_text().replace("PROMPT$ ", "\n") and time.monotonic() < deadline:
                time.sleep(0.05)
            assert started.exists() and "sleep 30" in echo.read_text(), "the script never got to `sleep 30`"
            proc.send_signal(signal.SIGINT)
            proc.wait(timeout=60)
        finally:
            proc.kill()
    return subprocess.CompletedProcess(proc.args, proc.returncode, echo.read_text(), messages.read_text()), log, spawn


def _pids(cmdline: str) -> list[str]:
    return subprocess.run(["pgrep", "-x", "-f", cmdline], capture_output=True, text=True, check=False).stdout.split()


def test_p6_88_ctrl_c_is_interrupted_and_ends_from_sigint_after_the_cleanup(tmp_path: Path):
    """SPEC "CLI": Ctrl-C ends the run with `Interrupted`, the step it stopped in and no traceback, and the
    process then ends from SIGINT itself (a shell reports 130), not with an exit status of its own.
    The block's breakout and `attach.breakout` run first, as after any failure, and the process is closed."""
    res, log, spawn = _interrupt(tmp_path)
    assert res.returncode == -signal.SIGINT, res.stderr
    assert "Traceback" not in res.stderr and "KeyboardInterrupt" not in res.stderr
    assert _lines(res) == ["Interrupted", "  at script.1.block.script.0 (cmd: sleep 30)"]
    assert log.read_text().split() == ["block", "attach"]
    progress = [line for line in res.stderr.splitlines() if line.startswith(">> ")]
    assert progress.index(">>   step interrupted") < progress.index(">> block breakout: long")
    assert progress.index(">> block breakout: long") < progress.index(">> breakout: detaching")
    assert _pids(spawn) == []


def test_p6_88_ctrl_c_with_traceback_flag(tmp_path: Path):
    """With `--traceback` the interrupt's traceback comes before the same two lines."""
    res, log, _ = _interrupt(tmp_path, "--traceback")
    lines = _lines(res)
    assert res.returncode == -signal.SIGINT, res.stderr
    assert lines[0] == "Traceback (most recent call last):" and "KeyboardInterrupt" in lines
    assert lines[-2:] == ["Interrupted", "  at script.1.block.script.0 (cmd: sleep 30)"]
    assert log.read_text().split() == ["block", "attach"]


def test_p6_88_ctrl_c_while_loading_is_interrupted_too(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
):
    """An interrupt before the run (here while the file is read) has no step to name."""
    def interrupt(*_args: object, **_kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.yaml, "load", interrupt)
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(cli.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    handler = signal.getsignal(signal.SIGINT)
    try:
        # the process signals itself; where that returns (here: replaced), it exits with status 130
        assert _main(monkeypatch, capsys, str(_script(tmp_path))) == (130, "", "Interrupted\n")
        assert killed == [(os.getpid(), signal.SIGINT)]
        assert signal.getsignal(signal.SIGINT) is signal.SIG_DFL  # nothing of Python's stands in the signal's way
    finally:
        signal.signal(signal.SIGINT, handler)


def test_p6_88_ctrl_c_stops_a_shell_loop_around_autobot(tmp_path: Path):
    """A shell that runs autobot in a loop stops at Ctrl-C, as it does for any command the signal kills: the
    next iteration, which would be the next device, doesn't start."""
    started = tmp_path / "started"
    path = _script(tmp_path, [{"cmd": f"echo x >> {started}"}, {"cmd": "sleep 30", "timeout": "20s"}])
    # a shell that isn't interactive drops the `PS1` it inherits, so the loop gives autobot the suite's
    autobot = f"PS1={shlex.quote(PS1)} {sys.executable} -W ignore -m autobot.cli {path}"
    loop = f"for i in 1 2; do {autobot}; echo ITER-$i-rc=$?; done; echo LOOP-DONE"
    env = {**os.environ, "NO_COLOR": "1"}
    child = pexpect.spawn("bash", ["--norc", "--noprofile", "-c", loop], env=env, encoding="utf-8", timeout=60)
    try:
        child.expect_exact(">> cmd: sleep 30")
        child.expect_exact("sleep 30")  # the session's echo: the command has reached the shell
        child.sendintr()
        seen = child.expect([pexpect.EOF, "ITER-1-rc", "LOOP-DONE"])
        output = child.before
    finally:
        child.close(force=True)
    assert seen == 0, "the loop went on after Ctrl-C"
    assert "Interrupted" in output and "Traceback" not in output
    assert started.read_text().split() == ["x"]  # one iteration


# -- stdout closed -----------------------------------------------------------------------


@pytest.mark.parametrize("flags", ["", "--traceback"])
def test_p6_91_closed_stdout_is_a_load_error(tmp_path: Path, flags: str):
    """SPEC "CLI": with stdout closed (`>&-`) there is nowhere for the session's output: the CLI says so and
    exits with status 1 before anything runs."""
    prepared = tmp_path / "prepared"
    path = _script(tmp_path, prepare=f"#!/bin/sh\ntouch {prepared}\n")
    res = subprocess.run(
        ["sh", "-c", f'exec "$0" -W ignore -m autobot.cli "$1" {flags} >&-', sys.executable, str(path)],
        check=False, capture_output=True, text=True, timeout=60,
    )
    assert res.returncode == 1, res.stderr
    assert res.stderr == (
        "Cannot write the session's output: stdout is closed (redirect it to /dev/null to discard the output)\n"
    )
    assert not prepared.exists()


# -- exit status ---------------------------------------------------------------------


def test_p6_89_exit_statuses_are_distinct(tmp_path: Path):
    """SPEC "CLI", exit status: 0 a completed run, 1 a script that can't be loaded, 2 a malformed command line,
    3 a failed run. (70 and 130: P6-84, P6-86 and P6-88.)"""
    assert (cli.EXIT_LOAD, cli.EXIT_RUN, cli.EXIT_UNEXPECTED, cli.EXIT_INTERRUPTED) == (1, 3, 70, 130)
    assert run_cli(make_doc([{"cmd": "true"}]), tmp_path).returncode == 0
    assert run_cli(make_doc([{"cmdd": "true"}]), tmp_path).returncode == 1
    assert run_cli(make_doc([{"cmd": "true"}]), tmp_path, "--no-such-option").returncode == 2
    assert run_cli(make_doc([{"cmd": "false"}]), tmp_path).returncode == 3
    # an ignored failure is no failure
    assert run_cli(make_doc([{"cmd": "false", "ignore_error": True}]), tmp_path).returncode == 0
