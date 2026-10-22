"""The report of a script's validation errors: one line per error, `<where>: <what> (got <value>) [<type>]`,
without a value that may be a password (see SPEC, CLI)."""

from __future__ import annotations

from typing import Any

import pydantic

from ..log import ConsoleReporter
from ..prompts import yaml_kind
from ..types import text
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


def one_per_value(errors: list[Any]) -> list[Any]:
    """A field that takes a string or a list of strings reports a wrong value once for each of the two, at
    `<field>.str` and `<field>.list[str]`. Make that one error: at the field, saying what it takes, for a
    value that is neither; at the item, for a list with a wrong item.

    For `cmd: 5` pydantic reports two errors, which become one at the field, with what it takes:

        {"type": "string_type", "loc": ("script", 0, "cmd", "cmd", "str"), "input": 5, ...}
        {"type": "list_type", "loc": ("script", 0, "cmd", "cmd", "list[str]"), "input": 5, ...}
        -> {"type": "string_type", "loc": ("script", 0, "cmd", "cmd"), "input": 5,
            "msg": "Input should be a string or a list of strings"}

    For `cmd: ["ok", 5]` the list alternative reports the item, and the item is the one error, at its index:

        {"type": "string_type", "loc": ("script", 0, "cmd", "cmd", "str"), "input": ["ok", 5], ...}
        {"type": "string_type", "loc": ("script", 0, "cmd", "cmd", "list[str]", 1), "input": 5, ...}
        -> {"type": "string_type", "loc": ("script", 0, "cmd", "cmd", 1), "input": 5, ...}

    Any other error is kept as it is, in its place.
    """
    def split(loc: tuple) -> tuple[tuple, object, tuple] | None:
        for i, key in enumerate(loc):
            if key == "str" or _is_list_member(key):
                return loc[:i], key, loc[i + 1 :]
        return None

    groups: dict[tuple, list[tuple[Any, object, tuple]]] = {}
    for err in errors:
        if parts := split(err["loc"]):
            groups.setdefault(parts[0], []).append((err, parts[1], parts[2]))
    replace: dict[int, list[Any]] = {}
    for base, members in groups.items():
        whole = [err for err, key, rest in members if key == "str" and not rest]
        lists = [(err, rest) for err, key, rest in members if _is_list_member(key)]
        if len(whole) != 1 or whole[0]["type"] != "string_type" or len(whole) + len(lists) != len(members):
            continue  # not the two members of one union: e.g. keys of a mapping that have these names
        value = whole[0]["input"]
        if isinstance(value, list):
            if not lists or not all(rest and isinstance(rest[0], int) for _, rest in lists):
                continue
            new = [{**err, "loc": (*base, *rest)} for err, rest in lists]
        elif len(lists) == 1 and not lists[0][1] and lists[0][0]["type"] == "list_type":
            # an entry of `expect` is typed like these fields, but a list there is an error of its own
            # (`grouped_expect`): only a string will do
            new = [{**whole[0], "loc": base, **({} if "expect" in base else {"msg": EITHER})}]
        else:
            continue
        replace[id(whole[0])] = new
        replace.update({id(err): [] for err, _ in lists})
    return [new for err in errors for new in replace.get(id(err), [err])]


def sent(loc: tuple) -> bool:
    """Whether an error at `loc` is about text that is sent to the device or is a value of `env`, which may
    be a password: a prompt's `send` and what is in it, a `line` step, an `env` value, the values of a `set`
    step."""
    if loc[:1] == ("env",):
        return True
    for i, key in enumerate(loc):
        if key == "send" and "prompts" in loc[:i]:
            return True
        # a step's type tag follows its index, and the step's own field the tag: `script.0.line.line`
        if key in ("line", "set") and i and isinstance(loc[i - 1], int) and loc[i + 1 : i + 2] == (key,):
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
