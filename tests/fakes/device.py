#!/usr/bin/env python3
"""Scriptable fake console device (test plan fixture F2).

Spawned as ``attach.spawn`` (or from a shell with ``line``). It prints a
banner, then login/password prompts in a configurable order, and finally
drops into a shell (or a bare ``PROMPT$ `` loop). Every line it reads is
appended to ``--log`` as ``<KIND>=<value>`` so tests can assert on what
Autobot actually sent, instead of parsing pty output.
"""

from __future__ import annotations

import argparse
import os
import sys
import termios
import time
import tty

PROMPTS = {"login": "login: ", "password": "Password: "}
SHELL_PROMPT = "PROMPT$ "


class Device:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.order = [p for p in args.order.split(",") if p and p != "none"]
        for p in self.order:
            if p not in PROMPTS:
                sys.exit(f"device: unknown prompt {p!r}")

    def log(self, kind: str, value: str) -> None:
        with open(self.args.log, "a") as f:
            f.write(f"{kind}={value}\n")
            f.flush()

    @staticmethod
    def write(text: str) -> None:
        os.write(1, text.encode())

    @staticmethod
    def readline() -> str:
        # canonical mode: one read() returns at most one line, so nothing is
        # left in a Python-side buffer when we exec the shell later
        data = os.read(0, 4096)
        if not data:
            sys.exit(0)
        return data.decode(errors="replace").rstrip("\r\n")

    def auth(self, first_prompt_written: bool = False) -> None:
        if not self.order:
            return
        failures = 0
        while True:
            got: dict[str, str] = {}
            for k, p in enumerate(self.order):
                if not (first_prompt_written and k == 0):
                    self.write(PROMPTS[p])
                first_prompt_written = False
                got[p] = self.readline()
                self.log(p.upper(), got[p])
            cred = f"{got.get('login', '')}:{got.get('password', '')}"
            if cred in self.args.accept:
                return
            failures += 1
            self.write("Login incorrect\n")
            if self.args.max_attempts and failures >= self.args.max_attempts:
                self.write("Too many failures\n")

    def rawdump(self, n: int) -> None:
        saved = termios.tcgetattr(0)
        tty.setraw(0)
        try:
            self.write("RAW> ")
            data = b""
            while len(data) < n:
                chunk = os.read(0, n - len(data))
                if not chunk:
                    sys.exit(0)
                data += chunk
        finally:
            termios.tcsetattr(0, termios.TCSANOW, saved)
        self.log("RAW", data.hex())
        self.write(f"\nRAW={data.hex()}\n")

    def then(self) -> None:
        if self.args.post_auth_delay:
            time.sleep(self.args.post_auth_delay)
        if self.args.then == "shell":
            env = {"PS1": SHELL_PROMPT, "TERM": "dumb", "PATH": os.environ.get("PATH", "")}
            os.execvpe("bash", ["bash", "--norc", "--noprofile", "-i"], env)
        while True:
            self.write(SHELL_PROMPT)
            self.log("LINE", self.readline())

    def run(self) -> None:
        a = self.args
        if a.silent:
            attrs = termios.tcgetattr(0)
            attrs[3] &= ~termios.ECHO
            termios.tcsetattr(0, termios.TCSANOW, attrs)
        first_written = False
        if a.same_chunk:
            first = PROMPTS[self.order[0]] if self.order else SHELL_PROMPT
            self.write(f"{a.banner}\n{first}")
            if not self.order:
                # the shell prompt is already on screen
                self.log("LINE", self.readline())
            first_written = bool(self.order)
        else:
            self.write(f"{a.banner}\n")
        if a.wait_enter:
            self.log("ENTER", self.readline())
        if a.exit_after_banner:
            sys.exit(0)
        if a.silent:
            while True:
                self.log("SILENT", self.readline())
        if a.rawdump:
            self.rawdump(a.rawdump)
        for _ in range(a.repeat):
            self.auth(first_written)
            first_written = False
            self.write(SHELL_PROMPT)
            self.log("LINE", self.readline())
        self.auth(first_written)
        self.then()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--log", required=True)
    p.add_argument("--banner", default="Welcome")
    p.add_argument("--wait-enter", action="store_true")
    p.add_argument("--same-chunk", action="store_true")
    p.add_argument("--order", default="login,password")
    p.add_argument("--accept", action="append", default=[])
    p.add_argument("--max-attempts", type=int, default=0)
    p.add_argument("--post-auth-delay", type=float, default=0)
    p.add_argument("--repeat", type=int, default=0)
    p.add_argument("--silent", action="store_true")
    p.add_argument("--exit-after-banner", action="store_true")
    p.add_argument("--rawdump", type=int, default=0)
    p.add_argument("--then", choices=["shell", "prompt"], default="shell")
    Device(p.parse_args()).run()


if __name__ == "__main__":
    main()
