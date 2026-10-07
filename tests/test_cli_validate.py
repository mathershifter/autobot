"""`autobot validate`: SPEC "CLI", "Checking scripts with `validate`".

The command loads a script with the code `run` loads it with and runs nothing. Most tests here compare the
two commands on the same file, so a change to one that the other doesn't follow fails.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pexpect
import pytest
import yaml
from conftest import ROOT, make_doc, plugin_dist

from autobot import cli, prepare
from autobot.runner import Runner
from autobot.session import ANSI_ESCAPE_RE

ESC = "\x1b"
# the program's name is `autobot` when installed and `cli.py` under `python -m autobot.cli`
USAGE = re.compile(r"usage: \S+ validate \[-h\] \[-a KEY=VALUE\] \[-q\] \[--traceback\]\s+script \[script \.\.\.\]\n")
EXAMPLES = sorted((ROOT / "examples").glob("*.autobot.yaml"))
STEP = {"cmd": "true"}


def _env(**extra: str) -> dict[str, str]:
    """The suite's environment without the variables that decide the styles, on a terminal type with color."""
    env = {k: v for k, v in os.environ.items() if k not in ("NO_COLOR", "FORCE_COLOR", "TERM")}
    return {**env, "TERM": "xterm", **extra}


def _cli(*argv: Any, cwd: Path | None = None, **extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-W", "ignore", "-m", "autobot.cli", *map(str, argv)],
        check=False, capture_output=True, text=True, timeout=120, env=_env(**extra), cwd=cwd,
    )


def _main(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], *argv: Any) -> tuple[Any, str, str]:
    """`cli.main()` in this process: its exit status (0 when it returns), stdout and stderr."""
    monkeypatch.setattr(sys, "argv", ["autobot", *map(str, argv)])
    code: Any = 0
    try:
        cli.main()
    except SystemExit as e:
        code = e.code
    out = capsys.readouterr()
    return code, out.out, out.err


class Markers:
    """A directory for scripts whose `prepare` and `spawn` would each leave a file behind."""

    def __init__(self, root: Path):
        self.root = root
        self.prepared, self.spawned = root / "prepared", root / "spawned"

    def doc(self, script: list | None = None, **kw: Any) -> dict[str, Any]:
        kw.setdefault("prepare", f"#!/bin/sh\ntouch {self.prepared}\n")
        kw.setdefault("spawn", f"touch {self.spawned}")
        return make_doc(script or [STEP], **kw)

    def write(self, name: str, doc: Any = None, *, raw: str | bytes | None = None, **kw: Any) -> Path:
        path = self.root / name
        data = raw if raw is not None else yaml.safe_dump(self.doc(**kw) if doc is None else doc)
        path.write_bytes(data if isinstance(data, bytes) else data.encode())
        return path

    def none(self) -> bool:
        return not self.prepared.exists() and not self.spawned.exists()


@pytest.fixture
def m(tmp_path: Path) -> Markers:
    return Markers(tmp_path)


# -- a valid script, and nothing runs ---------------------------------------------------


def test_p6_93_valid_script_prints_one_line_and_exits_0(m: Markers, tmp_path: Path):
    """SPEC "Checking scripts with `validate`": `<path>: valid` on stderr, nothing on stdout, status 0."""
    path = m.write("ok.autobot.yaml")
    res = _cli("validate", path)
    assert (res.returncode, res.stdout, res.stderr) == (0, "", f"{path}: valid\n")


def test_p6_93_nothing_runs_and_no_file_is_created(m: Markers, tmp_path: Path):
    """Neither `prepare` nor `spawn` runs, and no temp file is made: `TMPDIR` and the script's directory are
    as they were."""
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    path = m.write("ok.autobot.yaml", env={"A": "{{ args.a }}", "B": "{{ env.A }}-{{ env.NOT_SET_YET }}"})
    before = sorted(p.name for p in tmp_path.iterdir())
    res = _cli("validate", path, "-a", "a=1", TMPDIR=str(tmp))
    assert (res.returncode, res.stdout, res.stderr) == (0, "", f"{path}: valid\n")
    assert m.none()
    assert list(tmp.iterdir()) == []
    assert sorted(p.name for p in tmp_path.iterdir()) == before
    # the same script does run them
    assert _cli(path, "-a", "a=1", TMPDIR=str(tmp)).returncode == 3
    assert m.prepared.exists()


def test_p6_93_validate_reaches_no_spawn_no_prepare_and_no_subprocess(
    m: Markers, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    """In process, with every way out replaced by one that fails: the command touches none of them."""

    def never(*a: Any, **kw: Any) -> Any:
        raise AssertionError("validate ran something")

    for owner, name in (
        (pexpect, "spawn"), (subprocess, "Popen"), (prepare, "run"), (Runner, "_run_prepare"), (Runner, "run"),
        (prepare.tempfile, "NamedTemporaryFile"), (prepare.tempfile, "TemporaryFile"), (os, "fork"), (os, "forkpty"),
    ):
        monkeypatch.setattr(owner, name, never)
    path = m.write("ok.autobot.yaml")
    assert _main(monkeypatch, capsys, "validate", path) == (0, "", f"{path}: valid\n")


def test_p6_93_closed_stdout_is_no_error(m: Markers):
    """Nothing is written to stdout, so `>&-` changes nothing; `run` refuses it (P6-91)."""
    path = m.write("ok.autobot.yaml")
    res = subprocess.run(
        ["sh", "-c", 'exec "$0" -W ignore -m autobot.cli validate "$1" >&-', sys.executable, str(path)],
        check=False, capture_output=True, text=True, timeout=60, env=_env(),
    )
    assert (res.returncode, res.stderr) == (0, f"{path}: valid\n")
    assert m.none()


# -- the load errors: the report is `run`'s ----------------------------------------------

SECRET = "hunter2-31337"
EACH = {"each": "vars.creds", "fields": [{"match": "p:", "field": "password"}]}
# name -> (how the file is made, the arguments of both commands, how the report starts; {path} is the file)
LOAD_ERRORS: dict[str, tuple[Callable[[Markers], Path], tuple[str, ...], str]] = {
    "missing": (lambda m: m.root / "nope.autobot.yaml", (), "Cannot read script {path}: No such file or directory\n"),
    "directory": (lambda m: m.root, (), "Cannot read script {path}: Is a directory\n"),
    "yaml-syntax": (lambda m: m.write("s", raw="a: [1\n"), (), "YAML error in {path}, line 2, column 1: "),
    "yaml-duplicate-key": (
        lambda m: m.write("s", raw=yaml.safe_dump(m.doc()) + "script: []\n"), (), "YAML error in {path}, line "
    ),
    "yaml-two-documents": (lambda m: m.write("s", raw="a: 1\n---\nb: 2\n"), (), "YAML error in {path}, line 2, "),
    "yaml-bad-date": (
        lambda m: m.write("s", raw="vars: {d: 2001-99-99}\n"), (),
        "YAML error in {path}, line 1, column 11: invalid timestamp (month must be in 1..12); "
        "quote the value if it is meant as text\n",
    ),
    "yaml-huge-int": (
        lambda m: m.write("s", raw="vars:\n  n: " + "9" * 5000 + "\n"), (),
        "YAML error in {path}, line 2, column 6: invalid int (Exceeds the limit (4300 digits) ",
    ),
    "yaml-too-deep": (
        lambda m: m.write("s", raw="[" * 3000), (), "YAML error in {path}: the document is nested too deeply\n"
    ),
    "undecodable": (lambda m: m.write("s", raw=b"a: \xff\xfe\n"), (), "YAML error in {path}, position 3: "),
    "empty-file": (lambda m: m.write("s", raw=""), (), "Validation errors:\n  (document): "),
    "list-document": (lambda m: m.write("s", raw="- a\n- b\n"), (), "Validation errors:\n  (document): "),
    "unknown-step": (lambda m: m.write("s", script=[{"cmdd": "true"}]), (), "Validation errors:\n  script.0: "),
    "several-validation-errors": (
        lambda m: m.write("s", script=[{"cmd": "true", "timout": 5}, {"sleep": "5 minutes"}, {"return": 0}]),
        (), "Validation errors:\n  script.0.cmd.timout: Extra inputs are not permitted [extra_forbidden]\n",
    ),
    "old-version": (lambda m: m.write("s", m.doc() | {"autobot": "2026-08"}), (), "Validation errors:\n  autobot: "),
    "undefined-call": (lambda m: m.write("s", script=[{"call": "nope"}]), (), "Validation errors:\n  script.0."),
    "invalid-regex": (lambda m: m.write("s", errors=["("]), (), "Validation errors:\n  errors.0: "),
    "after-invalid-regex": (
        lambda m: m.write("s", script=[{"cmd": "true", "after": "("}]), (), "Validation errors:\n  script.0."
    ),
    "masked-send": (
        lambda m: m.write("s", prompts=[{"name": "p", "expect": ["x"], "send": [SECRET]}]),
        (), "Validation errors:\n  prompts.0.send",
    ),
    "masked-line": (lambda m: m.write("s", script=[{"line": {SECRET: 1}}]), (), "Validation errors:\n  script.0."),
    "masked-env": (lambda m: m.write("s", env={"PW": [SECRET]}), (), "Validation errors:\n  env.PW: "),
    "arg-without-equals": (lambda m: m.write("s"), ("-a", "bogus"), "--arg requires KEY=VALUE format, got: bogus\n"),
    "arg-empty": (lambda m: m.write("s"), ("-a", "k=v", "--arg", ""), "--arg requires KEY=VALUE format, got: \n"),
    "validation-before-arg": (
        lambda m: m.write("s", script=[{"cmdd": "true"}]), ("-a", "bogus"), "Validation errors:\n  script.0: "
    ),
    "env-template-syntax": (
        lambda m: m.write("s", env={"A": "{{ nope( }}"}), (), "Script error in {path}: env.A: template error: unexpected '}'"
    ),
    "env-zero-division": (
        lambda m: m.write("s", env={"A": "{{ 1/0 }}"}), (),
        "Script error in {path}: env.A: template error: ZeroDivisionError: division by zero\n",
    ),
    "env-cycle": (
        lambda m: m.write("s", env={"A": "{{ env.B }}", "B": "{{ env.A }}"}), (),
        "Script error in {path}: env cycle: A -> B -> A\n",
    ),
    "env-unset-without-prepare": (
        lambda m: m.write("s", env={"A": "{{ env.AB_VALIDATE_NOT_SET }}"}, prepare=None), (),
        "Script error in {path}: env.A: template error: env has no key 'AB_VALIDATE_NOT_SET'\n",
    ),
    "env-missing-arg": (
        lambda m: m.write("s", env={"H": "{{ args.host }}"}), ("-a", "other=1"),
        "Script error in {path}: env.H: template error: args has no key 'host'; pass it with --arg host=VALUE\n",
    ),
    "send-template-syntax": (
        lambda m: m.write("s", prompts=[{"name": "p", "expect": ["x"], "send": "{{ x "}]), (),
        "Script error in {path}: prompt 'p': template error: unexpected end of template",
    ),
    "send-each-unresolved": (
        lambda m: m.write("s", vars={"creds": [{"username": "a"}]}, prompts=[{"name": "login", "send": EACH}]), (),
        "Script error in {path}: prompt 'login': sendEach 'vars.creds': item 0 has no field 'password'\n",
    ),
}


@pytest.mark.parametrize("flags", [(), ("--traceback",)], ids=["plain", "traceback"])
@pytest.mark.parametrize("case", LOAD_ERRORS)
def test_p6_94_invalid_script_gets_the_report_of_run_and_an_invalid_line(m: Markers, case: str, flags: tuple):
    """For every class of load error the report is `run`'s, character for character, then `<path>: invalid`;
    both commands exit with status 1 and neither runs anything."""
    make, args, head = LOAD_ERRORS[case]
    path = make(m)
    run = _cli("run", path, *args, *flags)
    val = _cli("validate", path, *args, *flags)
    assert (run.returncode, val.returncode) == (1, 1), (run.stderr, val.stderr)
    assert (run.stdout, val.stdout) == ("", "")
    head = head.replace("{path}", str(path))
    assert head in run.stderr
    if flags and head.startswith("Script error"):
        # the traceback of an error comes before it; its frames are each command's own
        for res in (run, val):
            assert res.stderr.startswith("Traceback (most recent call last):\n")
        assert val.stderr.splitlines()[-2:] == [run.stderr.splitlines()[-1], f"{path}: invalid"]
    else:
        assert run.stderr.startswith(head)
        assert val.stderr == run.stderr + f"{path}: invalid\n"
    assert SECRET not in val.stderr
    assert m.none()


def test_p6_94_each_error_is_one_line_with_its_control_characters_escaped(m: Markers):
    """The report keeps `run`'s rules for a value that would break a line or reach the terminal."""
    path = m.write("s", script=[{"cmd\x1b[31m\n": "x"}])
    val = _cli("validate", path)
    lines = val.stderr.splitlines()
    assert lines[0] == "Validation errors:" and lines[-1] == f"{path}: invalid" and len(lines) == 3
    assert ESC not in val.stderr and "\\x1b[31m\\n" in lines[1]


def test_p6_105_what_yaml_types_cannot_hold_is_a_yaml_error(m: Markers):
    """SPEC load errors: a date that isn't one, an integer too long to read and a document nested too deeply
    are errors of the document, reported like any YAML error, and the scripts after them are checked."""
    ok, ok2 = m.write("ok"), m.write("ok2", prepare=None)
    bad = {
        "date": ("vars: {d: 2001-99-99}\n", ", line 1, column 11: invalid timestamp (month must be in 1..12); quote"),
        "key": ("vars:\n  2001-13-01: 1\n", ", line 2, column 3: invalid timestamp (month must be in 1..12); quote"),
        "zone": ("vars:\n  - 2001-01-01 00:00:00 +99:99\n", ", line 2, column 5: invalid timestamp (offset must be"),
        "digits": ("vars:\n  n: " + "9" * 5000 + "\n", ", line 2, column 6: invalid int (Exceeds the limit (4300 digits)"),
        "hex": ("vars: {n: 0x_}\n", ", line 1, column 11: invalid int (invalid literal for int() with base 16: ''); quote"),
        "flow": ("[" * 3000, ": the document is nested too deeply"),
        "block": ("\n".join(" " * i + "a:" for i in range(3000)), ": the document is nested too deeply"),
    }
    for name, (raw, problem) in bad.items():
        path = m.write(name, raw=raw)
        val, run = _cli("validate", ok, path, ok2), _cli("run", path)
        head = f"YAML error in {path}{problem}"
        assert (val.returncode, run.returncode) == (1, 1), name
        lines = val.stderr.splitlines()
        assert len(lines) == 4 and lines[0] == f"{ok}: valid" and lines[2:] == [f"{path}: invalid", f"{ok2}: valid"], name
        assert lines[1].startswith(head) and run.stderr == lines[1] + "\n", name
        assert "Unexpected error" not in val.stderr + run.stderr and "Traceback" not in val.stderr + run.stderr
    assert m.none()


def test_p6_105_quoted_values_and_ordinary_nesting_still_load(m: Markers):
    """The same text as a string, a real date and a document nested a hundred levels deep are valid."""
    doc = yaml.safe_dump(m.doc())
    path = m.write("s", raw=doc + "vars:\n  d: '2001-99-99'\n  real: 2001-12-31\n  n: '" + "9" * 5000 + "'\n  deep: "
                   + "[" * 100 + "]" * 100 + "\n")
    res = _cli("validate", path)
    assert (res.returncode, res.stderr) == (0, f"{path}: valid\n")


# -- --arg ------------------------------------------------------------------------------


def test_p6_95_arg_is_used_by_load_time_templates(m: Markers):
    """`env` may read `{{ args.KEY }}`: the script is valid with the argument and invalid without, as in `run`."""
    path = m.write("s", env={"H": "{{ args.host }}", "U": "{{ env.H }}/{{ args.user | default('admin') }}"})
    for args in (("-a", "host=h"), ("--arg", "host=a=b", "-a", "user=u"), ("-a", "host=x", "-a", "host=")):
        res = _cli("validate", path, *args)
        assert (res.returncode, res.stderr) == (0, f"{path}: valid\n"), args
    missing = _cli("validate", path)
    assert missing.returncode == 1
    assert missing.stderr == (
        f"Script error in {path}: env.H: template error: args has no key 'host'; pass it with --arg host=VALUE\n"
        f"{path}: invalid\n"
    )
    assert missing.stderr == _cli("run", path).stderr + f"{path}: invalid\n"
    assert m.none()


def test_p6_95_arg_that_only_a_step_reads_is_not_needed(m: Markers):
    """A template of a step is rendered during the run: `validate` can't tell that the argument is missing."""
    path = m.write("s", script=[{"cmd": "echo {{ args.msg }}"}], prepare=None, spawn="true")
    assert _cli("validate", path).returncode == 0


def test_p6_95_args_apply_to_every_script(m: Markers):
    a = m.write("a", env={"H": "{{ args.host }}"})
    b = m.write("b", env={"H": "{{ args.host }}", "P": "{{ args.port }}"})
    res = _cli("validate", a, b, "-a", "host=h")
    assert res.returncode == 1
    assert res.stderr.splitlines()[0] == f"{a}: valid" and res.stderr.splitlines()[-1] == f"{b}: invalid"
    for argv in (("-a", "host=h", "--arg", "port=22", a, b), (a, b, "-a", "host=h", "-a", "port=22")):
        res = _cli("validate", *argv)
        assert (res.returncode, res.stderr) == (0, f"{a}: valid\n{b}: valid\n")
    # the scripts are given together: an option between them is a malformed command line
    between = _cli("validate", a, "-a", "host=h", b)
    assert between.returncode == 2 and between.stderr.startswith("usage: ") and "unrecognized arguments" in between.stderr


def test_p6_95_malformed_arg_is_reported_for_each_script_that_reaches_the_check(m: Markers):
    """The `--arg` check comes after validation, as in `run`: a script that fails earlier has its own error."""
    ok, typo, ok2 = m.write("a"), m.write("b", script=[{"cmdd": 1}]), m.write("c")
    res = _cli("validate", ok, typo, ok2, "-a", "bogus")
    bad = "--arg requires KEY=VALUE format, got: bogus"
    lines = res.stderr.splitlines()
    assert res.returncode == 1
    assert lines[:2] == [bad, f"{ok}: invalid"] and lines[-2:] == [bad, f"{ok2}: invalid"]
    assert lines[2] == "Validation errors:" and lines[-3] == f"{typo}: invalid" and bad not in lines[2:-2]


@pytest.mark.parametrize("argv", [("-a",), ("--arg",), ("-a", "k=v")], ids=["no-value", "no-value-long", "no-script"])
def test_p6_95_arg_usage_errors_exit_2(m: Markers, argv: tuple):
    res = _cli("validate", *argv)
    assert res.returncode == 2 and res.stdout == "" and USAGE.match(res.stderr)


# -- several scripts, --quiet ---------------------------------------------------------


def _mixed(m: Markers) -> tuple[list[Path], str]:
    ok = m.write("ok.autobot.yaml")
    typo = m.write("typo.autobot.yaml", script=[{"cmdd": "true"}])
    missing = m.root / "missing.autobot.yaml"
    cycle = m.write("cycle.autobot.yaml", env={"A": "{{ env.A }}"})
    ok2 = m.write("ok2.autobot.yaml", prepare=None)
    report = (
        f"{ok}: valid\n"
        "Validation errors:\n"
        "  script.0: cannot determine step type; expected one of cmd, sleep, call, block, line, return, control "
        "or a registered plugin step (got a mapping with the key cmdd) [invalid_step]\n"
        f"{typo}: invalid\n"
        f"Cannot read script {missing}: No such file or directory\n"
        f"{missing}: invalid\n"
        f"Script error in {cycle}: env cycle: A -> A\n"
        f"{cycle}: invalid\n"
        f"{ok2}: valid\n"
    )
    return [ok, typo, missing, cycle, ok2], report


def test_p6_96_each_script_is_checked_and_reported_in_order(m: Markers):
    """An invalid script doesn't stop the command: every script gets its lines, and the status is 1."""
    paths, report = _mixed(m)
    res = _cli("validate", *paths)
    assert (res.returncode, res.stdout, res.stderr) == (1, "", report)
    assert m.none()


def test_p6_96_all_valid_is_status_0_and_a_script_may_be_given_twice(m: Markers):
    a, b = m.write("a"), m.write("b")
    res = _cli("validate", a, b, a)
    assert (res.returncode, res.stdout, res.stderr) == (0, "", f"{a}: valid\n{b}: valid\n{a}: valid\n")


def test_p6_96_relative_path_is_printed_as_given(m: Markers):
    m.write("a.autobot.yaml")
    res = _cli("validate", "a.autobot.yaml", "./nope.yaml", cwd=m.root)
    assert res.stderr == (
        "a.autobot.yaml: valid\nCannot read script ./nope.yaml: No such file or directory\n./nope.yaml: invalid\n"
    )


@pytest.mark.parametrize("flag", ["-q", "--quiet"])
def test_p6_96_quiet_prints_only_the_invalid_scripts(m: Markers, flag: str):
    paths, report = _mixed(m)
    res = _cli("validate", flag, *paths)
    assert res.returncode == 1 and res.stdout == ""
    assert res.stderr == "".join(line for line in report.splitlines(keepends=True) if not line.endswith(": valid\n"))
    res = _cli("validate", paths[0], paths[-1], flag)
    assert (res.returncode, res.stdout, res.stderr) == (0, "", "")


# -- every error of the last stage ------------------------------------------------------

SEVERAL = {
    "env": {"A": "{{ env.B }}", "B": "{{ env.A }}", "C": "{{ 1/0 }}"},
    "vars": {"creds": [{"username": "a"}], "pins": "1234"},
    "prompts": [
        {"name": "sh", "expect": [r"\$ "], "return": True},
        {"name": "confirm", "expect": ["sure"], "send": "{{ x "},
        {"name": "ok", "expect": ["fine"], "send": "yes"},
        {"name": "login", "send": EACH},
        {"name": "pin", "expect": ["PIN:"], "send": {"each": "vars.pins"}},
    ],
}


def test_p6_97_env_error_and_each_prompts_error_are_all_reported(m: Markers):
    """SPEC: within the last stage `validate` goes on where `run` stops. One line for `env` (its first
    error), then one for each prompt that has an error, in the order of `prompts`; the first is `run`'s."""
    path = m.write("s", **SEVERAL)
    val, run = _cli("validate", path), _cli("run", path)
    head = f"Script error in {path}: "
    assert val.returncode == 1 and val.stdout == ""
    assert val.stderr.splitlines() == [
        head + "env cycle: A -> B -> A",
        head + "prompt 'confirm': template error: unexpected end of template, expected 'end of print statement'.",
        head + "prompt 'login': sendEach 'vars.creds': item 0 has no field 'password'",
        head + "prompt 'pin': sendEach 'vars.pins': 'vars.pins' is a string, not a list",
        f"{path}: invalid",
    ]
    assert (run.returncode, run.stderr) == (1, val.stderr.splitlines(keepends=True)[0])
    assert m.none()


def test_p6_97_prompt_errors_alone_start_with_the_one_run_reports(m: Markers):
    path = m.write("s", vars=SEVERAL["vars"], prompts=SEVERAL["prompts"])
    val, run = _cli("validate", path), _cli("run", path)
    assert len(val.stderr.splitlines()) == 4
    assert run.stderr == val.stderr.splitlines(keepends=True)[0]


def test_p6_108_send_syntax_error_names_its_prompt(m: Markers):
    """Two prompts with the same mistake give two lines that say which prompt each is about, like the error
    of a `sendEach` and the error of rendering a `send`."""
    prompts = [
        {"name": "sh", "expect": [r"\$ "], "return": True},
        {"name": "confirm", "expect": ["sure"], "send": "{{ x "},
        {"name": "really", "expect": ["really"], "send": "{{ x "},
        {"name": "if", "expect": ["if"], "send": "{% if %}"},
    ]
    path = m.write("s", prompts=prompts)
    val, run = _cli("validate", path), _cli("run", path)
    head = f"Script error in {path}: prompt "
    end = "template error: unexpected end of template, expected 'end of print statement'."
    assert val.stderr.splitlines() == [
        f"{head}'confirm': {end}",
        f"{head}'really': {end}",
        f"{head}'if': template error: Expected an expression, got 'end of statement block'",
        f"{path}: invalid",
    ]
    assert (run.returncode, run.stderr) == (1, f"{head}'confirm': {end}\n")
    assert m.none()


def test_p6_108_block_prompt_send_syntax_error_names_its_prompt():
    """The prompts of a block are loaded on entering it: the same message, as a failed run."""
    from autobot.types import ScriptError
    from conftest import SHELL_PROMPT, run_script

    block = {"name": "b", "prompts": [SHELL_PROMPT, {"name": "more", "expect": ["--More--"], "send": "{{ x "}], "script": [STEP]}
    with pytest.raises(ScriptError) as ei:
        run_script([{"block": block}])
    assert str(ei.value) == "prompt 'more': template error: unexpected end of template, expected 'end of print statement'."


def test_p6_97_runner_raises_the_first_error_unless_given_a_list():
    """The one constructor serves both: it raises the first script error, or collects them all."""
    from autobot.models import Config
    from autobot.types import ScriptError

    config = Config.model_validate(make_doc([STEP], **SEVERAL))
    with pytest.raises(ScriptError, match="env cycle: A -> B -> A"):
        Runner(config, {})
    problems: list[ScriptError] = []
    Runner(config, {}, problems)
    assert len(problems) == 4 and str(problems[0]) == "env cycle: A -> B -> A"
    problems = []
    Runner(Config.model_validate(make_doc([STEP])), {}, problems)
    assert problems == []


# -- --traceback, unexpected errors, interrupts, exit statuses ---------------------------


def test_p6_98_traceback_flag_precedes_each_script_error(m: Markers):
    path = m.write("s", **SEVERAL)
    res = _cli("validate", path, "--traceback")
    plain = _cli("validate", path)
    lines = res.stderr.splitlines()
    assert res.returncode == 1
    assert lines[0] == "Traceback (most recent call last):"
    # the report is the same, with a traceback before each of its errors
    report = plain.stderr.splitlines()
    assert [line for line in lines if line in report] == report
    for at in (i for i, line in enumerate(lines) if line.startswith("Script error in ")):
        assert lines[at - 1].startswith("autobot.types.") and "Error: " in lines[at - 1]
    ok = m.write("ok")
    assert _cli("validate", ok, "--traceback").stderr == f"{ok}: valid\n"


def test_p6_98_unexpected_error_keeps_its_traceback_and_ends_the_command(
    m: Markers, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    """A bug is no verdict on the script: status 70 with the traceback, and the scripts after it aren't checked."""
    first, boom, last = m.write("first"), m.write("boom"), m.write("last")
    real = cli._load

    def load(path: str) -> object:
        if path == str(boom):
            raise KeyError("x")
        return real(path)

    monkeypatch.setattr(cli, "_load", load)
    code, out, err = _main(monkeypatch, capsys, "validate", first, boom, last)
    lines = err.splitlines()
    assert (code, out) == (70, "")
    assert lines[0] == f"{first}: valid"
    assert lines[1].startswith("Unexpected error in Autobot: this is a bug, not a problem with the script.")
    assert lines[2] == f"  while checking {boom}"
    assert lines[3] == "Traceback (most recent call last):" and lines[-1] == "KeyError: 'x'"
    assert str(boom) + ": " not in err and str(last) not in err


VALIDATOR_PLUGIN = '''
from __future__ import annotations

import pydantic


class BoomStep(pydantic.BaseModel):
    boom: str

    @pydantic.field_validator("boom")
    @classmethod
    def check(cls, value):
        return {}[value]


class BoomExecutor:
    key = "boom"
    model = BoomStep

    def execute(self, step, ctx, timeout):
        pass
'''


def test_p6_106_unexpected_error_names_the_script_being_checked(m: Markers, tmp_path: Path):
    """A bug that ends `validate` is about one of several scripts: the report says which. Here a plugin
    model's validator raises `KeyError`. `run` has one script, and its report stays as it is."""
    root = tmp_path / "plugins"
    root.mkdir()
    plugin_dist(root, "boom", VALIDATOR_PLUGIN, "BoomExecutor")
    first, boom, last = m.write("first"), m.write("boom", script=[{"boom": "x"}]), m.write("last")
    val = _cli("validate", first, boom, last, PYTHONPATH=str(root))
    lines = val.stderr.splitlines()
    assert (val.returncode, val.stdout) == (70, "")
    assert lines[0] == f"{first}: valid"
    assert lines[1].startswith("Unexpected error in Autobot: this is a bug, not a problem with the script.")
    assert lines[2] == f"  while checking {boom}"
    assert lines[3] == "Traceback (most recent call last):" and lines[-1] == "KeyError: 'x'"
    assert str(last) not in val.stderr and ": invalid" not in val.stderr
    run = _cli("run", boom, PYTHONPATH=str(root))
    assert run.returncode == 70 and "while checking" not in run.stderr
    assert run.stderr.splitlines()[1] == "Traceback (most recent call last):"


def test_p6_98_interrupt_while_validating_is_interrupted(
    m: Markers, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    def interrupt(path: str) -> object:
        raise KeyboardInterrupt

    def ended() -> None:
        raise SystemExit(130)

    monkeypatch.setattr(cli, "_load", interrupt)
    monkeypatch.setattr(cli, "_interrupted", ended)
    assert _main(monkeypatch, capsys, "validate", m.write("s")) == (130, "", "Interrupted\n")


def test_p6_98_exit_statuses(m: Markers):
    """0 every script valid, 1 any invalid, 2 a malformed command line."""
    ok, bad = m.write("ok"), m.write("bad", script=[{"cmdd": 1}])
    assert _cli("validate", ok).returncode == 0
    assert _cli("validate", bad).returncode == 1
    assert _cli("validate", bad, ok).returncode == 1
    for argv in ((), ("--no-such-option", ok), ("-q",)):
        res = _cli("validate", *argv)
        assert res.returncode == 2 and res.stdout == "" and res.stderr.startswith("usage: "), argv


# -- plain and styled output ------------------------------------------------------------


@pytest.mark.parametrize("extra", [{}, {"NO_COLOR": "1"}, {"NO_COLOR": "1", "FORCE_COLOR": "1"}, {"TERM": "dumb"}])
def test_p6_99_no_escape_sequences_on_a_pipe_or_under_no_color(m: Markers, extra: dict):
    paths, report = _mixed(m)
    res = _cli("validate", *paths, **extra)
    assert (res.stdout, res.stderr) == ("", report)
    assert ESC not in res.stderr


def test_p6_99_styles_change_no_word(m: Markers):
    """SPEC "Output", styles: `valid` green and `invalid` bold red after a plain path; the report as in `run`."""
    paths, report = _mixed(m)
    res = _cli("validate", *paths, FORCE_COLOR="1")
    assert res.stdout == "" and ANSI_ESCAPE_RE.sub("", res.stderr) == report
    lines = res.stderr.splitlines()
    assert lines[0] == f"{paths[0]}: {ESC}[32mvalid{ESC}[0m"
    assert f"{paths[1]}: {ESC}[1;31minvalid{ESC}[0m" in lines
    styled_run = _cli("run", paths[1], FORCE_COLOR="1").stderr
    assert ESC in styled_run and styled_run in res.stderr


@pytest.mark.parametrize(("extra", "styled"), [({}, True), ({"NO_COLOR": "1"}, False), ({"TERM": "dumb"}, False)])
def test_p6_99_on_a_terminal(m: Markers, extra: dict, styled: bool):
    paths, report = _mixed(m)
    child = pexpect.spawn(
        sys.executable, ["-W", "ignore", "-m", "autobot.cli", "validate", *map(str, paths)],
        env=_env(**extra), encoding="utf-8", dimensions=(24, 400), timeout=60,
    )
    out = child.read().replace("\r\n", "\n")
    child.close()
    assert child.exitstatus == 1
    assert (ESC in out) == styled
    assert ANSI_ESCAPE_RE.sub("", out) == report


# -- plugins ------------------------------------------------------------------------------

PLUGIN = '''
from __future__ import annotations

import os
import pathlib

import pydantic

if os.environ.get("AB_VALIDATE_IMPORTED"):
    pathlib.Path(os.environ["AB_VALIDATE_IMPORTED"]).touch()


class EchoStep(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    echo: str


class EchoExecutor:
    key = "echo"
    model = EchoStep

    def execute(self, step, ctx, timeout):
        raise AssertionError("validate executed a step")
'''


@pytest.fixture
def plugins(tmp_path: Path) -> Path:
    root = tmp_path / "plugins"
    root.mkdir()
    return plugin_dist(root, "echo", PLUGIN, "EchoExecutor")


def test_p6_100_plugin_step_is_valid_only_with_the_plugin_installed(m: Markers, plugins: Path, tmp_path: Path):
    """Validation depends on the installed plugins: the same script is valid with the plugin and has an
    `invalid_step` without it. Loading the plugin imports its module."""
    path = m.write("s", script=[{"echo": "hi", "when": "{{ vars.x }}"}])
    imported = tmp_path / "imported"
    res = _cli("validate", path, PYTHONPATH=str(plugins), AB_VALIDATE_IMPORTED=str(imported))
    assert (res.returncode, res.stdout, res.stderr) == (0, "", f"{path}: valid\n")
    assert imported.exists()
    without = _cli("validate", path)
    assert without.returncode == 1
    assert "Validation errors:\n  script.0: cannot determine step type;" in without.stderr
    assert without.stderr.endswith(f" [invalid_step]\n{path}: invalid\n")
    assert m.none()


def test_p6_100_plugin_step_fields_are_checked(m: Markers, plugins: Path):
    path = m.write("s", script=[{"echo": "hi", "ech0": "x"}, {"echo": 5}])
    val = _cli("validate", path, PYTHONPATH=str(plugins))
    run = _cli("run", path, PYTHONPATH=str(plugins))
    assert val.returncode == 1 and " [extra_forbidden]\n" in val.stderr and "script.1" in val.stderr
    assert val.stderr == run.stderr + f"{path}: invalid\n"


@pytest.mark.parametrize("flags", [(), ("--traceback",)], ids=["plain", "traceback"])
def test_p6_100_broken_plugin_is_reported_alone_and_no_script_is_checked(m: Markers, tmp_path: Path, flags: tuple):
    """A `Plugin error` is about no script: it is reported once, as by `run`, with status 1 and no verdict."""
    root = tmp_path / "broken"
    root.mkdir()
    plugin_dist(root, "broken", "raise ImportError('no such lib')\n", "X")
    ok, bad = m.write("ok"), m.write("bad", script=[{"cmdd": 1}])
    val = _cli("validate", ok, bad, *flags, PYTHONPATH=str(root))
    run = _cli("run", ok, *flags, PYTHONPATH=str(root))
    assert (val.returncode, val.stdout) == (1, "")
    assert val.stderr.splitlines()[-1].startswith("Plugin error: entry point 'broken' ")
    assert val.stderr == run.stderr
    assert (val.stderr.count("\n") == 1) == (not flags)
    assert m.none()


# -- the examples -------------------------------------------------------------------------


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_p6_101_example_validates_through_the_command(path: Path):
    """Both examples read their `--arg` values in steps only, so they are valid without any."""
    res = _cli("validate", path)
    assert (res.returncode, res.stdout, res.stderr) == (0, "", f"{path}: valid\n")


def test_p6_101_examples_validate_together_by_relative_path():
    assert len(EXAMPLES) == 2
    names = [str(p.relative_to(ROOT)) for p in EXAMPLES]
    res = _cli("validate", *names, "-a", "dut=1", "-a", "hostname=sw1", cwd=ROOT)
    assert (res.returncode, res.stdout, res.stderr) == (0, "", "".join(f"{n}: valid\n" for n in names))


# -- validate says what run would do -------------------------------------------------------

# scripts that load, whatever they do once they run
LOADS: dict[str, tuple[Callable[[Markers], Path], tuple[str, ...]]] = {
    "plain": (lambda m: m.write("s"), ()),
    "no-prepare": (lambda m: m.write("s", prepare=None), ()),
    "env-with-arg": (lambda m: m.write("s", env={"H": "{{ args.host }}"}), ("-a", "host=h")),
    "env-default-waits-for-prepare": (lambda m: m.write("s", env={"A": "{{ env.AB_VALIDATE_NOT_SET }}"}), ()),
    "step-template-error": (lambda m: m.write("s", script=[{"cmd": "echo {{ nope( }}"}]), ()),
    "step-reads-a-missing-arg": (lambda m: m.write("s", script=[{"cmd": "echo {{ args.msg }}"}]), ()),
    "spawn-renders-to-nothing": (lambda m: m.write("s", m.doc() | {"attach": {"spawn": "{{ '' }}"}}), ()),
    "spawn-not-found": (lambda m: m.write("s", m.doc() | {"attach": {"spawn": "no-such-command-ab"}}), ()),
    "block-send-each-unresolved": (
        lambda m: m.write("s", script=[{"block": {"name": "b", "prompts": [{"name": "l", "send": EACH}], "script": [STEP]}}]),
        (),
    ),
    "send-each": (lambda m: m.write("s", **(SEVERAL | {"env": {}, "prompts": SEVERAL["prompts"][:1]})), ()),
}
CORPUS = {**{k: (*v, True) for k, v in LOADS.items()}, **{k: (v[0], v[1], False) for k, v in LOAD_ERRORS.items()}}


@pytest.mark.parametrize("case", CORPUS)
def test_p6_102_validate_exits_0_exactly_when_run_gets_past_loading(
    m: Markers, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], case: str
):
    """`run` is stopped at the point where it would start to run the script, so nothing is spawned: it gets
    there for the scripts `validate` finds valid, and exits with the load-error status for the others."""
    make, args, loads = CORPUS[case]
    path = make(m)
    reached: list[Runner] = []

    class Loaded(BaseException):  # not an `Exception`, which `main` would report as a bug
        pass

    def run(self: Runner) -> None:
        reached.append(self)
        raise Loaded

    monkeypatch.setattr(Runner, "run", run)
    val_code, val_out, val_err = _main(monkeypatch, capsys, "validate", path, *args)
    assert reached == [] and val_out == ""
    monkeypatch.setattr(sys, "argv", ["autobot", "run", str(path), *args])
    run_code: Any = None
    try:
        cli.main()
    except SystemExit as e:
        run_code = e.code
    except Loaded:
        pass
    capsys.readouterr()
    assert (val_code == 0) == bool(reached) == loads
    assert (val_code, run_code) == ((0, None) if loads else (1, 1))
    assert val_err.endswith(f"{path}: {'valid' if loads else 'invalid'}\n")
    assert m.none()


def test_p6_102_a_valid_script_can_still_fail_once_it_runs(m: Markers):
    """What `validate` can't catch is a failed run (status 3), never a load error (status 1)."""
    for case in ("step-template-error", "step-reads-a-missing-arg", "spawn-renders-to-nothing", "spawn-not-found"):
        make, args = LOADS[case]
        path = make(m)
        assert _cli("validate", path, *args).returncode == 0, case
        run = _cli("run", path, *args)
        assert run.returncode == 3 and f"Run failed in {path}: " in run.stderr, case


# -- nothing runs, whatever the script holds: every audited operation of the interpreter ------

# `cli.main()` under an audit hook, which sees what the interpreter does whichever function asked for it:
# a process, a socket, a file opened for writing or removed, a change to the environment. Status 99 and
# an `AUDIT` line for each if there was any; the command's own status otherwise.
AUDITED = r"""
import os
import sys

from autobot import cli

WRITE = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
EXACT = {
    "os.system", "os.putenv", "os.unsetenv", "os.remove", "os.rename", "os.mkdir", "os.rmdir", "os.chmod",
    "os.chown", "os.truncate", "os.symlink", "os.link", "os.kill", "os.killpg", "os.utime", "os.chdir",
    "os.startfile", "os.forkpty", "os.fork", "pty.spawn",
}
PREFIX = ("os.exec", "os.spawn", "os.posix_spawn", "subprocess.", "socket.", "shutil.", "tempfile.")
seen = []


def hook(event, args):
    if event == "open":
        if isinstance(args[2], int) and args[2] & WRITE:
            seen.append(f"open for writing {args[0]!r}")
    elif event in EXACT or event.startswith(PREFIX):
        seen.append(f"{event} {args!r}")


sys.addaudithook(hook)
sys.argv = ["autobot", *sys.argv[1:]]
code = 0
try:
    cli.main()
except SystemExit as e:
    code = e.code
for line in seen:
    sys.stderr.write(f"AUDIT {line}\n")
sys.exit(99 if seen else code)
"""

def _audited(*argv: Any, cwd: Path) -> subprocess.CompletedProcess[str]:
    # -B: an import during the command must not write a .pyc, which would be a file opened for writing
    return subprocess.run(
        [sys.executable, "-B", "-W", "ignore", "-c", AUDITED, *map(str, argv)],
        check=False, capture_output=True, text=True, timeout=120, env=_env(), cwd=cwd,
    )


@pytest.mark.parametrize("case", CORPUS)
def test_p6_104_validate_starts_no_process_and_writes_no_file(m: Markers, case: str):
    """SPEC "Checking scripts with `validate`": nothing runs. For every script of the corpus, valid or not:
    no exec, fork or spawn, no socket, no file
    opened for writing or removed, no change to the environment, by whichever function."""
    make, args, loads = CORPUS[case]
    path = make(m)
    before = sorted(p.name for p in m.root.iterdir())
    res = _audited("validate", path, *args, cwd=m.root)
    assert "AUDIT " not in res.stderr, res.stderr
    assert (res.returncode, res.stdout) == (0 if loads else 1, "")
    assert res.stderr.endswith(f"{path}: {'valid' if loads else 'invalid'}\n")
    assert "Traceback" not in res.stderr
    assert sorted(p.name for p in m.root.iterdir()) == before and m.none()


def test_p6_104_the_audit_sees_what_run_does(m: Markers):
    """The check itself: the same harness on `run`, which does run `prepare`, reports it."""
    path = m.write("s")
    res = _audited("run", path, cwd=m.root)
    assert res.returncode == 99
    assert "AUDIT subprocess.Popen" in res.stderr or "AUDIT os.posix_spawn" in res.stderr or "AUDIT os.fork" in res.stderr
    assert "AUDIT open for writing" in res.stderr or "AUDIT tempfile." in res.stderr


# -- a path is printed on one line, whatever the file is called -----------------------------

ODD = {"newline": ("\n", "\\n"), "cr": ("\r", "\\r"), "tab": ("\t", "\\t"), "esc": ("\x1b[31m", "\\x1b[31m")}


@pytest.mark.parametrize("char", ODD)
def test_p6_107_verdict_line_shows_the_path_escaped(m: Markers, char: str):
    """SPEC: `<path>` is the script as given, with the characters that aren't printable as their escapes."""
    raw, shown = ODD[char]
    name = f"a{raw}b.yaml"
    m.write(name)
    m.write("bad" + name, script=[{"cmdd": 1}])
    res = _cli("validate", name, "bad" + name, cwd=m.root)
    lines = res.stderr.split("\n")
    assert res.returncode == 1 and res.stdout == ""
    assert lines[0] == f"a{shown}b.yaml: valid" and lines[-2:] == [f"bada{shown}b.yaml: invalid", ""]
    assert len(lines) == 5 and not set(res.stderr) & {"\r", "\t", ESC}
    styled = _cli("validate", name, cwd=m.root, FORCE_COLOR="1")
    assert styled.stderr == f"a{shown}b.yaml: {ESC}[32mvalid{ESC}[0m\n"


def test_p6_107_a_path_cannot_forge_another_scripts_line(m: Markers):
    """A name with a line break in it stays one line: no line of the output is `ok.yaml: valid`."""
    m.write("nl\nok.yaml: valid")
    res = _cli("validate", "nl\nok.yaml: valid", "zz\nok.yaml: valid\nq", cwd=m.root)
    assert res.returncode == 1
    assert res.stderr.split("\n") == [
        "nl\\nok.yaml: valid: valid",
        "Cannot read script zz\\nok.yaml: valid\\nq: No such file or directory",
        "zz\\nok.yaml: valid\\nq: invalid",
        "",
    ]


# how each report that names the script starts, with {path} for the name as it is shown
NAMED = {
    "cannot-read": (None, "Cannot read script {path}: No such file or directory", 1),
    "yaml": ("a: [1\n", "YAML error in {path}, line 2, column 1: ", 1),
    "yaml-reader": (b"a: \xff\xfe\n", "YAML error in {path}, position 3: ", 1),
    "yaml-value": ("vars: {d: 2001-99-99}\n", "YAML error in {path}, line 1, column 11: invalid timestamp", 1),
    "yaml-deep": ("[" * 3000, "YAML error in {path}: the document is nested too deeply", 1),
    "script-error": ({"env": {"A": "{{ env.A }}"}}, "Script error in {path}: env cycle: A -> A", 1),
    "run-failed": ({"spawn": "no-such-command-ab", "prepare": None}, "Run failed in {path}: ", 3),
}


@pytest.mark.parametrize("char", ODD)
@pytest.mark.parametrize("case", NAMED)
def test_p6_107_every_report_shows_the_path_escaped(m: Markers, case: str, char: str):
    """`Cannot read script`, `YAML error in`, `Script error in` and `Run failed in` name the script the same
    way, in `run` and in `validate`."""
    content, head, status = NAMED[case]
    raw, shown = ODD[char]
    name = f"s{raw}x.yaml"
    if isinstance(content, dict):
        m.write(name, **content)
    elif content is not None:
        m.write(name, raw=content)
    head = head.replace("{path}", f"s{shown}x.yaml")
    run = _cli("run", name, cwd=m.root)
    assert run.returncode == status
    assert head in run.stderr.split("\n") or any(line.startswith(head) for line in run.stderr.split("\n")), run.stderr
    assert not set(run.stderr) & {"\r", "\t", ESC} and f"s{raw}x.yaml" not in run.stderr
    if status == 1:
        val = _cli("validate", name, cwd=m.root)
        assert val.stderr == run.stderr + f"s{shown}x.yaml: invalid\n"


def test_p6_107_unexpected_error_shows_the_path_escaped(
    m: Markers, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    def load(path: str) -> object:
        raise KeyError("x")

    monkeypatch.setattr(cli, "_load", load)
    code, _, err = _main(monkeypatch, capsys, "validate", "a\nb\x1b[31m.yaml")
    assert code == 70 and err.splitlines()[1] == "  while checking a\\nb\\x1b[31m.yaml" and ESC not in err


def test_p6_107_a_name_that_is_not_utf8_is_shown_escaped(m: Markers):
    """A byte of the name that isn't UTF-8 is a lone surrogate in the argument: it is written as its escape."""
    name = b"s\xffx.yaml"
    Path(os.fsdecode(os.path.join(os.fsencode(m.root), name))).write_text(yaml.safe_dump(m.doc()))
    res = subprocess.run(
        [sys.executable, "-W", "ignore", "-m", "autobot.cli", "validate", name, b"nope\xfe.yaml"],
        check=False, capture_output=True, text=True, timeout=60, env=_env(), cwd=m.root,
    )
    assert res.returncode == 1
    assert res.stderr.splitlines() == [
        "s\\udcffx.yaml: valid",
        "Cannot read script nope\\udcfe.yaml: No such file or directory",
        "nope\\udcfe.yaml: invalid",
    ]


# -- the command line -----------------------------------------------------------------------


def test_p6_103_help_describes_the_command():
    top = _cli("--help")
    assert top.returncode == 0 and top.stderr == ""
    assert "{run,validate,schema}" in top.stdout
    assert "    validate            Check autobot scripts without running them" in top.stdout
    for flag in ("-h", "--help"):
        res = _cli("validate", flag)
        text = " ".join(res.stdout.split())
        assert res.returncode == 0 and res.stderr == ""
        assert USAGE.match(res.stdout)
        assert "attach.prepare is not run and no session is spawned" in text
        assert "Exit with status 1 if any script is invalid" in text
        for option in ("-a KEY=VALUE, --arg KEY=VALUE", "-q, --quiet", "--traceback"):
            assert option in text


def test_p6_103_run_stays_the_default_and_takes_one_script(m: Markers):
    """`validate` is a subcommand name like `run` and `schema`: a file of that name needs a path. `run`
    still takes exactly one script."""
    path = m.write("validate", script=[{"cmdd": 1}])
    bare = _cli("validate", cwd=m.root)
    assert bare.returncode == 2 and USAGE.match(bare.stderr)
    as_path = _cli("./validate", cwd=m.root)
    assert as_path.returncode == 1 and as_path.stderr.startswith("Validation errors:\n")
    assert "invalid\n" not in as_path.stderr.replace("[invalid_step]\n", "")
    assert _cli("validate", "validate", cwd=m.root).stderr.endswith("validate: invalid\n")
    two = _cli("run", path, path)
    assert two.returncode == 2 and two.stderr.startswith("usage: ")
