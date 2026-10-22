"""Jinja2 templating: the environments the engine renders with, their filters, `render`, and the checks of a
template's syntax and of a rendered regex (see SPEC, Jinja2 Templating)."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

import jinja2

from .errors import ScriptError
from .types import text


class _Environment(jinja2.Environment):
    def getattr(self, obj: Any, attribute: str) -> Any:
        # `x.name` on a mapping is the key `name` when there is one: `vars.values` is the key, not dict.values
        if isinstance(obj, Mapping) and attribute in obj:
            return obj[attribute]
        return super().getattr(obj, attribute)


def _no_boolean(value: Any) -> Any:
    # what `{{ ... }}` puts out. Only the expression's own value is checked: a boolean used inside the
    # expression, or inside a list or mapping it puts out, never gets here
    if isinstance(value, bool):
        raise ScriptError(
            f"template error: an expression gave a boolean ({text(value)}), which is never written as text; "
            "quote the value in the script, or say which text is meant, "
            "e.g. {{ value | string }} (True) or {{ value | tojson }} (true)"
        )
    return value


# text the engine sends or stores, and a condition, whose text is only read as a yes or no
_jinja_env = _Environment(undefined=jinja2.StrictUndefined, finalize=_no_boolean)
_condition_env = _Environment(undefined=jinja2.StrictUndefined)
# a value that is stored (`set`): the line break that ends the template is part of it
_value_env = _Environment(undefined=jinja2.StrictUndefined, finalize=_no_boolean, keep_trailing_newline=True)


def _search(s: Any, pattern: str) -> bool:
    try:
        return bool(re.search(pattern, str(s)))
    except re.error as e:
        raise ScriptError(f"template error: search: invalid regex {pattern!r}: {e}") from e


for _env in (_jinja_env, _condition_env, _value_env):
    _env.filters["contains"] = lambda s, substring: substring in str(s)
    _env.filters["search"] = _search


def render(template: Any, ctx: dict, *, condition: bool = False, keep_newline: bool = False) -> Any:
    """Render a template to text. `condition`: the text is only read as a yes or no (`when`), so a boolean
    is written out as Jinja2 does it instead of being refused. `keep_newline`: the text is stored, and keeps
    the line break that ends the template, unless whitespace control (`-}}`, `-%}`) removes it."""
    if not isinstance(template, str):
        return template
    env = _condition_env if condition else _value_env if keep_newline else _jinja_env
    try:
        return env.from_string(template).render(ctx)
    except jinja2.TemplateError as e:
        raise ScriptError(f"template error: {e}") from e
    except Exception as e:  # noqa: BLE001 - whatever an expression raises, e.g. {{ 1/0 }}, is a template error
        if isinstance(e, ScriptError):  # already reported, by a filter
            raise
        why = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
        raise ScriptError(f"template error: {why}") from e


def check_template(template: str) -> None:
    """Report a template syntax error now rather than when the template is rendered."""
    try:
        _jinja_env.parse(template)
    except jinja2.TemplateError as e:
        raise ScriptError(f"template error: {e}") from e


def check_regex(pattern: str, what: str) -> None:
    """Report a rendered pattern that isn't a valid regex as a script error, not a raw `re.error`."""
    try:
        re.compile(pattern)
    except re.error as e:
        raise ScriptError(f"{what}: invalid regex {pattern!r}: {e}") from e
