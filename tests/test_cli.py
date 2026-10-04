"""P6-21..40, P6-45, P6-59: CLI argument handling and error reporting (SPEC.md:348-355)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import FakeDevice, make_doc, run_cli

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
        ("autobot: 2026-10\nscript:\n  - cmd: a\n   - cmd: b\n", "line 4, column 4",
         "expected <block end>, but found '<block sequence start>'", "  while parsing a block collection (line 3, column 3)"),
        ('autobot: 2026-10\nscript:\n  - cmd: "a\n', "line 4, column 1",
         "found unexpected end of stream", "  while scanning a quoted scalar (line 3, column 10)"),
        ("autobot: 2026-10\nscript:\n\t- cmd: a\n", "line 3, column 1",
         "found character '\\t' that cannot start any token", "  while scanning for the next token"),
        ("autobot: 2026-10\n---\nautobot: 2026-10\n", "line 2, column 1",
         "but found another document", "  expected a single document in the stream (line 1, column 1)"),
        ("a: &x 1\nb: *y\n", "line 2, column 4", "found undefined alias 'y'", None),
        ("autobot: 2026-10\nscript: !!python/object:os.system x\n", "line 2, column 9",
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
        (b"autobot: 2026-10\nscript:\n  - cmd: echo \xff\xfe hi\n", "position 39: invalid start byte (utf-8 byte #xff)"),
        (b"autobot: 2026-10\nx: \xc3\n", "position 20: invalid continuation byte (utf-8 byte #xc3)"),
        (b"autobot: 2026-10\nscript:\n  - cmd: echo \x07 hi\n", "position 39: special characters are not allowed (character #x07)"),
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

    res = run_cli(None, tmp_path, raw='autobot: 2026-10\nscript: "[/x]"\n')
    assert _load_error(res) == "Validation errors:"
    assert '"input": "[/x]"' in res.stderr

    missing = tmp_path / "[/d]" / "[b]x.yaml"
    assert _load_error(_cli(str(missing))) == f"Cannot read script {missing}: No such file or directory"


def test_p6_30_cli_load_does_not_swallow_keyboard_interrupt(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Only file and YAML errors are caught while loading."""
    def interrupt(*_args: object, **_kwargs: object) -> None:
        raise KeyboardInterrupt

    path = tmp_path / "script.autobot.yaml"
    path.write_text("autobot: 2026-10\n")
    monkeypatch.setattr(cli.yaml, "load", interrupt)
    with pytest.raises(KeyboardInterrupt):
        cli._load(str(path))


# -- Runner construction: env and prompt send templates -----------------------


@pytest.mark.parametrize(
    ("env", "prompts", "message"),
    [
        ({"A": "{{ nope( }}"}, None, "template error: unexpected '}', expected ')'"),
        ({"A": "{{ 1/0 }}"}, None, "template error: ZeroDivisionError: division by zero"),
        ({"A": "{{ env.B }}", "B": "{{ 'a' + 1 }}"}, None, 'template error: TypeError: can only concatenate str (not "int") to str'),
        (None, [{"name": "p", "expect": ["x"], "send": "{{ x "}], "template error: unexpected end of template, expected 'end of print statement'."),
        ({"A": "{{ env.A }}x"}, None, "env cycle: A -> A"),
        ({"A": "{{ env.B }}", "B": "{{ env.A }}"}, None, "env cycle: A -> B -> A"),
        ({"A": "{{ env.B }}", "B": "{{ env.C }}", "C": "{{ env.A }}"}, None, "env cycle: A -> B -> C -> A"),
    ],
    ids=[
        "env-template", "env-zero-division", "env-nested-type-error",
        "send-template", "env-cycle-self", "env-cycle-mutual", "env-cycle-three",
    ],
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
        f"autobot: 2026-10\nattach:\n  spawn: touch {tmp_path / 'spawned'}\n"
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


# -- sendEach resolution at load (SPEC "sendEach") -------------------------------

EACH_VARS = {
    "creds": [{"username": "admin", "password": "pw1"}, {"username": "admin"}],
    "site": {"creds": [{"username": "u", "password": "p", "note": "unused"}]},
    "s": "hello",
    "n": 5,
    "nul": None,
    "lst": ["x"],
    "pins": ["1111", 2.5, True],
    "mixed": ["ok", {"username": "a"}],
    "nulls": [{"username": "a", "password": None}],
    "pairs": [{"username": "a", "password": "b"}],
}
UP = [{"match": "login:", "field": "username"}, {"match": "Password:", "field": "password"}]
SH = {"name": "sh", "expect": [r"PROMPT\$ "], "return": True}


def _each_doc(tmp_path: Path, send: dict, **kw: Any) -> dict:
    login = {"name": "login", "send": send}
    if "fields" not in send:
        login["expect"] = ["login:", "Password:"]
    return make_doc([GOT], vars=EACH_VARS, prompts=[SH, login], **kw)


def _marked_each_doc(tmp_path: Path, send: dict) -> dict:
    """`prepare` and `spawn` touch marker files, so a run that got that far is visible."""
    prepared, spawned = tmp_path / "prepared", tmp_path / "spawned"
    return _each_doc(tmp_path, send, prepare=f"#!/bin/sh\ntouch {prepared}\n", spawn=f"touch {spawned}")


@pytest.mark.parametrize(
    ("each", "fields", "problem"),
    [
        ("vars.nope", None, "no key 'nope' in 'vars'"),
        ("vars.site.nope", UP, "no key 'nope' in 'vars.site'"),
        ("vars.site.x.creds", UP, "no key 'x' in 'vars.site'"),
        ("vars.s.x", None, "'vars.s' is a string, not a mapping"),
        ("vars.lst.0", None, "'vars.lst' is a list, not a mapping"),
        ("vars.s", None, "'vars.s' is a string, not a list"),
        ("vars.n", None, "'vars.n' is a number, not a list"),
        ("vars.nul", None, "'vars.nul' is null, not a list"),
        ("vars.site", UP, "'vars.site' is a mapping, not a list"),
        ("vars.creds", UP, "item 1 has no field 'password'"),
        ("vars.mixed", UP[:1], "item 0 is a string, not a mapping"),
        ("vars.nulls", UP, "item 0 field 'password' is null, not a string, number or boolean"),
        ("vars.pairs", None, "item 0 is a mapping; without fields each item must be a string, number or boolean"),
    ],
    ids=[
        "missing-key", "missing-nested-key", "missing-middle-key", "through-string", "through-list",
        "string-not-list", "number-not-list", "null-not-list", "mapping-not-list", "missing-field",
        "item-not-mapping", "null-field", "mapping-item-without-fields",
    ],
)
def test_p6_38_cli_send_each_error_is_clean_error(tmp_path: Path, each: str, fields: list[dict] | None, problem: str):
    """SPEC sendEach: a top-level collection that can't be resolved is a `Script error`, before prepare/spawn."""
    send = {"each": each} if fields is None else {"each": each, "fields": fields}
    res = run_cli(_marked_each_doc(tmp_path, send), tmp_path)
    path = tmp_path / "script.autobot.yaml"
    assert _load_error(res) == f"Script error in {path}: prompt 'login': sendEach '{each}': {problem}"
    assert not (tmp_path / "prepared").exists()
    assert not (tmp_path / "spawned").exists()


@pytest.mark.parametrize("each", ["env.HOME", "args.pw", "session.before", "creds", "vars", "vars..creds", "vars.creds."])
def test_p6_39_cli_send_each_path_outside_vars_is_validation_error(tmp_path: Path, each: str):
    """SPEC sendEach: `each` is `vars` followed by keys; anything else fails validation."""
    res = run_cli(_marked_each_doc(tmp_path, {"each": each}), tmp_path, "--arg", "pw=secret")
    assert _load_error(res) == "Validation errors:"
    assert f"each must be a path under vars, like vars.creds, got: {each}" in res.stderr
    assert not (tmp_path / "prepared").exists()
    assert not (tmp_path / "spawned").exists()


@pytest.mark.parametrize(
    "send",
    [{"each": "vars.site.creds", "fields": UP}, {"each": "vars.pins"}, {"each": "vars.lst"}],
    ids=["nested-with-fields", "scalars-without-fields", "without-fields"],
)
def test_p6_40_cli_valid_send_each_runs(tmp_path: Path, send: dict):
    """SPEC sendEach: a resolvable collection loads, and the script runs."""
    res = run_cli(_each_doc(tmp_path, send), tmp_path, "--arg", "msg=hi")
    assert res.returncode == 0, res.stderr
    assert "got-2-hi" in res.stdout


def test_p6_46_cli_old_fields_list_is_validation_error_with_hint(tmp_path: Path):
    """SPEC "Migrating from 2026-08": `fields: [username, password]` fails validation and names the new form."""
    doc = _marked_each_doc(tmp_path, {"each": "vars.pairs", "fields": ["username", "password"]})
    doc["prompts"][1]["expect"] = [["login:", "Password:"]]
    res = run_cli(doc, tmp_path)
    assert _load_error(res) == "Validation errors:"
    errs = json.loads(res.stderr.split("Validation errors:\n", 1)[1])
    assert [(e["loc"], e["type"]) for e in errs] == [
        (["prompts", 1, "send", "fields", 0], "fields_entry"),
        (["prompts", 1, "send", "fields", 1], "fields_entry"),
    ]
    assert errs[1]["msg"] == (
        "since 2026-10 a fields entry pairs a regex with a field: "
        'write {match: <regex>, field: password} (see "Migrating from 2026-08" in SPEC.md)'
    )
    assert not (tmp_path / "prepared").exists()
    assert not (tmp_path / "spawned").exists()


def test_p6_47_cli_old_version_is_validation_error_with_hint(tmp_path: Path):
    """SPEC "Top-level fields": `autobot: 2026-08` fails validation and points to the migration section."""
    doc = _marked_each_doc(tmp_path, {"each": "vars.pins"})
    doc["autobot"] = "2026-08"
    res = run_cli(doc, tmp_path)
    assert _load_error(res) == "Validation errors:"
    [err] = json.loads(res.stderr.split("Validation errors:\n", 1)[1])
    assert (err["loc"], err["type"]) == (["autobot"], "unsupported_version")
    assert err["msg"] == (
        'autobot 2026-08 is no longer supported; use 2026-10 (see "Migrating from 2026-08" in SPEC.md)'
    )
    assert not (tmp_path / "prepared").exists()
    assert not (tmp_path / "spawned").exists()


# -- simple prompts: one send string (SPEC "prompts") ----------------------------

CONFIRM = {"name": "confirm", "expect": [r"continue\?", r"are you sure\?"], "send": "yes"}
SEND_TYPE_MSG = (
    "send must be a string; quote it, e.g. send: 'yes' or send: '1234' "
    "(unquoted, YAML reads yes, no, on, off, true, false and numbers as booleans or numbers)"
)
SEQUENCE_HINT = 'use sendEach with fields'


def _confirm_text(tmp_path: Path, fake_device: FakeDevice) -> tuple[str, Path]:
    spawn, log = fake_device("--order", "none", "--ask", "continue?", "--ask", "'are you sure?'")
    text = yaml.safe_dump(make_doc([{"cmd": "echo done"}], prompts=[SH, CONFIRM], spawn=spawn))
    assert "send: 'yes'" in text
    return text, log


def test_p6_54_cli_quoted_send_yes_is_sent(tmp_path: Path, fake_device: FakeDevice):
    """SPEC prompts: `send: 'yes'` answers any match, whichever alternative matches, with `yes`."""
    text, log = _confirm_text(tmp_path, fake_device)
    res = run_cli(None, tmp_path, raw=text)
    assert res.returncode == 0, res.stderr
    # after the questions the device runs a shell, which logs nothing
    assert FakeDevice.read(log) == ["ASK=yes", "ASK=yes"]
    assert "\ndone" in res.stdout


@pytest.mark.parametrize("value", ["yes", "no", "on", "true", "1234"])
def test_p6_55_cli_unquoted_send_scalar_is_validation_error_with_hint(
    tmp_path: Path, fake_device: FakeDevice, value: str
):
    """SPEC prompts: unquoted `send: yes` is a boolean in YAML, so it fails validation with a quoting hint."""
    text, log = _confirm_text(tmp_path, fake_device)
    res = run_cli(None, tmp_path, raw=text.replace("send: 'yes'", f"send: {value}"))
    assert _load_error(res) == "Validation errors:"
    [err] = json.loads(res.stderr.split("Validation errors:\n", 1)[1])
    assert (err["loc"], err["type"], err["msg"]) == (["prompts", 1, "send"], "send_type", SEND_TYPE_MSG)
    assert FakeDevice.read(log) == []


@pytest.mark.parametrize(
    ("prompt", "loc", "type_"),
    [
        ({"name": "login", "expect": ["login:", "Password:"], "send": ["admin", "pw"]}, ["prompts", 1, "send"], "send_list"),
        ({"name": "login", "expect": ["login:"], "send": [["admin", "pw1"], ["admin", "pw2"]]},
         ["prompts", 1, "send"], "send_list"),
        ({"name": "login", "expect": [["login:", "Password:"]], "send": "admin"}, ["prompts", 1, "expect", 0], "grouped_expect"),
    ],
    ids=["flat-send", "list-of-lists-send", "grouped-expect"],
)
def test_p6_56_cli_removed_prompt_forms_are_validation_errors_with_hint(
    tmp_path: Path, prompt: dict[str, Any], loc: list[Any], type_: str
):
    """SPEC "Migrating from 2026-08": a send list and a grouped expect fail validation and point to sendEach."""
    doc = _marked_each_doc(tmp_path, {"each": "vars.pins"})
    doc["prompts"][1] = prompt
    res = run_cli(doc, tmp_path)
    assert _load_error(res) == "Validation errors:"
    [err] = json.loads(res.stderr.split("Validation errors:\n", 1)[1])
    assert (err["loc"], err["type"]) == (loc, type_)
    assert SEQUENCE_HINT in err["msg"]
    assert 'see "Migrating from 2026-08" in SPEC.md' in err["msg"]
    assert not (tmp_path / "prepared").exists()
    assert not (tmp_path / "spawned").exists()


# -- empty expect and match (SPEC "prompts", "sendEach") -------------------------


EMPTY_REGEX_MSG = "a regex must not be empty: an empty regex matches at once, before any output"


@pytest.mark.parametrize(
    ("tail", "loc", "type_", "msg"),
    [
        ("  - name: p\n    expect: []\n    send: 'y'\n", ["prompts", 0, "expect"], "too_short",
         "expect must be a regex or a non-empty list of regexes"),
        ("  - name: p\n    expect: ''\n    return: true\n", ["prompts", 0, "expect"], "string_too_short",
         EMPTY_REGEX_MSG),
        ("  - name: p\n    send: {each: vars.c, fields: [{match: ['login:', ''], field: u}]}\n",
         ["prompts", 0, "send", "fields", 0, "match", 1], "string_too_short", EMPTY_REGEX_MSG),
    ],
    ids=["expect-empty-list", "expect-empty-string", "match-empty-entry"],
)
def test_p6_59_cli_empty_expect_or_match_is_validation_error(
    tmp_path: Path, tail: str, loc: list[Any], type_: str, msg: str
):
    """SPEC prompts, sendEach: an empty expect or regex fails validation; rc 1, before prepare/spawn."""
    res = run_cli(None, tmp_path, raw=_marked_head(tmp_path) + "prompts:\n" + tail + "script:\n  - cmd: echo one\n")
    assert _load_error(res) == "Validation errors:"
    [err] = json.loads(res.stderr.split("Validation errors:\n", 1)[1])
    assert (err["loc"], err["type"], err["msg"]) == (loc, type_, msg)
    assert not (tmp_path / "prepared").exists()
    assert not (tmp_path / "spawned").exists()


# -- empty values ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("tail", "loc", "msg"),
    [
        ("script:\n  - cmd: echo one\n    after:\n", ["script", 0, "cmd", "after"], "null (an empty value)"),
        ("  env:\nscript: []\n", ["attach", "env"], "null (an empty value)"),
        ("script:\n  - line: x\n    timeout: ~\n", ["script", 0, "line", "timeout"], "Extra inputs"),
        ("script:\n  - sleep:\n", ["script", 0, "sleep", "sleep"], "invalid duration: null"),
    ],
    ids=["step-after", "attach-env", "unsupported-prop", "sleep"],
)
def test_p6_45_cli_empty_value_is_validation_error(tmp_path: Path, tail: str, loc: list[Any], msg: str):
    """SPEC "YAML Script Structure": an empty value is invalid (omit the key); rc 1, before prepare/spawn."""
    res = run_cli(None, tmp_path, raw=_marked_head(tmp_path) + tail)
    assert _load_error(res) == "Validation errors:"
    [err] = json.loads(res.stderr.split("Validation errors:\n", 1)[1])
    assert err["loc"] == loc
    assert msg in err["msg"]
    assert not (tmp_path / "prepared").exists()
    assert not (tmp_path / "spawned").exists()
