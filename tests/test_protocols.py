"""P7-27, P7-28: the protocols between the runner, the session and the steps (SPEC "Common Step Properties",
the `ctx` of a plugin)."""

from __future__ import annotations

import inspect
import re

import pytest
from conftest import Timeline, handler_names, make_runner, run_script

from autobot import protocols
from autobot.errors import ScriptError
from autobot.models import Prompt
from autobot.runner import Runner
from autobot.session import PatternTable, PromptHandler, Session

SESSION_LIKE = {"ctx", "posix", "get_prompt", "sendline", "sendcontrol", "send", "check_rc", "sleep"}


def _members(proto: type) -> set[str]:
    return {name for name in vars(proto) if not name.startswith("_")}


def test_p7_27_session_conforms_to_session_like():
    """`Session` is a `SessionLike` by shape, with no inheritance: every member of the protocol is on it, and
    a runner's session is one."""
    assert protocols.SessionLike not in Session.__mro__
    assert isinstance(Session([]), protocols.SessionLike)
    assert isinstance(make_runner([]).session, protocols.SessionLike)
    assert _members(protocols.SessionLike) == SESSION_LIKE


def test_p7_27_session_like_methods_have_the_sessions_signatures():
    """A plugin written against the protocol calls `Session` as it is: each method has the same parameters."""
    for name in SESSION_LIKE - {"ctx", "posix"}:
        proto, real = getattr(protocols.SessionLike, name), getattr(Session, name)
        assert inspect.signature(proto).parameters.keys() == inspect.signature(real).parameters.keys(), name
        for p, q in zip(inspect.signature(proto).parameters.values(), inspect.signature(real).parameters.values()):
            assert (p.kind, p.default) == (q.kind, q.default), (name, p.name)
    for name in ("ctx", "posix"):
        assert isinstance(inspect.getattr_static(Session, name), property), name


def test_p7_28_runner_conforms_to_runner_context():
    """`Runner` is a `RunnerContext` by shape: every member of the protocol is on it."""
    runner: protocols.RunnerContext = make_runner([])
    assert isinstance(runner, Runner)
    for name in _members(protocols.RunnerContext):
        assert hasattr(runner, name), name


def test_p7_27_steps_use_only_the_protocols_members():
    """`steps.py` reaches the session through `ctx.session` with the protocol's members and nothing else,
    and imports nothing from `autobot.session`."""
    from pathlib import Path

    import autobot.steps

    source = Path(autobot.steps.__file__).read_text()
    used = set(re.findall(r"ctx\.session\.([a-z_]+)", source))
    assert used <= SESSION_LIKE, used - SESSION_LIKE
    assert "from .session import" not in source and "import session" not in source


# -- P7-28: ctx.prompts swaps the session's prompts and restores them -----------

TOP = [{"name": "top", "expect": [r"PROMPT\$ "], "return": True}]
BLK = [Prompt.model_validate({"name": "blk", "expect": [r"BLK\$ "], "return": True})]


def test_p7_28_prompts_swaps_for_the_body_and_restores_on_exit(timeline: Timeline):
    """SPEC block (lifecycle steps 1 and 5): the handlers are the block's inside `with ctx.prompts(...)` and
    the previous ones again after it, through the session's `restore_handlers`, twice."""
    r = make_runner([], prompts=TOP)
    with r.prompts(BLK):
        assert handler_names(r) == ["blk"]
    assert handler_names(r) == ["top"]
    assert [e for e in timeline if e[0] == "restore_handlers"] == [("restore_handlers", ["blk"]), ("restore_handlers", ["top"])]


def test_p7_28_prompts_restores_on_an_exception():
    """The restore is in a `finally`: an error in the body leaves the previous handlers in place, and goes on."""
    r = make_runner([], prompts=TOP)
    with pytest.raises(ZeroDivisionError):
        with r.prompts(BLK):
            assert handler_names(r) == ["blk"]
            1 / 0
    assert handler_names(r) == ["top"]
    with pytest.raises(KeyboardInterrupt):
        with r.prompts(BLK):
            raise KeyboardInterrupt
    assert handler_names(r) == ["top"]


def test_p7_28_prompts_that_cannot_be_built_swap_nothing(timeline: Timeline):
    """SPEC sendEach: a block's collection is resolved on entry; one that can't be is a `ScriptError` before
    anything is swapped, so there is nothing to restore."""
    r = make_runner([], prompts=TOP, vars={"creds": "not a list"})
    login = Prompt.model_validate({"name": "login", "expect": ["login:"], "send": {"each": "vars.creds"}})
    with pytest.raises(ScriptError, match=r"^prompt 'login': sendEach 'vars.creds': 'vars.creds' is a string, not a list$"):
        with r.prompts([*BLK, login]):
            raise AssertionError("the body must not run")
    assert handler_names(r) == ["top"]
    assert "restore_handlers" not in timeline.names()


def test_p7_28_prompts_builds_from_the_runs_vars():
    """The handlers are built when the body is entered, from `vars` as a `set` or `register` left it."""
    r = make_runner([], prompts=TOP, vars={"creds": []})
    login = Prompt.model_validate(
        {"name": "login", "send": {"each": "vars.creds", "fields": [{"match": "login:", "field": "user"}]}}
    )
    r.vars["creds"] = [{"user": "admin"}]
    with r.prompts([login]):
        [h] = r.session.save_handlers()
        assert (h.name, h.respond(0)) == ("login", "admin")


def test_p7_28_block_swaps_through_ctx_prompts(monkeypatch: pytest.MonkeyPatch):
    """`BlockExecutor` uses `ctx.prompts` for a block with `prompts`, and nothing of the kind for one without."""
    calls: list[list[str]] = []
    orig = Runner.prompts

    def prompts(self, prompts):
        calls.append([p.name for p in prompts])
        return orig(self, prompts)

    monkeypatch.setattr(Runner, "prompts", prompts)
    run_script([{"block": {"name": "a", "prompts": TOP, "script": []}}, {"block": {"name": "b", "script": []}}])
    assert calls == [["top"]]


# -- P7-28: the session keeps its own slice table ------------------------------


def test_p7_28_pattern_table_keeps_the_slices_and_leaves_the_handlers_alone():
    """`PatternTable` maps a pexpect index to its handler and the regex's index among the handler's own; the
    handlers get no `start`/`end` written to them, so one handler can be in two sessions' tables at once."""
    a = PromptHandler("a", [r"A1", r"A2"], [["x", "y"]], False, slots=[0, 1])
    b = PromptHandler("b", [r"B\$ "], [], True)
    table = PatternTable([a, b])
    assert table.patterns[table.line_break] == re.compile(r"\r\n", re.DOTALL)
    assert [table.handler_for(i) for i in range(2, 5)] == [(a, 0), (a, 1), (b, 0)]
    assert table.handler_for(table.line_break) is None and table.handler_for(table.stray) is None
    assert not hasattr(a, "start") and not hasattr(a, "end")
    other = PatternTable([b, a])  # the same handlers in another order: each table has its own slices
    assert other.handler_for(2) == (b, 0) and other.handler_for(4) == (a, 1)
    assert table.handler_for(2) == (a, 0)
