"""P1-23..26: a shell prompt with `posix: true` (SPEC "prompts", "cmd"): with top-level `errors`, a command
line that ends at it has its return code checked as well."""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

import pydantic
import pytest
from conftest import RC_PROBE, SHELL_PROMPT, FakeDevice, SentLog, run_vars

from autobot import models
from autobot.models import Config
from autobot.session import CommandError
from autobot.steps import StepFailure

PCT_ERR = ["% .*"]
POSIX = [{**SHELL_PROMPT, "posix": True}]
# a second shell prompt, set in the same bash: the shell a script enters from a CLI
SH = {"name": "bash", "expect": [r"SH\$ "], "posix": True}
CLI = {"name": "cli", "expect": [r"PROMPT\$ "], "return": True}
# not written out, so that the echo of the line holds neither prompt
TO_SH = "PS1=$(printf '%s%s ' S 'H$')"
TO_CLI = "PS1=$(printf '%s%s ' PROM 'PT$')"


# -- P1-23: the return code is checked at a posix prompt, with errors ----------


def test_p1_23_nonzero_exit_fails_the_step_and_stops_the_run(sent: SentLog):
    with pytest.raises(StepFailure, match="^command returned exit code 1$"):
        run_vars([{"cmd": "false"}, {"cmd": "echo next"}], prompts=POSIX, errors=PCT_ERR)
    assert sent.lines().count(RC_PROBE) == 1
    assert "echo next" not in sent.lines()


def test_p1_23_exit_code_of_the_last_line(sent: SentLog):
    with pytest.raises(StepFailure, match="^command returned exit code 7$"):
        run_vars([{"cmd": ["true", "(exit 7)"]}], prompts=POSIX, errors=PCT_ERR)
    run_vars([{"cmd": ["false", "true"]}], prompts=POSIX, errors=PCT_ERR)
    assert sent.lines().count(RC_PROBE) == 2


def test_p1_23_zero_exit_and_no_match_pass(sent: SentLog):
    out = run_vars(
        [{"cmd": "echo fine", "register": "out"}, {"cmd": "echo next", "register": "next"}],
        prompts=POSIX,
        errors=PCT_ERR,
    )
    assert (out["out"], out["next"]) == ("fine", "next")
    assert sent.lines().count(RC_PROBE) == 2


@pytest.mark.parametrize("cmd", ["echo '% bad'", "echo '% bad'; false", ["echo '% bad'", "echo second"]], ids=repr)
def test_p1_23_errors_match_still_fails_first(sent: SentLog, cmd: Any):
    """The patterns come first, per line: a match fails the step, and no `$?` check is made."""
    with pytest.raises(CommandError, match="^command error: % bad$"):
        run_vars([{"cmd": cmd}, {"cmd": "echo next"}], prompts=POSIX, errors=PCT_ERR)
    assert RC_PROBE not in sent.lines()
    assert not {"echo second", "echo next"} & set(sent.lines())


def test_p1_23_assert_replaces_the_check(sent: SentLog):
    run_vars([{"cmd": "echo running; false", "assert": "running"}], prompts=POSIX, errors=PCT_ERR)
    assert RC_PROBE not in sent.lines()
    with pytest.raises(StepFailure, match="^assertion failed"):
        run_vars([{"cmd": "true", "assert": "running"}], prompts=POSIX, errors=PCT_ERR)


def test_p1_23_ignore_error_covers_the_exit_code():
    out = run_vars(
        [{"cmd": "echo fine; false", "ignore_error": True, "register": "out"}, {"cmd": "echo next", "register": "n"}],
        prompts=POSIX,
        errors=PCT_ERR,
    )
    assert (out["out"], out["n"]) == ("fine", "next")


def test_p1_23_block_prompts(sent: SentLog):
    """A block's prompts say it too: inside the block the check is made, and after it it isn't."""
    block = {"block": {"name": "sh", "prompts": POSIX, "script": [{"cmd": "true"}]}}
    run_vars([{"cmd": "false"}, block, {"cmd": "false"}], errors=PCT_ERR)
    assert sent.lines().count(RC_PROBE) == 1
    block["block"]["script"] = [{"cmd": "false"}]
    with pytest.raises(StepFailure, match="^command returned exit code 1$"):
        run_vars([block], errors=PCT_ERR)


# -- P1-24: a prompt that isn't marked, and a script without errors ------------


@pytest.mark.parametrize("prompt", [SHELL_PROMPT, {**SHELL_PROMPT, "posix": False}], ids=["absent", "false"])
def test_p1_24_unmarked_prompt_with_errors_gets_no_check(sent: SentLog, prompt: dict):
    run_vars([{"cmd": "false"}, {"cmd": "echo next"}], prompts=[prompt], errors=PCT_ERR)
    assert RC_PROBE not in sent.lines()
    assert "echo next" in sent.lines()


def test_p1_24_cli_without_exit_codes_gets_no_check(sent: SentLog, fake_device: FakeDevice):
    """A CLI and a posix prompt in one script: nothing is sent to the CLI but the commands."""
    spawn, _ = fake_device("--order", "none", "--then", "editor", "--prompt", "'sw#'", "--cols", "80", "--wrap", "0a")
    prompts = [SH, {"name": "cli", "expect": [r"^sw#"], "return": True}]
    out = run_vars([{"cmd": "echo hi", "register": "out"}], spawn=spawn, prompts=prompts, errors=["^% .*"])
    assert out["out"] == "hi"
    assert sent.lines() == ["echo hi"]


@pytest.mark.parametrize("prompts", [[SHELL_PROMPT], POSIX], ids=["unmarked", "posix"])
def test_p1_24_without_errors_nothing_changes(sent: SentLog, prompts: list):
    """Without `errors` the return code is checked at every shell prompt, marked or not."""
    with pytest.raises(StepFailure, match="^command returned exit code 1$"):
        run_vars([{"cmd": "true"}, {"cmd": "false"}, {"cmd": "echo next"}], prompts=prompts)
    assert sent.lines().count(RC_PROBE) == 2
    assert "echo next" not in sent.lines()


# -- P1-25: a shell entered and left in the middle of a script ------------------


def test_p1_25_shell_entered_mid_script(sent: SentLog):
    """The session starts at the unmarked prompt. What counts is the prompt each command ends at."""
    script = [
        {"cmd": "false"},  # at the CLI's prompt: not checked
        {"cmd": TO_SH},  # ends at the shell's prompt: checked, 0
        {"cmd": "echo fine", "register": "out"},  # checked
        {"cmd": TO_CLI},  # ends at the CLI's prompt: not checked
        {"cmd": "false"},  # not checked
        {"cmd": "echo end", "register": "end"},
    ]
    out = run_vars(script, prompts=[SH, CLI], errors=PCT_ERR)
    assert (out["out"], out["end"]) == ("fine", "end")
    assert [line for line in sent.lines() if line] == [
        "false", TO_SH, RC_PROBE, "echo fine", RC_PROBE, TO_CLI, "false", "echo end",
    ]  # fmt: skip


def test_p1_25_failure_in_a_shell_entered_mid_script(sent: SentLog):
    with pytest.raises(StepFailure, match="^command returned exit code 3$"):
        run_vars([{"cmd": TO_SH}, {"cmd": "(exit 3)"}, {"cmd": "echo next"}], prompts=[SH, CLI], errors=PCT_ERR)
    assert "echo next" not in sent.lines()


def test_p1_25_errors_match_in_a_shell_entered_mid_script(sent: SentLog):
    with pytest.raises(CommandError, match="^command error: % bad$"):
        run_vars([{"cmd": TO_SH}, {"cmd": "echo '% bad'"}], prompts=[SH, CLI], errors=PCT_ERR)
    assert sent.lines().count(RC_PROBE) == 1  # of the line that entered the shell


@pytest.mark.parametrize(("first", "probes"), [(True, 1), (False, 0)], ids=["posix-first", "posix-second"])
def test_p1_25_of_two_prompts_that_match_the_first_defined_is_taken(sent: SentLog, first: bool, probes: int):
    """SPEC "prompts": where a CLI's regex also matches the shell's prompt, the posix prompt is listed first."""
    wide = {"name": "cli", "expect": [r"[A-Z]+\$ "], "return": True}
    posix = {**SHELL_PROMPT, "posix": True}
    run_vars([{"cmd": "true"}], prompts=[posix, wide] if first else [wide, posix], errors=PCT_ERR)
    assert sent.lines().count(RC_PROBE) == probes


# -- P1-26: validation, in the models and in the schema ----------------------------

MIN: dict[str, Any] = {"autobot": "2026-10", "attach": {"spawn": "ssh host"}, "script": []}
POSIX_MSG = "posix marks the shell prompt of a POSIX shell, and a prompt with send is no shell prompt; remove send or posix"


def doc(**prompt: Any) -> dict[str, Any]:
    return {**copy.deepcopy(MIN), "prompts": [{"name": "p", "expect": ["x"], **prompt}]}


def in_block(**prompt: Any) -> dict[str, Any]:
    block = {"name": "b", "prompts": [{"name": "p", "expect": ["x"], **prompt}], "script": []}
    return {**copy.deepcopy(MIN), "script": [{"block": block}]}


def errors_of(document: dict[str, Any]) -> list[dict[str, Any]]:
    with pytest.raises(pydantic.ValidationError) as e:
        Config.model_validate(document)
    return e.value.errors()


ACCEPTED = {
    "true": doc(posix=True),
    "false": doc(posix=False),
    "with-return": doc(**{"return": True, "posix": True}),
    "false-with-send": doc(posix=False, send="y"),
    "block": in_block(posix=True),
}


@pytest.mark.parametrize("document", list(ACCEPTED.values()), ids=list(ACCEPTED))
def test_p1_26_posix_accepted(both_validate: Callable, document: dict):
    assert both_validate(document) == (True, True)


def test_p1_26_posix_is_read_and_defaults_to_false():
    assert Config.model_validate(doc(posix=True)).prompts[0].posix is True
    assert Config.model_validate(doc()).prompts[0].posix is False


@pytest.mark.parametrize("value", ["yes", "true", 1, 0, None, [True]], ids=repr)
def test_p1_26_posix_is_a_strict_boolean(both_validate: Callable, value: Any):
    assert both_validate(doc(posix=value)) == (False, False)
    [err] = errors_of(doc(posix=value))
    assert err["loc"] == ("prompts", 0, "posix")


SENDS = {
    "string": {"send": "y"},
    "empty-string": {"send": ""},
    "sendEach": {"send": {"each": "vars.creds"}},
    "sendEach-fields": {"send": {"each": "vars.creds", "fields": [{"match": "login:", "field": "u"}]}, "expect": None},
}


@pytest.mark.parametrize("send", list(SENDS.values()), ids=list(SENDS))
@pytest.mark.parametrize("build", [doc, in_block], ids=["top-level", "block"])
def test_p1_26_posix_with_send_rejected(both_validate: Callable, build: Callable, send: dict):
    document = build(posix=True, **send)
    for holder in (document["prompts"] if "prompts" in document else document["script"][0]["block"]["prompts"]):
        if holder.get("expect", "") is None:
            del holder["expect"]
    assert both_validate(document) == (False, False)
    [err] = errors_of(document)
    at = ("prompts", 0) if build is doc else ("script", 0, "block", "block", "prompts", 0)
    assert (err["loc"], err["type"], err["msg"]) == ((*at, "posix"), "posix_with_send", POSIX_MSG)


def test_p1_26_posix_and_return_with_send_are_both_reported(both_validate: Callable):
    document = doc(**{"return": True, "posix": True, "send": "y"})
    assert both_validate(document) == (False, False)
    assert [(e["loc"], e["type"]) for e in errors_of(document)] == [
        (("prompts", 0, "send"), "return_with_send"),
        (("prompts", 0, "posix"), "posix_with_send"),
    ]


def test_p1_26_schema_and_model_have_the_same_prompt_fields(schema: dict[str, Any]):
    names = sorted(f.alias or name for name, f in models.Prompt.model_fields.items())
    assert sorted(schema["$defs"]["prompt"]["properties"]) == names == ["expect", "name", "posix", "return", "send"]
