#!/usr/bin/env python3
"""A child that reads its terminal itself, a few bytes at a time, and echoes them (test plan fixture F8).

It puts the terminal in raw mode, as a line editor does, writes ``PROMPT$ `` and then, for every byte it
reads, ``ESC[1m`` and the byte ``--gain`` times. Its writes block when nobody reads them, and it reads
nothing while one does: what a long line needs in order to stall. A line break ends the line: it writes
``\\r\\nDONE <bytes>\\r\\nPROMPT$ ``. A Ctrl-C drops the line: ``^C\\r\\nPROMPT$ ``. With ``--log`` each is
appended to the file, as ``LINE=<text>`` (``LINE=<bytes> bytes ending <last 8>`` for a long one) and
``INTERRUPT=<bytes dropped>``.

``--stop N`` stops reading after N bytes, for good or for ``--pause`` seconds. ``--exit N`` exits after N.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import tty


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log")
    ap.add_argument("--gain", type=int, default=1)
    ap.add_argument("--stop", type=int, default=0)
    ap.add_argument("--pause", type=float, default=0)
    ap.add_argument("--exit", type=int, default=0)
    a = ap.parse_args()

    def log(text: str) -> None:
        if a.log:
            with open(a.log, "a") as f:
                f.write(text + "\n")

    tty.setraw(0)
    os.write(1, b"PROMPT$ ")
    count, line = 0, b""
    stop = a.stop
    while True:
        limit = min(1024, n - count) if (n := stop or a.exit) else 1024
        chunk = os.read(0, limit)
        if not chunk:
            sys.exit(0)
        count += len(chunk)
        for byte in chunk:
            ch = bytes([byte])
            if ch == b"\n":
                text = line.decode(errors="replace")
                log(f"LINE={text}" if len(line) <= 80 else f"LINE={len(line)} bytes ending {text[-8:]}")
                os.write(1, b"\r\nDONE %d\r\nPROMPT$ " % len(line))
                line = b""
            elif ch == b"\x03":
                log(f"INTERRUPT={len(line)}")
                os.write(1, b"^C\r\nPROMPT$ ")
                line = b""
            else:
                line += ch
                os.write(1, b"\x1b[1m" + ch * a.gain)
        if a.exit and count >= a.exit:
            sys.exit(0)
        if stop and count >= stop:
            time.sleep(a.pause or 100000)
            stop = 0


if __name__ == "__main__":
    main()
