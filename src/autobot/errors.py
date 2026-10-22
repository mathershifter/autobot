"""The errors Autobot raises for what the script, the device or the environment did: the CLI reports them
without a traceback (see SPEC, Errors while the script runs)."""

from __future__ import annotations


class ScriptError(ValueError):
    """A problem of the script that shows only while it runs: a template, a rendered pattern, a `sendEach`
    collection. The CLI reports it without a traceback."""


class RunError(RuntimeError):
    """A failure of the run that the device or the environment explains: a failed command, a prompt out of
    responses, a failed `prepare`. The CLI reports it without a traceback."""


class StepFailure(RunError):
    """A command failure that `ignore_error` swallows (exit code, assert, upload)."""


class CommandError(RunError):
    """An `errors` pattern matched the output of a command; `output` is what the prompt wait captured."""

    def __init__(self, message: str, output: str = ""):
        super().__init__(message)
        self.output = output


class LineTooLong(RunError):
    """A line that the terminal of the spawned process would cut: it is not sent."""


class PartialLine(RunError):
    """A line that would be added to the part of an earlier one that is typed at the far side: it is not sent."""
