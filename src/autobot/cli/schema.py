"""The JSON schema `autobot schema` prints: read from the package, with a def per installed plugin step added
(see SPEC, CLI)."""

from __future__ import annotations

import copy
import importlib.resources
import json
import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..protocols import StepExecutor

SCHEMA_NAME = "autobot.2026-10.json"


class SchemaError(RuntimeError):
    """The schema is in neither place it is read from; the CLI reports it without a traceback."""


def load_schema() -> dict[str, Any]:
    """The JSON schema, read from the package it ships in.

    The repository keeps one copy, `schemas/`, and the package holds a link to it that a build turns into
    the file. In a source tree the link may be missing, or be a text file holding the link's target (a
    checkout made without symbolic links): `schemas/` is read then.
    """
    packaged = importlib.resources.files("autobot") / SCHEMA_NAME
    # this file is src/autobot/cli/schema.py: the repository's root is four levels up
    source = Path(__file__).resolve().parents[3] / "schemas" / SCHEMA_NAME
    for candidate in (packaged, source):
        try:
            schema = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(schema, dict):
            return schema
    raise SchemaError(f"neither {packaged} nor {source} holds the JSON schema")


def add_plugin_steps(schema: dict[str, Any], executors: list[StepExecutor]) -> dict[str, Any]:
    """Add a `<key>Step` def per plugin to `step.oneOf`, and take its key out of the `pluginStep` catch-all."""
    defs = schema["$defs"]
    one_of = defs["step"]["oneOf"]
    for executor in executors:
        name = f"{executor.key}Step"
        # the name as a JSON pointer token in a URI fragment: `~` and `/` escaped, the rest percent-encoded
        ref = f"#/$defs/{urllib.parse.quote(name.replace('~', '~0').replace('/', '~1'), safe='')}"
        prefix = f"{ref}/$defs/"
        model = executor.model.model_json_schema(ref_template=f"{prefix}{{model}}")
        step: dict[str, Any] = {"$defs": model.pop("$defs")} if "$defs" in model else {}
        # a recursive model's root is a $ref to its own def: inline a copy, so the step's root is open to
        # the common props, and keep the closed def for the nested references
        root = model.get("$ref", "")
        if root.startswith(prefix) and root.removeprefix(prefix) in step.get("$defs", {}):
            del model["$ref"]
            model |= copy.deepcopy(step["$defs"][root.removeprefix(prefix)])
        # the plugin's model sees every key but the common props, so its closing additionalProperties
        # moves out to cover the keys neither it nor stepCommon evaluates
        rest = model.pop("additionalProperties", None)
        step |= {"type": "object", "required": [executor.key], "allOf": [{"$ref": "#/$defs/stepCommon"}, model]}
        if rest is not None:
            step["unevaluatedProperties"] = rest
        defs[name] = step
        one_of.insert(-1, {"$ref": ref})
        defs["pluginStep"]["not"]["anyOf"].append({"required": [executor.key]})
    return schema
