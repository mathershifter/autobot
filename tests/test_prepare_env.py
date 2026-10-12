"""P5-65..77, P5-81, P5-98..103: `prepare`, a local script that runs before anything else and sets declared
variables through its standard output, and the environment of the spawned process (SPEC "prepare", "The
order of a run", "The environment of the spawned process")."""

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
from conftest import BASH, PS1, AttachRecorded, SpawnRecorded, Timeline, make_doc, make_runner, run_cli, run_vars

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


# -- P5-65: what prepare does to its own process sets nothing ---------------------


def test_p5_65_what_prepare_exports_reaches_no_template(prepared, monkeypatch: pytest.MonkeyPatch):
    """SPEC "prepare": only a line of its output sets a variable: a declared variable keeps its value,
    whatever the script exports or unsets, and a name it exports is not a key of `env`."""
    monkeypatch.setenv("AB_OS", "os")
    r = prepared(
        "export AB_OS=prep AB_A=prep AB_NEW=new\nunset AB_B\ncd /\n",
        env={"AB_OS": "dflt", "AB_A": "dflt", "AB_B": "dflt-b", "AB_C": "{{ env.AB_A }}-c"},
    )
    assert r.render("{{ env.AB_OS }} {{ env.AB_A }} {{ env.AB_B }} {{ env.AB_C }}") == "os dflt dflt-b dflt-c"
    with pytest.raises(ValueError, match="^template error: env has no key 'AB_NEW': the script's env section doesn't declare it$"):
        r.render("{{ env.AB_NEW }}")


def test_p5_65_what_prepare_exports_reaches_neither_the_spawned_process_nor_a_step(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """SPEC "prepare": seen from inside the spawned shell, and from a step's template; the working directory
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


def test_p5_65_prepare_is_written_as_it_is(prepared, tmp_path: Path):
    """SPEC "prepare": the script is not a template: `{{`, `{%` and `{#` are its own text, also where a
    template would be a syntax error or name something undefined."""
    out = tmp_path / "out"
    prepared(f"echo '{{{{ env.AB_A }}}} {{{{ vars.v }}}} {{{{ nope }}}} {{% endfor %}} {{# {{{{' > {out}\n", env={"AB_A": "a"}, vars={"v": "V"})
    assert out.read_text() == "{{ env.AB_A }} {{ vars.v }} {{ nope }} {% endfor %} {# {{\n"


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


def test_p5_66_non_utf8_byte_of_the_output_of_prepare_is_kept_as_the_environment_keeps_one(prepared):
    """SPEC "The output of `prepare`": the output is read as UTF-8, and a byte that isn't is a lone surrogate
    in the value, as in a value of the environment; it is no error."""
    r = prepared("printf 'AB_A=caf\\351\\n'\nprintf 'AB_B=caf\\303\\251\\n'\n", env={"AB_A": None, "AB_B": None})
    assert r._env == {"AB_A": "caf\udce9", "AB_B": "caf\u00e9"}


def test_p5_66_non_utf8_byte_of_the_output_of_prepare_is_sent_with_replacement():
    """SPEC "The environment in templates": sent to the session, it goes out as `?`."""
    out = run_vars([{"cmd": "echo R=[{{ env.AB_A }}]", "register": "tmpl"}], prepare="printf 'AB_A=caf\\351\\n'\n", env={"AB_A": None})
    assert out["tmpl"].splitlines()[-1] == "R=[caf?]"


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
    """SPEC "prepare": a non-zero exit status aborts the run, with the status; nothing is spawned."""
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
    """SPEC "prepare": however the script ends with status 0, the run goes on, and waits for no process the
    script leaves running with its output redirected."""
    began = time.monotonic()
    prepared(script)
    assert time.monotonic() - began < 2
    assert progress(capsys.readouterr().err) == [">> prepare: running local script (no shebang, using /bin/sh)", ">> prepare: done"]


# -- P5-68: output ------------------------------------------------------------


def test_p5_68_stdout_is_captured_and_stderr_is_the_script_s_own(prepared, capfd):
    """SPEC "Output": the script's stdout is read and never shown, its stderr is its own; Autobot's lines
    say that it runs, how many variables it set and that it is done, and no name or value."""
    prepared("echo AB_A=S3CRET\necho AB_B=S3CRET-b\necho to-stderr >&2\nexport AB_C=S3CRET\n", env={"AB_A": None, "AB_B": "b"})
    out, err = capfd.readouterr()
    assert out == ""
    assert "to-stderr\n" in err
    assert progress(err) == [
        ">> prepare: running local script (no shebang, using /bin/sh)", ">> prepare: 2 set", ">> prepare: done",
    ]
    assert "S3CRET" not in out + err and "AB_A" not in err


def test_p5_68_script_that_prints_nothing_gets_no_count_line(prepared, capfd):
    """SPEC "Output": the count line is for a script that set something."""
    prepared("echo to-stderr >&2\n", env={"AB_A": "a"})
    assert progress(capfd.readouterr().err) == [">> prepare: running local script (no shebang, using /bin/sh)", ">> prepare: done"]


# -- P5-69: the interpreter -----------------------------------------------------

BASH_ONLY = 'arr=(x y z)\n[[ ${{#arr[@]}} == 3 ]] && echo "${{arr[*]:1}}" "$0" "$#" > {out}\n'


@pytest.mark.parametrize(
    "shebang", ["#!/bin/bash", "#!/usr/bin/env bash", "#!/bin/bash -eu", "#!/usr/bin/env -S bash -eu", " \n﻿#!/bin/bash\r"]
)
def test_p5_69_shebang_picks_the_interpreter(prepared, prep_tmp: Path, tmp_path: Path, capsys, shebang: str):
    """SPEC "prepare": the script is executed, so its shebang line picks the interpreter and its argument;
    `$0` is the temp file, and there are no arguments. What comes before the `#!` is removed."""
    out = tmp_path / "out"
    prepared(f"{shebang}\n" + BASH_ONLY.format(out=out))
    words, script, count = out.read_text().rsplit(None, 2)
    assert words == "y z" and count == "0"
    assert script.startswith(f"{prep_tmp}/_autobot_") and script.endswith(".sh")
    assert progress(capsys.readouterr().err) == [">> prepare: running local script", ">> prepare: done"]


def test_p5_69_script_without_a_shebang_runs_under_sh(prepared, prep_tmp: Path, tmp_path: Path):
    """SPEC "prepare": `/bin/sh <temp file>`."""
    out = tmp_path / "out"
    prepared(f'echo "$0" "$#" > {out}\nps -o args= -p $$ >> {out}\n')
    script, args = out.read_text().splitlines()
    assert script.startswith(f"{prep_tmp}/_autobot_") and script.endswith(".sh 0")
    assert args == f"/bin/sh {script.removesuffix(' 0')}"


def test_p5_69_options_of_the_shebang_apply_to_the_script(timeline: Timeline, prep_tmp: Path):
    """SPEC "prepare": `#!/bin/sh -eu` runs the script with those options."""
    with pytest.raises(RuntimeError, match="^prepare script failed with exit code [12]$"):
        make_runner([], prepare='#!/bin/sh -eu\necho "$AB_NOT_SET"\n').run()
    assert "attach" not in timeline.names()


# -- P5-70: other interpreters ------------------------------------------------


def test_p5_70_other_interpreter_runs_the_script(prepared, prep_tmp, tmp_path: Path, capsys):
    """SPEC "prepare": any interpreter a shebang names, with the temp file as its script."""
    out = tmp_path / "out"
    prepared(
        f"#!{sys.executable}\nimport os, sys\n"
        f"open({str(out)!r}, 'w').write(sys.argv[0] + ' ' + str(len(os.listdir({str(prep_tmp)!r}))))\n"
    )
    script, files = out.read_text().split()
    assert script.startswith(f"{prep_tmp}/_autobot_") and files == "1"
    assert progress(capsys.readouterr().err) == [">> prepare: running local script", ">> prepare: done"]


def test_p5_70_other_interpreter_failure(timeline: Timeline, prep_tmp: Path):
    """SPEC "prepare": its exit status aborts the run."""
    with pytest.raises(RuntimeError, match="^prepare script failed with exit code 7$"):
        make_runner([], prepare=f"#!{sys.executable}\nraise SystemExit(7)\n").run()
    assert "attach" not in timeline.names()
    assert list(prep_tmp.iterdir()) == []


def test_p5_70_interpreter_that_cannot_be_started(timeline: Timeline, prep_tmp: Path):
    """SPEC "prepare": a script that can't be started aborts the run, with the shebang line and the reason."""
    message = r"^prepare script could not run \('#!/autobot/no/such/interpreter -x'\): \[Errno 2\] No such file or directory$"
    with pytest.raises(RuntimeError, match=message) as ei:
        make_runner([], prepare="#!/autobot/no/such/interpreter -x\ntrue\n").run()
    assert isinstance(ei.value.__cause__, OSError)
    assert "attach" not in timeline.names()
    assert list(prep_tmp.iterdir()) == []


# -- P5-72: the temp file -------------------------------------------------------


def test_p5_72_temp_dir_holds_the_script_and_nothing_else(prepared, prep_tmp: Path, tmp_path: Path, monkeypatch):
    """SPEC "prepare": one temp file, the script, for its owner alone; nothing of the environment
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
    """SPEC "prepare": SIGTERM and an interrupt remove the script's temp file; SIGKILL leaves it, and nothing
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
    """SPEC "prepare": the temp file's path is an argument, whatever characters it has."""
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


def test_p5_73_spawn_is_rendered_once_prepare_has_run(timeline: Timeline, prep_tmp: Path, monkeypatch, capsys):
    """SPEC "The order of a run": `prepare` runs first, `env` is resolved with what it printed, and `spawn`
    is rendered after that: it reads a variable that only `prepare` sets."""
    monkeypatch.setenv("AB_SHELL", "from-os")
    timeline.stop_attach = True
    r = make_runner(
        [], prepare="echo AB_HOST=sw1\necho AB_SHELL=ssh\n", spawn="{{ env.AB_SHELL }} admin@{{ env.AB_HOST }} {{ env.AB_C }}",
        env={"AB_SHELL": "telnet", "AB_HOST": None, "AB_C": "-p {{ env.AB_HOST }}"},
    )
    with pytest.raises(AttachRecorded, match="^ssh admin@sw1 -p sw1$"):
        r.run()
    assert capsys.readouterr().err.splitlines()[-3:] == [">> prepare: 2 set", ">> prepare: done", ">> attach: ssh admin@sw1 -p sw1"]


def test_p5_73_env_has_no_value_before_the_run():
    """SPEC "The order of a run": `env` is resolved by the run, once `prepare` has run, not when the runner
    is built: a required variable that only `prepare` gives is no error of the construction."""
    r = Runner(yaml_config(prepare="echo AB_A=a\n", env={"AB_A": None}), {})
    assert r._env == {}


@pytest.mark.parametrize(
    ("spawn", "message"),
    [
        ("{{ vars.cmd }}", r"attach\.spawn rendered to an empty command: '\{\{ vars\.cmd \}\}'"),
        (" {{ env.AB_SHELL }} ", r"attach\.spawn rendered to an empty command: ' \{\{ env\.AB_SHELL \}\} '"),
        ("{% if env.AB_A is defined %}ssh{% endif %}", r"attach\.spawn rendered to an empty command: .*"),
        ("ssh {{ vars.nope }}", "template error: 'dict object' has no attribute 'nope'"),
        ("ssh {{ env.AB_HOST }}", "template error: env has no key 'AB_HOST': the script's env section doesn't declare it"),
    ],
    ids=["empty", "empty-env", "empty-undeclared", "undefined", "exported-by-prepare"],
)
def test_p5_73_spawn_that_depends_on_a_value_fails_after_prepare(timeline, tmp_path: Path, spawn: str, message: str):
    """SPEC "The order of a run": a `spawn` that renders to no command, or reads what isn't there, stops the
    run once `prepare` has run, and nothing is spawned; a variable `prepare` exports is not a key of `env`."""
    marker = tmp_path / "prepared"
    r = make_runner([], prepare=f"touch {marker}\nexport AB_HOST=h AB_A=1\n", spawn=spawn, vars={"cmd": ""}, env={"AB_SHELL": ""})
    with pytest.raises(ValueError, match=f"^{message}$"):
        r.run()
    assert marker.exists()
    assert "attach" not in timeline.names()


@pytest.mark.parametrize(
    ("spawn", "message"),
    [("ssh {{ env.AB_HOST", "template error: unexpected end of template.*"), ("{% if env.X %}", "template error: Unexpected end of template.*")],
    ids=["syntax", "syntax-block"],
)
def test_p5_73_spawn_with_a_syntax_error_stops_the_run_before_prepare(timeline, tmp_path: Path, spawn: str, message: str):
    """SPEC "The order of a run": the syntax of `spawn` depends on no value, and is checked before
    `prepare` runs."""
    marker = tmp_path / "prepared"
    r = make_runner([], prepare=f"touch {marker}\n", spawn=spawn)
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
    """SPEC "prepare": it runs in Autobot's own environment with `TERM=dumb` and `NO_COLOR=1`."""
    for name in ("TERM", "NO_COLOR"):
        monkeypatch.delenv(name, raising=False)
    for name, value in os_env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("AB_OS", "os")
    seen = tmp_path / "seen"
    env = spawn_env(prepare=f'echo "$TERM/$NO_COLOR/$AB_OS" > {seen}\n', env={"TERM": "none"})
    assert seen.read_text() == "dumb/1/os\n"
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
    # and so is one that `prepare` printed
    assert in_shell(prepare="echo AB_A=printed\necho AB_OS=printed\n", env={"AB_A": None, "AB_OS": None}) == "from os|_|os|dumb|1"


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


# -- P5-98..103: the output of prepare --------------------------------------------

SECRET = "S3CRET-v4lue"


def yaml_config(**kw):
    from conftest import make_config

    return make_config([], **kw)


def lines(*out: str) -> str:
    """A `prepare` that prints these lines."""
    return "".join(f"printf '%s\\n' {shlex.quote(line)}\n" for line in out)


def test_p5_98_printed_lines_set_declared_variables_of_each_type(prepared):
    """SPEC "The output of `prepare`": a line `NAME=value` sets the declared variable, as a value of the type
    of its default: a string, a boolean, an integer, a number."""
    r = prepared(
        lines("AB_S=text", "AB_B=yes", "AB_I= -7 ", "AB_F=2.5", "AB_R=req"),
        env={"AB_S": "s", "AB_B": False, "AB_I": 1, "AB_F": 0.5, "AB_R": None, "AB_D": "{{ env.AB_S }}-{{ env.AB_I + 1 }}"},
    )
    assert r._env == {"AB_S": "text", "AB_B": True, "AB_I": -7, "AB_F": 2.5, "AB_R": "req", "AB_D": "text--6"}


def test_p5_98_step_reads_a_variable_only_prepare_sets(capfd):
    """SPEC "The order of a run": a `spawn` and a step read it; the value is never rendered."""
    out = run_vars(
        [{"cmd": "echo [{{ env.AB_HOST }}][{{ env.AB_N * 2 }}][$AB_SEEN]", "register": "out", "when": "{{ env.AB_ON }}"}],
        prepare=lines("AB_HOST={{ 1/0 }}", "AB_N=21", "AB_ON=on", "AB_SEEN=sw1"),
        spawn=f"env AB_SEEN={{{{ env.AB_SEEN }}}} {BASH}",
        env={"AB_HOST": None, "AB_N": 0, "AB_ON": False, "AB_SEEN": None},
    )
    assert out["out"] == "[{{ 1/0 }}][42][sw1]"
    assert ">> prepare: 4 set" in capfd.readouterr().err


@pytest.mark.parametrize(
    ("given", "printed", "environ", "expected"),
    [
        (True, True, True, "cli"), (True, True, False, "cli"), (True, False, True, "cli"),
        (False, True, True, "prep"), (False, True, False, "prep"),
        (False, False, True, "os"), (False, False, False, "dflt"),
    ],
    ids=["env-option", "option-over-prepare", "option-over-environment", "prepare", "prepare-over-default", "environment", "default"],
)
def test_p5_99_value_comes_from_env_option_then_prepare_then_the_environment_then_the_default(
    prepared, monkeypatch, given: bool, printed: bool, environ: bool, expected: str
):
    """SPEC "The environment in templates": the first of `--env`, a line `prepare` printed, the environment
    and the default."""
    if environ:
        monkeypatch.setenv("AB_A", "os")
    r = prepared(lines(*(["AB_A=prep"] if printed else [])), env={"AB_A": "dflt", "AB_B": "{{ env.AB_A }}!"}, given={"AB_A": "cli"} if given else {})
    assert r._env == {"AB_A": expected, "AB_B": f"{expected}!"}


def test_p5_99_empty_value_from_prepare_is_set(prepared, monkeypatch):
    """SPEC "The environment in templates": a variable set to the empty string is set, by `prepare` too: it
    comes before the environment's value and the default, and satisfies a required variable."""
    monkeypatch.setenv("AB_A", "os")
    r = prepared(lines("AB_A=", "AB_B=", "AB_C="), env={"AB_A": "dflt", "AB_B": None, "AB_C": True})
    assert r._env == {"AB_A": "", "AB_B": "", "AB_C": False}


def test_p5_100_form_of_a_line(prepared, capfd):
    """SPEC "The output of `prepare`": empty lines are skipped, a `\r` before the line break is dropped, the
    value is everything after the first `=`, as written, and of a name printed twice the last value wins."""
    script = (
        r"""printf '\n\nAB_A=a=b==c\n\r\nAB_B=  spaced "q" '\''s'\'' $HOME  \r\n\nAB_C=\nAB_D=first\nAB_D=\nAB_D=last\n\n'""" "\n"
        r"printf 'AB_E=no newline at the end'" "\n"
    )
    names = ("AB_A", "AB_B", "AB_C", "AB_D", "AB_E")
    r = prepared(script, env=dict.fromkeys(names))
    assert r._env == {
        "AB_A": "a=b==c", "AB_B": "  spaced \"q\" 's' $HOME  ", "AB_C": "", "AB_D": "last", "AB_E": "no newline at the end",
    }
    assert ">> prepare: 5 set" in capfd.readouterr().err


def test_p5_100_carriage_return_inside_a_line_is_part_of_the_value(prepared):
    """SPEC "The output of `prepare`": only a `\r` right before the line break is dropped, and one of them."""
    r = prepared(r"printf 'AB_A=a\rb\r\r\nAB_B=x\r'" "\n", env={"AB_A": None, "AB_B": None})
    assert r._env == {"AB_A": "a\rb\r", "AB_B": "x\r"}


UNDECLARED = "prepare: line {} of its output sets a variable the script's env section doesn't declare"
BAD_OUTPUT = {
    "no-equals": ([SECRET], "prepare: line 1 of its output is not NAME=value"),
    "no-equals-later": (["AB_A=a", "", f"token {SECRET}"], "prepare: line 3 of its output is not NAME=value"),
    "blank": (["AB_A=a", "   "], "prepare: line 2 of its output is not NAME=value"),
    "undeclared": ([f"AB_NOPE={SECRET}"], UNDECLARED.format(1)),
    "undeclared-later": (["AB_A=a", f"AB_B={SECRET}", f"ab_a={SECRET}"], UNDECLARED.format(3)),
    "spaced-name": ([f"AB_A ={SECRET}"], UNDECLARED.format(1)),
    "exported": ([f"export AB_A={SECRET}"], UNDECLARED.format(1)),
    "no-name": ([f"={SECRET}"], UNDECLARED.format(1)),
    # what stands before the `=` is no name, and the secret: a base64 token with its padding, a line of a log
    "token": ([f"{SECRET}=="], UNDECLARED.format(1)),
    "log-line": (["AB_A=a", f"password: {SECRET} url=http://x"], UNDECLARED.format(2)),
}


def texts(out: list[str]) -> set[str]:
    """What an error about the lines `out` must not show: the secret, and what a bad line has before its `=`."""
    return {SECRET} | {line.partition("=")[0] for line in out if line.strip() and not line.startswith(("AB_A=", "AB_B="))}


@pytest.mark.parametrize(("out", "message"), BAD_OUTPUT.values(), ids=BAD_OUTPUT.keys())
def test_p5_101_bad_line_fails_the_run_and_spawns_nothing(timeline: Timeline, prep_tmp: Path, capfd, out: list[str], message: str):
    """SPEC "The output of `prepare`": a line without `=`, or for a name the `env` section doesn't declare,
    is an error with the number of the line and no text of the line; nothing is spawned."""
    with pytest.raises(ValueError) as ei:
        make_runner([], prepare=lines(*out), env={"AB_A": "a", "AB_B": None}).run()
    assert str(ei.value) == message
    assert "attach" not in timeline.names()
    assert list(prep_tmp.iterdir()) == []
    out_, err = capfd.readouterr()
    assert progress(err) == [">> prepare: running local script (no shebang, using /bin/sh)"]
    assert not [text for text in texts(out) if text and text in out_ + err]


@pytest.mark.parametrize(("out", "message"), BAD_OUTPUT.values(), ids=BAD_OUTPUT.keys())
def test_p5_101_cli_reports_a_bad_line_without_its_text(tmp_path: Path, out: list[str], message: str):
    """SPEC "The output of `prepare`", "CLI": a failed run, status 3; neither the report nor the traceback
    of `--traceback` holds any text of the line, what stands before its `=` included."""
    spawned = tmp_path / "spawned"
    doc = make_doc([], prepare=lines(*out), env={"AB_A": "a", "AB_B": None}, spawn=f"touch {spawned}")
    for flags in ((), ("--traceback",)):
        res = run_cli(doc, tmp_path, *flags)
        assert res.returncode == 3 and res.stdout == ""
        assert res.stderr.splitlines()[-1] == f"Run failed in {tmp_path / 'script.autobot.yaml'}: {message}"
        assert ("Traceback" in res.stderr) == bool(flags)
        assert not [text for text in texts(out) if text and text in res.stderr]
        assert not spawned.exists()


def test_p5_102_text_from_prepare_that_is_not_of_the_type_names_prepare(timeline: Timeline, tmp_path: Path):
    """SPEC "The environment in templates" (Types): the error names the variable, the type and `prepare`,
    never the value, in the traceback either; `--env` for the same variable comes first, and is the one read."""
    for default, kind in ((True, "a boolean"), (3, "an integer"), (0.5, "a number")):
        with pytest.raises(ValueError) as ei:
            make_runner([], prepare=lines(f"AB_A={SECRET}"), env={"AB_A": default}).run()
        assert str(ei.value) == f"env.AB_A: the value from prepare is not {kind}"
        assert ei.value.__cause__ is None and ei.value.__suppress_context__
    assert "attach" not in timeline.names()
    doc = make_doc([], prepare=lines(f"AB_A={SECRET}"), env={"AB_A": 3}, spawn="true")
    res = run_cli(doc, tmp_path, "--traceback")
    assert res.returncode == 3 and SECRET not in res.stderr
    assert res.stderr.splitlines()[-1] == f"Run failed in {tmp_path / 'script.autobot.yaml'}: env.AB_A: the value from prepare is not an integer"
    timeline.stop_attach = True
    r = make_runner([], prepare=lines(f"AB_A={SECRET}"), env={"AB_A": 3}, given={"AB_A": "4"})
    with pytest.raises(AttachRecorded):
        r.run()
    assert r._env == {"AB_A": 4}


def test_p5_102_failing_script_sets_nothing(timeline: Timeline, capfd):
    """SPEC "prepare": after a non-zero exit nothing of the output is used: the run stops with the exit
    status, whatever the script printed, a line that is no `NAME=value` included."""
    with pytest.raises(RuntimeError, match="^prepare script failed with exit code 5$"):
        make_runner([], prepare=lines("AB_A=printed", SECRET, f"AB_NOPE={SECRET}") + "exit 5\n", env={"AB_A": None}).run()
    assert "attach" not in timeline.names()
    out, err = capfd.readouterr()
    assert SECRET not in out + err and "set" not in err


def test_p5_102_required_variable_that_prepare_gives_is_satisfied(prepared):
    """SPEC "The environment in templates" (Required): a line of `prepare` gives a required variable."""
    assert prepared(lines("AB_A=from prepare"), env={"AB_A": None})._env == {"AB_A": "from prepare"}


def test_p5_102_required_variable_nothing_gives_fails_after_prepare(tmp_path: Path):
    """SPEC "The order of a run", "CLI": `env` is resolved once `prepare` has run, so a required variable
    without a value is a failed run, status 3, and `prepare` has run; nothing is spawned."""
    marker, spawned = tmp_path / "prepared", tmp_path / "spawned"
    doc = make_doc([], prepare=f"touch {marker}\necho AB_A=a\n", env={"AB_A": None, "AB_B": None}, spawn=f"touch {spawned}")
    res = run_cli(doc, tmp_path)
    assert res.returncode == 3 and res.stdout == ""
    assert res.stderr.splitlines()[-1] == (
        f"Run failed in {tmp_path / 'script.autobot.yaml'}: env.AB_B is required: give it with --env AB_B=VALUE "
        "or in the environment"
    )
    assert marker.exists() and not spawned.exists()


def test_p5_103_prepare_sees_the_env_options_and_not_the_defaults(prepared, monkeypatch, tmp_path: Path):
    """SPEC "prepare": each `--env KEY=VALUE` is a variable of the script's environment, with the text as
    given, over the environment's; a default is not in it, and neither is anything rendered."""
    monkeypatch.setenv("AB_OS", "os")
    monkeypatch.setenv("AB_B", "os-b")
    seen = tmp_path / "seen"
    r = prepared(
        f'echo "[${{AB_A-unset}}][$AB_B][$AB_C][${{AB_D-unset}}][$AB_OS][$TERM]" > {seen}\necho "AB_D=$AB_C!"\n',
        env={"AB_A": "dflt", "AB_B": "dflt", "AB_C": 1, "AB_D": "{{ env.AB_A }}", "AB_OS": "dflt"},
        given={"AB_B": "cli {{ x }}", "AB_C": " 7 "},
    )
    assert seen.read_text() == "[unset][cli {{ x }}][ 7 ][unset][os][dumb]\n"
    assert r._env == {"AB_A": "dflt", "AB_B": "cli {{ x }}", "AB_C": 7, "AB_D": " 7 !", "AB_OS": "os"}


def test_p5_103_env_option_for_term_reaches_prepare_and_not_the_spawned_process(spawn_env, tmp_path: Path):
    """SPEC "prepare", "The environment of the spawned process": an `--env` value is set over the plain
    terminal for `prepare` alone."""
    seen = tmp_path / "seen"
    env = spawn_env(prepare=f'echo "$TERM" > {seen}\n', env={"TERM": None}, given={"TERM": "vt100"})
    assert seen.read_text() == "vt100\n" and env["TERM"] == "dumb"


def test_p5_103_script_in_another_language_sets_variables(prepared):
    """SPEC "prepare": the protocol is the output, whatever the language of the script."""
    script = "#!/usr/bin/env python3\nimport os, sys\nprint('AB_A=' + os.environ['AB_B'].upper())\nprint('AB_N=42')\nprint('note', file=sys.stderr)\n"
    r = prepared(script, env={"AB_A": None, "AB_B": None, "AB_N": 0}, given={"AB_B": "from-cli"})
    assert r._env == {"AB_A": "FROM-CLI", "AB_B": "from-cli", "AB_N": 42}


def test_p5_103_process_left_running_does_not_hold_the_run(prepared, tmp_path: Path):
    """SPEC "The output of `prepare`": the output is read when the script exits, so a process it leaves
    running with the script's stdout doesn't hold up the run, and what that process prints later isn't read."""
    pid, late = tmp_path / "pid", tmp_path / "late"
    began = time.monotonic()
    try:
        r = prepared(
            f"sh -c 'sleep 1; echo AB_B=late; echo AB_NOPE=late; touch {late}; exec sleep 60' &\necho $! > {pid}\necho AB_A=a\n",
            env={"AB_A": None, "AB_B": "dflt"},
        )
        assert time.monotonic() - began < 5 and r._env == {"AB_A": "a", "AB_B": "dflt"}
        while not late.exists():  # the process had the script's stdout, and wrote to it: it is the sleeper by then
            assert time.monotonic() - began < 15
            time.sleep(0.05)
    finally:
        os.kill(int(pid.read_text()), signal.SIGKILL)
