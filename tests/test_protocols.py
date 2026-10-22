"""P7-27, P7-28: the protocols between the runner, the session and the steps (SPEC "Common Step Properties",
the `ctx` of a plugin)."""

from __future__ import annotations

import inspect

from conftest import make_runner

from autobot import protocols
from autobot.runner import Runner
from autobot.session import Session

SESSION_LIKE = {"ctx", "posix", "get_prompt", "sendline", "sendcontrol", "check_rc", "sleep"}


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
