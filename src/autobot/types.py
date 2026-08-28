from __future__ import annotations

import re
from typing import Annotated, Any

import jinja2
import pydantic

ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
DURATION_RE = re.compile(r"^(\d+(?:\.\d+)?)(ms|s|m|h)$")
DURATION_MULT = {"ms": 0.001, "s": 1, "m": 60, "h": 3600}
_jinja_env = jinja2.Environment(undefined=jinja2.StrictUndefined)


def parse_duration(value: Any) -> float:
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return float(value)
    m = DURATION_RE.match(str(value))
    if m:
        return float(m.group(1)) * DURATION_MULT[m.group(2)]
    raise ValueError(f"invalid duration: {value}")


Duration = Annotated[float, pydantic.BeforeValidator(parse_duration)]
StringOrArray = str | list[str]


def render(template: Any, ctx: dict) -> str:
    if not isinstance(template, str):
        return template
    try:
        return _jinja_env.from_string(template).render(ctx)
    except jinja2.UndefinedError as e:
        raise ValueError(f"template error: {e}") from e


def ensure_list(value: StringOrArray | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]
