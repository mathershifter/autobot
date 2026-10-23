"""From the `prompts` of the script to the handlers of the session: a prompt's `send` as a template or as
the credential sets of a `sendEach` collection (see SPEC, Prompt Handling)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .errors import ScriptError
from .models import Prompt, SendEach
from .session import PromptHandler, SimpleHandler
from .templating import check_template
from .types import text, yaml_kind


def _scalar(value: Any) -> bool:
    return isinstance(value, (str, int, float)) and not isinstance(value, bool)


def _not_text(value: bool) -> str:
    # YAML reads yes, on and True as the same boolean, so the value as written isn't known here
    return (
        f"a boolean ({text(value)}), which is never sent as text; "
        "quote the value to send it as written, e.g. 'true' or 'yes'"
    )


def send_each_sets(name: str, send: SendEach, vars: dict[str, Any]) -> list[list[str]]:
    """Credential sets from `send.each` (a `vars.a.b` path of mapping keys), one per item."""

    def fail(problem: str) -> ScriptError:
        return ScriptError(f"prompt '{name}': sendEach '{send.each}': {problem}")

    obj: Any = vars
    at = "vars"
    for key in send.each.split(".")[1:]:
        if not isinstance(obj, dict):
            raise fail(f"'{at}' is {yaml_kind(obj)}, not a mapping")
        if key not in obj:
            raise fail(f"no key '{key}' in '{at}'")
        obj, at = obj[key], f"{at}.{key}"
    if not isinstance(obj, list):
        raise fail(f"'{at}' is {yaml_kind(obj)}, not a list")

    fields = [e.field for e in send.fields or []]
    sets: list[list[str]] = []
    for i, item in enumerate(obj):
        if not fields:
            if isinstance(item, bool):
                raise fail(f"item {i} is {_not_text(item)}")
            if not _scalar(item):
                raise fail(f"item {i} is {yaml_kind(item)}; without fields each item must be a string or number")
            sets.append([str(item)])
            continue
        if not isinstance(item, dict):
            raise fail(f"item {i} is {yaml_kind(item)}, not a mapping")
        for f in fields:
            if f not in item:
                raise fail(f"item {i} has no field '{f}'")
            if isinstance(item[f], bool):
                raise fail(f"item {i} field '{f}' is {_not_text(item[f])}")
            if not _scalar(item[f]):
                raise fail(f"item {i} field '{f}' is {yaml_kind(item[f])}, not a string or number")
        sets.append([str(item[f]) for f in fields])
    return sets


def build_handler(prompt: Prompt, vars: dict[str, Any], render: Callable[[str], str]) -> PromptHandler:
    """The session's handler for `prompt`: a shell prompt, a `send` template rendered by `render` each time it
    is sent, or a `sendEach` with its credential sets resolved from `vars` as they are now."""
    send = prompt.send
    expect = [e for e in prompt.expect or [] if isinstance(e, str)]  # grouped entries are rejected
    if send is None:  # a shell prompt (a return prompt can't have send)
        return PromptHandler(prompt.name, expect, [], True, posix=prompt.posix)
    if isinstance(send, str):
        # a literal send is a template, rendered each time it is sent
        try:
            check_template(send)
        except ScriptError as e:  # named like the error of rendering it, and of a sendEach
            raise ScriptError(f"prompt '{prompt.name}': {e}") from e
        return SimpleHandler(prompt.name, expect, send, render)
    patterns: list[str] = []
    slots: list[int | None] = []
    if send.fields:
        # entry k's regexes are alternatives that send field k of the current item
        for k, entry in enumerate(send.fields):
            patterns.extend(entry.match)
            slots.extend([k] * len(entry.match))
    patterns.extend(expect)
    slots.extend([None] * len(expect))
    responses = send_each_sets(prompt.name, send, vars)
    return PromptHandler(prompt.name, patterns, responses, False, slots)
