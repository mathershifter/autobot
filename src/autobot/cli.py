from __future__ import annotations

import argparse
import copy
import importlib.resources
import json
import sys
import traceback
import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pexpect
import pydantic
import yaml
from yaml.constructor import ConstructorError
from yaml.reader import ReaderError

from . import log
from .models import Config
from .registry import PluginError, registry
from .runner import Runner, trail
from .types import RunError, ScriptError

if TYPE_CHECKING:
    from .protocols import StepExecutor

EXIT_LOAD = 1  # the script can't be loaded: nothing ran
EXIT_RUN = 3  # the run failed
EXIT_UNEXPECTED = 70  # a bug in autobot or a plugin (EX_SOFTWARE)
EXIT_INTERRUPTED = 130  # Ctrl-C

# What a script, the device or the environment explains, so the user can act on it: reported without a
# traceback. Anything else that ends a run is a bug in autobot or in a plugin.
EXPECTED = (
    ScriptError,  # a template, a rendered pattern, a block's sendEach collection
    RunError,  # a failed command, a prompt out of responses, a failed prepare
    TimeoutError,
    EOFError,  # the connection closed
    pexpect.ExceptionPexpect,  # the spawn command wasn't found, the process couldn't be terminated
    OSError,  # the pty or a temp file
    UnicodeError,  # text that can't be encoded for the pty or the prepare script
    RecursionError,  # functions that call each other without end
)
CALLERS = 5

MERGE_TAG = "tag:yaml.org,2002:merge"
VALUE_TAG = "tag:yaml.org,2002:value"  # a plain `=` key, which flatten_mapping turns into the string "="
_MERGE = object()


class UniqueKeyLoader(yaml.SafeLoader):
    """`SafeLoader` that rejects a mapping key given twice instead of keeping the last value.

    Keys pulled in by a `<<` merge may be overridden; only keys written in the mapping itself must be unique.
    """

    def __init__(self, stream: object) -> None:
        super().__init__(stream)
        self._checked: set[yaml.Node] = set()

    # every mapping (merge sources too) is flattened before it's built, and flattening rewrites node.value,
    # so check the keys as written, once per node, before the first flatten
    def flatten_mapping(self, node: yaml.MappingNode) -> None:
        if node not in self._checked:
            self._checked.add(node)
            seen: dict[object, yaml.Node] = {}
            for k, _ in node.value:
                key = _MERGE if k.tag == MERGE_TAG else k.value if k.tag == VALUE_TAG else self.construct_object(k)
                try:
                    first = seen.setdefault(key, k)
                except TypeError:  # unhashable: construct_mapping reports it
                    continue
                if first is not k:
                    name = k.value if isinstance(k, yaml.ScalarNode) else key
                    raise ConstructorError("first defined", first.start_mark, f"found duplicate key {name!r}", k.start_mark)
        super().flatten_mapping(node)


def _load(path: str) -> object:
    try:
        with open(path, "rb") as f:
            return yaml.load(f, Loader=UniqueKeyLoader)
    except OSError as e:
        log.error(f"Cannot read script {path}", str(e.strerror or e))
    except yaml.MarkedYAMLError as e:
        mark = e.problem_mark
        at = f", line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        log.error(f"YAML error in {path}{at}", str(e.problem))
        if e.context:
            mark = e.context_mark
            at = f" (line {mark.line + 1}, column {mark.column + 1})" if mark else ""
            log.more(f"  {e.context}{at}")
    except ReaderError as e:
        what = "character" if e.encoding == "unicode" else f"{e.encoding} byte"
        log.error(f"YAML error in {path}, position {e.position}", f"{e.reason} ({what} #x{e.character:02x})")
    except yaml.YAMLError as e:
        log.error(f"YAML error in {path}", str(e))
    sys.exit(EXIT_LOAD)


def _traceback(args: argparse.Namespace | None, e: BaseException) -> None:
    if getattr(args, "traceback", False):
        log.more("".join(traceback.format_exception(e)).rstrip("\n"))


def _where(e: BaseException) -> None:
    """The step that was running, and the calls that led to it. A block is part of its steps' paths."""
    steps = trail(e)
    if not steps:
        return
    *outer, last = steps
    log.note("at", f"{last.path} ({last.what})")
    callers = [ref for ref in reversed(outer) if ref.key != "block"]
    for ref in callers[:CALLERS]:
        log.note("called from", f"{ref.path} ({ref.what})")
    if len(callers) > CALLERS:
        log.note("...", f"and {len(callers) - CALLERS} more callers")


def _unexpected(e: Exception) -> None:
    steps = trail(e)
    if steps and steps[-1].plugin:
        log.error(
            f"Unexpected error in plugin '{steps[-1].key}'",
            "this is a bug in the plugin, not in the script. "
            "Please report it to the plugin's author with the traceback below.",
        )
    else:
        log.error(
            "Unexpected error in Autobot",
            "this is a bug, not a problem with the script. Please report it with the traceback below.",
        )
    _where(e)
    log.more("".join(traceback.format_exception(e)).rstrip("\n"))


def _discover(args: argparse.Namespace | None = None) -> None:
    # validation depends on the installed plugins, so a broken one is reported first, before the script is read
    try:
        registry.discover()
    except PluginError as e:
        _traceback(args, e)
        log.error("Plugin error", str(e))
        sys.exit(EXIT_LOAD)


def _cmd_run(args):
    _discover(args)
    config_dict = _load(args.script)

    try:
        config = Config.model_validate(config_dict)
    except pydantic.ValidationError as e:
        log.error("Validation errors:")
        log.more(e.json(indent=2))
        sys.exit(EXIT_LOAD)

    cli_args = {}
    for item in args.arg:
        if "=" not in item:
            log.error("--arg requires KEY=VALUE format, got", item)
            sys.exit(EXIT_LOAD)
        key, value = item.split("=", 1)
        cli_args[key] = value

    try:
        runner = Runner(config, cli_args)
    except ScriptError as e:  # env rendering and prompt send templates, checked before prepare/spawn
        _traceback(args, e)
        log.error(f"Script error in {args.script}", str(e))
        sys.exit(EXIT_LOAD)
    try:
        runner.run()
    except EXPECTED as e:
        _traceback(args, e)
        reason = str(e) or type(e).__name__
        if isinstance(e, RecursionError):
            reason = f"functions call each other too deeply ({reason})"
        log.error(f"Run failed in {args.script}", reason)
        _where(e)
        sys.exit(EXIT_RUN)


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
    source = Path(__file__).resolve().parent.parent.parent / "schemas" / SCHEMA_NAME
    for candidate in (packaged, source):
        try:
            schema = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(schema, dict):
            return schema
    raise SchemaError(f"neither {packaged} nor {source} holds the JSON schema")


def _cmd_schema(args: argparse.Namespace | None = None):
    _discover(args)
    try:
        schema = load_schema()
    except SchemaError as e:
        log.error("Cannot read the schema", str(e))
        sys.exit(EXIT_LOAD)
    print(json.dumps(add_plugin_steps(schema, registry.plugin_executors()), indent=2))


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


def main():
    parser = argparse.ArgumentParser(description="Autobot console robot.")
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="Execute an autobot script")
    run_parser.add_argument("script", help="Path to the YAML script file")
    run_parser.add_argument(
        "-a",
        "--arg",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Pass arguments to the script (e.g. --arg console_host=10.0.0.1)",
    )

    schema_parser = subparsers.add_parser("schema", help="Print augmented JSON schema to stdout")
    for sub in (run_parser, schema_parser):
        sub.add_argument(
            "--traceback",
            action="store_true",
            help="Also print the Python traceback of an error that is reported without one",
        )

    if len(sys.argv) > 1 and sys.argv[1] not in ("run", "schema", "-h", "--help"):
        sys.argv.insert(1, "run")

    args = parser.parse_args()

    try:
        if args.command == "schema":
            _cmd_schema(args)
        elif args.command == "run":
            _cmd_run(args)
        else:
            parser.print_help()
            sys.exit(EXIT_LOAD)
    except KeyboardInterrupt as e:
        _traceback(args, e)
        log.error("Interrupted", style=log.WARN)
        _where(e)
        sys.exit(EXIT_INTERRUPTED)
    except Exception as e:  # noqa: BLE001 - a bug in autobot or a plugin: say so and keep the traceback
        _unexpected(e)
        sys.exit(EXIT_UNEXPECTED)


if __name__ == "__main__":
    main()
