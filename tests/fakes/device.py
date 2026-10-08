#!/usr/bin/env python3
"""Scriptable fake console device (test plan fixture F2).

Spawned as ``attach.spawn`` (or from a shell with ``line``). It prints a
banner, then login/password prompts in a configurable order, and finally
drops into a shell (or a bare ``PROMPT$ `` loop, or a line editor that
echoes each line itself, wrapped at its right margin). Each ``--ask TEXT`` asks
``TEXT `` once, before the login prompts. Every line it reads is
appended to ``--log`` as ``<KIND>=<value>`` so tests can assert on what
Autobot actually sent, instead of parsing pty output.

With ``--logout`` it is a console that can be logged out of: the shell is a login shell that the device
waits for, and in the bare prompt loop the line ``logout`` ends the loop. It then logs ``LOGOUT=``, and
asks for the login again. Ctrl-C at a login prompt does nothing, as at a getty. With ``--capital`` the
login prompt is ``Login: ``, and with ``--last-login`` the device prints a ``Last login: ...`` line once
the login is accepted, ``--banner-delay`` seconds after it and before ``--post-auth-delay``; with
``--split-banner SECS`` it writes ``Last login: ``, waits, and writes the rest, as a slow line delivers
it. With ``--lower-password`` the password prompt is ``password: ``. With ``--escape`` a
Ctrl-] typed at any of the device's own prompts ends the device, as it leaves the client of a console
server: it logs ``DETACH=``.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import termios
import time
import tty

PROMPTS = {"login": "login: ", "password": "Password: "}
SHELL_PROMPT = "PROMPT$ "
BANNER = "Welcome"


class Device:
    escape = ""  # the log, when a Ctrl-] ends the device

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
        if b"\x1d" in data and Device.escape:
            with open(Device.escape, "a") as f:
                f.write("DETACH=\n")
            os.write(1, b"\ndetached\n")
            sys.exit(0)
        return data.decode(errors="replace").rstrip("\r\n")

    def auth(self, first_prompt_written: bool = False) -> None:
        if not self.order:
            return
        while True:
            got: dict[str, str] = {}
            for k, p in enumerate(self.order):
                if not (first_prompt_written and k == 0):
                    text = "Login: " if p == "login" and self.args.capital else PROMPTS[p]
                    self.write(text.lower() if p == "password" and self.args.lower_password else text)
                first_prompt_written = False
                got[p] = self.readline()
                self.log(p.upper(), got[p])
            cred = f"{got.get('login', '')}:{got.get('password', '')}"
            if cred in self.args.accept:
                return
            self.write("Login incorrect\n")

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

    def console(self) -> None:
        """The shell or the prompt loop until it is logged out of, then the login again, without end."""
        env = {"PS1": SHELL_PROMPT, "TERM": "dumb", "PATH": os.environ.get("PATH", "")}
        while True:
            if self.args.then == "shell":
                if subprocess.call(["bash", "--norc", "--noprofile", "-i", "-l"], env=env) < 0:
                    sys.exit(0)  # the shell was ended by a signal, as when the session is closed: no logout
            else:
                line = ""
                while line != "logout":
                    self.write(SHELL_PROMPT)
                    line = self.readline()
                    self.log("LINE", line)
            self.log("LOGOUT", "")
            self.write("\n")
            self.auth()

    def then(self) -> None:
        if self.args.last_login:
            time.sleep(self.args.banner_delay)
            if self.args.split_banner:
                self.write("Last login: ")
                time.sleep(self.args.split_banner)
                self.write("Tue Oct  7 09:00:00 2026 from 10.0.0.1\n")
            else:  # in one write, as a program that prints a line does
                self.write("Last login: Tue Oct  7 09:00:00 2026 from 10.0.0.1\n")
        if self.args.post_auth_delay:
            time.sleep(self.args.post_auth_delay)
        if self.args.logout:
            self.console()
        if self.args.then == "shell":
            env = {"PS1": SHELL_PROMPT, "TERM": "dumb", "PATH": os.environ.get("PATH", "")}
            os.execvpe("bash", ["bash", "--norc", "--noprofile", "-i"], env)
        if self.args.then == "editor":
            self.editor()
        while True:
            self.write(SHELL_PROMPT)
            self.log("LINE", self.readline())

    def editor(self) -> None:
        a = self.args
        wrap = bytes.fromhex(a.wrap).decode()
        attrs = termios.tcgetattr(0)
        attrs[3] &= ~termios.ECHO
        termios.tcsetattr(0, termios.TCSANOW, attrs)
        while True:
            self.write(a.prompt)
            line = self.readline()
            self.log("LINE", line)
            echo, col = "", len(a.prompt)
            for ch in line:
                echo += ch
                col += 1
                if col == a.cols:
                    echo, col = echo + wrap, 0
            self.write(echo + "\n")
            if line.startswith("echo "):
                self.write(line[5:] + "\n")
            elif line:
                self.write("% Invalid input\n")

    def run(self) -> None:
        a = self.args
        if a.logout:
            signal.signal(signal.SIGINT, lambda signum, frame: None)  # not SIG_IGN: the shell's commands would inherit it
        if a.escape:
            Device.escape = a.log
            attrs = termios.tcgetattr(0)
            attrs[6][termios.VEOL] = b"\x1d"  # a Ctrl-] ends a read, like a line break
            termios.tcsetattr(0, termios.TCSANOW, attrs)
        if a.silent:
            attrs = termios.tcgetattr(0)
            attrs[3] &= ~termios.ECHO
            termios.tcsetattr(0, termios.TCSANOW, attrs)
        first_written = False
        if a.same_chunk:
            first = PROMPTS[self.order[0]] if self.order else SHELL_PROMPT
            self.write(f"{BANNER}\n{first}")
            if not self.order:
                # the shell prompt is already on screen
                self.log("LINE", self.readline())
            first_written = bool(self.order)
        else:
            self.write(f"{BANNER}\n")
        if a.wait_enter:
            self.log("ENTER", self.readline())
        if a.exit_after_banner:
            sys.exit(0)
        if a.silent:
            while True:
                self.log("SILENT", self.readline())
        if a.rawdump:
            self.rawdump(a.rawdump)
        for question in a.ask:
            self.write(f"{question} ")
            self.log("ASK", self.readline())
        for _ in range(a.repeat):
            self.auth(first_written)
            first_written = False
            self.write(SHELL_PROMPT)
            self.log("LINE", self.readline())
        self.auth(first_written)
        self.then()


def main() -> None:
    # the default actions, whatever the suite was started with (`nohup`, an ignored SIGINT): the device and
    # the shell it starts must end when the session is closed, and stop at Ctrl-C
    for sig in (signal.SIGHUP, signal.SIGINT, signal.SIGQUIT, signal.SIGTERM):
        signal.signal(sig, signal.SIG_DFL)
    p = argparse.ArgumentParser()
    p.add_argument("--log", required=True)
    p.add_argument("--wait-enter", action="store_true")
    p.add_argument("--same-chunk", action="store_true")
    p.add_argument("--order", default="login,password")
    p.add_argument("--accept", action="append", default=[])
    p.add_argument("--post-auth-delay", type=float, default=0)
    p.add_argument("--repeat", type=int, default=0)
    p.add_argument("--silent", action="store_true")
    p.add_argument("--exit-after-banner", action="store_true")
    p.add_argument("--rawdump", type=int, default=0)
    p.add_argument("--ask", action="append", default=[])
    p.add_argument("--then", choices=["shell", "prompt", "editor"], default="shell")
    p.add_argument("--prompt", default=SHELL_PROMPT)
    p.add_argument("--cols", type=int, default=80)
    p.add_argument("--wrap", default="")
    p.add_argument("--logout", action="store_true")
    p.add_argument("--escape", action="store_true")
    p.add_argument("--capital", action="store_true")
    p.add_argument("--last-login", action="store_true")
    p.add_argument("--banner-delay", type=float, default=0)
    p.add_argument("--split-banner", type=float, default=0)
    p.add_argument("--lower-password", action="store_true")
    Device(p.parse_args()).run()


if __name__ == "__main__":
    main()
