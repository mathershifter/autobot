"""The operator echo: what the session reads, written to stdout as the device sent it, less the escape sequences."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from .screen import ANSI_ESCAPE_RE

if TYPE_CHECKING:
    from .protocols import Reporter

# the start of a sequence ANSI_ESCAPE_RE removes, cut off by the end of a read: ESC, or an unfinished CSI
_PARTIAL_ESCAPE_RE = re.compile(r"\x1B(?:\[[0-?]*[ -/]*)?\Z")
# longer than any real sequence; a longer run after an ESC is written out rather than held
ESCAPE_HOLD = 64


class CleanWriter:
    """The operator echo: what is read from the session, without the escape sequences ANSI_ESCAPE_RE removes.
    The `reporter` is told where each write leaves the line, and keeps the loss when a write fails."""

    def __init__(self, stream, reporter: Reporter):
        self._stream = stream
        self._reporter = reporter
        self._held = ""

    def write(self, data):
        data = ANSI_ESCAPE_RE.sub("", self._held + data)
        self._held = ""
        # a sequence may arrive in two reads: keep its start back until the next one completes it
        m = _PARTIAL_ESCAPE_RE.search(data)
        if m and len(data) - m.start() <= ESCAPE_HOLD:
            data, self._held = data[: m.start()], data[m.start() :]
        if data:
            self._out(data)
            self._reporter.echoed(data)

    def _out(self, data: str = ""):
        """Write `data` and flush. A failure ends the run (`log.OutputLost`), and no write is tried after it."""
        with self._reporter.writing(self._stream):
            if data:
                self._write(data)
            self._stream.flush()

    def _write(self, data: str):
        try:
            self._stream.write(data)
        except UnicodeEncodeError:
            # the stream's encoding can't represent a character the session sent (e.g. an ASCII stdout):
            # write its escape rather than fail the step that was only reading
            encoding = getattr(self._stream, "encoding", None) or "ascii"
            self._stream.write(data.encode(encoding, "backslashreplace").decode(encoding))

    def flush(self):
        # pexpect flushes after every read, so this must not release what is held
        self._out()

    def close(self):
        """Write out what is still held: nothing more will come to complete it. The stream stays open.
        Nothing is written to a stream that could not be written before: that has ended the run already."""
        held, self._held = self._held, ""
        if self._reporter.lost:  # nothing is written after the write that failed
            return
        self._out(held)
        if held:
            self._reporter.echoed(held)
