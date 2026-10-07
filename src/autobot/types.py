from __future__ import annotations

import math
import re
import sys
from collections.abc import Mapping
from typing import Annotated, Any

import jinja2
import jinja2.meta
import jinja2.sandbox
import pydantic
from pydantic_core import PydanticCustomError

ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
DURATION_RE = re.compile(r"^([0-9]+(?:\.[0-9]+)?)(ms|s|m|h)$")
DURATION_MULT = {"ms": 0.001, "s": 1, "m": 60, "h": 3600}
DURATION_MAX = sys.float_info.max



GLOBALS = ("range", "dict", "namespace")


class _Environment(jinja2.sandbox.ImmutableSandboxedEnvironment):
    """Templates are the script's, and a script may come from anyone: they reach the values they are given
    and Jinja2's own filters and tests, not Python's internals, and they change nothing they are given."""

    def __init__(self, **kw: Any) -> None:
        super().__init__(undefined=jinja2.StrictUndefined, **kw)
        self.globals = {name: self.globals[name] for name in GLOBALS}

    def getattr(self, obj: Any, attribute: str) -> Any:
        # `x.name` on a mapping is the key `name` when there is one: `vars.values` is the key, not dict.values
        if isinstance(obj, Mapping) and attribute in obj:
            return obj[attribute]
        return super().getattr(obj, attribute)


def text(value: Any) -> str:
    """A scalar of the script as text: a boolean as YAML writes it, `true` or `false`."""
    return str(value).lower() if isinstance(value, bool) else str(value)


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
_jinja_env = _Environment(finalize=_no_boolean)
_condition_env = _Environment()


class ScriptError(ValueError):
    """A problem of the script that shows only while it runs: a template, a rendered pattern, a `sendEach`
    collection. The CLI reports it without a traceback."""


class RunError(RuntimeError):
    """A failure of the run that the device or the environment explains: a failed command, a prompt out of
    responses, a failed `prepare`. The CLI reports it without a traceback."""


class EnvError(ScriptError):
    """An `env` reference cycle or nesting limit, found while a default is rendered; not a template error."""


def _search(s: Any, pattern: str) -> bool:
    try:
        return bool(re.search(pattern, str(s)))
    except re.error as e:
        raise ScriptError(f"template error: search: invalid regex {pattern!r}: {e}") from e


for _env in (_jinja_env, _condition_env):
    _env.filters["contains"] = lambda s, substring: substring in str(s)
    _env.filters["search"] = _search


def parse_duration(value: Any) -> float:
    if value is None:
        raise ValueError("invalid duration: null")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # compared before the conversion, which rounds: the schema's `maximum` is the largest double
        if value != value or abs(value) > DURATION_MAX:
            raise ValueError(f"invalid duration: {value} (not a finite number)")
        seconds = float(value)
        if seconds < 0:
            raise ValueError(f"invalid duration: {value}")
        return seconds
    m = DURATION_RE.fullmatch(str(value))
    if m:
        seconds = float(m.group(1)) * DURATION_MULT[m.group(2)]
        if not math.isfinite(seconds):
            raise ValueError(f"invalid duration: {value} (not a finite number)")
        return seconds
    raise ValueError(f"invalid duration: {text(value)}")


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


def render(template: Any, ctx: dict, *, condition: bool = False) -> Any:
    """Render a template to text. `condition`: the text is only read as a yes or no (`when`), so a boolean
    is written out as Jinja2 does it instead of being refused."""
    if not isinstance(template, str):
        return template
    try:
        return (_condition_env if condition else _jinja_env).from_string(template).render(ctx)
    except jinja2.sandbox.SecurityError as e:
        raise ScriptError(f"template error: not allowed in a template: {e}") from e
    except jinja2.TemplateError as e:
        raise ScriptError(f"template error: {e}") from e
    except Exception as e:  # noqa: BLE001 - whatever an expression raises, e.g. {{ 1/0 }}, is a template error
        # already reported: by a render inside this one (an env default), or by a filter
        if isinstance(e, ScriptError):
            raise
        why = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
        raise ScriptError(f"template error: {why}") from e


def check_template(template: str) -> None:
    """Report a template syntax error now rather than when the template is rendered."""
    try:
        _jinja_env.parse(template)
    except jinja2.TemplateError as e:
        raise ScriptError(f"template error: {e}") from e


def template_names(template: str) -> set[str]:
    """The context variables a template reads, e.g. `env` or `vars`."""
    try:
        return jinja2.meta.find_undeclared_variables(_jinja_env.parse(template))
    except jinja2.TemplateError as e:
        raise ScriptError(f"template error: {e}") from e


def check_regex(pattern: str, what: str) -> None:
    """Report a rendered pattern that isn't a valid regex as a script error, not a raw `re.error`."""
    try:
        re.compile(pattern)
    except re.error as e:
        raise ScriptError(f"{what}: invalid regex {pattern!r}: {e}") from e


def ensure_list(value: StringOrArray | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]
