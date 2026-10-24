"""P8-61: the `raw` step and `session.send` (SPEC "raw — Send keystrokes (no Return, no prompt wait)").

A `raw` step types a list of pieces with no Return and no prompt wait. A piece of exactly `^` and one
more character is a control key (the same table as `control`); every other piece is text, rendered like a
`line` and sent verbatim. Pieces go out back to back with no settle; the settle applies to a following
line only when the last piece was a control key. `session.send(text)` is the plugin seam.
"""

from __future__ import annotations

import math
import os
import time
from typing import Any

import pydantic
import pytest
from conftest import run_script, steps

from autobot import protocols
from autobot.models import Config
from autobot.runner import Runner
from autobot.session import Session


def _recorder(s: Session, monkeypatch: pytest.MonkeyPatch) -> list[tuple[float, bytes]]:
    """Record every write to the session's pty as (time, bytes)."""
    assert s._cld is not None
    fd, write, times = s._cld.child_fd, os.write, []

    def os_write(target: int, data: Any) -> int:
        if target == fd:
            times.append((time.monotonic(), bytes(data)))
        return write(target, data)

    monkeypatch.setattr(os, "write", os_write)
    return times


# -- the bytes sent and their order -------------------------------------------------------------------


def test_p8_61_pieces_are_sent_in_order_with_no_line_break(attached_runner, monkeypatch):
    """SPEC "raw": each piece goes out verbatim, a control key as its byte and text as itself, in order,
    with no Return appended anywhere."""
    r: Runner = attached_runner()
    times = _recorder(r.session, monkeypatch)
    r.run_steps(steps([{"raw": ["^a", "b", "^[", ":wq"]}]))
    assert [data for _, data in times] == [b"\x01", b"b", b"\x1b", b":wq"]


def test_p8_61_text_is_sent_verbatim_with_no_line_break(shell_session: Session, monkeypatch):
    """`session.send` sends the text as it is, no line break, and ends the at-a-prompt state as a control
    does: the far side is no longer at a known prompt."""
    s = shell_session
    s.get_prompt(timeout=5)
    times = _recorder(s, monkeypatch)
    s.send("abc", timeout=5)
    assert [data for _, data in times] == [b"abc"]
    assert s._prompt.at_prompt is False and s._prompt.held is None and s._send.solicit is True


def test_p8_61_caret_forms_are_text(attached_runner, monkeypatch):
    """SPEC "raw": only a piece of exactly `^` and one control key is a control key; `^`, `^abc` and `a^b`
    are text, sent as written."""
    r: Runner = attached_runner()
    times = _recorder(r.session, monkeypatch)
    r.run_steps(steps([{"raw": ["^", "^abc", "a^b", "^^"]}]))
    # `^^` is exactly two characters and `^` is a control key (Ctrl+^, RS = 0x1e)
    assert [data for _, data in times] == [b"^", b"^abc", b"a^b", b"\x1e"]


def test_p8_61_text_piece_is_rendered_like_a_line(attached_runner, monkeypatch):
    """SPEC "raw": a text piece is a Jinja2 template; a rendered `^a` is still text (classified as written)."""
    r: Runner = attached_runner(vars={"x": "^a"})
    times = _recorder(r.session, monkeypatch)
    r.run_steps(steps([{"raw": ["{{ vars.x }}"]}]))
    assert [data for _, data in times] == [b"^a"]


# -- no settle between pieces; settle after a trailing control key --------------------------------------


def test_p8_61_no_settle_between_pieces(attached_runner, monkeypatch):
    """SPEC "raw": pieces go out at once, even a text piece right after a control key."""
    from autobot import session as session_mod

    r: Runner = attached_runner()
    times = _recorder(r.session, monkeypatch)
    r.run_steps(steps([{"raw": ["^a", "b"]}]))
    (t0, _), (t1, _) = times
    assert t1 - t0 < session_mod.CONTROL_SETTLE  # no half-second pause between the control key and the text


def test_p8_61_trailing_control_key_settles_the_next_line(shell_session: Session, monkeypatch):
    """SPEC "raw"/"control": when the last piece is a control key, the next line waits out the settle;
    when the last piece is text, it does not."""
    from autobot import session as session_mod

    s = shell_session
    s.get_prompt(timeout=5)
    times = _recorder(s, monkeypatch)
    # last piece text: control_at is cleared, so the following line is sent at once
    s.send("x", timeout=5)
    s.sendline("echo one", solicit=True, timeout=5)
    assert s._send.control_at == -math.inf
    (text_at, _), (line1_at, _) = times[0], times[1]
    assert line1_at - text_at < session_mod.CONTROL_SETTLE
    # last piece a control key: control_at is set, so the following line waits out the settle
    times.clear()
    s.sendcontrol("u", timeout=5)
    s.sendline("echo two", solicit=True, timeout=5)
    (ctrl_at, _), (line2_at, _) = times[0], times[1]
    assert line2_at - ctrl_at >= session_mod.CONTROL_SETTLE


# -- validation ---------------------------------------------------------------------------------------


def _doc(step: dict[str, Any]) -> dict[str, Any]:
    return {"autobot": "2026-10", "attach": {"spawn": "ssh h"}, "script": [step]}


def _errors(step: dict[str, Any]) -> list[dict[str, Any]]:
    with pytest.raises(pydantic.ValidationError) as ei:
        Config.model_validate(_doc(step))
    return ei.value.errors()


@pytest.mark.parametrize("key", ["^1", "^ ", "^\n", "^é"])
def test_p8_61_bad_control_key_is_a_validation_error(key: str):
    """SPEC "raw": a `^`-and-one-character piece whose character is no control key is a `control_char` error
    at the step's `raw`, with the message of the `control` step."""
    [err] = _errors({"raw": [key]})
    assert (err["loc"], err["type"]) == (("script", 0, "raw", "raw"), "control_char")
    assert err["msg"] == f"a control value is one character, a letter or one of @ ` [ {{ \\ | ] }} ^ ~ _ ?, got '{key[1]}'"


@pytest.mark.parametrize("value", [5, None, {"a": 1}, True])
def test_p8_61_non_string_piece_is_a_validation_error(value: Any):
    """SPEC "raw": a piece that isn't a string is a validation error at the item."""
    [err] = _errors({"raw": ["ok", value]})
    assert err["loc"] == ("script", 0, "raw", "raw", 1)
    assert err["type"] == "string_type"


def test_p8_61_raw_as_a_non_list_is_an_error():
    """SPEC "raw": the value is a list of pieces; a null or a bare string is a type error at the field."""
    assert _errors({"raw": None})[0]["type"] == "list_type"
    assert _errors({"raw": "x"})[0]["type"] == "list_type"


@pytest.mark.parametrize("value", [[], ["^a", "b"], ["^[", ":wq"], ["", "^^", "a^b", "^abc", "^"]])
def test_p8_61_accepted_raw_values(value: list[str]):
    """SPEC "raw": an empty list and lists of control keys and text are accepted."""
    Config.model_validate(_doc({"raw": value}))


# -- sends nothing cases ------------------------------------------------------------------------------


def test_p8_61_empty_list_sends_nothing(attached_runner, monkeypatch):
    """SPEC "raw": an empty list is accepted and sends nothing."""
    r: Runner = attached_runner()
    times = _recorder(r.session, monkeypatch)
    r.run_steps(steps([{"raw": []}]))
    assert times == []


def test_p8_61_empty_string_piece_sends_nothing(shell_session: Session, monkeypatch):
    """SPEC "raw": an empty string piece sends nothing."""
    s = shell_session
    s.get_prompt(timeout=5)
    times = _recorder(s, monkeypatch)
    s.send("", timeout=5)
    assert times == []


# -- the partial-line flag is left alone --------------------------------------------------------------


def test_p8_61_raw_leaves_the_partial_line_flag_alone(shell_session: Session, monkeypatch):
    """SPEC "raw": `send` leaves the partial-line flag as it is and never raises `PartialLine`; what was
    typed is the author's to finish. (A `line` or the `$?` check would be refused here.)"""
    s = shell_session
    s.get_prompt(timeout=5)
    s._send.partial = True  # as a send that failed partway leaves it
    times = _recorder(s, monkeypatch)
    s.send("more", timeout=5)  # no PartialLine
    assert [data for _, data in times] == [b"more"]
    assert s._send.partial is True
    s.sendcontrol("c", timeout=5)  # clear it for the fixture's teardown
    s.get_prompt(timeout=5)


# -- the plugin seam ----------------------------------------------------------------------------------


def test_p8_61_send_is_a_session_like_member_for_plugins():
    """SPEC "raw"/Plugins: `session.send` is part of the `SessionLike` protocol, with the session's own
    signature, so a plugin may send keystrokes of its own."""
    assert isinstance(Session([]), protocols.SessionLike)
    assert hasattr(protocols.SessionLike, "send")


def test_p8_61_raw_runs_through_the_executor_and_reports(capsys):
    """SPEC "raw"/Output: the step reports one line per piece, `raw sent: ^<C>` for a control key and
    `raw sent` for text (never the text)."""
    out = run_script([{"raw": ["^a", "b"]}], attach_script=[])
    assert isinstance(out, Runner)
    err = capsys.readouterr().err
    assert ">> raw sent: ^A" in err and ">> raw sent\n" in err
