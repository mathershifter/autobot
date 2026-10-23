"""The types the models are written with: the annotated types of the fields, and how a scalar of the script
reads as text."""

from __future__ import annotations

import datetime
from typing import Annotated, Any

import pydantic
from pydantic_core import PydanticCustomError

from .duration import parse_duration


def text(value: Any) -> str:
    """A scalar of the script as text: a boolean as YAML writes it, `true` or `false`."""
    return str(value).lower() if isinstance(value, bool) else str(value)


_KINDS = {
    dict: "a mapping", list: "a list", str: "a string", int: "a number", float: "a number", bool: "a boolean",
    datetime.date: "a timestamp", datetime.datetime: "a timestamp", bytes: "binary data", set: "a set",
}


def yaml_kind(value: Any) -> str:
    """What a value of the script is, as YAML names it: `a mapping`, `a timestamp`, `null`."""
    return "null" if value is None else _KINDS.get(type(value), f"a {type(value).__name__}")


def reject_null(value: Any) -> Any:
    if value is None:
        raise PydanticCustomError("null_value", "null (an empty value) is not allowed; omit the key instead")
    return value


Duration = Annotated[float, pydantic.BeforeValidator(parse_duration)]
StringOrArray = str | list[str]
# an optional field: the key may be omitted (the field is then None), but an explicit null is rejected
type Omittable[T] = Annotated[T | None, pydantic.BeforeValidator(reject_null)]
# an optional field whose default isn't None (a list, a mapping, a flag): the same rule for an explicit null
type NotNull[T] = Annotated[T, pydantic.BeforeValidator(reject_null)]


def ensure_list(value: StringOrArray | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]
