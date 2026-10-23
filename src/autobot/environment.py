"""The `env` of the templates: the variables the script's `env` section declares, each with the value of
`--env`, else the one of the environment, as the type of its default (see SPEC, env)."""

from __future__ import annotations

import math
import re
from typing import Any

from jinja2 import StrictUndefined

from .errors import ScriptError

_TRUTH = {"1": True, "true": True, "yes": True, "on": True, "0": False, "false": False, "no": False, "off": False, "": False}
_TYPES = {bool: "a boolean", int: "an integer", float: "a number"}


def typed(key: str, value: str, default: Any, source: str) -> Any:
    """Text from `source` (`--env` or the environment) as a value of the type of the default of `key`."""
    try:
        if isinstance(default, bool):
            return _TRUTH[value.strip().lower()]
        if isinstance(default, int):
            if not re.fullmatch(r"[+-]?[0-9]+", value.strip()):
                raise ValueError
            return int(value)
        if isinstance(default, float):
            if not math.isfinite(number := float(value)):
                raise ValueError
            return number
    except (KeyError, ValueError):
        # without the value, which may be a secret, and without the exception that holds it
        raise ScriptError(f"env.{key}: the value from {source} is not {_TYPES[type(default)]}") from None
    return value


class Env(dict):  # holds its keys and nothing else: a template reads attributes too
    __slots__ = ()

    def __missing__(self, key: Any) -> Any:
        return StrictUndefined(hint=f"env has no key {key!r}: the script's env section doesn't declare it")


def resolve_env(declared: dict[str, Any], typed_given: dict[str, Any], environ: dict[str, str]) -> Env:
    """`env` in templates: the variables `declared` (the `env` section: name to default), and nothing else.
    Each has the value of `typed_given` (`--env`, typed already), else the one of `environ` (the environment
    `prepare` left, or the one it would have been given), as the type of its default, else its default."""
    env = Env()
    for key, default in declared.items():
        if key in typed_given:
            env[key] = typed_given[key]
        elif key in environ:
            env[key] = typed(key, environ[key], default, "the environment")
        elif default is None:
            raise ScriptError(f"env.{key} is required: give it with --env {key}=VALUE or in the environment")
        else:
            env[key] = default
    return env
