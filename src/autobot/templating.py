"""Jinja2 templating: the environments the engine renders with and their filters (`Templater`), and the
checks of a template's syntax and of a rendered regex (see SPEC, Jinja2 Templating).

`templater` is the default instance, which a runner built without a templater of its own renders with, and
whose `check_template` is the default syntax check of `prompts.build_handler`. `check_regex` is a free
function: the runner and the steps check a rendered pattern with it, with no templater involved.
"""

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


def _search(s: Any, pattern: str) -> bool:
    try:
        return bool(re.search(pattern, str(s)))
    except re.error as e:
        raise ScriptError(f"template error: search: invalid regex {pattern!r}: {e}") from e


class Templater:
    """The three Jinja2 environments a run renders with, with the custom filters (SPEC, Custom Filters)
    registered on each: `text` for what the engine sends or stores, `condition` for a `when`, whose text is
    only read as a yes or no, and `value` for a value that is stored (`set`), of which the line break that
    ends the template is part. A runner has one; `templater` below is the default."""

    def __init__(self) -> None:
        self.text = _Environment(undefined=jinja2.StrictUndefined, finalize=_no_boolean)
        self.condition = _Environment(undefined=jinja2.StrictUndefined)
        self.value = _Environment(undefined=jinja2.StrictUndefined, finalize=_no_boolean, keep_trailing_newline=True)
        for env in (self.text, self.condition, self.value):
            env.filters["contains"] = lambda s, substring: substring in str(s)
            env.filters["search"] = _search

    def render(self, template: Any, ctx: dict, *, condition: bool = False, keep_newline: bool = False) -> Any:
        """Render a template to text. `condition`: the text is only read as a yes or no (`when`), so a boolean
        is written out as Jinja2 does it instead of being refused. `keep_newline`: the text is stored, and keeps
        the line break that ends the template, unless whitespace control (`-}}`, `-%}`) removes it."""
        if not isinstance(template, str):
            return template
        env = self.condition if condition else self.value if keep_newline else self.text
        try:
            return env.from_string(template).render(ctx)
        except jinja2.TemplateError as e:
            raise ScriptError(f"template error: {e}") from e
        except Exception as e:  # noqa: BLE001 - whatever an expression raises, e.g. {{ 1/0 }}, is a template error
            if isinstance(e, ScriptError):  # already reported, by a filter
                raise
            why = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
            raise ScriptError(f"template error: {why}") from e

    def check_template(self, template: str) -> None:
        """Report a template syntax error now rather than when the template is rendered."""
        try:
            self.text.parse(template)
        except jinja2.TemplateError as e:
            raise ScriptError(f"template error: {e}") from e


templater = Templater()


def check_regex(pattern: str, what: str) -> None:
    """Report a rendered pattern that isn't a valid regex as a script error, not a raw `re.error`."""
    try:
        re.compile(pattern)
    except re.error as e:
        raise ScriptError(f"{what}: invalid regex {pattern!r}: {e}") from e
