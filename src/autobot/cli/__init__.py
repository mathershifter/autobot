"""The `autobot` command: `run`, `validate` and `schema` (see SPEC, CLI). The command line is parsed and
dispatched here; the script is read by `loader`, its validation errors are rendered by `validation`, the
schema comes from `schema`, a signal is handled by `signals`, and `report` says how a run ended."""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from collections.abc import Sequence
from typing import Any

import pydantic

from ..errors import BreakoutError, ScriptError
from ..log import ERROR, OK, WARN, ConsoleReporter, OutputLost
from ..models import PLUGINS, Config
from ..registry import PluginError, registry
from ..report import trail
from ..runner import Runner
from . import loader, report, schema, signals, validation
from .report import (
    EXIT_BREAKOUT,
    EXIT_INTERRUPTED,
    EXIT_LOAD,
    EXIT_RUN,
    EXIT_UNEXPECTED,
    EXPECTED,
    LoadError,
    visible,
)

__all__ = [
    "EXIT_BREAKOUT", "EXIT_INTERRUPTED", "EXIT_LOAD", "EXIT_RUN", "EXIT_UNEXPECTED", "EXPECTED", "LoadError", "main",
]


def _discover(rep: ConsoleReporter, args: argparse.Namespace | None = None) -> None:
    # validation depends on the installed plugins, so a broken one is reported first, before the script is read
    try:
        registry.discover()
    except PluginError as e:
        report.show_traceback(rep, args, e)
        rep.error("Plugin error", str(e))
        raise LoadError from None


def _config(rep: ConsoleReporter, script: str) -> Config:
    """The validation stage, which `run` and `validate` share: the file read and parsed, and the document
    validated against the models. Nothing of the script is rendered or run. A load error is reported, and
    raised as `LoadError`."""
    config_dict = loader.load(rep, script)
    try:
        return Config.model_validate(config_dict, context={PLUGINS: registry})
    except pydantic.ValidationError as e:
        validation.report(rep, e)
        raise LoadError from None


def _runner(rep: ConsoleReporter, args: argparse.Namespace, config: Config) -> Runner:
    """What `run` adds to the validation stage before it runs anything: the `--env` values checked, each
    for its form and a key the script declares, and `prompts` resolved. An error is reported, and raised
    as `LoadError`."""
    given = {}
    for item in args.env:
        key, eq, value = item.partition("=")
        if not eq or not key:  # no `env` key is empty
            rep.error("--env requires KEY=VALUE format")  # without the argument, which may be a value
            raise LoadError
        given[key] = value

    try:
        return Runner(config, given, reporter=rep)
    except ScriptError as e:  # the `--env` keys and prompt send templates, checked before prepare/spawn
        report.show_traceback(rep, args, e)
        rep.error(f"Script error in {visible(args.script)}", str(e))
        raise LoadError from None


def _cmd_run(rep: ConsoleReporter, args: argparse.Namespace) -> None:
    if sys.stdout is None:
        # fd 1 is closed (`>&-`): the session's output has nowhere to go, and the next file opened, the
        # session's pty for one, would become fd 1
        rep.error(
            "Cannot write the session's output",
            "stdout is closed (redirect it to /dev/null to discard the output)",
        )
        raise LoadError
    _discover(rep, args)
    runner = _runner(rep, args, _config(rep, args.script))
    try:
        with contextlib.ExitStack() as stack:
            # from the spawn on: until then `prepare` deals with a signal itself
            runner.before_attach = lambda: stack.enter_context(signals.ended_by_signal(rep))
            runner.run()
    except OutputLost as e:
        report.lost(rep, args, e)
        raise
    except BreakoutError as e:
        report.left_behind(rep, args, e)
        sys.exit(EXIT_BREAKOUT)
    except EXPECTED as e:
        if report.broad(e) and report.plugins_own(e):
            raise  # a bug in the plugin, like any other exception of its own
        report.show_traceback(rep, args, e)
        reason = str(e) or type(e).__name__
        if isinstance(e, RecursionError) and any(ref.key == "call" for ref in trail(e)):
            reason = f"functions call each other too deeply ({reason})"
        rep.error(f"Run failed in {visible(args.script)}", reason)
        report.where(rep, e)
        if report.broad(e) and not args.traceback:
            rep.hint("(run with --traceback for details)")
        report.left_behind(rep, args, e)
        sys.exit(EXIT_RUN)
    rep.say("run completed", "ok")


def _cmd_validate(rep: ConsoleReporter, args: argparse.Namespace) -> None:
    """`run`'s validation stage for each script, and nothing after it: no template is rendered and nothing
    runs. Nothing is written to stdout, so a closed one is no error."""
    _discover(rep, args)
    failed = False
    for script in args.script:
        try:
            _config(rep, script)
        except LoadError:
            failed = True
            rep.verdict(visible(script), "invalid", ERROR)
        except Exception as e:  # noqa: BLE001 - a bug, in autobot or in a plugin's model: say which script
            report.unexpected(rep, e, script)
            sys.exit(EXIT_UNEXPECTED)
        else:
            if not args.quiet:
                rep.verdict(visible(script), "valid", OK)
    if failed:
        raise LoadError


def _cmd_schema(rep: ConsoleReporter, args: argparse.Namespace | None = None) -> None:
    _discover(rep, args)
    try:
        document = schema.load_schema()
    except schema.SchemaError as e:
        rep.error("Cannot read the schema", str(e))
        raise LoadError from None
    with rep.writing(sys.stdout):
        print(json.dumps(schema.add_plugin_steps(document, registry.plugin_executors()), indent=2), flush=True)


def main(argv: Sequence[str] | None = None, reporter: ConsoleReporter | None = None) -> None:
    """`argv`: the arguments after the command's name; this process's own (`sys.argv[1:]`) when none is
    given, and `sys.argv` is not changed either way. `reporter`: where the command reports; built here,
    for this process's stderr, when none is given."""
    rep = reporter if reporter is not None else ConsoleReporter()
    code: Any = None
    rep.clear()
    try:
        _main(rep, list(sys.argv[1:] if argv is None else argv))
    except OutputLost:  # reported where there was something to say, and a stream to say it on
        code = EXIT_RUN
    except SystemExit as e:
        code = 0 if e.code is None else e.code
    finally:
        # output that could not be written is a failure of any command that had not failed already
        if signals.discard_unwritten() and not code:
            code = EXIT_RUN
        rep.clear()
    if code is not None:
        sys.exit(code)


def _main(rep: ConsoleReporter, argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="autobot", description="Autobot console robot.")
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="Execute an autobot script")
    run_parser.add_argument("script", help="Path to the YAML script file")
    validate_parser = subparsers.add_parser(
        "validate",
        help="Check autobot scripts without running them",
        description="Validate each script as `run` does before anything else, and stop there: no template "
        "is rendered, prepare is not run and no session is spawned. For each script, print "
        "`<script>: valid`, or the error `run` would report and `<script>: invalid`, on stderr. Exit with "
        "status 1 if any script is invalid.",
    )
    validate_parser.add_argument("script", nargs="+", help="Path to a YAML script file")
    run_parser.add_argument(
        "-e",
        "--env",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Set a variable that the script's env section declares (e.g. --env CONSOLE_HOST=10.0.0.1)",
    )
    validate_parser.add_argument("-q", "--quiet", action="store_true", help="Print nothing for a valid script")

    schema_parser = subparsers.add_parser("schema", help="Print augmented JSON schema to stdout")
    for sub in (run_parser, validate_parser, schema_parser):
        sub.add_argument(
            "--traceback",
            action="store_true",
            help="Also print the Python traceback of an error that is reported without one",
        )

    # from here on an interrupt, or a bug, is reported as one of the run's, so parsing is inside the try:
    # an interrupt before the run prints `Interrupted` alone (SPEC, Errors while the script runs)
    args: argparse.Namespace | None = None
    try:
        if argv and argv[0] not in ("run", "validate", "schema", "-h", "--help"):
            argv = ["run", *argv]  # a script alone is `run <script>`
        if argv[:1] == ["validate"]:
            # its options may come between the scripts, which the parser of the subcommands doesn't allow
            args = validate_parser.parse_intermixed_args(argv[1:])
            args.command = "validate"
        else:
            args = parser.parse_args(argv)

        if args.command == "schema":
            _cmd_schema(rep, args)
        elif args.command == "run":
            _cmd_run(rep, args)
        elif args.command == "validate":
            _cmd_validate(rep, args)
        else:
            parser.print_help()
            sys.exit(EXIT_LOAD)
    except KeyboardInterrupt as e:
        report.show_traceback(rep, args, e)
        rep.error("Interrupted", style=WARN)
        report.where(rep, e)
        report.left_behind(rep, args, e)
        signals.interrupted()
    except Exception as e:  # noqa: BLE001 - a bug in autobot or a plugin: say so and keep the traceback
        report.unexpected(rep, e)
        report.left_behind(rep, args, e)
        sys.exit(EXIT_UNEXPECTED)
