"""P5-65..77, P5-81: `attach.prepare`, a local script that sets nothing for the run, and the environment of the
spawned process (SPEC "attach", "The environment of the spawned process")."""

from __future__ import annotations

import os
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pexpect
import pytest
import yaml
from conftest import BASH, PS1, AttachRecorded, SpawnRecorded, Timeline, make_doc, make_runner, run_vars

from autobot.runner import Runner

NAMES = ("AB_A", "AB_B", "AB_C", "AB_OS", "AB_GONE", "AB_NEW", "AB_HOST", "AB_SHELL")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in NAMES:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def prep_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Temp dir for the file of `prepare`, so a leftover is seen."""
    tdir = tmp_path / "tmp"
    tdir.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tdir))
    return tdir


@pytest.fixture
def prepared(timeline: Timeline, prep_tmp: Path):
    """Run a script up to the spawn, which is recorded and not made; returns the runner."""
    timeline.stop_attach = True

    def run(script: str, **kw) -> Runner:
        r = make_runner([], prepare=script, **kw)
        with pytest.raises(AttachRecorded):
            r.run()
        assert list(prep_tmp.iterdir()) == []
        return r

    return run


@pytest.fixture
def spawn_env(spawned, prep_tmp: Path):
    """Run a script up to `pexpect.spawn`, which is recorded and not made; returns the env it was given."""
    spawned.stop = True

    def run(**kw) -> dict[str, str]:
        with pytest.raises(SpawnRecorded):
            make_runner([], **kw).run()
        assert list(prep_tmp.iterdir()) == []
        return spawned[-1][1]["env"]

    return run


def progress(err: str) -> list[str]:
    """Autobot's lines up to the spawn."""
    return [line for line in err.splitlines() if line.startswith(">> ") and not line.startswith(">> attach: ")]


# -- P5-65: prepare sets nothing ------------------------------------------------


def test_p5_65_what_prepare_exports_reaches_no_template(prepared, monkeypatch: pytest.MonkeyPatch):
    """SPEC "attach": `prepare` sets nothing for the run: a declared variable keeps its value, whatever the
    script exports or unsets, and a name it exports is not a key of `env`."""
    monkeypatch.setenv("AB_OS", "os")
    r = prepared(
        "export AB_OS=prep AB_A=prep AB_NEW=new\nunset AB_B\ncd /\n",
        env={"AB_OS": "dflt", "AB_A": "dflt", "AB_B": "dflt-b", "AB_C": "{{ env.AB_A }}-c"},
    )
    assert r.render("{{ env.AB_OS }} {{ env.AB_A }} {{ env.AB_B }} {{ env.AB_C }}") == "os dflt dflt-b dflt-c"
    with pytest.raises(ValueError, match="^template error: env has no key 'AB_NEW': the script's env section doesn't declare it$"):
        r.render("{{ env.AB_NEW }}")


def test_p5_65_what_prepare_exports_reaches_neither_the_spawned_process_nor_a_step(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """SPEC "attach": seen from inside the spawned shell, and from a step's template; the working directory
    of the run stays what it was."""
    monkeypatch.setenv("AB_OS", "from os")
    monkeypatch.setenv("AB_GONE", "os")
    cwd = os.getcwd()
    out = run_vars(
        [
            {"cmd": 'echo "[$AB_OS][${AB_A-unset}][${AB_GONE-unset}][${AB_NEW-unset}][$TERM]"', "register": "shell"},
            {"cmd": 'echo "[{{ env.AB_OS }}][{{ env.AB_A }}][{{ env.AB_NEW | default(\'undeclared\') }}]"', "register": "tmpl"},
            {"cmd": "pwd", "register": "pwd"},
        ],
        prepare=f"export AB_OS=prep AB_A=prep AB_NEW=new TERM=vt100\nunset AB_GONE\ncd {tmp_path}\n",
        env={"AB_OS": "dflt", "AB_A": "dflt"},
    )
    assert out["shell"] == "[from os][unset][os][unset][dumb]"
    assert out["tmpl"] == "[from os][dflt][undeclared]"
    assert out["pwd"] == cwd == os.getcwd()


def test_p5_65_prepare_is_a_template_with_the_declared_variables(prepared, monkeypatch, tmp_path: Path):
    """SPEC "attach": `prepare` is rendered first, with `env` and `vars`; the declared variables are not in
    its environment, which is Autobot's own."""
    monkeypatch.setenv("AB_HOST", "from-os")
    out = tmp_path / "out"
    prepared(
        f"echo '{{{{ env.AB_HOST }}}} {{{{ env.AB_A }}}} {{{{ vars.v }}}}' \"$AB_HOST ${{AB_A-unset}} ${{AB_B-unset}}\" > {out}\n",
        env={"AB_HOST": "dflt", "AB_A": "dflt-a", "AB_B": None},
        vars={"v": "V"},
        given={"AB_B": "cli"},
    )
    assert out.read_text() == "from-os dflt-a V from-os unset unset\n"


# -- P5-66: a value that isn't UTF-8 ----------------------------------------------


def test_p5_66_non_utf8_value_in_a_command_is_sent_with_replacement(monkeypatch: pytest.MonkeyPatch):
    """SPEC "The environment in templates": a byte that isn't UTF-8 is a lone surrogate in `env`, goes to the
    session as `?`, and reaches the spawned process as it is."""
    monkeypatch.setenv("AB_A", "caf\udce9")
    out = run_vars(
        [{"cmd": "echo R=[{{ env.AB_A }}]", "register": "tmpl"}, {"cmd": 'printf %s "$AB_A" | od -An -tx1', "register": "raw"}],
        env={"AB_A": None},
    )
    # the echo of the line isn't recognised (it has `?` where the sent text has the byte) and stays in front
    assert out["tmpl"].splitlines()[-1] == "R=[caf?]"
    assert out["raw"].split() == ["63", "61", "66", "e9"]


def test_p5_66_non_utf8_value_cannot_be_written_into_prepare(timeline: Timeline, prep_tmp: Path, monkeypatch):
    """SPEC "attach": the script is written as strict UTF-8: the run fails, and the temp file is removed."""
    monkeypatch.setenv("AB_A", "caf\udce9")
    with pytest.raises(UnicodeError):
        make_runner([], prepare="echo {{ env.AB_A }}\n", env={"AB_A": None}).run()
    assert list(prep_tmp.iterdir()) == []
    assert "attach" not in timeline.names()


# -- P5-67: exit status -------------------------------------------------------

FAILING = {
    "exit": "touch {marker}\nexit 3\n",
    "exit-in-function": "f() {{ exit 3; }}\ntouch {marker}\nf\n",
    "last-command": "touch {marker}\n(exit 3)\n",
    "set-e": "set -e\ntouch {marker}\n(exit 3)\nrm {marker}\n",
    "bash-shebang": "#!/bin/bash\ntouch {marker}\nexit 3\n",
    "sh-e-shebang": "#!/bin/sh -e\ntouch {marker}\n(exit 3)\nrm {marker}\n",
    "env-shebang": "#!/usr/bin/env -S bash -eu\ntouch {marker}\n(exit 3)\nrm {marker}\n",
}


@pytest.mark.parametrize("script", FAILING.values(), ids=FAILING.keys())
def test_p5_67_failing_script_aborts_the_run(timeline: Timeline, prep_tmp: Path, tmp_path: Path, capsys, script: str):
    """SPEC "attach": a non-zero exit status aborts the run, with the status; nothing is spawned."""
    marker = tmp_path / "ran"
    with pytest.raises(RuntimeError, match="^prepare script failed with exit code 3$"):
        make_runner([], prepare=script.format(marker=marker)).run()
    assert marker.exists()
    assert "attach" not in timeline.names()
    assert list(prep_tmp.iterdir()) == []
    assert progress(capsys.readouterr().err) == [">> prepare: running local script" + (
        "" if script.startswith("#!") else " (no shebang, using /bin/sh)"
    )]


SUCCEEDING = {
    "exit-0": "exit 0\nexit 3\n",
    "own-exit-trap": "trap 'true' EXIT\nexit 0\n",
    "exec": "exec true\n",
    "background-process": "sleep 3 > /dev/null 2>&1 &\n",
    "positional-parameters": "[ $# -eq 0 ] && set -- a b c && shift\n",
    "path-and-python-home": "export PATH=/nonexistent PYTHONHOME=/nonexistent PYTHONPATH=/nonexistent\n",
}


@pytest.mark.parametrize("script", SUCCEEDING.values(), ids=SUCCEEDING.keys())
def test_p5_67_script_that_exits_zero_lets_the_run_go_on(prepared, capsys, script: str):
    """SPEC "attach": however the script ends with status 0, the run goes on, and waits for no process the
    script leaves running."""
    began = time.monotonic()
    prepared(script)
    assert time.monotonic() - began < 2
    assert progress(capsys.readouterr().err) == [">> prepare: running local script (no shebang, using /bin/sh)", ">> prepare: done"]


# -- P5-68: output ------------------------------------------------------------


def test_p5_68_script_output_is_its_own_and_autobot_prints_two_lines(prepared, capfd):
    """SPEC "Output": the script's stdout and stderr are its own; Autobot's lines say that it runs and that
    it is done, and nothing about its environment."""
    prepared("echo to-stdout\necho to-stderr >&2\nexport AB_A=S3CRET\nunset HOME\n")
    out, err = capfd.readouterr()
    assert out == "to-stdout\n"
    assert "to-stderr\n" in err
    assert progress(err) == [">> prepare: running local script (no shebang, using /bin/sh)", ">> prepare: done"]
    assert "environment" not in err and "S3CRET" not in out + err


# -- P5-69: the interpreter -----------------------------------------------------

BASH_ONLY = '{{% raw %}}arr=(x y z)\n[[ ${{#arr[@]}} == 3 ]] && echo "${{arr[*]:1}}" "$0" "$#" > {out}{{% endraw %}}\n'


@pytest.mark.parametrize(
    "shebang", ["#!/bin/bash", "#!/usr/bin/env bash", "#!/bin/bash -eu", "#!/usr/bin/env -S bash -eu", " \n﻿#!/bin/bash\r"]
)
def test_p5_69_shebang_picks_the_interpreter(prepared, prep_tmp: Path, tmp_path: Path, capsys, shebang: str):
    """SPEC "attach": the script is executed, so its shebang line picks the interpreter and its argument;
    `$0` is the temp file, and there are no arguments. What comes before the `#!` is removed."""
    out = tmp_path / "out"
    prepared(f"{shebang}\n" + BASH_ONLY.format(out=out))
    words, script, count = out.read_text().rsplit(None, 2)
    assert words == "y z" and count == "0"
    assert script.startswith(f"{prep_tmp}/_autobot_") and script.endswith(".sh")
    assert progress(capsys.readouterr().err) == [">> prepare: running local script", ">> prepare: done"]


def test_p5_69_script_without_a_shebang_runs_under_sh(prepared, prep_tmp: Path, tmp_path: Path):
    """SPEC "attach": `/bin/sh <temp file>`."""
    out = tmp_path / "out"
    prepared(f'echo "$0" "$#" > {out}\nps -o args= -p $$ >> {out}\n')
    script, args = out.read_text().splitlines()
    assert script.startswith(f"{prep_tmp}/_autobot_") and script.endswith(".sh 0")
    assert args == f"/bin/sh {script.removesuffix(' 0')}"


def test_p5_69_options_of_the_shebang_apply_to_the_script(timeline: Timeline, prep_tmp: Path):
    """SPEC "attach": `#!/bin/sh -eu` runs the script with those options."""
    with pytest.raises(RuntimeError, match="^prepare script failed with exit code [12]$"):
        make_runner([], prepare='#!/bin/sh -eu\necho "$AB_NOT_SET"\n').run()
    assert "attach" not in timeline.names()


# -- P5-70: other interpreters ------------------------------------------------


def test_p5_70_other_interpreter_runs_the_script(prepared, prep_tmp, tmp_path: Path, capsys):
    """SPEC "attach": any interpreter a shebang names, with the temp file as its script."""
    out = tmp_path / "out"
    prepared(
        f"#!{sys.executable}\nimport os, sys\n"
        f"open({str(out)!r}, 'w').write(sys.argv[0] + ' ' + str(len(os.listdir({str(prep_tmp)!r}))))\n"
    )
    script, files = out.read_text().split()
    assert script.startswith(f"{prep_tmp}/_autobot_") and files == "1"
    assert progress(capsys.readouterr().err) == [">> prepare: running local script", ">> prepare: done"]


def test_p5_70_other_interpreter_failure(timeline: Timeline, prep_tmp: Path):
    """SPEC "attach": its exit status aborts the run."""
    with pytest.raises(RuntimeError, match="^prepare script failed with exit code 7$"):
        make_runner([], prepare=f"#!{sys.executable}\nraise SystemExit(7)\n").run()
    assert "attach" not in timeline.names()
    assert list(prep_tmp.iterdir()) == []


def test_p5_70_interpreter_that_cannot_be_started(timeline: Timeline, prep_tmp: Path):
    """SPEC "attach": a script that can't be started aborts the run, with the shebang line and the reason."""
    message = r"^prepare script could not run \('#!/autobot/no/such/interpreter -x'\): \[Errno 2\] No such file or directory$"
    with pytest.raises(RuntimeError, match=message) as ei:
        make_runner([], prepare="#!/autobot/no/such/interpreter -x\ntrue\n").run()
    assert isinstance(ei.value.__cause__, OSError)
    assert "attach" not in timeline.names()
    assert list(prep_tmp.iterdir()) == []


# -- P5-72: the temp file -------------------------------------------------------


def test_p5_72_temp_dir_holds_the_script_and_nothing_else(prepared, prep_tmp: Path, tmp_path: Path, monkeypatch):
    """SPEC "attach": one temp file, the rendered script, for its owner alone; nothing of the environment
    is written anywhere."""
    monkeypatch.setenv("AB_OS", "S3CRET-os")
    out = tmp_path / "out"
    # the needle is split so that the script's own file doesn't hold it
    prepared(f'ls {prep_tmp} > {out}\nstat -c %a "$0" >> {out}\ngrep -rl "S3CRET""-os" {prep_tmp} /dev/null >> {out}\ntrue\n')
    name, mode = out.read_text().splitlines()
    assert name.startswith("_autobot_") and name.endswith(".sh") and mode == "700"


def test_p5_72_no_descriptor_is_left_open(prepared):
    """The script's file is closed when `prepare` ends."""
    fds = "/proc/self/fd" if os.path.isdir("/proc/self/fd") else "/dev/fd"
    before = sorted(os.listdir(fds))
    prepared("true\n")
    assert sorted(os.listdir(fds)) == before


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT, signal.SIGKILL], ids=lambda s: s.name)
def test_p5_72_signal_while_prepare_runs(tmp_path: Path, sig: signal.Signals):
    """SPEC "attach": SIGTERM and an interrupt remove the script's temp file; SIGKILL leaves it, and nothing
    that holds a value of the environment."""
    tdir, started = tmp_path / "tmp", tmp_path / "started"
    tdir.mkdir()
    path = tmp_path / "script.autobot.yaml"
    path.write_text(yaml.safe_dump(make_doc([], prepare=f"touch {started}\nexec sleep 20 > /dev/null 2>&1\n")))
    proc = subprocess.Popen(
        [sys.executable, "-m", "autobot.cli", str(path)],
        env={**os.environ, "TMPDIR": str(tdir), "AB_OS": "S3CRET-os"},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 20
        while not started.exists():
            assert proc.poll() is None and time.monotonic() < deadline, proc.stderr.read()
            time.sleep(0.05)
        proc.send_signal(sig)
        err = proc.communicate(timeout=20)[1]
    finally:
        proc.kill()
    left = list(tdir.iterdir())
    if sig == signal.SIGKILL:
        assert proc.returncode == -signal.SIGKILL
        assert [f.suffix for f in left] == [".sh"]
        assert b"S3CRET" not in left[0].read_bytes()
    else:
        assert left == []
        assert proc.returncode == -sig, err  # after Ctrl-C, too, the CLI ends from the signal
        assert "Traceback" not in err


def test_p5_72_temp_path_is_never_read_as_shell_code(timeline: Timeline, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """SPEC "attach": the temp file's path is an argument, whatever characters it has."""
    marker = tmp_path / "injected"
    tdir = tmp_path / f"we ird'\"; touch {marker.name}; $(touch {marker.name}) `touch {marker.name}` #"
    tdir.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tdir))
    monkeypatch.chdir(tmp_path)
    timeline.stop_attach = True
    ran = tmp_path / "ran"
    for script in (f"echo sh >> {ran}\n", f"#!/bin/bash\necho bash >> {ran}\nexit 0\n"):
        with pytest.raises(AttachRecorded):
            make_runner([], prepare=script).run()
    assert ran.read_text() == "sh\nbash\n"
    assert not marker.exists()
    assert list(tdir.iterdir()) == []


# -- P5-73: order -------------------------------------------------------------


def test_p5_73_spawn_is_rendered_before_prepare_runs(timeline: Timeline, prep_tmp: Path, tmp_path: Path, monkeypatch, capsys):
    """SPEC "attach" lifecycle: `spawn` is rendered with `env` as it is when the run starts; `prepare` runs
    after that and changes nothing of it."""
    monkeypatch.setenv("AB_HOST", "sw1")
    timeline.stop_attach = True
    r = make_runner(
        [], prepare="export AB_HOST=prep AB_SHELL=prep\n", spawn="{{ env.AB_SHELL }} admin@{{ env.AB_HOST }}",
        env={"AB_SHELL": "ssh", "AB_HOST": None},
    )
    with pytest.raises(AttachRecorded, match="^ssh admin@sw1$"):
        r.run()
    assert capsys.readouterr().err.splitlines()[-1] == ">> attach: ssh admin@sw1"


@pytest.mark.parametrize(
    ("spawn", "message"),
    [
        ("{{ vars.cmd }}", r"attach\.spawn rendered to an empty command: '\{\{ vars\.cmd \}\}'"),
        (" {{ env.AB_SHELL }} ", r"attach\.spawn rendered to an empty command: ' \{\{ env\.AB_SHELL \}\} '"),
        ("{% if env.AB_A is defined %}ssh{% endif %}", r"attach\.spawn rendered to an empty command: .*"),
        ("ssh {{ vars.nope }}", "template error: 'dict object' has no attribute 'nope'"),
        ("ssh {{ env.AB_HOST }}", "template error: env has no key 'AB_HOST': the script's env section doesn't declare it"),
        ("ssh {{ env.AB_HOST", "template error: unexpected end of template.*"),
        ("{% if env.X %}", "template error: Unexpected end of template.*"),
    ],
    ids=["empty", "empty-env", "empty-undeclared", "undefined", "exported-by-prepare", "syntax", "syntax-block"],
)
def test_p5_73_spawn_that_fails_stops_the_run_before_prepare(timeline, tmp_path: Path, spawn: str, message: str):
    """SPEC "attach": a `spawn` that names no command or can't be rendered stops the run before `prepare`,
    whether it reads `env` or not; a variable `prepare` would export is not a key of `env`."""
    marker = tmp_path / "prepared"
    r = make_runner([], prepare=f"touch {marker}\nexport AB_HOST=h AB_A=1\n", spawn=spawn, vars={"cmd": ""}, env={"AB_SHELL": ""})
    with pytest.raises(ValueError, match=f"^{message}$"):
        r.run()
    assert not marker.exists()
    assert "attach" not in timeline.names()


# -- P5-75..77: the environment of the spawned process --------------------------


def test_p5_75_spawned_process_inherits_the_environment_and_nothing_of_the_script(monkeypatch: pytest.MonkeyPatch):
    """SPEC "The environment of the spawned process": checked from inside the spawned shell. A declared
    variable is for templates: neither its default nor its `--env` value is in the process's environment."""
    monkeypatch.setenv("AB_OS", "from os")
    monkeypatch.setenv("AB_B", "os-b")
    show = 'echo "[$AB_OS][${AB_A-unset}][$AB_B][${AB_C-unset}][$TERM][$NO_COLOR]"'
    out = run_vars(
        [{"cmd": show, "register": "out"}, {"cmd": "echo \"[{{ env.AB_A }}][{{ env.AB_B }}][{{ env.AB_C }}]\"", "register": "tmpl"}],
        env={"AB_A": "dflt", "AB_B": "dflt", "AB_C": None},
        given={"AB_C": "cli"},
    )
    assert out["out"] == "[from os][unset][os-b][unset][dumb][1]"
    assert out["tmpl"] == "[dflt][os-b][cli]"


def test_p5_76_plain_terminal_over_the_inherited_environment(spawn_env, monkeypatch):
    """SPEC "The environment of the spawned process": Autobot's own environment with `TERM=dumb` and
    `NO_COLOR=1`, and nothing else, whatever the script declares and whatever `prepare` does."""
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("AB_OS", "os")
    expected = {**os.environ, "TERM": "dumb", "NO_COLOR": "1"}
    assert spawn_env(env={"AB_C": "template-only", "AB_OS": "dflt"}, given={"AB_OS": "cli"}) == expected
    assert spawn_env(prepare="true\n") == expected
    assert spawn_env(prepare="export TERM=vt100 AB_A=prep AB_OS=prep\nunset NO_COLOR PATH\n") == expected
    assert spawn_env(prepare=f"#!{sys.executable}\nimport os\nos.environ['TERM'] = 'x'\n") == expected


@pytest.mark.parametrize("os_env", [{}, {"TERM": "xterm-256color", "NO_COLOR": "0"}, {"TERM": "dumb", "NO_COLOR": "1"}], ids=["os-unset", "os-set", "os-plain"])
def test_p5_76_prepare_runs_on_the_plain_terminal_too(spawn_env, monkeypatch, tmp_path, os_env):
    """SPEC "attach": `prepare` runs in Autobot's own environment with `TERM=dumb` and `NO_COLOR=1`; a
    declared `TERM` reads the environment Autobot was started with."""
    for name in ("TERM", "NO_COLOR"):
        monkeypatch.delenv(name, raising=False)
    for name, value in os_env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("AB_OS", "os")
    seen = tmp_path / "seen"
    env = spawn_env(prepare=f'echo "$TERM/$NO_COLOR/$AB_OS/{{{{ env.TERM }}}}" > {seen}\n', env={"TERM": "none"})
    assert seen.read_text() == f"dumb/1/os/{os_env.get('TERM', 'none')}\n"
    assert (env["TERM"], env["NO_COLOR"]) == ("dumb", "1")


def test_p5_77_spawn_is_looked_up_in_the_path_of_the_environment(tmp_path: Path, monkeypatch, capfd):
    """SPEC "The environment of the spawned process": `spawn` is found in the `PATH` Autobot was started
    with, not in one that `prepare` exports."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    cmd = bindir / "ab-device"
    cmd.write_text('#!/bin/sh\necho "device says ${AB_A-nothing}"\n')
    cmd.chmod(cmd.stat().st_mode | stat.S_IXUSR)
    kw = {"prompts": []}
    with pytest.raises(pexpect.ExceptionPexpect, match="The command was not found or was not executable: ab-device"):
        make_runner([], spawn="ab-device", prepare=f'export PATH="{bindir}:$PATH" AB_A=hi\n', **kw).run()
    # on the spawn line, `env` is the command that is looked up, and it looks up the rest in the `PATH` it sets
    make_runner([], spawn=f"env PATH={bindir}:/usr/bin:/bin AB_A=there ab-device", **kw).run()
    assert "device says there" in capfd.readouterr().out
    make_runner([], spawn="sh -c 'echo still-found'", prepare="export PATH=/nonexistent\n", **kw).run()
    assert "still-found" in capfd.readouterr().out
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    make_runner([], spawn="ab-device", **kw).run()
    assert "device says nothing" in capfd.readouterr().out
    # without a `PATH`, the system default path
    monkeypatch.delenv("PATH")
    make_runner([], spawn="sh -c 'echo no-path'", **kw).run()
    assert "no-path" in capfd.readouterr().out


# -- P5-81: the environment and the spawn line, seen from inside the spawned shell --

# `_` for a variable that isn't set. With the prompt it is under 80 columns: on a terminal that isn't
# dumb, readline wraps a longer line, and the wrapped echo is not the line that was sent
SHOW = 'echo "${AB_OS-_}|${AB_A-_}|${AB_GONE-_}|${TERM-_}|${NO_COLOR-_}"'
assert len(PS1 + SHOW) < 80


def in_shell(**kw) -> str:
    return run_vars([{"cmd": SHOW, "register": "out"}], **kw)["out"]


@pytest.fixture
def os_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """An environment with a variable of its own, on a terminal that has color."""
    monkeypatch.setenv("AB_OS", "from os")
    monkeypatch.setenv("AB_GONE", "os")
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)


def test_p5_81_shell_has_the_inherited_environment_and_a_plain_terminal(os_env):
    """SPEC "The environment of the spawned process": Autobot's own, with `TERM=dumb` and `NO_COLOR=1`."""
    assert in_shell() == "from os|_|os|dumb|1"
    # a variable of the `env` section is for templates only
    assert in_shell(env={"AB_A": "dflt"}) == "from os|_|os|dumb|1"
    assert in_shell(env={"AB_A": None}, given={"AB_A": "cli"}) == "from os|_|os|dumb|1"
    assert in_shell(prepare="export AB_OS=prep AB_A=new TERM=vt100\nunset AB_GONE NO_COLOR\n") == "from os|_|os|dumb|1"


def test_p5_81_variable_on_the_spawn_line(os_env):
    """SPEC "The environment of the spawned process": `env X=... cmd` on the `spawn` line sets a variable
    for the command, from a template too; it is the command's own line, so it has the last word."""
    assert in_shell(spawn=f"env AB_A='on the line' AB_OS={{{{ vars.v }}}} {BASH}", vars={"v": "tmpl"}) == (
        "tmpl|on the line|os|dumb|1"
    )
    out = in_shell(spawn=f"env -u AB_GONE AB_A={{{{ env.AB_A }}}}-line TERM=vt100 {BASH}", env={"AB_A": None}, given={"AB_A": "cli"})
    assert out == "from os|cli-line|_|vt100|1"
    # the way to hand a variable of the `env` section to the command
    assert in_shell(spawn=f"env AB_A={{{{ env.AB_A }}}} {BASH}", env={"AB_A": "dflt"}) == "from os|dflt|os|dumb|1"


def test_p5_81_env_i_on_the_spawn_line_gives_a_minimal_environment(os_env):
    """SPEC "The environment of the spawned process": `spawn: env -i ...` inherits nothing."""
    bash = shutil.which("bash")
    out = run_vars(
        [{"cmd": "compgen -e", "register": "names"}, {"cmd": SHOW, "register": "out"}],
        spawn=f"env -i PS1={shlex.quote(PS1)} PATH=/usr/bin:/bin {bash} --norc --noprofile -i",
    )
    # bash exports its own bookkeeping; `TERM` is bash's shell variable for a terminal it wasn't told about
    assert set(out["names"].split()) - {"PWD", "OLDPWD", "SHLVL", "_"} == {"PATH", "PS1"}
    assert out["out"] == "_|_|_|dumb|_"


@pytest.mark.parametrize("value", ["a b  c", "it's a 'q' = $HOME", "touch /nonexistent/injected"], ids=["spaces", "quotes", "command"])
def test_p5_81_quoted_template_value_on_the_spawn_line_arrives_whole(os_env, value: str):
    """SPEC "The environment of the spawned process": the rendered `spawn` is split into words, so a
    templated value is quoted; then spaces and single quotes in it are part of the value."""
    out = run_vars(
        [{"cmd": 'echo "<$AB_A>"', "register": "out"}],
        spawn=f'env "AB_A={{{{ env.AB_V }}}}" {BASH}', env={"AB_V": None}, given={"AB_V": value},
    )
    assert out["out"] == f"<{value}>"


def test_p5_81_entry_without_a_name_is_not_in_the_run_environment(tmp_path: Path):
    """SPEC "The environment of the spawned process": an entry with an empty name, which a program can be
    started with, is left out: the process is spawned, without it."""
    out = tmp_path / "out"
    doc = make_doc([{"cmd": f"env | grep -c '^=' > {out}", "ignore_error": True}])
    path = tmp_path / "script.autobot.yaml"
    path.write_text(yaml.safe_dump(doc))
    env = {**os.environ, "": "nameless"}
    res = subprocess.run(
        [sys.executable, "-m", "autobot.cli", str(path)], check=False, capture_output=True, text=True, env=env, timeout=60
    )
    assert res.returncode == 0 and res.stderr.splitlines()[-1] == ">> run completed"
    assert "Traceback" not in res.stdout + res.stderr and "nameless" not in res.stdout
    assert out.read_text() == "0\n"
