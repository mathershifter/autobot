"""P6-19/20: every example validates against the models and the schema, and its shell prompt regexes read whole prompts."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import ROOT, SHELL_ENV, run_vars

from autobot.cli import UniqueKeyLoader
from autobot.models import Config
from autobot.types import ANSI_ESCAPE_RE

EXAMPLES = sorted((ROOT / "examples").glob("*.yaml"))


def load(path: Path) -> Any:
    return yaml.load(path.read_text(), Loader=UniqueKeyLoader)


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_p6_19_examples_validate_model(path: Path):
    """SPEC.md:12: examples are valid scripts."""
    Config.model_validate(load(path))


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_p6_20_examples_validate_schema(schema_validator: Any, path: Path):
    """SPEC.md:12: examples are valid against the JSON schema."""
    errors = [e.message for e in schema_validator.iter_errors(load(path))]
    assert errors == []


# -- the examples' shell prompt regexes ---------------------------------------

PROMPT_SAMPLES = {
    "sonic.autobot.yaml": [
        "admin@sonic:~$ ",
        "admin@sonic:/var/log$ ",
        "admin@sonic-bmc.lab:~$ ",
        "root@sonic:~# ",
    ],
    "eos-bootstrap.autobot.yaml": [
        "switch>",
        "switch> ",
        "switch# ",
        "switch(config)# ",
        "switch(config-if-Et1/1)# ",
        "switch(config-s-sess)(s1)# ",
        "[admin@switch ~]$ ",
        "bash-4.2# ",
        "-bash-4.2$ ",
    ],
}


def shell_prompts(name: str) -> list[dict[str, Any]]:
    doc = load(ROOT / "examples" / name)
    return [p for p in doc["prompts"] if p.get("return") or "send" not in p]


def shell_regexes(name: str) -> list[str]:
    return [e for p in shell_prompts(name) for e in ([p["expect"]] if isinstance(p["expect"], str) else p["expect"])]


def read_prompt(patterns: list[str], text: str) -> tuple[str, str] | None:
    """Read `text` as get_prompt does; return (match, leftover) of the first shell prompt."""
    # pexpect compiles string patterns with re.DOTALL and searches the unread output
    regexes = [re.compile(r"\r\n"), ANSI_ESCAPE_RE, *(re.compile(p, re.DOTALL) for p in patterns)]
    while True:
        found = [(m.start(), i, m) for i, r in enumerate(regexes) if (m := r.search(text))]
        if not found:
            return None
        _, i, m = min(found, key=lambda f: f[:2])
        if i > 1:
            return m.group(0), text[m.end() :]
        text = text[m.end() :]


def test_prompt_samples_cover_examples():
    assert sorted(PROMPT_SAMPLES) == sorted(p.name for p in EXAMPLES)


@pytest.mark.parametrize("before", ["", "hello\r\n", "\x1b[?2004l\rhello\r\n"], ids=["bare", "output", "ansi"])
@pytest.mark.parametrize(
    ("name", "prompt"),
    [(n, p) for n, ps in PROMPT_SAMPLES.items() for p in ps],
    ids=repr,
)
def test_example_prompt_regex_ends_at_prompt_char(name: str, prompt: str, before: str):
    """The match includes the prompt character and leaves nothing of the prompt unread."""
    found = read_prompt(shell_regexes(name), before + prompt)
    assert found is not None, f"no shell prompt regex of {name} matches {prompt!r}"
    match, leftover = found
    assert leftover == ""
    assert match.rstrip()[-1] in "$#>"
    assert prompt.endswith(match)


@pytest.mark.parametrize("name", sorted(PROMPT_SAMPLES))
def test_example_prompt_regex_skips_command_lines(name: str):
    """A line that only starts like a prompt (an echoed command) isn't a prompt."""
    for prompt in PROMPT_SAMPLES[name]:
        assert read_prompt(shell_regexes(name), f"{prompt}echo hi\r\n") is None


@pytest.mark.parametrize(
    ("name", "ps1"),
    [
        ("sonic.autobot.yaml", "admin@sonic:~$ "),
        ("sonic.autobot.yaml", "root@sonic:~# "),
        ("eos-bootstrap.autobot.yaml", "switch(config)# "),
    ],
)
def test_example_prompt_regex_registers_clean_output(name: str, ps1: str):
    """Against a real bash with that PS1, register sees only the command's output."""
    out = run_vars(
        [{"cmd": "echo hello", "register": "a"}, {"cmd": "echo again {{ vars.a }}", "register": "b"}],
        prompts=shell_prompts(name),
        attach_env={**SHELL_ENV, "PS1": ps1},
    )
    assert out["a"] == "hello"
    assert out["b"] == "again hello"
