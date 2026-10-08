"""The terminal of the spawned process, its pty: the window and the environment it starts with, the mode it
is in and the length of line it takes, and the writing of bytes to it.

What is known of one platform or another is here. What the text that comes back shows is in `screen`.
"""

from __future__ import annotations

import errno
import fcntl
import os
import re
import select
import sys
import termios
import time
from collections.abc import Callable

DEFAULT_ENV = {"TERM": "dumb", "NO_COLOR": "1"}

# The window of the spawned pty. A line editor on a terminal that wraps clears the screen and writes the
# prompt again for a line that doesn't fit on the screen, so the screen is tall: 40,000 characters. Not
# taller: a full-screen program draws every row, and a device may take its terminal length from this
# window and accept only so much.
PTY_ROWS = 500
PTY_COLS = 80


# What a write to a pty whose other side is gone fails with: the session is closed, as an EOF on a read says.
CLOSED_ERRNOS = (errno.EIO, errno.EPIPE, errno.ENXIO)
# A write of which the pty takes nothing, although it is reported writable, is tried again after this long.
SEND_RETRY = 0.01

# The bytes of a line, its line break included, that the Linux terminal takes in canonical mode
# (N_TTY_BUF_SIZE). The terminals of most devices a script reaches are Linux ones.
LINUX_CANON = 4096
# What a system may answer for its own limit and be believed: no less than POSIX lets a system have
# (_POSIX_MAX_CANON), and no more than any terminal's line buffer. `fpathconf` answers -1 for "no limit"
# and for "can't say"; a wrong limit would refuse lines that the terminal takes.
CANON_RANGE = range(255, 65536 + 1)


def canon_limit(fd: int) -> int | None:
    """The bytes of one line, its line break included, that the terminal `fd` takes in canonical mode.
    None where the limit isn't known: nothing is refused there."""
    if sys.platform.startswith("linux"):
        return LINUX_CANON  # not fpathconf: it reports 255 for a pty, the POSIX constant, which the kernel doesn't go by
    if sys.platform == "darwin":
        try:
            limit = os.fpathconf(fd, "PC_MAX_CANON")
        except Exception:  # noqa: BLE001 - whatever it is, the limit isn't known
            return None
        if isinstance(limit, int) and limit in CANON_RANGE:
            return limit
    return None


_CONTROL_KEYS = {"@": 0, "`": 0, "[": 27, "{": 27, "\\": 28, "|": 28, "]": 29, "}": 29, "^": 30, "~": 30, "_": 31, "?": 127}


def control_byte(char: str) -> bytes:
    """The control character of a key: Ctrl+A to Ctrl+Z in either case, and the punctuation keys. Empty for
    any other key."""
    char = char.lower()
    if len(char) == 1 and "a" <= char <= "z":
        return bytes([ord(char) - ord("a") + 1])
    return bytes([_CONTROL_KEYS[char]]) if char in _CONTROL_KEYS else b""


def run_environ() -> dict[str, str]:
    """The environment of a run: the process's own, on a plain terminal. An entry without a name is left
    out: no process can be started with one."""
    return {**{k: v for k, v in os.environ.items() if k}, **DEFAULT_ENV}


def closed(e: BaseException) -> bool:
    """Whether `e` is what a write to a pty whose other side is gone fails with."""
    return isinstance(e, OSError) and e.errno in CLOSED_ERRNOS


class Pty:
    """The pty of a child, by the file descriptor of its side here."""

    def __init__(self, fd: int):
        self.fd = fd
        self.done = 0  # the bytes of the last write that the pty took

    def longest(self, data: bytes) -> tuple[int, int | None] | None:
        """The bytes of the longest line in `data`, as the terminal breaks lines in the mode it is in now,
        and the bytes of one line that it takes: None when it is not in canonical mode or the limit isn't
        known. None for the two when there is no terminal to ask."""
        try:
            attrs = termios.tcgetattr(self.fd)
            icrnl, canonical = bool(attrs[0] & termios.ICRNL), bool(attrs[3] & termios.ICANON)
        except Exception:  # noqa: BLE001 - no terminal to ask (a closed one: the write says so) or no answer to go by
            return None
        breaks = rb"[\r\n]" if icrnl else rb"\n"
        longest = max(len(part) for part in re.split(breaks, data))
        limit = canon_limit(self.fd) if canonical else None
        return longest, limit

    def write(self, data: bytes, deadline: float, timeout: float, what: str, reader: Callable[[], object]):
        """Write `data`, by `deadline`, calling `reader` to read what the child writes whenever the pty
        takes no more: a child that echoes stops reading once nobody reads its echo.

        `timeout` and `what` are for the message of a write that isn't done by `deadline`. However the
        write ends, `done` is how much of `data` the pty took.
        """
        fd = self.fd
        self.done, idle = 0, False
        if fd < 0:  # closed here, not by the child
            raise OSError(errno.EBADF, os.strerror(errno.EBADF))
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        try:
            while self.done < len(data):
                try:
                    wrote = os.write(fd, data[self.done :])
                except BlockingIOError:
                    wrote = 0
                if wrote > 0:
                    self.done += wrote
                    idle = False
                    continue
                # the pty took nothing: every pass from here on checks the deadline
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"timed out after {timeout}s while sending {what} ({self.done} of {len(data)} bytes sent)"
                    )
                if idle:
                    time.sleep(min(SEND_RETRY, remaining))
                readable, writable, _ = select.select([fd], [fd], [], remaining)
                read = False
                if readable:
                    try:
                        reader()
                        read = True
                    except BlockingIOError:
                        pass  # reported readable, and nothing to read
                # reported writable though it took nothing, or readable with nothing to read: poll
                idle = bool(writable) or not read
        finally:
            fcntl.fcntl(fd, fcntl.F_SETFL, flags)
