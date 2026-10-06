from __future__ import annotations

import argparse
import copy
import importlib.resources
import json
import os
import signal
import sys
import traceback
import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

import pexpect
import pydantic
import yaml
from yaml.constructor import ConstructorError
from yaml.reader import ReaderError

from . import log
from .models import Config
from .registry import PluginError, registry
from .runner import Runner, _kind, trail
from .types import RunError, ScriptError, text

if TYPE_CHECKING:
    from .protocols import StepExecutor

EXIT_LOAD = 1  # the script can't be loaded: nothing ran
EXIT_RUN = 3  # the run failed
EXIT_UNEXPECTED = 70  # a bug in autobot or a plugin (EX_SOFTWARE)
EXIT_INTERRUPTED = 130  # Ctrl-C, where the process can't end from the signal itself

# What a script, the device or the environment explains, so the user can act on it: reported without a
# traceback. Anything else that ends a run is a bug in autobot or in a plugin.
EXPECTED = (
    ScriptError,  # a template, a rendered pattern, a block's sendEach collection
    RunError,  # a failed command, a prompt out of responses, a failed prepare
    TimeoutError,  # an OSError as well, but named for what it means here: it is never one of BROAD
    EOFError,  # the connection closed
    pexpect.ExceptionPexpect,  # the spawn command wasn't found, the process couldn't be terminated
    OSError,  # the pty or a temp file
    UnicodeError,  # text that can't be encoded for the prepare script
    RecursionError,  # functions that call each other without end
)
# Expected only for their built-in class, which a bug can raise as well: the report says where the
# traceback is, and from a plugin's own code they are the plugin's bug. A timeout isn't one of them.
BROAD = (OSError, UnicodeError, RecursionError)
CALLERS = 5
PACKAGE = os.path.dirname(os.path.abspath(__file__)) + os.sep

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


GOT_MAX = 60
# Error types that get no `(got <value>)`: the location says it all (a missing or an unknown key), the
# type's name says what the value is (empty), or the message is written with the value in it
NO_VALUE = frozenset({
    "missing", "extra_forbidden", "string_too_short", "too_short",
    "value_error", "assertion_error", "unsupported_version", "control_char", "invalid_regex", "undefined_function",
})
EITHER = "Input should be a string or a list of strings"


def _is_list_member(key: object) -> bool:
    return isinstance(key, str) and key.startswith("list[")


def _one_per_value(errors: list[Any]) -> list[Any]:
    """A field that takes a string or a list of strings reports a wrong value once for each of the two, at
    `<field>.str` and `<field>.list[str]`. Make that one error: at the field, saying what it takes, for a
    value that is neither; at the item, for a list with a wrong item."""
    def split(loc: tuple) -> tuple[tuple, object, tuple] | None:
        for i, key in enumerate(loc):
            if key == "str" or _is_list_member(key):
                return loc[:i], key, loc[i + 1 :]
        return None

    groups: dict[tuple, list[tuple[Any, object, tuple]]] = {}
    for err in errors:
        if parts := split(err["loc"]):
            groups.setdefault(parts[0], []).append((err, parts[1], parts[2]))
    replace: dict[int, list[Any]] = {}
    for base, members in groups.items():
        whole = [err for err, key, rest in members if key == "str" and not rest]
        lists = [(err, rest) for err, key, rest in members if _is_list_member(key)]
        if len(whole) != 1 or whole[0]["type"] != "string_type" or len(whole) + len(lists) != len(members):
            continue  # not the two members of one union: e.g. keys of a mapping that have these names
        value = whole[0]["input"]
        if isinstance(value, list):
            if not lists or not all(rest and isinstance(rest[0], int) for _, rest in lists):
                continue
            new = [{**err, "loc": (*base, *rest)} for err, rest in lists]
        elif len(lists) == 1 and not lists[0][1] and lists[0][0]["type"] == "list_type":
            # an entry of `expect` is typed like these fields, but a list there is an error of its own
            # (`grouped_expect`): only a string will do
            new = [{**whole[0], "loc": base, **({} if "expect" in base else {"msg": EITHER})}]
        else:
            continue
        replace[id(whole[0])] = new
        replace.update({id(err): [] for err, _ in lists})
    return [new for err in errors for new in replace.get(id(err), [err])]


def _sent(loc: tuple) -> bool:
    """Whether an error at `loc` is about text that is sent to the device or set in an environment, which may
    be a password: a prompt's `send` and what is in it, a `line` step, an `env` or `attach.env` value."""
    if loc[:1] == ("env",) or loc[:2] == ("attach", "env"):
        return True
    for i, key in enumerate(loc):
        if key == "send" and "prompts" in loc[:i]:
            return True
        # a step's type tag follows its index, and the step's own field the tag: `script.0.line.line`
        if key == "line" and i and isinstance(loc[i - 1], int) and loc[i + 1 : i + 2] == ("line",):
            return True
    return False


def _got(type_: str, msg: str, value: object, loc: tuple = ()) -> str:
    """The offending value, as YAML writes it, for a validation error whose message doesn't show it."""
    if type_ in NO_VALUE or _sent(loc):
        return ""
    if isinstance(value, dict):
        keys = ", ".join(map(str, value))
        shown = f"a mapping with the key{'s' if len(value) != 1 else ''} {keys}" if value else "an empty mapping"
    elif isinstance(value, list):
        shown = f"a list of {len(value)} item{'s' if len(value) != 1 else ''}"
    elif value is None or isinstance(value, (bool, int, float)):
        shown = "null" if value is None else text(value)
    elif isinstance(value, str):
        shown = repr(value)
        if shown in msg:  # quoted in full, as a message that shows the value does: not a word that happens to match
            return ""
    else:
        shown = _kind(value)
    if len(shown) > GOT_MAX:
        shown = shown[:GOT_MAX] + "..."
    return f" (got {shown})"


def _visible(text: str) -> str:
    """`text` on one line and safe for a terminal: every character that isn't printable (a line break, a tab,
    ESC and the other control characters) as its escape, e.g. `\\n`, `\\x1b`."""
    return "".join(c if c.isprintable() else c.encode("unicode_escape").decode("ascii") for c in text)


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


def _broad(e: BaseException) -> bool:
    return isinstance(e, BROAD) and not isinstance(e, TimeoutError)


def _plugins_own(e: BaseException) -> bool:
    """Whether a plugin's own code raised `e`, not the engine under it: the step that was running is a
    plugin step, and the last frame of autobot in the traceback is the runner's call of its `execute`.
    A frame of the session or of a step below that call means the plugin only called the engine."""
    steps = trail(e)
    if not (steps and steps[-1].plugin):
        return False
    last = None
    tb = e.__traceback__
    while tb is not None:
        code = tb.tb_frame.f_code
        if os.path.abspath(code.co_filename).startswith(PACKAGE):
            last = code
        tb = tb.tb_next
    return last is not None and last.co_name == "_step" and os.path.basename(last.co_filename) == "runner.py"


def _unexpected(e: Exception) -> None:
    # under a plugin step, whatever fails unexpectedly is the plugin's doing: its own code, or a step it
    # built that no script could hold
    plugin = next((ref for ref in reversed(trail(e)) if ref.plugin), None)
    if plugin:
        log.error(
            f"Unexpected error in plugin '{plugin.key}'",
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


def _interrupted() -> NoReturn:
    """End as a process that SIGINT killed: a shell reports status 130, and a loop around autobot stops,
    which it wouldn't for a process that exits with a status of its own."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (AttributeError, OSError, ValueError):  # no stream, or a closed one
            pass
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    os.kill(os.getpid(), signal.SIGINT)
    sys.exit(EXIT_INTERRUPTED)  # the signal is blocked or didn't arrive


def _discover(args: argparse.Namespace | None = None) -> None:
    # validation depends on the installed plugins, so a broken one is reported first, before the script is read
    try:
        registry.discover()
    except PluginError as e:
        _traceback(args, e)
        log.error("Plugin error", str(e))
        sys.exit(EXIT_LOAD)


def _cmd_run(args):
    if sys.stdout is None:
        # fd 1 is closed (`>&-`): the session's output has nowhere to go, and the next file opened, the
        # session's pty for one, would become fd 1
        log.error(
            "Cannot write the session's output",
            "stdout is closed (redirect it to /dev/null to discard the output)",
        )
        sys.exit(EXIT_LOAD)
    _discover(args)
    config_dict = _load(args.script)

    try:
        config = Config.model_validate(config_dict)
    except pydantic.ValidationError as e:
        log.error("Validation errors:")
        for err in _one_per_value(e.errors()):
            where = ".".join(map(str, err["loc"])) or "(document)"
            what = err["msg"].removeprefix("Value error, ")
            # the message is the model's, or a plugin model's, and may show a value of the script as it is
            log.problem(_visible(where), _visible(what + _got(err["type"], what, err["input"], err["loc"])), err["type"])
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
        if _broad(e) and _plugins_own(e):
            raise  # a bug in the plugin, like any other exception of its own
        _traceback(args, e)
        reason = str(e) or type(e).__name__
        if isinstance(e, RecursionError) and any(ref.key == "call" for ref in trail(e)):
            reason = f"functions call each other too deeply ({reason})"
        log.error(f"Run failed in {args.script}", reason)
        _where(e)
        if _broad(e) and not args.traceback:
            log.hint("(run with --traceback for details)")
        sys.exit(EXIT_RUN)
    log.say("run completed", "ok")


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
        _interrupted()
    except Exception as e:  # noqa: BLE001 - a bug in autobot or a plugin: say so and keep the traceback
        _unexpected(e)
        sys.exit(EXIT_UNEXPECTED)


if __name__ == "__main__":
    main()
