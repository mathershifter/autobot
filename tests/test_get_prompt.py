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
    SHELL_ENV,
    SHELL_PROMPT,
    FakeDevice,
    SentLog,
    make_runner,
)

from autobot.runner import Runner
from autobot.session import Session

LOGIN_FLAT = {"name": "login", "expect": ["login:", "Password:"], "send": ["admin", "secret"]}


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
        {"name": "sh", "expect": [r"PROMPT\$ "], "return": True, "send": ["x"]},
        {"name": "sh", "expect": [r"PROMPT\$ "]},
    ],
    ids=["return-true-with-send", "no-send"],
)
def test_p4_02_shell_prompt_forms(device, sent: SentLog, prompt: dict[str, Any]):
    """SPEC.md:38, 341: return: true, or no send, marks a shell prompt."""
    r, log = device([prompt], "--wait-enter", "--order", "none", "--then", "prompt")
    sent.clear()
    r.session.get_prompt(timeout=5)
    assert sent.lines() == []
    assert log_of(log) == ["ENTER="]


def test_p4_03_empty_send_raises_no_response(device):
    """SPEC.md:341-342: a prompt with an empty send list has no response."""
    login = {"name": "login", "expect": ["login:"], "send": []}
    r, _ = device([SHELL_PROMPT, login], "--wait-enter")
    with pytest.raises(RuntimeError, match="no response available"):
        r.session.get_prompt(timeout=5)


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
    login = {"name": "login", "expect": ["login:"], "send": ["admin"]}
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
    with pytest.raises(TimeoutError, match="timed out waiting for prompt"):
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


def test_p4_10_flat_send_list_login_then_password(device):
    """SPEC.md:39-40: a flat send list answers prompts in order."""
    r, log = device(
        [SHELL_PROMPT, LOGIN_FLAT], "--wait-enter", "--order", "login,password",
        "--accept", "admin:secret",
    )
    r.session.get_prompt(timeout=10)
    assert log_of(log) == ["ENTER=", "LOGIN=admin", "PASSWORD=secret"]


def test_p4_11_list_of_lists_credential_cycling(device):
    """SPEC.md:41: grouped attempts are tried in turn."""
    login = {"name": "login", "expect": ["login:", "Password:"],
             "send": [["admin", "pass1"], ["admin", "pass2"]]}
    r, log = device([SHELL_PROMPT, login], "--wait-enter", "--accept", "admin:pass2")
    r.session.get_prompt(timeout=10)
    assert log_of(log) == [
        "ENTER=", "LOGIN=admin", "PASSWORD=pass1", "LOGIN=admin", "PASSWORD=pass2",
    ]


def test_p4_12_send_each_with_fields(device):
    """SPEC.md:44-53: sendEach emits the named fields of each item in order."""
    login = {"name": "login", "expect": ["login:", "Password:"],
             "send": {"each": "vars.creds", "fields": ["username", "password"]}}
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


def test_p4_14_responses_exhausted(device):
    """SPEC.md:342: running out of responses raises."""
    login = {"name": "login", "expect": ["login:", "Password:"], "send": ["admin", "bad"]}
    r, _ = device([SHELL_PROMPT, login], "--wait-enter")
    with pytest.raises(RuntimeError, match="prompt 'login': responses exhausted"):
        r.session.get_prompt(timeout=10)


def test_p4_15_handlers_reset_per_get_prompt(device):
    """SPEC.md:342: responses start over on every get_prompt call."""
    r, log = device([SHELL_PROMPT, LOGIN_FLAT], "--wait-enter", "--repeat", "2",
                    "--accept", "admin:secret", "--then", "prompt")
    r.session.get_prompt(timeout=10)
    r.session.sendline("again")
    r.session.get_prompt(timeout=10)
    assert log_of(log).count("LOGIN=admin") == 2
    assert "LINE=again" in log_of(log)


def test_p4_16_grouped_expect_login_first(device):
    """SPEC.md:37, README:147: grouped expect on a login-first device."""
    login = {"name": "login", "expect": [["login:", "Password:"]], "send": ["admin", "secret"]}
    r, log = device([SHELL_PROMPT, login], "--wait-enter", "--order", "login,password",
                    "--accept", "admin:secret")
    r.session.get_prompt(timeout=10)
    assert log_of(log) == ["ENTER=", "LOGIN=admin", "PASSWORD=secret"]


@pytest.mark.slow
def test_p4_18_login_prompt_in_same_chunk_as_banner(device, sent: SentLog):
    """SPEC.md:81, 341: a login prompt that arrives with the banner is answered."""
    r, log = device([SHELL_PROMPT, LOGIN_FLAT], "--same-chunk", "--order", "login,password",
                    "--accept", "admin:secret", kick=False)
    try:
        r.session.get_prompt(timeout=12)
    except RuntimeError:
        pass  # exhausted after the wrong mapping; the log shows why
    assert log_of(log)[:2] == ["LOGIN=admin", "PASSWORD=secret"]
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


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="finding #7")
def test_p4_20_send_template_rendered_at_send_time(fake_device: FakeDevice):
    """SPEC.md:317, 322: send templates see vars registered by earlier steps."""
    spawn, log = fake_device("--order", "login,password", "--accept", "admin:secret")
    login = {"name": "login", "expect": ["login:", "Password:"],
             "send": ["{{ vars.user }}", "secret"]}
    try:
        runner = make_runner(
            [
                {"cmd": "echo admin", "register": "user"},
                {"line": spawn},
                {"cmd": "true"},
            ],
            prompts=[SHELL_PROMPT, login],
        )
    except ValueError as e:
        raise AssertionError(f"send was rendered before the script ran: {e}") from e
    runner.run()
    assert "LOGIN=admin" in FakeDevice.read(log)
