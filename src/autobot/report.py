"""What a run notes on the error that ends it, for the report: the steps that were running when it was raised,
and what the run may have left behind (see SPEC, A breakout that doesn't finish; Errors while the script runs)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .models import PluginStep

WHAT_MAX = 72


@dataclass(frozen=True)
class StepRef:
    """A step of the script, for error reports: its path, its key and what it does."""

    path: str
    key: str
    what: str
    plugin: bool = False


def step_ref(step: Any, path: str) -> StepRef:
    if isinstance(step, PluginStep):
        key = step.plugin_key_ or "plugin"
        return StepRef(path, key, key, True)
    fields = getattr(type(step), "model_fields", None)
    if not fields:  # not a step model: the registry reports it
        return StepRef(path, type(step).__name__, type(step).__name__)
    # a built-in step's first field is its key
    name, info = next(iter(fields.items()))
    key, value = info.alias or name, getattr(step, name)
    more = ""
    if key == "block":
        value = value.name
    elif key == "cmd" and isinstance(value, list):
        more = f" (+{len(value) - 1} more)" if len(value) > 1 else ""
        value = value[0] if value else ""
    elif key not in ("cmd", "call"):  # a `line` may be a password; the others say nothing more than the key
        return StepRef(path, key, key)
    # as written, not rendered, and only the first line
    first = next((l for l in str(value).splitlines() if l.strip()), "").strip()
    if len(first) > WHAT_MAX:
        first = first[:WHAT_MAX] + "..."
    return StepRef(path, key, f"{key}: {first}{more}" if first or more else key)


@dataclass(frozen=True)
class Left:
    """What a run may have left behind: the errors that ended its breakouts, in the order they were raised,
    and the prompts it answered with credentials."""

    breakouts: tuple[BaseException, ...]
    logins: tuple[str, ...]
    pending: bool = False  # no shell prompt was reached after the credentials: the login may not have happened


def trail(error: BaseException) -> tuple[StepRef, ...]:
    """The steps that were running when `error` was raised, outermost first; empty outside a step."""
    return getattr(error, "autobot_trail", ())


def left(error: BaseException) -> Left | None:
    """What the run that `error` ended may have left behind; None when every breakout finished."""
    return getattr(error, "autobot_left", None)
