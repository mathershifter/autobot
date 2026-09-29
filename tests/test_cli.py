"""P6-21..30: CLI argument handling and error reporting (SPEC.md:348-355)."""

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
    def interrupt(_stream: object) -> None:
        raise KeyboardInterrupt

    path = tmp_path / "script.autobot.yaml"
    path.write_text("autobot: 2026-08\n")
    monkeypatch.setattr(cli.yaml, "safe_load", interrupt)
    with pytest.raises(KeyboardInterrupt):
        cli._load(str(path))
