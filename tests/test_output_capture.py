from __future__ import annotations

import os
from typing import Any

import pytest

from autobot.models import Config
from autobot.runner import Runner
from autobot.session import strip_echo

BASH = "bash --norc --noprofile -i"


def run(
    script: list[dict[str, Any]],
    errors: list[str] | None = None,
    spawn: str = BASH,
) -> dict[str, Any]:
    for step in script:
        step.setdefault("timeout", "10s")
    cfg = Config.model_validate(
        {
            "autobot": "2026-08",
            "prompts": [{"name": "sh", "expect": [r"PROMPT\$ "]}],
            "errors": errors or [],
            "attach": {
                "spawn": spawn,
                "timeout": 10,
                "env": {"TERM": "dumb", "PS1": "PROMPT$ ", "PATH": os.environ["PATH"]},
                "script": [{"return": 1}],
            },
            "script": script,
        }
    )
    Runner(cfg, {}).run()
    return cfg.vars


def test_newlines_preserved():
    out = run([{"cmd": "printf 'one\\ntwo\\n'", "register": "out"}])
    assert out["out"] == "one\ntwo"


def test_echo_removed():
    out = run([{"cmd": "echo hello", "register": "out"}])
    assert out["out"] == "hello"


def test_long_command_echo_removed():
    cmd = "echo " + " ".join(f"word{i}" for i in range(40))
    out = run([{"cmd": cmd, "register": "out"}])
    assert out["out"] == " ".join(f"word{i}" for i in range(40))


@pytest.mark.parametrize(
    "cmd", ["false-cmd-xyz; echo stopped", "false-cmd-xyz; echo stopped # is-system-running"]
)
def test_assert_does_not_match_echo(cmd: str):
    with pytest.raises(RuntimeError, match="assertion failed"):
        run([{"cmd": cmd, "assert": "running"}])


def test_assert_matches_output():
    out = run([{"cmd": "echo running", "assert": "running", "register": "out"}])
    assert out["out"] == "running"


def test_errors_ignore_echo():
    out = run(
        [{"cmd": "printf 'x\\n' # % comment", "register": "out"}],
        errors=["% .*"],
    )
    assert out["out"] == "x"


def test_errors_fire_on_output():
    with pytest.raises(RuntimeError, match="command error: % bad input"):
        run([{"cmd": "echo '% bad input'"}], errors=["% .*"])


def test_list_registers_all_lines():
    out = run([{"cmd": ["echo a", "echo b", "echo c"], "register": "out"}])
    assert out["out"] == "a\nb\nc"


def test_multiline_string_registers_all_lines():
    out = run([{"cmd": "echo a\n# ! a comment\necho b\n", "register": "out"}], errors=["! .*"])
    assert out["out"] == "a\nb"


def test_list_error_on_middle_line_raises():
    with pytest.raises(RuntimeError, match="command error: % middle"):
        run(
            [{"cmd": ["echo a", "echo '% middle'", "touch /nonexistent-autobot-marker"]}],
            errors=["% .*"],
        )


def test_list_error_on_middle_line_stops_remaining_lines():
    out = run(
        [
            {"cmd": ["echo a", "echo '% middle'", "echo c"], "ignore_error": True, "register": "out"},
            {"cmd": "echo after", "register": "after"},
        ],
        errors=["% .*"],
    )
    assert out["out"] == "a\n% middle"
    assert out["after"] == "after"


def test_ignored_error_registers_output():
    out = run(
        [
            {"cmd": "echo before; echo '% oops'", "ignore_error": True, "register": "out"},
            {"cmd": "echo next", "register": "next"},
        ],
        errors=["% .*"],
    )
    assert out["out"] == "before\n% oops"
    assert out["next"] == "next"


def test_ignored_rc_failure_registers_output():
    out = run([{"cmd": "echo partial; false", "ignore_error": True, "register": "out"}])
    assert out["out"] == "partial"


def test_echo_off_keeps_output():
    out = run(
        [
            {"cmd": "stty -echo"},
            {"cmd": "printf 'real\\n'", "register": "out"},
        ],
        spawn="bash --norc --noprofile --noediting -i",
    )
    assert out["out"] == "real"


def test_embedded_script_register():
    out = run([{"cmd": "#!/bin/sh\necho one\necho two\n", "register": "out"}])
    assert out["out"] == "one\ntwo"


def test_no_trailing_newline_output_dropped():
    out = run([{"cmd": "printf 'x\\ny'", "register": "out"}])
    assert out["out"] == "x"


@pytest.mark.parametrize(
    ("text", "sent", "expected"),
    [
        ("echo hi\nhi\n", "echo hi", "hi\n"),
        ("echo a very\r long line\nout\n", "echo a very long line", "out\n"),
        ("echo a\n b\nout\n", "echo a b", "out\n"),
        ("\r<9 word10\nout\n", "echo word9 word10", "out\n"),
        ("real\n", "printf 'real\\n'", "real\n"),
        ("hi\n", "", "hi\n"),
    ],
)
def test_strip_echo(text: str, sent: str, expected: str):
    assert strip_echo(text, sent) == expected
