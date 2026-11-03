"""The report of a script's validation errors: one line per error, `<where>: <what> (got <value>) [<type>]`,
without a value that may be a password (see SPEC, CLI)."""

from __future__ import annotations

from typing import Any, NamedTuple

import pydantic

from ..log import ConsoleReporter
from ..types import text, yaml_kind
from .report import visible

GOT_MAX = 60
# Error types that get no `(got <value>)`: the location says it all (a missing or an unknown key), the
# type's name says what the value is (empty), or the message is written with the value in it
NO_VALUE = frozenset({
    "missing", "extra_forbidden", "string_too_short", "too_short",
    "value_error", "assertion_error", "unsupported_version", "control_char", "invalid_regex", "undefined_function",
})
EITHER = "Input should be a string or a list of strings"


def _is_list_member(key: object) -> bool:
    return isinstance(key, str) and key.startswith("list[")


class Member(NamedTuple):
    """An error under one alternative of a string-or-list field: the error, the alternative's key in its
    location (`str` or `list[str]`), and the rest of the location under that key."""

    err: Any
    key: object
    rest: tuple


def _member(err: Any) -> tuple[tuple, Member] | None:
    """The field's location and the member, for an error under an alternative; None for any other error."""
    loc = err["loc"]
    for i, key in enumerate(loc):
        if key == "str" or _is_list_member(key):
            return loc[:i], Member(err, key, loc[i + 1 :])
    return None


def _union(members: list[Member]) -> tuple[Member, list[Member]] | None:
    """The member at the string alternative itself and those under the list alternative, when `members` are
    the errors of one string-or-list union: one `string_type` error at `str`, every other under `list[str]`.
    None otherwise, e.g. for keys of a mapping that have these names."""
    whole = [m for m in members if m.key == "str" and not m.rest]
    lists = [m for m in members if _is_list_member(m.key)]
    if len(whole) == 1 and whole[0].err["type"] == "string_type" and len(whole) + len(lists) == len(members):
        return whole[0], lists
    return None


def one_per_value(errors: list[Any]) -> list[Any]:
    """A field that takes a string or a list of strings reports a wrong value once for each of the two, at
    `<field>.str` and `<field>.list[str]`. Make that one error: at the field, saying what it takes, for a
    value that is neither (`cmd: 5`: one `string_type` error at `script.0.cmd.cmd`, with `EITHER` as its
    message); at the item, for a list with a wrong item (`cmd: ["ok", 5]`: the `list[str]` alternative's
    error for item 1, moved to `script.0.cmd.cmd.1`). Any other error is kept as it is, in its place.
    """
    groups: dict[tuple, list[Member]] = {}
    for err in errors:
        if found := _member(err):
            base, member = found
            groups.setdefault(base, []).append(member)
    replace: dict[int, list[Any]] = {}
    for base, members in groups.items():
        if not (union := _union(members)):
            continue
        whole, lists = union
        value = whole.err["input"]
        if isinstance(value, list):
            if not lists or not all(m.rest and isinstance(m.rest[0], int) for m in lists):
                continue
            new = [{**m.err, "loc": (*base, *m.rest)} for m in lists]
        elif len(lists) == 1 and not lists[0].rest and lists[0].err["type"] == "list_type":
            # an entry of `expect` is typed like these fields, but a list there is an error of its own
            # (`grouped_expect`): only a string will do
            new = [{**whole.err, "loc": base, **({} if "expect" in base else {"msg": EITHER})}]
        else:
            continue
        replace[id(whole.err)] = new
        replace.update({id(m.err): [] for m in lists})
    return [new for err in errors for new in replace.get(id(err), [err])]


def sent(loc: tuple) -> bool:
    """Whether an error at `loc` is about text that is sent to the device or is a value of `env`, which may
    be a password: a prompt's `send` and what is in it, a `line` or `raw` step, an `env` value, the values
    of a `set` step."""
    if loc[:1] == ("env",):
        return True
    for i, key in enumerate(loc):
        if key == "send" and "prompts" in loc[:i]:
            return True
        # a step's type tag follows its index, and the step's own field the tag: `script.0.line.line`
        if key in ("line", "raw", "set") and i and isinstance(loc[i - 1], int) and loc[i + 1 : i + 2] == (key,):
            return True
    return False


def got(type_: str, msg: str, value: object, loc: tuple = ()) -> str:
    """The offending value, as YAML writes it, for a validation error whose message doesn't show it."""
    if type_ in NO_VALUE or sent(loc):
        return ""
    if isinstance(value, dict):
        keys = ", ".join(map(str, value))
        shown = f"a mapping with the key{'s' if len(value) != 1 else ''} {keys}" if value else "an empty mapping"
    elif isinstance(value, list):
        shown = f"a list of {len(value)} item{'s' if len(value) != 1 else ''}"
    elif value is None or isinstance(value, (bool, int, float)):
        shown = "null" if value is None else text(value)
    elif isinstance(value, str):
        shown = repr(value)
        if shown in msg:  # quoted in full, as a message that shows the value does: not a word that happens to match
            return ""
    else:
        shown = yaml_kind(value)
    if len(shown) > GOT_MAX:
        shown = shown[:GOT_MAX] + "..."
    return f" (got {shown})"


def report(rep: ConsoleReporter, error: pydantic.ValidationError) -> None:
    """`Validation errors:` and one line per error, as the models report them."""
    rep.error("Validation errors:")
    for err in one_per_value(error.errors()):
        where = ".".join(map(str, err["loc"])) or "(document)"
        what = err["msg"].removeprefix("Value error, ")
        # the message is the model's, or a plugin model's, and may show a value of the script as it is
        rep.problem(visible(where), visible(what + got(err["type"], what, err["input"], err["loc"])), err["type"])
