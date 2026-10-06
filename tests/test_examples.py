"""P6-19/20, P6-74, P6-76: every example validates against the models and the schema, and its shell prompt regexes read whole
prompts and nothing else."""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import ROOT, run_vars

from autobot.cli import UniqueKeyLoader
from autobot.models import Config
from autobot.session import STRAY_RE, PromptHandler, Session
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
        "root@sonic:/# ",
        "admin@sonic:~$",
        "admin@str-7260cx3-acs-1:/usr/share/sonic$ ",
        "root@bmc_1:~# ",
    ],
    "eos-bootstrap.autobot.yaml": [
        "switch>",
        "switch> ",
        "switch# ",
        "switch(config)# ",
        "switch(config-if-Et1/1)# ",
        "switch(config-s-sess)(s1)# ",
        "[admin@switch ~]$ ",
        "[admin@switch ~]# ",
        "[root@lab-sw1.example.net /var/log]# ",
        "bash-4.2# ",
        "bash-5.1# ",
        "bash-5.1$ ",
        "-bash-4.2$ ",
        "-bash-4.2# ",
        "bash-5.12$ ",
        "bash$ ",
    ],
}

# P6-74: text that ends a read with a prompt character and isn't a prompt
NOT_PROMPTS = {
    "sonic.autobot.yaml": [
        "disk usage 5 > ",
        "price in $",
        "! comment #",
        "<html>",
        "$ ",
        "# ",
        "admin@sonic",
        # P6-76: output with user@host: in it that ends a read with '$' or '#'; the unanchored regex took these
        "scp admin@host:/x $",
        "scp admin@host:/x $ ",
        "rsync -a . admin@10.0.0.1:/srv/ # ",
        "Warning: Permanently added to admin@host: known hosts #",
        "see admin@sonic:~$ ",
        "mail from: root@sonic: cost in $",
        # and what only looks like the prompt
        "admin@sonic:~",
        "admin@sonic ~$ ",
        "@sonic:~$ ",
        "admin@:~$ ",
        "admin@sonic:~> ",
    ],
    "eos-bootstrap.autobot.yaml": [
        # the four of the audit: every part of the old bash regex was optional
        "disk usage 5 > ",
        "price in $",
        "! comment #",
        "<html>",
        # the bare prompt characters it matched as well
        "$ ",
        "# ",
        "> ",
        "$",
        "#",
        ">",
        " # ",
        "-$ ",
        # things that only look like the bash prompts (a line of the hostname characters and '>' or '#'
        # is an EOS prompt, so 'bash> ' or '/bin/bash# ' on a line of its own still is one)
        "-4.2$ ",
        "bash-5.1 $ ",
        "/bin/bash$ ",
        "bash-5.1 # ",
        "which bash$ ",
        "see [admin@switch ~]$ ",
        "[not a prompt]$ ",
        "[admin@switch]$ ",
        "Total: 5 items> ",
        "<rpc-reply>",
        "x = a>",
        "100% #",
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
    # then the stray CR, NUL and BEL at the start of the unread output, which lose a tie to the prompts
    regexes = [re.compile(r"\r\n"), ANSI_ESCAPE_RE, *(re.compile(p, re.DOTALL) for p in patterns), STRAY_RE]
    while True:
        found = [(m.start(), i, m) for i, r in enumerate(regexes) if (m := r.search(text))]
        if not found:
            return None
        _, i, m = min(found, key=lambda f: f[:2])
        if 1 < i < len(regexes) - 1:
            return m.group(0), text[m.end() :]
        text = text[m.end() :]


def test_prompt_samples_cover_examples():
    assert sorted(PROMPT_SAMPLES) == sorted(NOT_PROMPTS) == sorted(p.name for p in EXAMPLES)


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


@pytest.mark.parametrize("before", ["", "hello\r\n", "\x1b[?2004l\rhello\r\n", "show version\r\n\r\n"])
@pytest.mark.parametrize(
    ("name", "text"),
    [(n, t) for n, ts in NOT_PROMPTS.items() for t in ts],
    ids=repr,
)
def test_p6_74_example_prompt_regex_ignores_output_ending_in_a_prompt_char(name: str, text: str, before: str):
    """A read that ends in `>`, `#` or `$` isn't a prompt unless the line is one.

    The EOS example's second regex was all optional parts and a prompt character, so it matched
    `disk usage 5 > `, `price in $`, `! comment #` and `<html>`: the engine took output for a
    shell prompt and sent the next command into a command that was still running.
    """
    assert read_prompt(shell_regexes(name), before + text) is None


@pytest.mark.parametrize("lead", ["\r", "\x00", "\x07", "\r\x00\x00"], ids=["cr", "nul", "bel", "cr-nul-nul"])
@pytest.mark.parametrize("prompt", PROMPT_SAMPLES["eos-bootstrap.autobot.yaml"], ids=repr)
def test_p6_74_eos_prompt_after_a_control_character_is_still_a_prompt(prompt: str, lead: str):
    """A lone CR, NUL padding or a bell of a console server before the prompt doesn't defeat `^`: the engine discards them."""
    found = read_prompt(shell_regexes("eos-bootstrap.autobot.yaml"), f"output\r\n{lead}{prompt}")
    assert found is not None
    match, leftover = found
    assert (match, leftover) == (prompt, "")


@pytest.mark.parametrize("lead", ["", "\r", "\x00", "\x07", "\r\x00\x00", "\r\n", "\x1b[?2004h", "\x1b[0m\r"], ids=repr)
@pytest.mark.parametrize("prompt", PROMPT_SAMPLES["eos-bootstrap.autobot.yaml"], ids=repr)
def test_p6_74_eos_prompts_through_the_engine(prompt: str, lead: str):
    """The engine itself, not this file's copy of its reading: `^` finds each prompt behind what the engine consumes."""
    s = Session([PromptHandler("cli", shell_regexes("eos-bootstrap.autobot.yaml"), [], True)])
    assert s._is_shell_prompt(lead + prompt) is True
    assert s._is_shell_prompt(f"{lead}{prompt}echo hi") is False


@pytest.mark.parametrize(
    "text",
    ["--More--\r        \rswitch# ", "abc\rswitch# ", "abc\x07switch# ", "abc\x00bash-5.1$ ", "foo[admin@switch ~]$ "],
    ids=repr,
)
def test_p6_74_eos_prompt_behind_text_on_its_line_is_not_a_prompt(text: str):
    """What `^` costs: a prompt with text before it on its line, also text ended by a CR, NUL or BEL, isn't matched."""
    assert read_prompt(shell_regexes("eos-bootstrap.autobot.yaml"), text) is None
    s = Session([PromptHandler("cli", shell_regexes("eos-bootstrap.autobot.yaml"), [], True)])
    assert s._is_shell_prompt(text) is False


@pytest.mark.parametrize(
    ("text", "match"),
    [
        # the example's comment: output with no final newline in front of the prompt
        ("fooswitch#", "fooswitch#"),  # reads as a hostname, so the EOS regex takes all of it
        ("fooswitch(config)# ", "fooswitch(config)# "),
        ("done. switch#", None),
        ("100% switch> ", None),
        ("foobash-5.1$ ", None),
        ("foo-bash-4.2$ ", None),
        ("foo[admin@switch ~]$ ", None),
    ],
    ids=repr,
)
def test_p6_74_eos_prompt_after_output_with_no_final_newline(text: str, match: str | None):
    """What the example's comment says of `printf foo`: the bash prompts aren't recognised behind output, and
    the EOS prompt only when the output could be part of a hostname, in which case it is matched with it."""
    found = read_prompt(shell_regexes("eos-bootstrap.autobot.yaml"), text)
    assert (found and found[0]) == match


def test_p6_74_eos_regexes_start_at_the_line_and_each_has_a_prompt():
    """Every shell regex of the EOS example starts with `^`, and each sample is matched by one of them."""
    regexes = shell_regexes("eos-bootstrap.autobot.yaml")
    assert len(regexes) == 3
    assert all(r.startswith("^") and r.endswith(" ?$") for r in regexes)
    matched = {i for p in PROMPT_SAMPLES["eos-bootstrap.autobot.yaml"] for i, r in enumerate(regexes) if re.search(r, p)}
    assert matched == {0, 1, 2}
    # no regex can match the empty string or a lone prompt character any more
    for r in regexes:
        assert not any(re.fullmatch(r, t) for t in ["", "$", "#", ">", "$ ", "# ", "> "])


# -- P6-76: the SONiC example's prompt regex starts at its line ------------------

SONIC = "sonic.autobot.yaml"


@pytest.mark.parametrize("lead", ["", "\r", "\x00", "\x07", "\r\x00\x00", "\r\n", "\x1b[?2004h", "\x1b[0m\r"], ids=repr)
@pytest.mark.parametrize("prompt", PROMPT_SAMPLES[SONIC], ids=repr)
def test_p6_76_sonic_prompts_through_the_engine(prompt: str, lead: str):
    """`^` finds each prompt behind what the engine consumes or discards: line breaks, escape sequences, stray CR, NUL, BEL."""
    assert read_prompt(shell_regexes(SONIC), f"output\r\n{lead}{prompt}") == (prompt, "")
    s = Session([PromptHandler("cli", shell_regexes(SONIC), [], True)])
    assert s._is_shell_prompt(lead + prompt) is True
    assert s._is_shell_prompt(f"{lead}{prompt}echo hi") is False


@pytest.mark.parametrize(
    "text",
    [
        "fooadmin@sonic:~$ ",  # printf foo: `fooadmin` reads as a user name, but only by luck (see the next ones)
        "foo admin@sonic:~$ ",
        "100%admin@sonic:~$ ",
        "(venv) admin@sonic:~$ ",
        "[1] admin@sonic:~$ ",
        "--More--\r        \radmin@sonic:~$ ",
        "abc \radmin@sonic:~$ ",
        "abc \x07root@sonic:~# ",
    ],
    ids=repr,
)
def test_p6_76_sonic_prompt_behind_text_on_its_line(text: str):
    """What `^` costs: a prompt with other text before it on its line isn't matched, unless that text
    happens to read as part of the user name (`printf foo` leaves `fooadmin@sonic:~$ `)."""
    expected = text.startswith("fooadmin")
    assert (read_prompt(shell_regexes(SONIC), text) is not None) is expected
    s = Session([PromptHandler("cli", shell_regexes(SONIC), [], True)])
    assert s._is_shell_prompt(text) is expected


def test_p6_76_sonic_regex_starts_at_the_line():
    """The one shell regex of the SONiC example starts with `^`, ends at the prompt character and uses no lookaround."""
    [regex] = shell_regexes(SONIC)
    assert regex.startswith("^") and regex.endswith("[$#] ?$")
    assert "(?" not in regex
    assert not any(re.fullmatch(regex, t) for t in ["", "$", "#", "$ ", "# ", "admin@sonic", "admin@sonic:"])


def test_p6_76_spec_and_readme_quote_the_sonic_regex():
    """SPEC "Prompt Handling" and the README give the example's regex as their shell prompt regex."""
    [regex] = shell_regexes(SONIC)
    assert f"`'{regex}'`" in (ROOT / "SPEC.md").read_text()
    assert (ROOT / "README.md").read_text().count(f"'{regex}'") == 2


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
        ("eos-bootstrap.autobot.yaml", "[admin@switch ~]$ "),
        ("eos-bootstrap.autobot.yaml", "-bash-4.2$ "),
        ("eos-bootstrap.autobot.yaml", r"\s-\v\$ "),  # bash's own default: bash-<version>$
    ],
)
def test_example_prompt_regex_registers_clean_output(name: str, ps1: str):
    """Against a real bash with that PS1, register sees only the command's output."""
    out = run_vars(
        [{"cmd": "echo hello", "register": "a"}, {"cmd": "echo again {{ vars.a }}", "register": "b"}],
        prompts=shell_prompts(name),
        prepare=f"export PS1={shlex.quote(ps1)}\n",
    )
    assert out["a"] == "hello"
    assert out["b"] == "again hello"
