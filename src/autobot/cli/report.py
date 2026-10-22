"""How the CLI ends and reports: the exit statuses, the errors reported without a traceback, and the reports
of where a run failed, what it left behind and what an unexpected error is (see SPEC, Errors while the
script runs)."""

from __future__ import annotations

import argparse
import contextlib
import os
import traceback

import pexpect

from ..errors import BreakoutError, RunError, ScriptError
from ..log import ConsoleReporter, OutputLost
from ..report import left, trail

EXIT_LOAD = 1  # the script can't be loaded: nothing ran
EXIT_RUN = 3  # the run failed
EXIT_BREAKOUT = 4  # the script completed, and a breakout did not finish
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
# What is left when a run ends without its breakouts: after SIGTERM, SIGHUP, or a write of its output that failed
UNKNOWN = "the device may be in an unknown state and may still be logged in"
UNCLEAN = f"the session was closed without running the breakouts; {UNKNOWN}"

# the `autobot` package: the frames of its modules, the CLI's included, are Autobot's in a traceback
PACKAGE = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + os.sep


class LoadError(SystemExit):
    """A load error, raised once it is reported: the script can't be loaded, and the CLI exits with status 1."""

    def __init__(self) -> None:
        super().__init__(EXIT_LOAD)


def visible(text: str) -> str:
    """`text` on one line and safe for a terminal: every character that isn't printable (a line break, a tab,
    ESC and the other control characters) as its escape, e.g. `\\n`, `\\x1b`. A script's path is printed
    this way wherever a report names it: a file name can hold any of them."""
    return "".join(c if c.isprintable() else c.encode("unicode_escape").decode("ascii") for c in text)


def show_traceback(rep: ConsoleReporter, args: argparse.Namespace | None, e: BaseException) -> None:
    """The traceback of `e`, when `--traceback` asks for one that the report leaves out."""
    if getattr(args, "traceback", False):
        rep.more("".join(traceback.format_exception(e)).rstrip("\n"))


def where(rep: ConsoleReporter, e: BaseException) -> None:
    """The step that was running, and the calls that led to it. A block is part of its steps' paths."""
    steps = trail(e)
    if not steps:
        return
    *outer, last = steps
    rep.note("at", f"{last.path} ({last.what})")
    callers = [ref for ref in reversed(outer) if ref.key != "block"]
    for ref in callers[:CALLERS]:
        rep.note("called from", f"{ref.path} ({ref.what})")
    if len(callers) > CALLERS:
        rep.note("...", f"and {len(callers) - CALLERS} more callers")


def left_behind(rep: ConsoleReporter, args: argparse.Namespace | None, e: BaseException) -> None:
    """After the report of how the run ended: each breakout that did not finish, and what that may mean."""
    state = left(e)
    if state is None:
        return
    head = f"Breakout failed in {visible(getattr(args, 'script', ''))}"
    for error in state.breakouts:
        if error is e:  # the interrupt that ended the breakout is what ended the run: reported already
            continue
        if isinstance(error, KeyboardInterrupt):
            show_traceback(rep, args, error)
            rep.error(head, "interrupted")
            where(rep, error)
        elif isinstance(error, EXPECTED) and not (broad(error) and plugins_own(error)):
            show_traceback(rep, args, error)
            rep.error(head, str(error) or type(error).__name__)
            where(rep, error)
            if broad(error) and not getattr(args, "traceback", False):
                rep.hint("(run with --traceback for details)")
        else:  # a bug, in Autobot or in a plugin, not something the device did: with its traceback
            rep.error(head, f"unexpected error ({type(error).__name__}): {error}")
            unexpected(rep, error)  # type: ignore[arg-type]
    completed = isinstance(e, BreakoutError)
    if all(isinstance(error, EOFError) for error in state.breakouts):
        # nothing more could be said to the far side: over plain ssh the close is the logout
        head = "Connection closed before a breakout finished"
        why = ("the script completed; " if completed else "") + "a console behind a console server may still be logged in"
    else:
        head = "Session may be left logged in"
        why = ("the script completed, but " if completed else "") + "a breakout did not finish"
    if state.logins:
        names = f"prompt{'s' if len(state.logins) != 1 else ''} {', '.join(repr(name) for name in state.logins)}"
        if state.pending:
            why += f"; credentials were sent ({names}), and no shell prompt was reached after them"
        else:
            why += f"; the run sent credentials ({names})"
    rep.error(head, why)


def broad(e: BaseException) -> bool:
    return isinstance(e, BROAD) and not isinstance(e, TimeoutError)


def plugins_own(e: BaseException) -> bool:
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


def unexpected(rep: ConsoleReporter, e: Exception, script: str | None = None) -> None:
    """A bug, in Autobot or in a plugin: whose it is, where it was, and the traceback. `script`: the one of
    several that was being checked (`validate`), named after where it failed."""
    # under a plugin step, whatever fails unexpectedly is the plugin's doing: its own code, or a step it
    # built that no script could hold
    plugin = next((ref for ref in reversed(trail(e)) if ref.plugin), None)
    if plugin:
        rep.error(
            f"Unexpected error in plugin '{plugin.key}'",
            "this is a bug in the plugin, not in the script. "
            "Please report it to the plugin's author with the traceback below.",
        )
    else:
        rep.error(
            "Unexpected error in Autobot",
            "this is a bug, not a problem with the script. Please report it with the traceback below.",
        )
    where(rep, e)
    if script is not None:
        rep.note("while checking", visible(script))
    rep.more("".join(traceback.format_exception(e)).rstrip("\n"))


def lost(rep: ConsoleReporter, args: argparse.Namespace, e: OutputLost) -> None:
    """Report a run whose output was lost, on stderr if stderr can still be written, with the breakouts
    that didn't finish."""
    state = left(e)
    if state and e in state.breakouts:
        how = "the session was closed before the breakouts finished"
    elif trail(e):
        how = "the session was closed without running the breakouts"
    else:  # at the spawn, or at the close, after the breakouts
        how = "the session was closed"
    with contextlib.suppress(OutputLost):  # stderr may be the stream that is lost
        show_traceback(rep, args, e)
        rep.error(f"Run failed in {visible(args.script)}", f"{e}; {how}; {UNKNOWN}")
        where(rep, e)
        left_behind(rep, args, e)
