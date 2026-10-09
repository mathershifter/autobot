"""P6-19/20, P6-74, P6-76, P6-112, P6-113: every example validates against the models and the schema, its shell prompt regexes read
whole prompts and nothing else, and its breakout logs out and makes sure of it."""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import ROOT, SHELL_PROMPT, FakeDevice, SentLog, make_doc, run_vars

from autobot.cli import UniqueKeyLoader
from autobot.models import Config
from autobot.runner import BreakoutError, Runner, left, trail
from autobot.screen import ANSI_ESCAPE_RE, STRAY_RE
from autobot.session import PromptHandler, Session

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


# -- P6-112: the examples log out, and make sure of it -----------------------------------------------

LOGIN = {
    "name": "login",
    "send": {"each": "vars.creds", "fields": [{"match": "[Ll]ogin:", "field": "username"}, {"match": "Password:", "field": "password"}]},
}
CREDS = {"creds": [{"username": "admin", "password": "secret"}]}
CONSOLE = ("--accept", "admin:secret", "--logout", "--escape")
# after a `line` that starts with `echo rea''dy`: the wait for that output, so the shell has read the line
# before the breakout's Ctrl-C, which would discard a line that is still unread
RUNS = {"block": {"name": "the program runs"}, "after": "ready\r\n", "timeout": "10s"}


def breakout_of(path: Path) -> list[dict[str, Any]]:
    return load(path)["attach"]["breakout"]


def test_p6_112_examples_are_valid_for_the_cli():
    """`autobot validate` on both examples: each is `valid`."""
    res = subprocess.run(
        [sys.executable, "-W", "ignore", "-m", "autobot.cli", "validate", *map(str, EXAMPLES)],
        capture_output=True, text=True, check=False, timeout=60,
    )
    assert res.returncode == 0, res.stderr
    assert res.stderr.splitlines() == [f"{path}: valid" for path in EXAMPLES]


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_p6_112_example_breakout_clears_the_line_logs_out_and_waits_for_the_login_prompt(path: Path):
    """SPEC "Logging out": the shape of each example's `attach.breakout`. A control character first, after
    a pause in which the far side reads what was sent before it, the logout as a `line`, the wait for
    a login prompt at the end of the output on the step after it, with a timeout, and no `cmd`, whose
    prompt wait would answer the login prompt."""
    steps = breakout_of(path)
    assert steps[0] == {"control": "c", "delay_before": "2s"} and steps[1] == {"line": "logout"}
    assert steps[2] == {"control": "]", "after": "[Ll]ogin: ?$", "timeout": "30s"}
    assert not any("cmd" in step for step in steps)
    assert steps[3:] == ([{"line": "logout"}] if path.name.startswith("eos") else [])


def console_server(
    fake_device: FakeDevice, path: Path, script: list[dict[str, Any]], wait: str | None = None, opts: tuple[str, ...] = ()
) -> tuple[dict[str, Any], Path]:
    """A stand-in for what the example attaches to: the fake console, which asks for a login, runs a
    real bash, asks for the login again after `logout`, and ends at Ctrl-], like the client of a console
    server. For the EOS example it is started from a login shell, the jump host. The breakout is the
    example's own; `wait` shortens the timeout of its wait."""
    device, log = fake_device(*CONSOLE, *opts)
    breakout = breakout_of(path)
    if wait:
        breakout[2]["timeout"] = wait
    kw: dict[str, Any] = {"spawn": device}
    if path.name.startswith("eos"):
        kw = {
            "spawn": "bash --norc --noprofile -i -l",
            # the wait for the device's banner reads past the jump host's own prompt, as the example's
            # `after: "attached to"` does
            "attach_script": [{"line": device}, {"block": {"name": "attached"}, "after": "Welcome"}],
        }
    return make_doc(script, prompts=[SHELL_PROMPT, LOGIN], vars=CREDS, breakout=breakout, **kw), log


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
@pytest.mark.parametrize(
    "script",
    [[{"cmd": "echo configured"}], [{"cmd": "false"}], [{"cmd": "sleep 30", "timeout": "1s"}], [{"cmd": "true"}, {"line": "echo half-typed \\"}]],
    ids=["completed", "failed", "command-running", "half-typed"],
)
def test_p6_112_example_breakout_logs_out_and_leaves_the_console_server(
    fake_device: FakeDevice, sent: SentLog, path: Path, script: list[dict[str, Any]]
):
    """Each example's breakout, as it is written, against the stand-in: the device logs the logout, which
    the breakout waits for, and then the Ctrl-] is sent, and for the EOS example the jump host's `logout`.
    Nothing waits for those two to be read before the session is closed, so nothing is asserted of what
    they do. After a script that completed, one that failed, one whose command is still running and one
    that left a line typed."""
    doc, log = console_server(fake_device, path, script)
    runner = Runner(Config.model_validate(doc), {})
    try:
        runner.run()
    except (RuntimeError, TimeoutError) as e:
        assert left(e) is None, e
    assert FakeDevice.read(log)[:3] == ["LOGIN=admin", "PASSWORD=secret", "LOGOUT="]
    breakout = [("ctrl", "c"), ("line", "logout"), ("ctrl", "]"), *([("line", "logout")] if path.name.startswith("eos") else [])]
    assert sent[-len(breakout):] == breakout


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_p6_112_example_breakout_fails_when_the_logout_does_not_happen(fake_device: FakeDevice, sent: SentLog, path: Path):
    """A program that ignores Ctrl-C takes the `logout` line: no login prompt comes, the breakout fails at
    the step that waits, and the steps after it (the Ctrl-], the jump host's logout) are not sent."""
    doc, log = console_server(fake_device, path, [{"cmd": "trap '' INT"}, {"line": "echo rea''dy; cat"}, RUNS], wait="3s")
    with pytest.raises(BreakoutError, match=r"^a breakout did not finish \(TimeoutError\): timed out after 3.0s waiting for the after pattern ") as ei:
        Runner(Config.model_validate(doc), {}).run()
    assert [ref.path for ref in trail(ei.value)] == ["attach.breakout.2"]
    assert FakeDevice.read(log) == ["LOGIN=admin", "PASSWORD=secret"]
    assert sent[-2:] == [("ctrl", "c"), ("line", "logout")]


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_p6_112_example_breakout_takes_login_with_a_capital_for_the_prompt(fake_device: FakeDevice, path: Path):
    """A device that asks `Login: `: the example's wait is met, as its own login prompt is."""
    doc, log = console_server(fake_device, path, [{"cmd": "echo configured"}], opts=("--capital",))
    Runner(Config.model_validate(doc), {}).run()
    assert FakeDevice.read(log)[:3] == ["LOGIN=admin", "PASSWORD=secret", "LOGOUT="]


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_p6_112_example_breakout_is_not_met_by_text_with_the_word_in_it(fake_device: FakeDevice, path: Path):
    """Unread output with `login:` in it, and a program that takes the `logout` line: the example's wait
    is for a prompt at the end of the output, so the breakout fails, and the device's log has no logout."""
    script = [{"cmd": "trap '' INT"}, {"line": "echo rea''dy; echo Last login: Tue Oct 7; cat"}, RUNS]
    doc, log = console_server(fake_device, path, script, wait="3s")
    with pytest.raises(BreakoutError):
        Runner(Config.model_validate(doc), {}).run()
    assert FakeDevice.read(log) == ["LOGIN=admin", "PASSWORD=secret"]


# -- P6-113: the examples' login prompt answers a login prompt, and nothing else --------------------------

TWO = {"creds": [{"username": "admin", "password": "secret"}, {"username": "root", "password": "other"}]}


def login_of(path: Path) -> dict[str, Any]:
    return next(p for p in load(path)["prompts"] if p["name"] == "login")


def logged_in(fake_device: FakeDevice, path: Path, *opts: str) -> tuple[dict[str, Any], Path]:
    """A script with the example's own `login` prompt and two credential sets, against the fake console."""
    device, log = fake_device("--accept", "admin:secret", *opts)
    return make_doc([{"cmd": "echo in", "register": "out"}], spawn=device, prompts=[SHELL_PROMPT, login_of(path)], vars=TWO), log


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_p6_113_example_login_prompt_is_one_at_the_end_of_the_output(path: Path):
    assert login_of(path)["send"]["fields"] == [
        {"match": "[Ll]ogin: ?$", "field": "username"},
        {"match": "[Pp]assword: ?$", "field": "password"},
    ]


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
@pytest.mark.parametrize(
    "opts",
    [(), ("--last-login",), ("--capital",), ("--lower-password",), ("--capital", "--lower-password", "--last-login"), ("--same-chunk", "--last-login")],
    ids=["plain", "last-login-banner", "Login", "password", "all-three", "banner-and-prompt-in-one-write"],
)
def test_p6_113_example_login_prompt_answers_the_prompts_and_not_the_banner(fake_device: FakeDevice, sent: SentLog, path: Path, opts: tuple[str, ...]):
    """SPEC "sendEach": `Login:` and `login:`, `Password:` and `password:` are answered, once each, and
    the `Last login: ...` line that follows is not: the second credential set is never sent, and the
    command runs at the shell."""
    doc, log = logged_in(fake_device, path, *opts)
    runner = Runner(Config.model_validate(doc), {})
    runner.run()
    assert FakeDevice.read(log) == ["LOGIN=admin", "PASSWORD=secret"]
    assert sent.lines()[:3] == ["admin", "secret", "echo in"] and "root" not in sent.lines()
    assert runner.config.vars["out"] == "in"


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_p6_113_banner_that_arrives_in_pieces_is_the_documented_limit(fake_device: FakeDevice, sent: SentLog, path: Path):
    """The limit SPEC names: the device writes `Last login: `, and the rest two seconds later. While the
    first piece is the last thing that has arrived, it is a login prompt for the regex, and the second
    user name is sent, to the shell. (With the host name in the regex it would not be.)"""
    doc, _ = logged_in(fake_device, path, "--last-login", "--split-banner", "2")
    try:
        Runner(Config.model_validate(doc), {}).run()
    except (RuntimeError, TimeoutError):
        pass  # what the shell makes of a user name is not the point
    assert sent.lines()[:3] == ["admin", "secret", "root"]


@pytest.mark.parametrize("pattern", ["(?:L|l)ogin:", "login:"])
def test_p6_113_login_regex_without_the_end_answers_the_banner(fake_device: FakeDevice, sent: SentLog, pattern: str):
    """What the `$` is for: a regex that finds the word anywhere takes `Last login: ...` for a second login
    prompt and types the next user name at the shell."""
    device, log = fake_device("--accept", "admin:secret", "--last-login")
    login = {"name": "login", "send": {"each": "vars.creds", "fields": [{"match": pattern, "field": "username"}, {"match": "(?:P|p)assword:", "field": "password"}]}}
    doc = make_doc([{"cmd": "echo in", "timeout": "3s"}], spawn=device, prompts=[SHELL_PROMPT, login], vars=TWO)
    try:
        Runner(Config.model_validate(doc), {}).run()
    except (RuntimeError, TimeoutError):
        pass
    assert sent.lines()[:3] == ["admin", "secret", "root"]
