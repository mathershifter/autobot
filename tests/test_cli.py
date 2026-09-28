"""P6-21..24: CLI argument handling and error reporting (SPEC.md:348-355)."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import make_doc, run_cli

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
