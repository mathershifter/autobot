"""P6-21..32: CLI argument handling and error reporting (SPEC.md:348-355)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from conftest import make_doc, run_cli

from autobot import cli

# the echoed command shows `$((1+1))` literally; only the output has `got-2-`
GOT = {"cmd": 'echo "got-$((1+1))-{{ args.msg }}"'}


def test_p6_21_cli_arg_passed_to_templates(tmp_path: Path):
    """SPEC.md:351-355: --arg KEY=VALUE is available as args.KEY."""
    res = run_cli(make_doc([GOT]), tmp_path, "--arg", "msg=hi")
    assert res.returncode == 0, res.stderr
    assert "got-2-hi" in res.stdout


def test_p6_22_cli_arg_value_may_contain_equals(tmp_path: Path):
    """SPEC.md:355: only the first '=' separates key and value."""
    res = run_cli(make_doc([GOT]), tmp_path, "--arg", "msg=a=b")
    assert res.returncode == 0, res.stderr
    assert "got-2-a=b" in res.stdout


def test_p6_23_cli_arg_without_equals_is_clean_error(tmp_path: Path):
    """SPEC.md:355: a malformed --arg is reported without a traceback."""
    res = run_cli(make_doc([GOT]), tmp_path, "--arg", "bad")
    assert res.returncode == 1
    assert "--arg requires KEY=VALUE" in res.stderr
    assert "Traceback" not in res.stderr


@pytest.mark.parametrize("raw", ["", "- a\n- b\n"], ids=["empty-file", "top-level-list"])
def test_p6_24_cli_non_mapping_yaml_is_clean_error(tmp_path: Path, raw: str):
    """SPEC.md:12, 18: a non-mapping document is a validation error, not a crash."""
    res = run_cli(None, tmp_path, raw=raw)
    assert "Traceback" not in res.stderr
    assert res.returncode == 1
    assert "alidation" in res.stderr


# -- #19: load errors are reported without a traceback -------------------------


def _cli(*argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "autobot.cli", *argv],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _first_line(stderr: str) -> str:
    return next(line for line in stderr.splitlines() if "RuntimeWarning" not in line)


def _load_error(res: subprocess.CompletedProcess[str]) -> str:
    assert res.returncode == 1, res.stderr
    assert "Traceback" not in res.stderr
    assert res.stdout == ""
    return _first_line(res.stderr)


@pytest.mark.parametrize("form", [(), ("run",)], ids=["default", "run"])
@pytest.mark.parametrize(
    ("case", "reason"),
    [("missing", "No such file or directory"), ("directory", "Is a directory"), ("no-permission", "Permission denied")],
)
def test_p6_25_cli_unreadable_script_is_clean_error(tmp_path: Path, form: tuple[str, ...], case: str, reason: str):
    """SPEC.md CLI: an unreadable script is `Cannot read script <path>: <reason>`, rc 1."""
    path = tmp_path / "script.autobot.yaml"
    if case == "directory":
        path.mkdir()
    elif case == "no-permission":
        if os.geteuid() == 0:
            pytest.fail("run the suite as a non-root user; root ignores file modes")
        path.write_text(yaml.safe_dump(make_doc([GOT])))
        path.chmod(0)
    res = _cli(*form, str(path))
    assert _load_error(res) == f"Cannot read script {path}: {reason}"


@pytest.mark.parametrize(
    ("raw", "where", "problem", "context"),
    [
        ("autobot: 2026-08\nscript:\n  - cmd: a\n   - cmd: b\n", "line 4, column 4",
         "expected <block end>, but found '<block sequence start>'", "  while parsing a block collection (line 3, column 3)"),
        ('autobot: 2026-08\nscript:\n  - cmd: "a\n', "line 4, column 1",
         "found unexpected end of stream", "  while scanning a quoted scalar (line 3, column 10)"),
        ("autobot: 2026-08\nscript:\n\t- cmd: a\n", "line 3, column 1",
         "found character '\\t' that cannot start any token", "  while scanning for the next token"),
        ("autobot: 2026-08\n---\nautobot: 2026-08\n", "line 2, column 1",
         "but found another document", "  expected a single document in the stream (line 1, column 1)"),
        ("a: &x 1\nb: *y\n", "line 2, column 4", "found undefined alias 'y'", None),
        ("autobot: 2026-08\nscript: !!python/object:os.system x\n", "line 2, column 9",
         "could not determine a constructor for the tag 'tag:yaml.org,2002:python/object:os.system'", None),
    ],
    ids=["bad-indent", "unclosed-quote", "tab-indent", "two-documents", "undefined-alias", "python-tag"],
)
def test_p6_26_cli_yaml_syntax_error_is_clean_error(
    tmp_path: Path, raw: str, where: str, problem: str, context: str | None
):
    """SPEC.md CLI: invalid YAML is `YAML error in <path>, line L, column C: <problem>`, rc 1."""
    res = run_cli(None, tmp_path, raw=raw)
    path = tmp_path / "script.autobot.yaml"
    assert _load_error(res) == f"YAML error in {path}, {where}: {problem}"
    lines = [line for line in res.stderr.splitlines() if "RuntimeWarning" not in line]
    assert lines[1:] == ([context] if context else [])


@pytest.mark.parametrize(
    ("data", "detail"),
    [
        (b"autobot: 2026-08\nscript:\n  - cmd: echo \xff\xfe hi\n", "position 39: invalid start byte (utf-8 byte #xff)"),
        (b"autobot: 2026-08\nx: \xc3\n", "position 20: invalid continuation byte (utf-8 byte #xc3)"),
        (b"autobot: 2026-08\nscript:\n  - cmd: echo \x07 hi\n", "position 39: special characters are not allowed (character #x07)"),
    ],
    ids=["latin1-bytes", "truncated-utf8", "control-char"],
)
def test_p6_27_cli_undecodable_script_is_clean_error(tmp_path: Path, data: bytes, detail: str):
    """SPEC.md CLI: bytes that aren't valid UTF-8 are a `YAML error`, rc 1."""
    path = tmp_path / "script.autobot.yaml"
    path.write_bytes(data)
    assert _load_error(_cli(str(path))) == f"YAML error in {path}, {detail}"


def test_p6_28_cli_utf16_with_bom_loads(tmp_path: Path):
    """SPEC.md CLI: UTF-16 with a byte order mark is accepted."""
    path = tmp_path / "script.autobot.yaml"
    path.write_bytes(yaml.safe_dump(make_doc([GOT])).encode("utf-16"))
    res = _cli(str(path), "--arg", "msg=hi")
    assert res.returncode == 0, res.stderr
    assert "got-2-hi" in res.stdout


def test_p6_29_cli_errors_print_markup_like_text_verbatim(tmp_path: Path):
    """Error text is printed literally: `[/x]` is not rich markup and doesn't crash."""
    res = run_cli(make_doc([GOT]), tmp_path, "--arg", "[/x]")
    assert _load_error(res) == "--arg requires KEY=VALUE format, got: [/x]"

    res = run_cli(None, tmp_path, raw='autobot: 2026-08\nscript: "[/x]"\n')
    assert _load_error(res) == "Validation errors:"
    assert '"input": "[/x]"' in res.stderr

    missing = tmp_path / "[/d]" / "[b]x.yaml"
    assert _load_error(_cli(str(missing))) == f"Cannot read script {missing}: No such file or directory"


def test_p6_30_cli_load_does_not_swallow_keyboard_interrupt(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Only file and YAML errors are caught while loading."""
    def interrupt(*_args: object, **_kwargs: object) -> None:
        raise KeyboardInterrupt

    path = tmp_path / "script.autobot.yaml"
    path.write_text("autobot: 2026-08\n")
    monkeypatch.setattr(cli.yaml, "load", interrupt)
    with pytest.raises(KeyboardInterrupt):
        cli._load(str(path))


# -- Runner construction: env and prompt send templates -----------------------


@pytest.mark.parametrize(
    ("env", "prompts", "message"),
    [
        ({"A": "{{ nope( }}"}, None, "template error: unexpected '}', expected ')'"),
        (None, [{"name": "p", "expect": ["x"], "send": ["{{ x "]}], "template error: unexpected end of template, expected 'end of print statement'."),
        ({"A": "{{ env.A }}x"}, None, "env nesting too deep (>10 iterations), unresolved: ['A']"),
    ],
    ids=["env-template", "send-template", "env-too-deep"],
)
def test_p6_31_cli_script_error_at_runner_load_is_clean_error(
    tmp_path: Path, env: dict[str, str] | None, prompts: list | None, message: str
):
    """SPEC.md CLI: `env` and prompt `send` errors are `Script error in <path>: ...`, rc 1, before prepare/spawn."""
    prepared, spawned = tmp_path / "prepared", tmp_path / "spawned"
    kwargs = {"prompts": prompts} if prompts is not None else {}
    doc = make_doc([GOT], env=env, prepare=f"#!/bin/sh\ntouch {prepared}\n", spawn=f"touch {spawned}", **kwargs)
    res = run_cli(doc, tmp_path)
    path = tmp_path / "script.autobot.yaml"
    assert _load_error(res) == f"Script error in {path}: {message}"
    assert not prepared.exists()
    assert not spawned.exists()


def test_p6_32_cli_runtime_errors_are_not_caught_as_load_errors(tmp_path: Path):
    """Only building the Runner is guarded: a template error in a step still fails at run time, after attach."""
    res = run_cli(make_doc([{"cmd": "echo {{ nope( }}"}]), tmp_path)
    assert res.returncode == 1
    assert ">> attach: " in res.stderr
    assert "Script error" not in res.stderr
    assert "ValueError: template error: " in res.stderr


# -- duplicate mapping keys ------------------------------------------------------


def _marked_head(tmp_path: Path) -> str:
    """Lines 1-4 of a script whose `prepare` and `spawn` touch marker files."""
    return (
        f"autobot: 2026-08\nattach:\n  spawn: touch {tmp_path / 'spawned'}\n"
        f'  prepare: "#!/bin/sh\\ntouch {tmp_path / "prepared"}\\n"\n'
    )


@pytest.mark.parametrize(
    ("tail", "where", "key", "first"),
    [
        ("script:\n  - cmd: echo one\nscript:\n  - cmd: echo two\n", "line 7, column 1", "script", "line 5, column 1"),
        ("script:\n  - cmd: echo one\n    when: 'false'\n    cmd: echo two\n", "line 8, column 5", "cmd", "line 6, column 5"),
        ("env:\n  HOST: a\n  HOST: b\nscript:\n  - cmd: echo one\n", "line 7, column 3", "HOST", "line 6, column 3"),
    ],
    ids=["top-level", "step", "env"],
)
def test_p6_33_cli_duplicate_key_is_clean_error(tmp_path: Path, tail: str, where: str, key: str, first: str):
    """SPEC.md CLI: a key given twice in one mapping is a `YAML error` naming both places, rc 1, before prepare."""
    res = run_cli(None, tmp_path, raw=_marked_head(tmp_path) + tail)
    path = tmp_path / "script.autobot.yaml"
    assert _load_error(res) == f"YAML error in {path}, {where}: found duplicate key '{key}'"
    lines = [line for line in res.stderr.splitlines() if "RuntimeWarning" not in line]
    assert lines[1:] == [f"  first defined ({first})"]
    assert not (tmp_path / "prepared").exists()
    assert not (tmp_path / "spawned").exists()


@pytest.mark.parametrize(
    ("raw", "key", "line"),
    [
        ("attach:\n  spawn: a\n  timeout: 5\n  spawn: b\n", "spawn", 4),
        ("prompts:\n  - name: a\n    expect: [x]\n    name: b\n", "name", 4),
        ("fn:\n  f:\n    script:\n      - {cmd: a, cmd: b}\n", "cmd", 4),
        ("vars:\n  a:\n    b:\n      c: 1\n      c: 2\n", "c", 5),
        ("vars:\n  m:\n    <<: {x: 1, x: 2}\n", "x", 3),
        ("vars:\n  a: &a {x: 1}\n  b: &b {y: 1}\n  m:\n    <<: *a\n    <<: *b\n", "<<", 6),
        ('vars:\n  m: {x: 1, "x": 2}\n', "x", 2),
    ],
    ids=["attach", "prompt", "fn-step", "nested-vars", "merge-source", "two-merge-keys", "quoted-same-key"],
)
def test_p6_34_duplicate_key_rejected_at_any_level(raw: str, key: str, line: int):
    """SPEC.md: keys are unique in every mapping, including inside `<<` merge sources; one `<<` per mapping."""
    with pytest.raises(yaml.constructor.ConstructorError) as exc:
        yaml.load(raw, Loader=cli.UniqueKeyLoader)
    assert exc.value.problem == f"found duplicate key '{key}'"
    assert exc.value.problem_mark.line + 1 == line
    assert exc.value.context == "first defined"


def test_p6_35_cli_merge_key_override_is_accepted(tmp_path: Path):
    """SPEC.md: a key set next to `<<` overrides the merged value; it isn't a duplicate."""
    doc = make_doc([{"cmd": 'echo "over-{{ vars.over.a }}-{{ vars.over.b }}"'}])
    raw = yaml.safe_dump(doc) + "vars:\n  base: &base {a: 1, b: 2}\n  over:\n    <<: *base\n    b: 3\n"
    res = run_cli(None, tmp_path, raw=raw)
    assert res.returncode == 0, res.stderr
    assert "over-1-3" in res.stdout


@pytest.mark.parametrize(
    "raw",
    [
        "a: &a {x: 1, y: 1}\nb: {<<: *a, x: 2}\n",
        "a: &a {x: 1}\nb: &b {x: 2, z: 0}\nc: {<<: [*a, *b], z: 9}\n",
        "a: &a {x: 1}\nc: {<<: &b {<<: *a, x: 2}}\nd: *b\n",
        "m: {=: a}\n",
    ],
    ids=["override", "merge-list", "merged-then-aliased", "value-key"],
)
def test_p6_36_merge_keys_load_like_safe_load(raw: str):
    """Keys without duplicates load exactly as `yaml.safe_load` loads them, `<<` merges included."""
    assert yaml.load(raw, Loader=cli.UniqueKeyLoader) == yaml.safe_load(raw)


def test_p6_37_cli_keys_that_only_look_alike_are_accepted(tmp_path: Path):
    """SPEC.md: keys are compared as loaded, so `1` (an integer) and `"1"` (a string) are different keys."""
    doc = make_doc([{"cmd": "echo \"keys-{{ vars.m | length }}-{{ vars.m[1] }}-{{ vars.m['1'] }}\""}])
    raw = yaml.safe_dump(doc) + 'vars:\n  m: {1: int, "1": str}\n'
    res = run_cli(None, tmp_path, raw=raw)
    assert res.returncode == 0, res.stderr
    assert "keys-2-int-str" in res.stdout
