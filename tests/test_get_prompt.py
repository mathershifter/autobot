"""P4: ``get_prompt``, prompt forms and credential cycling.

SPEC.md:33-53 and 336-346. Session-level tests build handlers through
``Runner.build_handler`` (via ``make_runner``) so prompt models go through
the real path. The device side is ``tests/fakes/device.py`` (fixture F2).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import (
    BASH,
    RC_PROBE,
    SHELL_ENV,
    SHELL_PROMPT,
    FakeDevice,
    SentLog,
    make_runner,
    run_script,
    run_vars,
)

from autobot.models import SendEach
from autobot.runner import Runner, send_each_sets
from autobot.session import HELD_GRACE, PromptHandler, Session, _mid_echo

# sendEach fields entries: each pairs a regex (or alternatives) with an item field
UP_FIELDS = [{"match": "login:", "field": "username"}, {"match": "Password:", "field": "password"}]
LOGIN_EACH = {"name": "login", "send": {"each": "vars.creds", "fields": UP_FIELDS}}
ADMIN = {"creds": [{"username": "admin", "password": "secret"}]}


def creds(*passwords: str) -> dict[str, Any]:
    return {"creds": [{"username": "admin", "password": p} for p in passwords]}


@pytest.fixture
def device(fake_device: FakeDevice) -> Iterator[Callable[..., tuple[Runner, Path]]]:
    """Factory: a Runner attached to the fake device, kicked by default.

    With ``--wait-enter`` the device blocks after its banner until the kick
    arrives, so the banner and the first prompt never share a chunk.
    """
    runners: list[Runner] = []

    def factory(
        prompts: list[dict[str, Any]], *opts: str, vars: dict | None = None, kick: bool = True
    ) -> tuple[Runner, Path]:
        spawn, log = fake_device(*opts)
        r = make_runner([], prompts=prompts, vars=vars)
        runners.append(r)
        r.session.attach(spawn, env=dict(SHELL_ENV), timeout=5)
        if kick:
            r.session.sendline("")
        return r, log

    yield factory
    for r in runners:
        r.session.detach()


def log_of(path: Path) -> list[str]:
    return FakeDevice.read(path)


def test_p4_01_get_prompt_never_sends_command(shell_session: Session, sent: SentLog):
    """SPEC.md:338 (and 294): get_prompt only waits; it sends nothing itself."""
    shell_session.get_prompt(timeout=5)
    shell_session.sendline("echo hi")
    sent.clear()
    assert shell_session.get_prompt(timeout=5) == "hi\n"
    assert sent.lines() == []
    assert shell_session.ctx["match"] == "PROMPT$ "


@pytest.mark.parametrize(
    "prompt",
    [
        {"name": "sh", "expect": [r"PROMPT\$ "], "return": True},
        {"name": "sh", "expect": [r"PROMPT\$ "]},
    ],
    ids=["return-true", "no-send"],
)
def test_p4_02_shell_prompt_forms(device, sent: SentLog, prompt: dict[str, Any]):
    """SPEC.md:38, 341: return: true, or no send, marks a shell prompt."""
    r, log = device([prompt], "--wait-enter", "--order", "none", "--then", "prompt")
    sent.clear()
    r.session.get_prompt(timeout=5)
    assert sent.lines() == []
    assert log_of(log) == ["ENTER="]


@pytest.mark.slow
def test_p4_04_solicit_newline_after_idle(device, sent: SentLog):
    """SPEC.md:343: after 5s idle, one empty newline solicits the prompt."""
    r, _ = device([SHELL_PROMPT], "--wait-enter", "--order", "none", "--then", "prompt", kick=False)
    start = time.monotonic()
    r.session.get_prompt(timeout=15)
    assert time.monotonic() - start >= 5
    assert sent.lines() == [""]


@pytest.mark.slow
def test_p4_05_solicit_newline_only_once(device, sent: SentLog):
    """SPEC.md:343: the solicit newline is sent once only."""
    r, _ = device([SHELL_PROMPT], "--silent", kick=False)
    with pytest.raises(TimeoutError):
        r.session.get_prompt(timeout=11)
    assert sent.lines() == [""]


@pytest.mark.slow
def test_p4_06_no_solicit_after_handler_fired(device, sent: SentLog):
    """SPEC.md:343: no solicit newline once a handler has been activated."""
    login = {"name": "login", "expect": ["login:"], "send": "admin"}
    r, log = device(
        [SHELL_PROMPT, login],
        "--wait-enter", "--order", "login", "--accept", "admin:",
        "--post-auth-delay", "6", "--then", "prompt",
    )
    sent.clear()
    r.session.get_prompt(timeout=15)
    assert sent.lines() == ["admin"]
    assert log_of(log) == ["ENTER=", "LOGIN=admin"]


def test_p4_07_timeout_error_at_deadline(device):
    """SPEC.md:344: the overall timeout raises TimeoutError."""
    r, _ = device([SHELL_PROMPT], "--silent", kick=False)
    start = time.monotonic()
    with pytest.raises(TimeoutError, match=r"^timed out after 1s waiting for a shell prompt \('sh'\)$"):
        r.session.get_prompt(timeout=1)
    assert time.monotonic() - start < 2


def test_p4_08_eof_error_on_child_exit(device):
    """SPEC.md:336: the child exiting raises EOFError."""
    r, _ = device([SHELL_PROMPT], "--wait-enter", "--exit-after-banner")
    with pytest.raises(EOFError):
        r.session.get_prompt(timeout=10)


def test_p4_09_eof_is_not_timeout(device):
    """SPEC.md:336: EOF is reported as soon as it happens, not at a poll timeout."""
    r, _ = device([SHELL_PROMPT], "--wait-enter", "--exit-after-banner")
    start = time.monotonic()
    with pytest.raises(EOFError):
        r.session.get_prompt(timeout=10)
    assert time.monotonic() - start < 2


def test_p4_12_send_each_with_fields(device):
    """SPEC sendEach: each fields entry sends its field of the current item."""
    login = {"name": "login", "send": {"each": "vars.creds", "fields": UP_FIELDS}}
    creds = [{"username": "admin", "password": "pass1"}, {"username": "admin", "password": "pass2"}]
    r, log = device([SHELL_PROMPT, login], "--wait-enter", "--accept", "admin:pass2",
                    vars={"creds": creds})
    r.session.get_prompt(timeout=10)
    assert log_of(log) == [
        "ENTER=", "LOGIN=admin", "PASSWORD=pass1", "LOGIN=admin", "PASSWORD=pass2",
    ]


def test_p4_13_send_each_without_fields_stringifies(device):
    """SPEC.md:53: without fields, each item is converted to a string."""
    pin = {"name": "pin", "expect": ["Password:"], "send": {"each": "vars.pins"}}
    r, log = device([SHELL_PROMPT, pin], "--wait-enter", "--order", "password",
                    "--accept", ":1234", vars={"pins": [1111, 1234]})
    r.session.get_prompt(timeout=10)
    assert log_of(log) == ["ENTER=", "PASSWORD=1111", "PASSWORD=1234"]


def test_p4_21_send_each_nested_path_ignores_other_keys(device):
    """SPEC sendEach: `each` may name nested keys; only the named fields are sent."""
    login = {"name": "login", "send": {"each": "vars.site.creds", "fields": UP_FIELDS}}
    creds = [{"username": "admin", "password": "pass1", "note": "x"}, {"username": "admin", "password": "pass2"}]
    r, log = device([SHELL_PROMPT, login], "--wait-enter", "--accept", "admin:pass2",
                    vars={"site": {"creds": creds}})
    r.session.get_prompt(timeout=10)
    assert log_of(log) == [
        "ENTER=", "LOGIN=admin", "PASSWORD=pass1", "LOGIN=admin", "PASSWORD=pass2",
    ]


def test_p4_22_send_each_scalar_items_without_fields(device):
    """SPEC sendEach: without fields, a string or number item is sent as a string; a quoted 'True' is a string."""
    pin = {"name": "pin", "expect": ["Password:"], "send": {"each": "vars.pins"}}
    r, log = device([SHELL_PROMPT, pin], "--wait-enter", "--order", "password",
                    "--accept", ":True", vars={"pins": ["1111", 2.5, "True"]})
    r.session.get_prompt(timeout=10)
    assert log_of(log) == ["ENTER=", "PASSWORD=1111", "PASSWORD=2.5", "PASSWORD=True"]


def test_p4_22_send_each_boolean_item_sends_nothing(device, sent: SentLog):
    """SPEC sendEach: a boolean item is an error when the prompts are loaded, before anything is spawned or sent.

    `pins: ["1111", 2.5, true]` used to send `True` as the third PIN.
    """
    pin = {"name": "pin", "expect": ["Password:"], "send": {"each": "vars.pins"}}
    with pytest.raises(ValueError) as ei:
        device([SHELL_PROMPT, pin], "--wait-enter", "--order", "password", vars={"pins": ["1111", 2.5, True]})
    assert str(ei.value) == (
        "prompt 'pin': sendEach 'vars.pins': item 2 is a boolean (true), which is never sent as text; "
        "quote the value to send it as written, e.g. 'true' or 'yes'"
    )
    assert sent == []


def test_p4_23_send_each_empty_list_fails_at_send_time(device):
    """SPEC sendEach, Response selection: an empty collection loads; the prompt has no response when it fires."""
    login = {"name": "login", "send": {"each": "vars.creds", "fields": UP_FIELDS[:1]}}
    r, _ = device([SHELL_PROMPT, login], "--wait-enter", vars={"creds": []})
    with pytest.raises(RuntimeError, match="^prompt 'login': no response available"):
        r.session.get_prompt(timeout=5)


def test_p4_14_responses_exhausted(device):
    """SPEC "Response selection": running out of credential sets raises."""
    r, _ = device([SHELL_PROMPT, LOGIN_EACH], "--wait-enter", vars=creds("bad"))
    with pytest.raises(RuntimeError, match="prompt 'login': responses exhausted"):
        r.session.get_prompt(timeout=10)


def test_p4_15_handlers_reset_per_get_prompt(device):
    """SPEC "Response selection": the selection starts over on every get_prompt call."""
    r, log = device([SHELL_PROMPT, LOGIN_EACH], "--wait-enter", "--repeat", "2",
                    "--accept", "admin:secret", "--then", "prompt", vars=ADMIN)
    r.session.get_prompt(timeout=10)
    r.session.sendline("again")
    r.session.get_prompt(timeout=10)
    assert log_of(log).count("LOGIN=admin") == 2
    assert "LINE=again" in log_of(log)


def test_p4_17_fields_password_first(device):
    """SPEC "Response selection": fields entry k sends field k of the current item.

    A password-first device gets the password at ``Password:`` and the user
    name at ``login:``.
    """
    r, log = device([SHELL_PROMPT, LOGIN_EACH], "--wait-enter", "--order", "password,login",
                    "--accept", "admin:secret", vars=ADMIN)
    r.session.get_prompt(timeout=10)
    assert log_of(log) == ["ENTER=", "PASSWORD=secret", "LOGIN=admin"]


def test_p4_17_fields_password_first_credential_cycling(device):
    """SPEC "Response selection" table row 2: a repeated entry advances the set."""
    r, log = device([SHELL_PROMPT, LOGIN_EACH], "--wait-enter", "--order", "password,login",
                    "--accept", "admin:p2", vars=creds("p1", "p2"))
    r.session.get_prompt(timeout=10)
    assert log_of(log) == [
        "ENTER=", "PASSWORD=p1", "LOGIN=admin", "PASSWORD=p2", "LOGIN=admin",
    ]


def test_p4_17_fields_password_only(device):
    """SPEC "Response selection" table row 3 (``ssh admin@host``).

    ``Password:`` twice: the second match repeats the entry and advances to
    the next set, so one password per set is sent.
    """
    r, log = device([SHELL_PROMPT, LOGIN_EACH], "--wait-enter", "--order", "password",
                    "--accept", ":p2", vars=creds("p1", "p2"))
    r.session.get_prompt(timeout=10)
    assert log_of(log) == ["ENTER=", "PASSWORD=p1", "PASSWORD=p2"]


def test_p4_17_fields_exhaustion(device):
    """SPEC "Response selection": a repeated entry with no next set -> exhausted."""
    r, log = device([SHELL_PROMPT, LOGIN_EACH], "--wait-enter", "--order", "password,login",
                    vars=creds("bad"))
    with pytest.raises(RuntimeError, match=r"^prompt 'login': responses exhausted$"):
        r.session.get_prompt(timeout=10)
    assert log_of(log) == ["ENTER=", "PASSWORD=bad", "LOGIN=admin"]


def test_p4_17_fields_login_first_exhaustion_after_two_sets(device):
    """SPEC "Response selection" table row 4: two sets, a third ``login:`` exhausts."""
    r, log = device([SHELL_PROMPT, LOGIN_EACH], "--wait-enter", "--order", "login,password",
                    vars=creds("pw1", "pw2"))
    with pytest.raises(RuntimeError, match=r"^prompt 'login': responses exhausted$"):
        r.session.get_prompt(timeout=10)
    assert log_of(log) == [
        "ENTER=", "LOGIN=admin", "PASSWORD=pw1", "LOGIN=admin", "PASSWORD=pw2",
    ]


def test_p4_24_send_each_match_alternatives_send_the_same_field(device):
    """SPEC sendEach: the regexes of one fields entry are alternatives for one field.

    ``login:`` and ``Password:`` both send ``password``, and they share one
    fired state: the second match is a repeat, so the item advances.
    """
    either = [{"match": ["login:", "Password:"], "field": "password"}]
    login = {"name": "login", "send": {"each": "vars.creds", "fields": either}}
    creds = [{"username": "u1", "password": "pw1"}, {"username": "u2", "password": "pw2"}]
    r, log = device([SHELL_PROMPT, login], "--wait-enter", "--order", "login,password",
                    "--accept", "pw1:pw2", vars={"creds": creds})
    r.session.get_prompt(timeout=10)
    assert log_of(log) == ["ENTER=", "LOGIN=pw1", "PASSWORD=pw2"]


def test_p4_25_send_each_without_fields_patterns_share_one_state(device):
    """SPEC sendEach: without fields, every expect regex sends the current item; any repeat advances."""
    pin = {"name": "pin", "expect": ["login:", "Password:"], "send": {"each": "vars.pins"}}
    r, log = device([SHELL_PROMPT, pin], "--wait-enter", "--order", "login,password",
                    "--accept", "1111:2222", vars={"pins": [1111, 2222]})
    r.session.get_prompt(timeout=10)
    # per-pattern state would send 1111 again at Password:
    assert log_of(log) == ["ENTER=", "LOGIN=1111", "PASSWORD=2222"]


def test_p4_18_login_prompt_in_same_chunk_as_banner(device, sent: SentLog):
    """SPEC.md:81, 341: a login prompt that arrives with the banner is answered."""
    r, log = device([SHELL_PROMPT, LOGIN_EACH], "--same-chunk", "--order", "login,password",
                    "--accept", "admin:secret", vars=ADMIN, kick=False)
    r.session.get_prompt(timeout=5)
    assert log_of(log) == ["LOGIN=admin", "PASSWORD=secret"]
    assert "" not in sent.lines()


def test_p4_19_shell_prompt_in_same_chunk_as_banner(device):
    """SPEC.md:81, 341: a shell prompt that arrives with the banner is detected."""
    r, _ = device([SHELL_PROMPT], "--same-chunk", "--order", "none", "--then", "prompt", kick=False)
    try:
        r.session.get_prompt(timeout=3)
        found = True
    except TimeoutError:
        found = False
    assert found, "the prompt in the banner chunk was swallowed by the initial attach wait"


def test_p4_20_send_template_rendered_at_send_time(fake_device: FakeDevice):
    """SPEC prompts: a send template sees vars registered by earlier steps."""
    spawn, log = fake_device("--order", "none", "--ask", "Name?")
    ask = {"name": "name", "expect": [r"Name\? "], "send": "{{ vars.user }}"}
    try:
        runner = make_runner(
            [
                {"cmd": "echo admin", "register": "user"},
                {"line": spawn},
                {"cmd": "true"},
            ],
            prompts=[SHELL_PROMPT, ask],
        )
    except ValueError as e:
        raise AssertionError(f"send was rendered before the script ran: {e}") from e
    runner.run()
    assert FakeDevice.read(log) == ["ASK=admin"]


def test_send_template_syntax_error_at_load():
    """#7: send is rendered when sent, but a syntax error still fails at load."""
    login = {"name": "login", "expect": ["login:"], "send": "{{ vars.user "}
    with pytest.raises(ValueError, match=r"^prompt 'login': template error: "):
        make_runner([], prompts=[SHELL_PROMPT, login])


# -- P4-26..31: simple prompts, one send string (SPEC "prompts") -------------

CONFIRM = {"name": "confirm", "expect": [r"continue\?", r"are you sure\?"], "send": "yes"}


def test_p4_26_simple_prompt_answered_each_time_it_appears(device):
    """SPEC "Response selection": a simple prompt sends its string on any match, each time it appears, with no exhaustion."""
    r, log = device([SHELL_PROMPT, CONFIRM], "--wait-enter", "--order", "none",
                    "--ask", "continue?", "--ask", "continue?", "--ask", "continue?", "--then", "prompt")
    r.session.get_prompt(timeout=10)
    assert log_of(log) == ["ENTER=", "ASK=yes", "ASK=yes", "ASK=yes"]


def test_p4_27_simple_prompt_alternatives(device):
    """SPEC prompts: the expect regexes are alternatives; each of them sends the string."""
    r, log = device([SHELL_PROMPT, CONFIRM], "--wait-enter", "--order", "none",
                    "--ask", "'are you sure?'", "--ask", "continue?", "--then", "prompt")
    r.session.get_prompt(timeout=10)
    assert log_of(log) == ["ENTER=", "ASK=yes", "ASK=yes"]


def test_p4_28_simple_prompt_single_expect_string(device):
    """SPEC prompts: expect may be a single regex."""
    confirm = {"name": "confirm", "expect": r"continue\?", "send": "yes"}
    r, log = device([SHELL_PROMPT, confirm], "--wait-enter", "--order", "none",
                    "--ask", "continue?", "--then", "prompt")
    r.session.get_prompt(timeout=10)
    assert log_of(log) == ["ENTER=", "ASK=yes"]


def test_p4_29_simple_prompt_template(device):
    """SPEC prompts: send is a template, rendered each time it is sent."""
    confirm = {**CONFIRM, "send": "{{ vars.answer | upper }}"}
    r, log = device([SHELL_PROMPT, confirm], "--wait-enter", "--order", "none",
                    "--ask", "continue?", "--ask", "continue?", "--then", "prompt", vars={"answer": "y"})
    r.session.get_prompt(timeout=10)
    assert log_of(log) == ["ENTER=", "ASK=Y", "ASK=Y"]


def test_p4_30_simple_prompt_empty_send_presses_enter(device, sent: SentLog):
    """SPEC prompts: `send: ""` answers with an empty line."""
    confirm = {**CONFIRM, "send": ""}
    r, log = device([SHELL_PROMPT, confirm], "--wait-enter", "--order", "none",
                    "--ask", "continue?", "--then", "prompt")
    sent.clear()
    r.session.get_prompt(timeout=10)
    assert sent.lines() == [""]
    assert log_of(log) == ["ENTER=", "ASK="]


def test_p4_31_simple_prompt_undefined_variable_aborts(device):
    """SPEC prompts: an undefined variable in send is reported when it is sent, naming the prompt."""
    confirm = {**CONFIRM, "send": "{{ vars.nope }}"}
    r, log = device([SHELL_PROMPT, confirm], "--wait-enter", "--order", "none", "--ask", "continue?")
    with pytest.raises(ValueError, match=r"^prompt 'confirm': template error: "):
        r.session.get_prompt(timeout=10)
    assert log_of(log) == ["ENTER="]


# -- P4-32..34: prompt state across a handler swap (SPEC "Prompt state across a swap") --


def shell(*patterns: str) -> PromptHandler:
    return PromptHandler("b", list(patterns), [], True)


def ask(*patterns: str) -> PromptHandler:
    return PromptHandler("ask", list(patterns), [["x"]], False)


BOLD, OFF = "\x1b[1m", "\x1b[0m"

# case -> (handlers, the prompt text the last wait matched, still at the prompt)
PROMPT_TEXTS: dict[str, tuple[Callable[[], list[PromptHandler]], str, bool]] = {
    "same": (lambda: [shell(r"PROMPT\$ ")], "PROMPT$ ", True),
    "anchored": (lambda: [shell(r"^PROMPT\$ $")], "PROMPT$ ", True),
    "suffix": (lambda: [shell(r"\$ ")], "PROMPT$ ", True),
    "other": (lambda: [shell(r"BLK\$ ")], "PROMPT$ ", False),
    "no-handlers": (list, "PROMPT$ ", False),
    "send-starts-first": (lambda: [shell(r"\$ "), ask("PROMPT")], "PROMPT$ ", False),
    "tie-shell-defined-first": (lambda: [shell(r"PROMPT\$ "), ask("PROMPT")], "PROMPT$ ", True),
    "tie-send-defined-first": (lambda: [ask("PROMPT"), shell(r"PROMPT\$ ")], "PROMPT$ ", False),
    "escape-skipped": (lambda: [shell(r"^PROMPT\$ ")], f"{BOLD}PROMPT$ {OFF}", True),
    "escape-starts-first": (lambda: [shell(r"PT\$ ")], f"PROM{BOLD}PT$ ", True),
    "escape-inside": (lambda: [shell(r"PROMPT\$ ")], f"PROM{BOLD}PT$ ", False),
    "line-break-skipped": (lambda: [shell(r"^PROMPT\$ ")], "x\r\nPROMPT$ ", True),
    "invalid-regex": (lambda: [shell("(")], "PROMPT$ ", False),
}


@pytest.mark.parametrize("case", PROMPT_TEXTS)
def test_p4_32_prompt_text_checked_like_a_prompt_wait(case: str):
    """SPEC "Prompt state across a swap": earliest match wins, ties by order, breaks and escapes skipped."""
    handlers, text, expected = PROMPT_TEXTS[case]
    assert Session(handlers())._is_shell_prompt(text) is expected


# case -> (handlers after the swap, still at the prompt)
SWAPS: dict[str, tuple[Callable[[], list[PromptHandler]], bool]] = {
    "same": (lambda: [shell(r"PROMPT\$ ")], True),
    "suffix": (lambda: [shell(r"[A-Z]+\$ ")], True),
    "added-question": (lambda: [shell(r"PROMPT\$ "), ask(r"continue\? ")], True),
    "other": (lambda: [shell(r"BLK\$ ")], False),
}


@pytest.mark.parametrize("case", SWAPS)
def test_p4_33_swap_keeps_a_recognized_prompt(shell_session: Session, sent: SentLog, case: str):
    """SPEC "Prompt state across a swap": a recognized prompt is reused at once, sending nothing.

    An unrecognized one isn't: the wait reads new output and, at its first poll timeout
    (here the 1 s deadline), sends its one solicit newline. The shell prints the same
    prompt again, which still doesn't match.
    """
    handlers, kept = SWAPS[case]
    s = shell_session
    s.get_prompt(timeout=5)
    s.sendline("echo hi")
    assert s.get_prompt(timeout=5) == "hi\n"
    sent.clear()
    s.restore_handlers(handlers())
    start = time.monotonic()
    if kept:
        assert s.get_prompt(timeout=5) == ""
        assert time.monotonic() - start < 0.5
        assert sent.lines() == []
    else:
        with pytest.raises(TimeoutError):
            s.get_prompt(timeout=1)
        assert sent.lines() == [""]
    assert s.ctx == {"before": "hi\n", "match": "PROMPT$ "}


def test_p4_34_swap_never_starts_prompt_state(shell_session: Session):
    """SPEC "Prompt state across a swap": a session that isn't at a prompt stays that way.

    The last prompt text still matches the new handlers, but a line was sent after it,
    so the wait reads that line's output instead of returning at once.
    """
    s = shell_session
    s.get_prompt(timeout=5)
    s.sendline("echo hi")
    s.restore_handlers([shell(r"PROMPT\$ ")])
    assert s.get_prompt(timeout=5) == "hi\n"
    assert s.ctx["before"] == "hi\n"


QUESTION = PromptHandler("q", [r"continue\? "], [["y"]], False)

PROMPT_NAMES = pytest.mark.parametrize(
    ("handlers", "names"),
    [
        ([PromptHandler("sh", [r"PROMPT\$ "], [], True), QUESTION, PromptHandler("root", ["# "], [], True)],
         "('sh', 'root')"),
        ([QUESTION], "(none defined)"),
    ],
    ids=["names", "none"],
)


@PROMPT_NAMES
def test_p4_35_timeout_names_shell_prompts(handlers: list[PromptHandler], names: str):
    """SPEC get_prompt: the overall timeout names the current shell prompts, in order; answer-only prompts aren't."""
    s = Session(handlers)
    s.attach(BASH, env={**SHELL_ENV, "PS1": "X> "}, timeout=5)  # a prompt no handler matches
    try:
        with pytest.raises(TimeoutError) as ei:
            s.get_prompt(timeout=1)
        assert str(ei.value) == f"timed out after 1s waiting for a shell prompt {names}"
    finally:
        s.detach()


@PROMPT_NAMES
def test_p4_36_eof_names_shell_prompts(handlers: list[PromptHandler], names: str):
    """SPEC get_prompt: the child exiting raises EOFError naming the shell prompts, as the timeout does."""
    s = Session(handlers)
    s.attach(BASH, env={**SHELL_ENV, "PS1": "X> "}, timeout=5)  # a prompt no handler matches
    try:
        s.sendline("exit")
        with pytest.raises(EOFError) as ei:
            s.get_prompt(timeout=10)
        assert str(ei.value) == f"connection closed while waiting for a shell prompt {names}"
    finally:
        s.detach()


# -- P4-39: sendEach items are strings or numbers ------------------------------

NOT_SCALAR = {
    "date": ("2026-10-04", "a timestamp"),
    "datetime": ("2026-10-04 12:00:00", "a timestamp"),
    "binary": ("!!binary aGk=", "binary data"),
    "set": ("!!set {a}", "a set"),
    "null": ("~", "null"),
    "list": ("[a]", "a list"),
}


@pytest.mark.parametrize("case", NOT_SCALAR)
def test_p4_39_send_each_item_must_be_string_or_number(case: str):
    """SPEC sendEach: an item, or a field's value, that YAML reads as something else is an error, not its `str()`.

    An unquoted date used to be sent as `2026-10-04` by accident of `str()`, and `!!binary` as `b'hi'`.
    """
    literal, kind = NOT_SCALAR[case]
    pins = yaml.safe_load(f"[ok, {literal}]")
    with pytest.raises(ValueError) as ei:
        send_each_sets("p", SendEach(each="vars.pins"), {"pins": pins})
    assert str(ei.value) == (
        f"prompt 'p': sendEach 'vars.pins': item 1 is {kind}; without fields each item must be a string or number"
    )
    send = SendEach.model_validate({"each": "vars.creds", "fields": [{"match": "x", "field": "pw"}]})
    with pytest.raises(ValueError) as ei:
        send_each_sets("p", send, {"creds": [{"pw": pins[1]}]})
    assert str(ei.value) == (
        f"prompt 'p': sendEach 'vars.creds': item 0 field 'pw' is {kind}, not a string or number"
    )


def test_p4_39_send_each_scalars_sent_as_str():
    """SPEC sendEach: strings and numbers are accepted and converted with `str()`; a quoted boolean word is a string."""
    pins = yaml.safe_load("['2026-10-04', 1234, 2.5, 'true', 'False', 'yes', 0, 1, '']")
    assert send_each_sets("p", SendEach(each="vars.pins"), {"pins": pins}) == [
        ["2026-10-04"], ["1234"], ["2.5"], ["true"], ["False"], ["yes"], ["0"], ["1"], [""],
    ]


# every unquoted spelling YAML reads as a boolean -> how the error shows it
YAML_BOOLEANS = {
    "true": "true", "True": "true", "TRUE": "true", "yes": "true", "Yes": "true", "on": "true", "On": "true",
    "false": "false", "False": "false", "no": "false", "NO": "false", "off": "false",
}
QUOTE_IT = "which is never sent as text; quote the value to send it as written, e.g. 'true' or 'yes'"


@pytest.mark.parametrize("literal", list(YAML_BOOLEANS))
def test_p4_43_send_each_boolean_is_an_error(literal: str):
    """SPEC sendEach: a boolean is never sent. As an item or as a field's value it is an error that says to quote it.

    It used to be sent as Python's `True` or `False`, whatever the script spelled (`yes`, `on`, `true`).
    """
    shown = YAML_BOOLEANS[literal]
    pins = yaml.safe_load(f"[ok, 7, {literal}]")
    assert isinstance(pins[2], bool)
    with pytest.raises(ValueError) as ei:
        send_each_sets("p", SendEach(each="vars.pins"), {"pins": pins})
    assert str(ei.value) == f"prompt 'p': sendEach 'vars.pins': item 2 is a boolean ({shown}), {QUOTE_IT}"
    send = SendEach.model_validate({"each": "vars.creds", "fields": [{"match": "x", "field": "u"}, {"match": "y", "field": "pw"}]})
    creds = yaml.safe_load(f"[{{u: admin, pw: secret}}, {{u: admin, pw: {literal}}}]")
    with pytest.raises(ValueError) as ei:
        send_each_sets("p", send, {"creds": creds})
    assert str(ei.value) == f"prompt 'p': sendEach 'vars.creds': item 1 field 'pw' is a boolean ({shown}), {QUOTE_IT}"
    # quoted, the same spelling is the text that is sent
    assert send_each_sets("p", SendEach(each="vars.pins"), {"pins": yaml.safe_load(f"['{literal}']")}) == [[literal]]


def test_p4_43_boolean_in_a_field_no_entry_sends_is_not_looked_at():
    """Only the fields the entries name are sent, so only they are checked."""
    send = SendEach.model_validate({"each": "vars.creds", "fields": [{"match": "x", "field": "u"}]})
    creds = yaml.safe_load("[{u: admin, enabled: true}]")
    assert send_each_sets("p", send, {"creds": creds}) == [["admin"]]


def test_p4_43_block_prompt_boolean_stops_the_block_before_it_sends(sent: SentLog):
    """A block's prompts are loaded on entering the block: the error comes before its `enter` steps send anything."""
    block = {
        "name": "b",
        "prompts": [SHELL_PROMPT, {"name": "pin", "expect": "PIN:", "send": {"each": "vars.pins"}}],
        "enter": [{"line": "echo entered"}],
    }
    with pytest.raises(ValueError, match=r"^prompt 'pin': sendEach 'vars.pins': item 0 is a boolean \(false\), "):
        run_vars([{"cmd": "echo before"}, {"block": block}], vars={"pins": [False]})
    assert sent.commands() == ["echo before"]


# -- P4-37..38: no solicit newline while a command is running ----------------

SILENT_7S = [
    {"cmd": "sleep 7; echo first", "register": "one", "timeout": "20s"},
    {"cmd": "echo second", "register": "two"},
    {"cmd": "echo third", "register": "three"},
]
SILENT_CHECKS = {"errors": {"errors": ["NOMATCH"]}, "rc": {}}


@pytest.mark.slow
@pytest.mark.parametrize("check", SILENT_CHECKS)
def test_p4_37_no_solicit_while_a_command_runs(sent: SentLog, check: str):
    """SPEC get_prompt step 3: a command silent for more than 5 s isn't answered with a solicit newline.

    The newline made the shell print a second prompt, so every later step captured the
    output of the command before it.
    """
    v = run_vars(SILENT_7S, **SILENT_CHECKS[check])
    assert (v["one"], v["two"], v["three"]) == ("first", "second", "third")
    assert "" not in sent.lines()


@pytest.mark.slow
def test_p4_37_assert_sees_its_own_command_after_a_silent_one(sent: SentLog):
    """SPEC get_prompt step 3: after a long silent command, `assert` checks its own command's output."""
    run_vars([SILENT_7S[0], {"cmd": "echo second", "assert": "^second$"}])
    assert "" not in sent.lines()


@pytest.mark.slow
@pytest.mark.parametrize("send", ["command", "line", "control", "nothing"])
def test_p4_38_solicit_only_for_a_wait_not_after_a_command(device, sent: SentLog, send: str):
    """SPEC get_prompt step 3: a wait after a raw send (`line`, `return`, `control`) or after nothing solicits."""
    r, _ = device([SHELL_PROMPT], "--silent", kick=False)
    if send == "command":
        r.session.sendline("x")
    elif send == "line":
        r.session.sendline("x", solicit=True)
    elif send == "control":
        r.session.sendcontrol("a")
    sent.clear()
    with pytest.raises(TimeoutError):
        r.session.get_prompt(timeout=6)
    assert sent.lines() == ([] if send == "command" else [""])
    # the timed-out wait consumed the command: the next wait follows no send, so it solicits
    sent.clear()
    with pytest.raises(TimeoutError):
        r.session.get_prompt(timeout=6)
    assert sent.lines() == [""]


WAITS_FOR_RETURN = "sh -c 'read x; echo CONNECTED'"  # like an idle console: silent until Return is pressed


@pytest.mark.slow
def test_p4_41_enter_an_idle_console_with_line(sent: SentLog):
    """SPEC "block", get_prompt: after `line`, the next `cmd` presses Return once and then runs."""
    v = run_vars([{"cmd": "true"}, {"line": WAITS_FOR_RETURN}, {"cmd": "echo in", "register": "out", "timeout": "12s"}])
    assert v["out"] == "in"
    assert sent.lines().count("") == 1


@pytest.mark.slow
def test_p4_41_cmd_that_waits_for_return_times_out():
    """SPEC "block", get_prompt: the same command sent with `cmd` gets no Return press and times out."""
    with pytest.raises(TimeoutError, match=r"^timed out after 7(\.0)?s waiting for a shell prompt \('sh'\)$"):
        run_vars([{"cmd": WAITS_FOR_RETURN, "timeout": "7s"}])


@pytest.mark.slow
def test_p4_40_solicit_after_a_timed_out_rc_check(device, sent: SentLog):
    """SPEC get_prompt: a `$?` check that times out counts like a timed-out wait; the next wait solicits."""
    r, _ = device([SHELL_PROMPT], "--silent", kick=False)
    with pytest.raises(TimeoutError, match=r"waiting for the exit code of the command \(echo \$\?\)$"):
        r.session.check_rc(timeout=1)
    assert sent.lines() == [RC_PROBE]
    sent.clear()
    with pytest.raises(TimeoutError):
        r.session.get_prompt(timeout=6)
    assert sent.lines() == [""]


# -- P4-42: `^` in a prompt regex is the start of a line -------------------------

RAW_DEVICE = """
import os, sys, termios, time
raw = "--cooked" not in sys.argv
attrs = termios.tcgetattr(0)
attrs[3] &= ~termios.ECHO          # no tty echo: the session reads exactly what is written here
if raw:
    attrs[1] &= ~termios.OPOST     # and no \\n -> \\r\\n on the way out
termios.tcsetattr(0, termios.TCSANOW, attrs)
chunks = [bytes.fromhex(c) for c in sys.argv[1].split(",")]
os.write(1, b"ready\\r\\nPROMPT$ ")
while os.read(0, 4096):
    for i, chunk in enumerate(chunks):
        if i:
            time.sleep(0.3)        # the next chunk arrives in a read of its own
        os.write(1, chunk)
"""

ANCHORED = r"^PROMPT\$ $"
UNANCHORED = r"PROMPT\$ $"


@pytest.fixture
def raw_device(tmp_path: Path) -> Iterator[Callable[..., Session]]:
    """Factory: a Session on a device that answers every line with the given chunks, byte for byte."""
    import sys

    path = tmp_path / "raw_device.py"
    path.write_text(RAW_DEVICE)
    sessions: list[Session] = []

    def factory(*chunks: str, regex: str = ANCHORED, cooked: bool = False) -> Session:
        s = Session([PromptHandler("sh", [regex], [], True)])
        sessions.append(s)
        spawn = f"{sys.executable} {path} {','.join(c.encode().hex() for c in chunks)}"
        s.attach(spawn + (" --cooked" if cooked else ""), env={"PATH": "/usr/bin:/bin"}, timeout=5)
        assert s.get_prompt(timeout=5) == "ready\n"
        s.sendline("x")
        return s

    yield factory
    for s in sessions:
        s.detach()


STRAY = {
    "cr": "\r",
    "nul": "\x00",
    "bel": "\x07",
    "nul-nul": "\x00\x00",
    "cr-nul-bel-cr": "\r\x00\x07\r",
}


@pytest.mark.parametrize("regex", [ANCHORED, UNANCHORED], ids=["anchored", "unanchored"])
@pytest.mark.parametrize("lead", list(STRAY.values()), ids=list(STRAY))
def test_p4_42_prompt_after_a_stray_control_character(raw_device: Callable[..., Session], lead: str, regex: str):
    """SPEC "Prompt Handling": a CR, NUL or BEL before a prompt is discarded, so `^` still finds the prompt.

    The anchored regex used to time out here. The unanchored one matched before and matches now,
    with the same captured output.
    """
    s = raw_device(f"out\r\n{lead}PROMPT$ ", regex=regex)
    assert s.get_prompt(timeout=3) == "out\n"
    assert s.ctx == {"before": "out\n", "match": "PROMPT$ "}


@pytest.mark.parametrize("regex", [ANCHORED, UNANCHORED], ids=["anchored", "unanchored"])
def test_p4_42_prompt_after_cr_cr_lf(raw_device: Callable[..., Session], regex: str):
    """`\\r\\r\\n` is a line break with a CR before it, as it always was: one empty line, then the prompt."""
    s = raw_device("out\r\n\r\r\nPROMPT$ ", regex=regex)
    assert s.get_prompt(timeout=3) == "out\n\n"


@pytest.mark.parametrize("regex", [ANCHORED, UNANCHORED], ids=["anchored", "unanchored"])
@pytest.mark.parametrize(
    "chunks",
    [("out\r", "\nPROMPT$ "), ("out\r\n\r", "\nPROMPT$ "), ("out", "\r", "\n", "PROMPT$ ")],
    ids=["cr|lf", "crlf-cr|lf", "byte-by-byte"],
)
def test_p4_42_crlf_split_across_two_reads(raw_device: Callable[..., Session], chunks: tuple[str, ...], regex: str):
    """A `\\r` whose `\\n` is still to come is not taken for a stray one: the pair stays a line break."""
    s = raw_device(*chunks, regex=regex)
    assert s.get_prompt(timeout=3) == "".join(chunks).replace("\r\n", "\n").removesuffix("PROMPT$ ")


@pytest.mark.parametrize("regex", [ANCHORED, UNANCHORED], ids=["anchored", "unanchored"])
def test_p4_42_stray_cr_in_a_read_of_its_own(raw_device: Callable[..., Session], regex: str):
    """A lone `\\r` at the end of a read waits for what follows; followed by the prompt, it is dropped."""
    s = raw_device("out\r\n\r", "PROMPT$ ", regex=regex)
    assert s.get_prompt(timeout=3) == "out\n"


@pytest.mark.parametrize("regex", [ANCHORED, UNANCHORED], ids=["anchored", "unanchored"])
@pytest.mark.parametrize(
    "line",
    ["10%\r50%\r100%", "ab\x00cd\x07ef", "a\r", "y\x00", "\x00", "\x07\x00", "tab\there"],
    ids=["progress", "nul-bel-inside", "cr-at-end", "nul-at-end", "only-nul", "only-bel-nul", "tab"],
)
def test_p4_42_control_characters_inside_a_line_are_captured(raw_device: Callable[..., Session], line: str, regex: str):
    """Only the start of the unread output is affected: a line that rewrites itself is captured as before.

    A run of them that makes up the whole line is kept as well, since a line break follows it.
    (No line reads as `x`, the line the device was sent: with no echo, that one would be taken for it.)
    (A `\\r` right before the line break was always dropped, as part of the break.)
    """
    s = raw_device(f"{line}\r\nPROMPT$ ", regex=regex)
    assert s.get_prompt(timeout=3) == line.rstrip("\r") + "\n"


@pytest.mark.parametrize("lead", ["\r", "\x00", "\x07\r"], ids=["cr", "nul", "bel-cr"])
def test_p4_42_stray_characters_at_the_start_of_an_output_line_are_dropped(raw_device: Callable[..., Session], lead: str):
    """The one change to captured output: these characters at the start of a line are no longer in it."""
    s = raw_device(f"one\r\n{lead}two\rthree\r\nPROMPT$ ")
    assert s.get_prompt(timeout=3) == "one\ntwo\rthree\n"


def test_p4_42_anchored_prompt_is_not_found_in_the_middle_of_a_line(raw_device: Callable[..., Session]):
    """`^` means what it says: text before the prompt on its line, also before a CR, is not a line start."""
    s = raw_device("out\r\nnot a PROMPT$ ")
    with pytest.raises(TimeoutError):
        s.get_prompt(timeout=1)
    s2 = raw_device("out\r\nredrawn\rPROMPT$ ")
    with pytest.raises(TimeoutError):
        s2.get_prompt(timeout=1)
    # the unanchored regex finds both, as before
    assert raw_device("out\r\nnot a PROMPT$ ", regex=UNANCHORED).get_prompt(timeout=3) == "out\n"
    assert raw_device("out\r\nredrawn\rPROMPT$ ", regex=UNANCHORED).get_prompt(timeout=3) == "out\n"


def test_p4_42_line_feed_through_a_pty_is_a_line_break(raw_device: Callable[..., Session]):
    """A device that prints `\\n` alone reaches the session as `\\r\\n` (the pty's ONLCR), so `^` holds.

    With output processing off, the bare `\\n` arrives; it is not a line break for the engine, so the
    anchored regex doesn't match and the unanchored one captures nothing (no change here).
    """
    assert raw_device("out\nPROMPT$ ", cooked=True).get_prompt(timeout=3) == "out\n"
    with pytest.raises(TimeoutError):
        raw_device("out\nPROMPT$ ").get_prompt(timeout=1)
    assert raw_device("out\nPROMPT$ ", regex=UNANCHORED).get_prompt(timeout=3) == "out\n"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("PROMPT$ ", True),
        ("\rPROMPT$ ", True),
        ("\x00\x07PROMPT$ ", True),
        ("\r\nPROMPT$ ", True),
        ("\r\r\nPROMPT$ ", True),
        ("\x1b[0m\rPROMPT$ ", True),
        ("\r\x1b[0mPROMPT$ ", True),
        ("x\rPROMPT$ ", False),
        ("x PROMPT$ ", False),
        ("\r", False),
        ("", False),
    ],
    ids=repr,
)
def test_p4_42_prompt_swap_reads_the_prompt_the_same_way(text: str, expected: bool):
    """`_is_shell_prompt` (a block's prompt swap) discards what `get_prompt` discards."""
    s = Session([PromptHandler("sh", [ANCHORED], [], True)])
    assert s._is_shell_prompt(text) is expected


def test_p4_42_prompt_regex_wins_a_tie_with_the_stray_characters():
    """A prompt regex that itself starts at a leading CR still matches there (ties go to the prompts)."""
    s = Session([PromptHandler("login", [r"\rlogin: $"], [["x"]], False), PromptHandler("sh", [r"^\$ $"], [], True)])
    assert s._is_shell_prompt("\rlogin: ") is False  # the login prompt, not a shell prompt
    assert s._is_shell_prompt("\r$ ") is True


# -- P4-44: a prompt written again inside the echo of the line that was sent (the device is sent `x`) --


@pytest.mark.parametrize("regex", [r"^PROMPT\$ ", r"PROMPT\$ "], ids=["anchored", "unanchored"])
@pytest.mark.parametrize(
    "chunks",
    [
        # readline, single-byte locale, a line that ends at the margin: blank, return, cursor-up, padding
        ("x \r\x1b[A\x00\x00PROMPT$ \x1b[K\x00x\r\nout\r\nPROMPT$ ",),
        ("x \r\x1b[APROMPT$ ", "x\r\nout\r\nPROMPT$ "),
        ("x \r\x1b[APROMPT$ ", "\rPROMPT$ ", "x\r\nout\r\nPROMPT$ "),
    ],
    ids=["one-read", "two-reads", "written-twice"],
)
def test_p4_44_prompt_inside_the_echo_does_not_end_the_wait(raw_device: Callable[..., Session], chunks, regex: str):
    """The wait goes on to the prompt after the output, and the echo is removed from what it captured."""
    s = raw_device(*chunks, regex=regex)
    assert s.get_prompt(timeout=3) == "out\n"
    assert s.ctx == {"before": "out\n", "match": "PROMPT$ "}


def test_p4_44_prompt_after_the_line_break_of_the_echo_ends_the_wait(raw_device: Callable[..., Session]):
    """The echo is complete at its line break: the prompt after it is the prompt, at once."""
    s = raw_device("x", "\r\nPROMPT$ ", "x\r\nout\r\nPROMPT$ ")
    started = time.monotonic()
    assert s.get_prompt(timeout=20) == ""
    assert time.monotonic() - started < 2


QUICK = 0.6  # a wait that isn't held ends well within this
GRACE = (HELD_GRACE - 0.2, HELD_GRACE + 1.0)  # one that is held and accepted ends one poll late


@pytest.mark.parametrize(
    ("chunks", "regex"),
    [
        (("xPROMPT$ ",), r"PROMPT\$ $"),
        # what the regex leaves unmatched stays unread: a blank, the tail of a longer prompt
        (("xPROMPT$ ",), r"PROMPT\$"),
        (("xPROMPT$ > ",), r"PROMPT\$ "),
        # a stray character that is dropped only when something follows it, and the start of a sequence
        (("xPROMPT$ \r",), r"PROMPT\$ "),
        (("xPROMPT$ \x00",), r"PROMPT\$ "),
        (("xPROMPT$ \x07",), r"PROMPT\$ "),
        (("xPROMPT$ \x1b[",), r"PROMPT\$ "),
        # a sequence after it is consumed: the hold goes on, and ends one poll after it
        (("xPROMPT$ \x1b[K",), r"PROMPT\$ "),
    ],
    ids=["all-matched", "blank", "tail", "cr", "nul", "bel", "partial-escape", "escape"],
)
def test_p4_44_held_prompt_is_the_prompt_when_nothing_comes(
    raw_device: Callable[..., Session], sent: SentLog, chunks: tuple[str, ...], regex: str
):
    """A device that echoes the line and prints its prompt on the same line: the prompt is held for one
    written again, and taken for the prompt after one poll in which nothing arrives. No Return is pressed
    for it, also by a wait that may solicit, and what is unread stays unread."""
    s = raw_device(*chunks, regex=regex)
    started = time.monotonic()
    assert s.get_prompt(timeout=20) == ""
    assert GRACE[0] < time.monotonic() - started < GRACE[1]
    assert s.ctx["match"].startswith("PROMPT$")
    assert s._at_prompt
    # a wait that follows no command, as after a `line`
    s.sendline("x", solicit=True)
    started = time.monotonic()
    assert s.get_prompt(timeout=20) == ""
    # what the first wait left unread comes first now; where it shows something, this is no echo of `x`
    held = not "".join(chunks).endswith(("> ", "\x1b["))
    assert (GRACE[0] if held else 0) < time.monotonic() - started < (GRACE[1] if held else QUICK)
    assert sent.lines() == ["x", "x"]


def test_p4_44_held_prompt_is_taken_at_a_timeout_shorter_than_the_poll(raw_device: Callable[..., Session]):
    s = raw_device("xPROMPT$ ", regex=UNANCHORED)
    started = time.monotonic()
    assert s.get_prompt(timeout=0.4) == ""
    assert 0.3 < time.monotonic() - started < 0.4 + QUICK


def test_p4_44_what_arrives_in_pieces_keeps_the_prompt_held(raw_device: Callable[..., Session]):
    """The rest of the echo comes in two pieces, the second 1.5 s after the first: the poll in which the
    first arrived was not a quiet one, so the prompt is still held when the second comes."""
    s = raw_device("x \r\x1b[APROMPT$ ", "x", "", "", "", "", "\r\nout\r\nPROMPT$ ", regex=r"^PROMPT\$ ")
    assert s.get_prompt(timeout=20) == "out\n"


def test_p4_44_blank_that_arrives_later_is_waited_out(raw_device: Callable[..., Session], sent: SentLog):
    """The unmatched blank comes 0.3 s after the prompt: that poll isn't quiet, the next one is. No Return
    is pressed after the poll that wasn't quiet either, by a wait that may solicit."""
    s = raw_device("xPROMPT$", " ", regex=r"PROMPT\$")
    for solicit in (False, True):
        if solicit:
            s.sendline("x", solicit=True)
        started = time.monotonic()
        assert s.get_prompt(timeout=20) == ""
        assert 2 * HELD_GRACE - 0.2 < time.monotonic() - started < 2 * HELD_GRACE + 1.0
    assert sent.lines() == ["x", "x"]


def test_p4_44_hold_ends_when_the_echo_goes_on(raw_device: Callable[..., Session]):
    """After the echo's line break the prompt is no longer held: a command that then prints nothing for
    longer than the grace period is waited for, to its prompt."""
    s = raw_device("x \r\x1b[APROMPT$ ", "x\r\nout\r\n", "", "", "", "", "", "", "PROMPT$ ", regex=r"^PROMPT\$ ")
    started = time.monotonic()
    assert s.get_prompt(timeout=20) == "out\n"
    assert time.monotonic() - started > 2.0
    assert s._cld is not None and not s._cld.buffer


def test_p4_44_redraw_without_a_return_of_the_devices(raw_device: Callable[..., Session]):
    """The prompt and the line again with no `\\r` before them: the held prompt counts as the return,
    so the line after it is read as written again and the echo is removed."""
    s = raw_device("xPROMPT$ x\r\nout\r\nPROMPT$ ", regex=r"PROMPT\$ ")
    assert s.get_prompt(timeout=3) == "out\n"
    assert s.ctx["before"] == "out\n"


def test_p4_44_accepted_prompt_is_not_held_again(raw_device: Callable[..., Session], sent: SentLog):
    """The line is judged once: a wait after a Return, which shows nothing, ends at the prompt at once."""
    s = raw_device("xPROMPT$ ", regex=UNANCHORED)
    assert s.get_prompt(timeout=20) == ""
    s.sendline("", solicit=True)
    started = time.monotonic()
    assert s.get_prompt(timeout=20) == ""
    assert time.monotonic() - started < QUICK
    assert sent.lines() == ["x", ""]


# -- an `after` wait that reads a prompt the prompt wait would hold


def test_p4_44_after_that_ends_at_a_held_prompt_hands_it_to_the_prompt_wait(raw_device: Callable[..., Session]):
    """The same-line device: the `after` wait doesn't put the session at a prompt, and the prompt wait
    that follows takes the prompt after one quiet poll, without reading or sending anything."""
    s = raw_device("xPROMPT$ ", regex=UNANCHORED)
    s.expect([r"PROMPT\$ $"], timeout=3)
    assert not s._at_prompt
    assert s._held == ("x\r", "xPROMPT$ ", "PROMPT$ ")
    started = time.monotonic()
    assert s.get_prompt(timeout=20, capture=False, solicit=False) == ""
    assert GRACE[0] < time.monotonic() - started < GRACE[1]
    assert s._at_prompt and s._held is None


def test_p4_44_after_that_ends_at_a_redrawn_prompt_reads_on(raw_device: Callable[..., Session]):
    s = raw_device("x \r\x1b[APROMPT$ ", "x\r\nout\r\nPROMPT$ ", regex=r"^PROMPT\$ $")
    s.expect([r"PROMPT\$ $"], timeout=3)
    assert not s._at_prompt and s._held is not None
    assert s.get_prompt(timeout=3) == "out\n"


@pytest.mark.parametrize("send", ["line", "control"])
def test_p4_44_prompt_handed_on_by_after_is_dropped_by_a_send(raw_device: Callable[..., Session], send: str):
    s = raw_device("xPROMPT$ ", regex=UNANCHORED)
    s.expect([r"PROMPT\$ $"], timeout=3)
    assert s._held is not None
    s.sendline("y") if send == "line" else s.sendcontrol("a")
    assert s._held is None


def raw_spawn(tmp_path: Path, *chunks: str) -> str:
    import sys

    path = tmp_path / "raw_device.py"
    path.write_text(RAW_DEVICE)
    return f"{sys.executable} {path} {','.join(c.encode().hex() for c in chunks)}"


def test_p4_44_cmd_with_after_on_a_device_that_prompts_on_the_echo_line(tmp_path: Path, sent: SentLog):
    """`line: x`, then a `cmd` with `after` that matches the prompt: the command is sent one poll later,
    with no Return pressed and no timeout."""
    started = time.monotonic()
    run_script(
        [{"cmd": "true"}, {"line": "x"}, {"cmd": "echo hi", "after": r"xPROMPT\$ $", "register": "out"}],
        spawn=raw_spawn(tmp_path, "xPROMPT$ "),
        prompts=[{"name": "sh", "expect": [r"PROMPT\$ $"], "return": True}],
        errors=["NOPE"],
    )
    assert sent.lines() == ["true", "x", "echo hi"]
    assert GRACE[0] < time.monotonic() - started < GRACE[1] + 1


def test_p4_44_cmd_with_after_at_a_redrawn_prompt_waits_for_the_real_one(tmp_path: Path, sent: SentLog):
    """The device writes the prompt again inside every echo: an `after` that matches there is followed by
    a prompt wait that reads the rest of the echo and the output, and only then is the command sent."""
    out = run_script(
        [
            {"cmd": "x", "register": "first"},
            {"line": "x"},
            {"cmd": "x", "after": r"PROMPT\$ $", "register": "second"},
        ],
        spawn=raw_spawn(tmp_path, "x \r\x1b[APROMPT$ ", "x\r\nout\r\nPROMPT$ "),
        prompts=[{"name": "sh", "expect": [r"^PROMPT\$ $"], "return": True}],
        errors=["NOPE"],
    ).config.vars
    assert out == {"first": "out", "second": "out"}
    assert sent.lines() == ["x", "x", "x"]


def test_p4_44_output_without_an_echo_is_not_held(raw_device: Callable[..., Session]):
    """No echo, and output that is not the start of the line: the prompt ends the wait at once."""
    started = time.monotonic()
    assert raw_device("yPROMPT$ ", regex=UNANCHORED).get_prompt(timeout=20) == ""
    assert raw_device("x^CPROMPT$ ", regex=UNANCHORED).get_prompt(timeout=20) == ""
    assert raw_device("\x1b[K\rPROMPT$ ").get_prompt(timeout=20) == ""
    assert raw_device("PROMPT$ ").get_prompt(timeout=20) == ""
    assert time.monotonic() - started < 3


def test_p4_44_is_shell_prompt_reads_as_the_wait_does():
    s = Session([PromptHandler("sh", [r"PROMPT\$ "], [], True)])
    assert s._is_shell_prompt("x \rPROMPT$ ", whole=True)
    assert not s._is_shell_prompt("x \rPROMPT$ ", whole=True, sent="x")
    assert not s._is_shell_prompt("ec\rPROMPT$ ", whole=True, sent="echo hi")
    assert s._is_shell_prompt("x \rPROMPT$ x\r\nPROMPT$ ", whole=True, sent="x")
    assert s._is_shell_prompt("y\rPROMPT$ ", whole=True, sent="x")
    assert s._is_shell_prompt("PROMPT$ ", whole=True, sent="x")
    assert s._is_shell_prompt("x\r\nPROMPT$ ", whole=True, sent="x")


@pytest.mark.parametrize(
    ("read", "sent", "expected"),
    [
        ("echo hi", "echo hi", True),
        ("echo hi \r", "echo hi", True),
        ("ec", "echo hi", True),
        ("echo h\rho h", "echo hi", True),
        (" \x00e\x07", "echo hi", True),
        # nothing of the line yet, or something else
        ("", "echo hi", False),
        (" \r\x00", "echo hi", False),
        ("cho", "echo hi", False),
        ("echo hi!", "echo hi", False),
        ("echo ho", "echo hi", False),
        # a line break came: the echo is over
        ("echo hi\n", "echo hi", False),
        ("\nec", "echo hi", False),
        # nothing was sent that shows
        ("x", "", False),
        ("x", None, False),
        ("", " \x07", False),
    ],
)
def test_p4_44_mid_echo(read: str, sent: str | None, expected: bool):
    """A prompt is held only while what was read since the send shows the start of the sent line."""
    assert _mid_echo(read, sent) is expected
