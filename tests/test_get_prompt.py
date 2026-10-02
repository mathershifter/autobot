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
from conftest import (
    BASH,
    SHELL_ENV,
    SHELL_PROMPT,
    FakeDevice,
    SentLog,
    make_runner,
)

from autobot.runner import Runner
from autobot.session import PromptHandler, Session

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
    """SPEC sendEach: without fields, a string, number or boolean item is sent as a string."""
    pin = {"name": "pin", "expect": ["Password:"], "send": {"each": "vars.pins"}}
    r, log = device([SHELL_PROMPT, pin], "--wait-enter", "--order", "password",
                    "--accept", ":True", vars={"pins": ["1111", 2.5, True]})
    r.session.get_prompt(timeout=10)
    assert log_of(log) == ["ENTER=", "PASSWORD=1111", "PASSWORD=2.5", "PASSWORD=True"]


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
    with pytest.raises(ValueError, match=r"^template error: "):
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


@pytest.mark.parametrize(
    ("handlers", "names"),
    [
        ([PromptHandler("sh", [r"PROMPT\$ "], [], True), QUESTION, PromptHandler("root", ["# "], [], True)],
         "('sh', 'root')"),
        ([QUESTION], "(none defined)"),
    ],
    ids=["names", "none"],
)
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
