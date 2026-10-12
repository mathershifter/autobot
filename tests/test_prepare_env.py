"""P5-65..77, P5-81, P5-98..105: `prepare`, a local shell script that is sourced before anything else and
sets declared variables through the environment its shell leaves, and the environment of the spawned process
(SPEC "prepare", "What `prepare` sets", "The order of a run", "The environment of the spawned process")."""

from __future__ import annotations

import fcntl
import os
import re
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
import pydantic
import pytest
import yaml
from conftest import BASH, PS1, AttachRecorded, SpawnRecorded, Timeline, make_config, make_doc, make_runner, run_cli, run_vars

from autobot import prepare
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


RUNNING = ">> prepare: running local script (no shebang, using /bin/sh)"
UNREAD = ">> prepare: its environment could not be read"
DONE = ">> prepare: done"
SECRET = "S3CRET-v4lue"


def progress(err: str) -> list[str]:
    """Autobot's lines up to the spawn."""
    return [line for line in err.splitlines() if line.startswith(">> ") and not line.startswith(">> attach: ")]


# -- P5-65: what prepare sets is for the declared variables alone -------------------


def test_p5_65_what_prepare_sets_reaches_a_template_under_a_declared_name_alone(prepared, monkeypatch: pytest.MonkeyPatch):
    """SPEC "What `prepare` sets": a declared variable has the value the script left, over the one of
    Autobot's environment; a name the section doesn't declare is not a key of `env`, whatever the script
    does with it."""
    monkeypatch.setenv("AB_OS", "os")
    r = prepared(
        "export AB_OS=prep AB_NEW=new\nAB_A=prep\nAB_PLAIN=plain\ncd /\n",
        env={"AB_OS": "dflt", "AB_A": "dflt", "AB_B": "dflt-b", "AB_C": "{{ env.AB_A }}-c"},
    )
    assert r.render("{{ env.AB_OS }} {{ env.AB_A }} {{ env.AB_B }} {{ env.AB_C }}") == "prep prep dflt-b prep-c"
    assert list(r._env) == ["AB_OS", "AB_A", "AB_B", "AB_C"]
    for name in ("AB_NEW", "AB_PLAIN", "PWD", "PATH"):
        with pytest.raises(ValueError, match=f"^template error: env has no key '{name}': the script's env section doesn't declare it$"):
            r.render("{{ env.%s }}" % name)


def test_p5_65_what_prepare_sets_reaches_a_step_and_not_the_spawned_process(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """SPEC "What `prepare` sets": seen from inside the spawned shell, which has Autobot's environment and
    nothing of the script's, and from a step's template; the working directory of the run stays what it was."""
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
    assert out["tmpl"] == "[prep][prep][undeclared]"
    assert out["pwd"] == cwd == os.getcwd()


def test_p5_65_prepare_is_written_as_it_is(prepared, tmp_path: Path):
    """SPEC "prepare": the script is not a template: `{{`, `{%` and `{#` are its own text, also where a
    template would be a syntax error or name something undefined; and so is a value it sets."""
    out = tmp_path / "out"
    text = "{{ env.AB_A }} {{ vars.v }} {{ nope }} {% endfor %} {# {{"
    r = prepared(f"AB_B='{text}'\necho \"$AB_B\" > {out}\n", env={"AB_A": "a", "AB_B": None}, vars={"v": "V"})
    assert out.read_text() == text + "\n"
    assert r._env == {"AB_A": "a", "AB_B": text}


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


def test_p5_66_non_utf8_byte_of_a_value_prepare_sets_is_kept_as_the_environment_keeps_one(prepared):
    """SPEC "What `prepare` sets": the environment the shell leaves is decoded as Autobot's own is: a byte
    that isn't UTF-8 is a lone surrogate in the value; it is no error."""
    r = prepared("AB_A=$(printf 'caf\\351')\nAB_B=$(printf 'caf\\303\\251')\n", env={"AB_A": None, "AB_B": None})
    assert r._env == {"AB_A": "caf\udce9", "AB_B": "caf\u00e9"}


def test_p5_66_non_utf8_byte_of_a_value_prepare_sets_is_sent_with_replacement():
    """SPEC "The environment in templates": sent to the session, it goes out as `?`."""
    out = run_vars([{"cmd": "echo R=[{{ env.AB_A }}]", "register": "tmpl"}], prepare="AB_A=$(printf 'caf\\351')\n", env={"AB_A": None})
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


def test_p5_67_failing_script_sets_nothing(timeline: Timeline, capfd):
    """SPEC "prepare": after a non-zero exit nothing of what the script set is used, and nothing says that
    it was: the run stops with the exit status."""
    with pytest.raises(RuntimeError, match="^prepare script failed with exit code 5$"):
        make_runner([], prepare=f"AB_A={SECRET}\nexit 5\n", env={"AB_A": 3}).run()
    assert "attach" not in timeline.names()
    out, err = capfd.readouterr()
    assert progress(err) == [RUNNING] and SECRET not in out + err


SUCCEEDING = {
    "exit-0": "AB_A=set\nexit 0\nAB_A=later\nexit 3\n",
    "exit-in-function": "f() { AB_A=set; exit 0; }\nf\nAB_A=later\n",
    "return": "AB_A=set\nreturn 0\nAB_A=later\n",
    "background-process": "sleep 3 > /dev/null 2>&1 &\nAB_A=set\n",
    "positional-parameters": "[ $# -eq 0 ] && set -- a b c && shift\nAB_A=set\n",
    "subshell": "(AB_A=inner; exit 0)\nAB_A=set\n",
    "python-home": "export PYTHONHOME=/nonexistent PYTHONPATH=/nonexistent AB_A=set\n",
}
SHEBANGS = ["", *(f"#!{path}\n" for path in map(shutil.which, prepare.SHELLS) if path)]


def installed(shell: str) -> pytest.MarkDecorator:
    return pytest.mark.skipif(not shutil.which(shell), reason=f"{shell} is not installed")


# no shebang and each shell: one that isn't installed is a skipped case, with the reason
EACH_SHELL = [pytest.param("", id="none"), *(pytest.param(f"#!{shutil.which(s)}\n", id=s, marks=installed(s)) for s in prepare.SHELLS)]


def dump_fd() -> int:
    """The descriptor the next `prepare` gives its shell for the environment."""
    fd = fcntl.fcntl(0, fcntl.F_DUPFD, prepare._FD)
    os.close(fd)
    return fd


@pytest.fixture
def sourced(prep_tmp: Path, tmp_path: Path, capfd):
    """`prepare.run` in an environment of its own, `PATH`, an empty `HOME` and the marker `AB_OS`; returns
    the environment the shell left and what went to stderr. A test asserts on the markers alone, so a failure
    shows no environment."""

    def run(script: str, **extra: str) -> tuple[dict[str, str], str]:
        env = prepare.run(script, {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "AB_OS": f"{SECRET}-os", **extra})
        assert list(prep_tmp.iterdir()) == []
        out, err = capfd.readouterr()
        assert out == ""
        return env, err

    return run


@pytest.mark.parametrize("shebang", EACH_SHELL)
@pytest.mark.parametrize("script", SUCCEEDING.values(), ids=SUCCEEDING.keys())
def test_p5_67_script_that_exits_zero_sets_its_variables_however_it_ends(prepared, capsys, script: str, shebang: str):
    """SPEC "prepare", "What `prepare` sets": however the script ends with status 0 (its last command, an
    `exit 0` in the middle or in a function), the run goes on with what the script had set by then, in every
    shell; it waits for no process the script leaves running."""
    began = time.monotonic()
    r = prepared(shebang + script, env={"AB_A": "dflt"})
    assert time.monotonic() - began < 2
    assert r._env == {"AB_A": "set"}
    assert progress(capsys.readouterr().err) == [">> prepare: running local script" if shebang else RUNNING, DONE]


# -- P5-68: output ------------------------------------------------------------


def test_p5_68_what_the_script_prints_is_shown_and_not_read(prepared, capfd):
    """SPEC "What `prepare` sets", "Output": the script's stdout goes to Autobot's stderr, like its stderr,
    and nothing of it is read, a line or a NUL-ended entry that names a declared variable included;
    Autobot's lines say that the script runs and that it is done, and no name or value."""
    r = prepared(
        "echo AB_A=evil\nprintf 'AB_B=evil\\0'\necho chatter\necho to-stderr >&2\nAB_A=S3CRET\nexport AB_C=S3CRET\n",
        env={"AB_A": None, "AB_B": "b"},
    )
    out, err = capfd.readouterr()
    assert out == ""
    assert "AB_A=evil\nAB_B=evil\0chatter\nto-stderr\n" in err
    assert r._env == {"AB_A": "S3CRET", "AB_B": "b"}
    assert progress(err) == [RUNNING, DONE]
    assert "S3CRET" not in out + err


def test_p5_68_environment_the_shell_leaves_is_never_shown(prepared, capfd, monkeypatch):
    """SPEC "Output": in any shell, wherever the script ends: an `exit` in the middle, or in a function
    whose output is redirected, runs the trap that writes the environment with the redirections of the
    command that was running."""
    monkeypatch.setenv("AB_OS", "S3CRET-os")
    for shebang in SHEBANGS:
        for script in ("AB_A=S3CRET\nexit 0\n", "f() { AB_A=S3CRET; exit 0; }\nf > /dev/null\n", "AB_A=S3CRET\n"):
            assert prepared(shebang + script, env={"AB_A": None})._env == {"AB_A": "S3CRET"}
    out, err = capfd.readouterr()
    assert out == "" and "S3CRET" not in err and "\0" not in err


@pytest.mark.parametrize("shebang", EACH_SHELL)
def test_p5_68_trace_of_the_shell_shows_nothing_of_the_environment_it_leaves(sourced, shebang: str):
    """SPEC "What `prepare` sets" (No value is shown): with `-x`, in the shebang or set by the script, the
    shell traces its commands, the script's assignment among them, and not the environment it writes out."""
    env, err = sourced((shebang.rstrip() + " -x\n" if shebang else "set -x\n") + "AB_A=traced\n")
    assert env.get("AB_A") == "traced" and env.get("AB_OS") == f"{SECRET}-os"
    assert "AB_A=traced" in err and SECRET not in err and "\0" not in err


# -- P5-69: the shell ----------------------------------------------------------

BASH_ONLY = 'arr=(x y z)\n[[ ${{#arr[@]}} == 3 ]] && echo "${{arr[*]:1}}" "$0" "$#" > {out}\nAB_A=${{arr[1]^^}}\n'


@pytest.mark.parametrize(
    "shebang",
    ["#!/bin/bash", "#! /bin/bash", "#!/usr/bin/env bash", "#!/bin/bash -eu", "#!/bin/bash -e -u", "#!/usr/bin/env -S bash -eu", " \n\ufeff#!/bin/bash\r"],
)
def test_p5_69_shebang_picks_the_shell(prepared, prep_tmp: Path, tmp_path: Path, capsys, shebang: str):
    """SPEC "prepare": the shell the shebang names sources the script, with the shebang's options; `$0` is
    the temp file, and there are no arguments. What comes before the `#!` is removed."""
    out = tmp_path / "out"
    r = prepared(f"{shebang}\n" + BASH_ONLY.format(out=out), env={"AB_A": None})
    words, script, count = out.read_text().rsplit(None, 2)
    assert words == "y z" and count == "0"
    assert script.startswith(f"{prep_tmp}/_autobot_") and script.endswith(".sh")
    assert r._env == {"AB_A": "Y"}
    assert progress(capsys.readouterr().err) == [">> prepare: running local script", DONE]


@pytest.mark.parametrize("shell", [pytest.param(s, marks=installed(s)) for s in prepare.SHELLS])
def test_p5_69_each_shell_leaves_its_environment(prepared, tmp_path: Path, monkeypatch, shell: str):
    """SPEC "prepare": each of the shells, named by its path or through `env`, with and without options:
    a plain assignment, an `export`, a value with a line break and an `unset` are read from all of them."""
    monkeypatch.setenv("AB_OS", "os")
    seen = tmp_path / "seen"
    body = f"AB_A=plain\nexport AB_B='two\nlines='\nunset AB_OS\nps -o comm= -p $$ > {seen}\nexit 0\n"
    path = shutil.which(shell)
    for shebang in (f"#!{path}", f"#!/usr/bin/env {shell}", f"#!{path} -eu", f"#!/usr/bin/env -S {shell} -eu"):
        r = prepared(f"{shebang}\n{body}", env={"AB_A": None, "AB_B": None, "AB_OS": "dflt"})
        assert r._env == {"AB_A": "plain", "AB_B": "two\nlines=", "AB_OS": "dflt"}
        assert seen.read_text().strip() == shell


def test_p5_69_script_without_a_shebang_is_sourced_by_sh(prepared, prep_tmp: Path, tmp_path: Path):
    """SPEC "prepare": `/bin/sh -c <wrapper> <temp file>`: `$0` is the temp file, and there are no arguments;
    the wrapper names a descriptor of 173 or more, and the shell has it."""
    out = tmp_path / "out"
    prepared(f'echo "$0" "$#" > {out}\nps -o args= -p $$ >> {out}\nls /dev/fd/ >> {out}\n')
    script, args, *fds = out.read_text().splitlines()
    assert script.startswith(f"{prep_tmp}/_autobot_") and script.endswith(".sh 0")
    fd = int(re.search(r">/dev/fd/(\d+) ", args)[1])
    assert fd >= 173 and str(fd) in fds
    assert args == f"/bin/sh -c {prepare._WRAPPER % fd} {script.removesuffix(' 0')}"
    assert prepare._WRAPPER % 173 == 'set -a; trap "env -0 >/dev/fd/173 || :" EXIT; . "$0"'


def test_p5_69_options_of_the_shebang_apply_to_the_script(timeline: Timeline, prep_tmp: Path):
    """SPEC "prepare": `#!/bin/sh -eu` sources the script with those options."""
    with pytest.raises(RuntimeError, match="^prepare script failed with exit code [12]$"):
        make_runner([], prepare='#!/bin/sh -eu\necho "$AB_NOT_SET"\n').run()
    assert "attach" not in timeline.names()


def test_p5_69_shell_that_cannot_be_started(timeline: Timeline, prep_tmp: Path):
    """SPEC "prepare": a shell that can't be started aborts the run, with the shebang line and the reason."""
    message = r"^prepare script could not run \('#!/autobot/no/such/bash -x'\): \[Errno 2\] No such file or directory$"
    with pytest.raises(RuntimeError, match=message) as ei:
        make_runner([], prepare="#!/autobot/no/such/bash -x\ntrue\n").run()
    assert isinstance(ei.value.__cause__, OSError)
    assert "attach" not in timeline.names()
    assert list(prep_tmp.iterdir()) == []


# -- P5-70: a shebang that names no shell ---------------------------------------

NOT_SHELLS = {
    "#!/usr/bin/env python3": "python3",
    f"#!{sys.executable}": sys.executable,
    "#!/usr/bin/env -S python3 -u": "python3",
    "#!/usr/bin/perl -w": "/usr/bin/perl",
    "#!/usr/bin/fish": "/usr/bin/fish",
    "#!/bin/bash5": "/bin/bash5",
    "#! /usr/bin/env\tnode": "node",
    " \n\ufeff#!/usr/bin/awk -f\r": "/usr/bin/awk",
    "#!/usr/bin/env": "",
    "#!/usr/bin/env -i": "",
    "#!": "",
}


@pytest.mark.parametrize(("shebang", "name"), NOT_SHELLS.items())
def test_p5_70_shebang_that_names_no_shell_is_a_validation_error(shebang: str, name: str):
    """SPEC "prepare": the script is a shell script; a shebang that names anything else is found when the
    script is loaded, with the interpreter it names."""
    with pytest.raises(pydantic.ValidationError) as ei:
        make_config([], prepare=f"{shebang}\nprint('AB_A=1')\n")
    (error,) = ei.value.errors()
    assert (error["loc"], error["type"]) == (("prepare",), "value_error")
    assert error["msg"] == f"Value error, prepare is a shell script: its shebang names {name!r}"


@pytest.mark.parametrize(
    "shebang",
    ["#!/bin/sh", "#!/bin/dash -e", "#!/usr/bin/env ksh", "#!/usr/local/bin/zsh", "#!/usr/bin/env -S -i zsh -f", "#!/usr/bin/env -- bash", "# !/usr/bin/python3", "true #!/usr/bin/python3"],
)
def test_p5_70_shebang_that_names_a_shell_is_valid(shebang: str):
    """SPEC "prepare": `sh`, `bash`, `dash`, `ksh` and `zsh`, by the file name of the interpreter, by its
    path or through `env`; a first line that doesn't start with `#!` is no shebang."""
    assert make_config([], prepare=f"{shebang}\ntrue\n").prepare.startswith(shebang)


@pytest.mark.parametrize("shebang", ["#!/bin/sh --", "#!/bin/sh -", "#!/bin/bash -eu --", "#!/usr/bin/env -S zsh -f -", "#!/usr/bin/env ksh -- -e"])
def test_p5_70_shebang_that_ends_the_options_of_its_shell_is_a_validation_error(shebang: str):
    """SPEC "prepare": `-c` and what follows it come after the words of the shebang, so `--` or `-` after
    the shell is found when the script is loaded, like a shebang that names no shell."""
    with pytest.raises(pydantic.ValidationError) as ei:
        make_config([], prepare=f"{shebang}\nAB_A=1\n")
    (error,) = ei.value.errors()
    assert (error["loc"], error["type"]) == (("prepare",), "value_error")
    assert error["msg"] == "Value error, prepare is a shell script: '--' or '-' in its shebang ends the shell's options, and -c follows them"


def test_p5_70_cli_reports_the_shebang_before_anything_runs(tmp_path: Path):
    """SPEC "prepare", "CLI": `run` and `validate` report it as a validation error, status 1; nothing runs."""
    marker = tmp_path / "ran"
    script = f"#!/usr/bin/env python3\nopen({str(marker)!r}, 'w')\n"
    doc = make_doc([], prepare=script, spawn=f"touch {marker}")
    report = ["Validation errors:", "  prepare: prepare is a shell script: its shebang names 'python3' [value_error]"]
    res = run_cli(doc, tmp_path)
    assert res.returncode == 1 and res.stdout == "" and res.stderr.splitlines()[-2:] == report
    path = tmp_path / "script.autobot.yaml"
    res = subprocess.run(
        [sys.executable, "-W", "ignore", "-m", "autobot.cli", "validate", str(path)], check=False, capture_output=True, text=True, timeout=120
    )
    assert res.returncode == 1 and res.stdout == "" and res.stderr.splitlines() == [*report, f"{path}: invalid"]
    assert not marker.exists()


# -- P5-72: the temp file -------------------------------------------------------


def test_p5_72_temp_dir_holds_the_script_and_nothing_else(prepared, prep_tmp: Path, tmp_path: Path, monkeypatch):
    """SPEC "prepare": one temp file, the script, for its owner alone; nothing of the environment
    is written to a file with a name."""
    monkeypatch.setenv("AB_OS", "S3CRET-os")
    out = tmp_path / "out"
    # the needle is split so that the script's own file doesn't hold it
    prepared(f'ls {prep_tmp} > {out}\nstat -c %a "$0" >> {out}\ngrep -rl "S3CRET""-os" {prep_tmp} /dev/null >> {out}\ntrue\n')
    name, mode = out.read_text().splitlines()
    assert name.startswith("_autobot_") and name.endswith(".sh") and mode == "600"


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
    """SPEC "The order of a run": `prepare` runs first, `env` is resolved with what it set, and `spawn`
    is rendered after that: it reads a variable that only `prepare` sets."""
    monkeypatch.setenv("AB_SHELL", "from-os")
    timeline.stop_attach = True
    r = make_runner(
        [], prepare="AB_HOST=sw1\nAB_SHELL=ssh\n", spawn="{{ env.AB_SHELL }} admin@{{ env.AB_HOST }} {{ env.AB_C }}",
        env={"AB_SHELL": "telnet", "AB_HOST": None, "AB_C": "-p {{ env.AB_HOST }}"},
    )
    with pytest.raises(AttachRecorded, match="^ssh admin@sw1 -p sw1$"):
        r.run()
    assert capsys.readouterr().err.splitlines()[-2:] == [DONE, ">> attach: ssh admin@sw1 -p sw1"]


def test_p5_73_env_has_no_value_before_the_run():
    """SPEC "The order of a run": `env` is resolved by the run, once `prepare` has run, not when the runner
    is built: a required variable that only `prepare` gives is no error of the construction."""
    r = Runner(make_config([], prepare="AB_A=a\n", env={"AB_A": None}), {})
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
    ids=["empty", "empty-env", "empty-undeclared", "undefined", "set-by-prepare-undeclared"],
)
def test_p5_73_spawn_that_depends_on_a_value_fails_after_prepare(timeline, tmp_path: Path, spawn: str, message: str):
    """SPEC "The order of a run": a `spawn` that renders to no command, or reads what isn't there, stops the
    run once `prepare` has run, and nothing is spawned; a variable `prepare` sets that the section doesn't
    declare is not a key of `env`."""
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
    assert spawn_env(prepare="TERM=vt100\nAB_OS=prep\n", env={"TERM": None, "AB_OS": None}) == expected


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
    # and so is one that `prepare` set
    assert in_shell(prepare="AB_A=set\nexport AB_OS=set\n", env={"AB_A": None, "AB_OS": None}) == "from os|_|os|dumb|1"


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
    # and one that `prepare` set
    assert in_shell(spawn=f"env AB_A={{{{ env.AB_A }}}} {BASH}", prepare="AB_A=handed\n", env={"AB_A": None}) == "from os|handed|os|dumb|1"


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


# -- P5-98..104: what prepare sets ------------------------------------------------


@pytest.mark.parametrize("form", ["{0}={1}", "export {0}={1}", "{0}={1}; export {0}", "readonly {0}={1}"], ids=["plain", "export", "export-later", "readonly"])
def test_p5_98_assignment_sets_a_declared_variable_of_each_type(prepared, form: str):
    """SPEC "What `prepare` sets": a variable the script assigns, exported or not, sets the declared variable
    of that name, as a value of the type of its default: a string, a boolean, an integer, a number."""
    values = {"AB_S": "text", "AB_B": "yes", "AB_I": "' -7 '", "AB_F": "2.5", "AB_R": "req"}
    r = prepared(
        "".join(form.format(name, value) + "\n" for name, value in values.items()),
        env={"AB_S": "s", "AB_B": False, "AB_I": 1, "AB_F": 0.5, "AB_R": None, "AB_D": "{{ env.AB_S }}-{{ env.AB_I + 1 }}"},
    )
    assert r._env == {"AB_S": "text", "AB_B": True, "AB_I": -7, "AB_F": 2.5, "AB_R": "req", "AB_D": "text--6"}


def test_p5_98_variables_of_a_sourced_file_and_of_a_command_substitution_count(prepared, tmp_path: Path):
    """SPEC "What `prepare` sets": the example of the section: a file the script sources sets variables like
    the script itself, and so does an assignment from a command's output."""
    creds = tmp_path / "credentials.sh"
    creds.write_text("AB_TOKEN='tok=en'\nexport AB_USER=admin\nAB_OTHER=x\n")
    r = prepared(f". {creds}\nAB_HOST=$(echo jump1)\n", env={"AB_HOST": None, "AB_TOKEN": None, "AB_USER": "nobody"})
    assert r._env == {"AB_HOST": "jump1", "AB_TOKEN": "tok=en", "AB_USER": "admin"}


def test_p5_98_name_the_section_does_not_declare_is_not_taken(prepared, capfd):
    """SPEC "What `prepare` sets": a misspelled name sets nothing, and is no error; a required variable that
    nothing sets fails the run."""
    r = prepared("AB_HSOT=sw1\nexport ab_host=sw1 AB_NEW=new\n", env={"AB_HOST": "dflt"})
    assert r._env == {"AB_HOST": "dflt"}
    assert progress(capfd.readouterr().err) == [RUNNING, DONE]
    with pytest.raises(ValueError, match="^env.AB_HOST is required: give it with --env AB_HOST=VALUE or in the environment$"):
        make_runner([], prepare="AB_HSOT=sw1\n", env={"AB_HOST": None}).run()


@pytest.mark.parametrize("shebang", EACH_SHELL)
def test_p5_98_set_plus_a_ends_the_exporting_of_what_the_script_assigns(sourced, shebang: str):
    """SPEC "What `prepare` sets" (A plain assignment counts): after `set +a` an assignment is the shell's own
    and is not read, so data too large for an environment can be held; what was assigned before is still
    exported, an `export` still counts, and commands still run."""
    script = "AB_A=before\nset +a\nAB_BIG=$(head -c 200000 /dev/zero | tr '\\0' x)\nAB_B=after\nexport AB_C=exported\nAB_D=$(echo ran)\nexport AB_D\n"
    env, err = sourced(shebang + script)
    assert [env.get(name) for name in ("AB_A", "AB_B", "AB_C", "AB_D", "AB_BIG")] == ["before", None, "exported", "ran", None]
    assert progress(err) == [">> prepare: running local script" if shebang else RUNNING, DONE]


@pytest.mark.parametrize("shebang", EACH_SHELL)
def test_p5_98_exported_data_too_large_for_an_environment_leaves_nothing_to_read(sourced, shebang: str):
    """SPEC "What `prepare` sets" (A plain assignment counts): a plain assignment of 3 MB is exported, and
    no command can be started with it, the `env` of the wrapper included: the warning, and nothing is set."""
    env, err = sourced(shebang + "AB_A=before\nAB_BIG=$(head -c 3000000 /dev/zero | tr '\\0' x)\nls / > /dev/null || AB_B=failed\n")
    assert "AB_A" not in env and env.get("AB_OS") == f"{SECRET}-os"
    assert UNREAD in progress(err) and SECRET not in err and "\0" not in err


def test_p5_98_step_reads_a_variable_only_prepare_sets():
    """SPEC "The order of a run": a `spawn` and a step read it; the value is never rendered."""
    out = run_vars(
        [{"cmd": "echo [{{ env.AB_HOST }}][{{ env.AB_N * 2 }}][$AB_SEEN]", "register": "out", "when": "{{ env.AB_ON }}"}],
        prepare="AB_HOST='{{ 1/0 }}'\nAB_N=21\nAB_ON=on\nAB_SEEN=sw1\n",
        spawn=f"env AB_SEEN={{{{ env.AB_SEEN }}}} {BASH}",
        env={"AB_HOST": None, "AB_N": 0, "AB_ON": False, "AB_SEEN": None},
    )
    assert out["out"] == "[{{ 1/0 }}][42][sw1]"


@pytest.mark.parametrize(
    ("given", "script", "environ", "expected"),
    [
        (True, "AB_A=prep\n", True, "cli"), (True, "AB_A=prep\n", False, "cli"), (True, "", True, "cli"), (True, "unset AB_A\n", True, "cli"),
        (False, "AB_A=prep\n", True, "prep"), (False, "AB_A=prep\n", False, "prep"),
        (False, "", True, "os"), (False, "", False, "dflt"),
        (False, "unset AB_A\n", True, "dflt"), (False, "AB_A=prep\nunset AB_A\n", True, "dflt"), (False, "unset AB_A\nAB_A=prep\n", True, "prep"),
    ],
    ids=[
        "env-option", "option-over-prepare", "option-over-environment", "option-over-unset", "prepare", "prepare-over-default",
        "environment", "default", "unset", "set-and-unset", "unset-and-set",
    ],
)
def test_p5_99_value_comes_from_env_option_then_the_environment_prepare_left_then_the_default(
    prepared, monkeypatch, given: bool, script: str, environ: bool, expected: str
):
    """SPEC "The environment in templates": the first of `--env`, the environment `prepare` left (a
    variable the script set, or one of Autobot's that it left alone; one it unset is not in it) and the
    default."""
    if environ:
        monkeypatch.setenv("AB_A", "os")
    r = prepared(script + "true\n", env={"AB_A": "dflt", "AB_B": "{{ env.AB_A }}!"}, given={"AB_A": "cli"} if given else {})
    assert r._env == {"AB_A": expected, "AB_B": f"{expected}!"}


def test_p5_99_required_variable_the_script_unsets_has_no_value(timeline: Timeline, monkeypatch):
    """SPEC "The environment in templates": a variable of Autobot's environment that the script unsets is
    not set: a required one fails the run."""
    monkeypatch.setenv("AB_A", "os")
    with pytest.raises(ValueError, match="^env.AB_A is required: give it with --env AB_A=VALUE or in the environment$"):
        make_runner([], prepare="unset AB_A\n", env={"AB_A": None}).run()
    assert "attach" not in timeline.names()


def test_p5_99_empty_value_from_prepare_is_set(prepared, monkeypatch):
    """SPEC "The environment in templates": a variable set to the empty string is set, by `prepare` too: it
    comes before the default, and satisfies a required variable."""
    monkeypatch.setenv("AB_A", "os")
    r = prepared("AB_A=\nAB_B=''\nexport AB_C=\n", env={"AB_A": "dflt", "AB_B": None, "AB_C": True})
    assert r._env == {"AB_A": "", "AB_B": "", "AB_C": False}


@pytest.mark.parametrize("script", [None, "true\n", "exec true\n", "#!/bin/bash\ntrue\n"], ids=["no-prepare", "prepare", "unread", "bash"])
def test_p5_99_declared_variable_reads_the_environment_of_the_run_with_and_without_a_prepare(timeline, prep_tmp, monkeypatch, script):
    """SPEC "The environment in templates": without a `prepare`, and with one whose environment can't be
    read, a declared variable is looked up in the environment a `prepare` is given, Autobot's own with
    `TERM=dumb` and `NO_COLOR=1`: a declared `TERM` is `dumb` either way."""
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("AB_OS", "os")
    timeline.stop_attach = True
    r = make_runner([], prepare=script, env={"TERM": "none", "NO_COLOR": 0, "AB_OS": "dflt"})
    with pytest.raises(AttachRecorded):
        r.run()
    assert r._env == {"TERM": "dumb", "NO_COLOR": 1, "AB_OS": "os"}


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        (None, "os"),
        pytest.param("#!/usr/bin/env dash\ntrue\n", "dflt", marks=installed("dash")),
        pytest.param("#!/usr/bin/env bash\ntrue\n", "os", marks=installed("bash")),
        pytest.param("#!/usr/bin/env zsh\ntrue\n", "os", marks=installed("zsh")),
    ],
    ids=["no-prepare", "dash", "bash", "zsh"],
)
def test_p5_99_name_that_is_no_shell_name_is_not_in_the_environment_dash_leaves(timeline, prep_tmp, monkeypatch, script, expected: str):
    """SPEC "The environment in templates": a shell may drop the entries of its environment whose names it
    can't have as variables. dash does, so a declared `MY.VAR` that the environment has is found without a
    `prepare` and has its default after one that dash sources; bash and zsh pass such an entry on."""
    for name in ("MY.VAR", "my-var", "1X"):
        monkeypatch.setenv(name, "os")
    timeline.stop_attach = True
    r = make_runner([], prepare=script, env={"MY.VAR": "dflt", "my-var": "dflt", "1X": "dflt"})
    with pytest.raises(AttachRecorded):
        r.run()
    assert r._env == {"MY.VAR": expected, "my-var": expected, "1X": expected}
    assert r.render("{{ env['MY.VAR'] }}") == expected


VALUES = {
    "line-breaks": "one\ntwo\n\nthree\n",
    "equals": "a=b==c=",
    "quotes": "it's \"q\" $HOME `x` \\ ",
    "spaces": "  lead and trail  ",
    "control": "tab\tcr\rbell\x07",
    "template": "{{ 1/0 }} {% endfor %} {#",
    "unicode": "café ☃",
    "entry": "x\nAB_B=evil\n",
    "long": "0123456789abcdef" * 4096,
}


@pytest.mark.parametrize("value", VALUES.values(), ids=VALUES.keys())
@pytest.mark.parametrize("shebang", EACH_SHELL)
def test_p5_100_value_is_taken_exactly(prepared, value: str, shebang: str):
    """SPEC "What `prepare` sets": a value is taken as the shell has it: line breaks, `=`, quotes, spaces at
    its ends; nothing in it is read as another variable, and nothing is rendered."""
    r = prepared(f"{shebang}AB_A={shlex.quote(value)}\n", env={"AB_A": None, "AB_B": "dflt"})
    assert r._env == {"AB_A": value, "AB_B": "dflt"}


def test_p5_102_text_from_prepare_that_is_not_of_the_type_names_the_environment(timeline: Timeline, tmp_path: Path):
    """SPEC "The environment in templates" (Types): the error names the variable, the type and the
    environment, never the value, in the traceback either; `--env` for the same variable comes first, and is
    the one read."""
    for default, kind in ((True, "a boolean"), (3, "an integer"), (0.5, "a number")):
        with pytest.raises(ValueError) as ei:
            make_runner([], prepare=f"AB_A={SECRET}\n", env={"AB_A": default}).run()
        assert str(ei.value) == f"env.AB_A: the value from the environment is not {kind}"
        assert ei.value.__cause__ is None and ei.value.__suppress_context__
    assert "attach" not in timeline.names()
    doc = make_doc([], prepare=f"AB_A={SECRET}\n", env={"AB_A": 3}, spawn="true")
    res = run_cli(doc, tmp_path, "--traceback")
    assert res.returncode == 3 and SECRET not in res.stderr
    assert res.stderr.splitlines()[-1] == f"Run failed in {tmp_path / 'script.autobot.yaml'}: env.AB_A: the value from the environment is not an integer"
    timeline.stop_attach = True
    r = make_runner([], prepare=f"AB_A={SECRET}\n", env={"AB_A": 3}, given={"AB_A": "4"})
    with pytest.raises(AttachRecorded):
        r.run()
    assert r._env == {"AB_A": 4}


def test_p5_102_required_variable_that_prepare_sets_is_satisfied(prepared):
    """SPEC "The environment in templates" (Required): `prepare` gives a required variable its value."""
    assert prepared("AB_A='from prepare'\n", env={"AB_A": None})._env == {"AB_A": "from prepare"}


def test_p5_102_required_variable_nothing_gives_fails_after_prepare(tmp_path: Path):
    """SPEC "The order of a run", "CLI": `env` is resolved once `prepare` has run, so a required variable
    without a value is a failed run, status 3, and `prepare` has run; nothing is spawned."""
    marker, spawned = tmp_path / "prepared", tmp_path / "spawned"
    doc = make_doc([], prepare=f"touch {marker}\nAB_A=a\n", env={"AB_A": None, "AB_B": None}, spawn=f"touch {spawned}")
    res = run_cli(doc, tmp_path)
    assert res.returncode == 3 and res.stdout == ""
    assert res.stderr.splitlines()[-1] == (
        f"Run failed in {tmp_path / 'script.autobot.yaml'}: env.AB_B is required: give it with --env AB_B=VALUE "
        "or in the environment"
    )
    assert marker.exists() and not spawned.exists()


def test_p5_103_prepare_sees_the_env_options_and_not_the_defaults(prepared, monkeypatch, tmp_path: Path):
    """SPEC "prepare": each `--env KEY=VALUE` is a variable of the script's environment, with the text as
    given, over the environment's; a default is not in it, and neither is anything rendered. The `--env`
    value stays the variable's, whatever the script does with it."""
    monkeypatch.setenv("AB_OS", "os")
    monkeypatch.setenv("AB_B", "os-b")
    seen = tmp_path / "seen"
    r = prepared(
        f'echo "[${{AB_A-unset}}][$AB_B][$AB_C][${{AB_D-unset}}][$AB_OS][$TERM]" > {seen}\nAB_D="$AB_C!"\nAB_B=prep\nunset AB_C\n',
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


def test_p5_103_process_left_running_does_not_hold_the_run(prepared, tmp_path: Path):
    """SPEC "What `prepare` sets": the environment is read when the shell exits, so a process the script
    leaves running doesn't hold up the run, and what that process writes later, to its stdout or to the
    descriptor of the environment, isn't read."""
    pid, late = tmp_path / "pid", tmp_path / "late"
    began = time.monotonic()
    try:
        r = prepared(
            f"bash -c 'sleep 1; echo AB_B=late; printf \"AB_B=late\\0\" >&{dump_fd()}; touch {late}; exec sleep 60' &\necho $! > {pid}\nAB_A=a\n",
            env={"AB_A": None, "AB_B": "dflt"},
        )
        assert time.monotonic() - began < 5 and r._env == {"AB_A": "a", "AB_B": "dflt"}
        while not late.exists():  # the process had the shell's descriptors, and wrote to them: it is the sleeper by then
            assert time.monotonic() - began < 15
            time.sleep(0.05)
    finally:
        os.kill(int(pid.read_text()), signal.SIGKILL)


UNREADABLE = {
    "exec": "AB_A=set\nexec true\n",
    "own-exit-trap": "AB_A=set\ntrap 'echo bye' EXIT\n",
    "own-exit-trap-and-exit": "AB_A=set\ntrap : EXIT\nexit 0\n",
    "no-env-on-path": "AB_A=set\nPATH=/nonexistent\n",
    "no-env-on-path-set-e": "#!/bin/sh -eu\nAB_A=set\nPATH=/nonexistent\n",
    "env-function": "AB_A=set\nenv() { :; }\n",
    "env-without-0": "AB_A=set\nmkdir @TMP@/bin\nprintf '#!/bin/sh\\n[ $# = 0 ] && echo AB_A=line\\n' > @TMP@/bin/env\nchmod +x @TMP@/bin/env\nPATH=@TMP@/bin:$PATH\n",
    "no-exec": "#!/bin/sh -n\nAB_A=set\n",
    "descriptor-closed": "#!/bin/bash\nAB_A=set\nexec @FD@>&-\n",
}


@pytest.mark.parametrize("script", UNREADABLE.values(), ids=UNREADABLE.keys())
def test_p5_104_environment_that_cannot_be_read_sets_nothing_and_is_a_warning(prepared, monkeypatch, capfd, tmp_path: Path, script: str):
    """SPEC "What `prepare` sets": a script that replaces the shell, sets its own `EXIT` trap, leaves no
    `env` on `PATH` or one that isn't the command (a function, a program without `-0`), isn't executed (`-n`)
    or closes the descriptor of the environment exits 0 and leaves nothing to read: the run goes on, one line
    says so, and the declared variables are looked up as without a `prepare`."""
    monkeypatch.setenv("AB_B", "os")
    script = script.replace("@TMP@", str(tmp_path)).replace("@FD@", str(dump_fd()))
    r = prepared(script, env={"AB_A": "dflt", "AB_B": "dflt", "AB_C": None}, given={"AB_C": "cli"})
    assert r._env == {"AB_A": "dflt", "AB_B": "os", "AB_C": "cli"}
    out, err = capfd.readouterr()
    assert out == "" and "\0" not in err
    assert progress(err) == [">> prepare: running local script" if script.startswith("#!") else RUNNING, UNREAD, DONE]


def test_p5_104_required_variable_is_not_set_by_an_environment_that_cannot_be_read(tmp_path: Path):
    """SPEC "What `prepare` sets", "CLI": the warning, and then the error of the variable: a failed run."""
    doc = make_doc([], prepare="AB_A=set\nexec true\n", env={"AB_A": None}, spawn=f"touch {tmp_path / 'spawned'}")
    res = run_cli(doc, tmp_path)
    assert res.returncode == 3 and res.stdout == ""
    assert res.stderr.splitlines()[-4:] == [
        RUNNING, UNREAD, DONE,
        f"Run failed in {tmp_path / 'script.autobot.yaml'}: env.AB_A is required: give it with --env AB_A=VALUE or in the environment",
    ]
    assert not (tmp_path / "spawned").exists()


# -- P5-105: where the environment is written ---------------------------------------

OWN_DESCRIPTORS = {
    "lock-file": 'exec 9>"@D@/prepare.lock"\nflock -n 9 || exit 1\nAB_A=S3CRET-v4lue\n',
    "appended-log": 'exec 9>>"@D@/log" 3>"@D@/three"\nAB_A=S3CRET-v4lue\n',
    "descriptor-to-stderr": "exec 9>&2\nAB_A=S3CRET-v4lue\n",
    "exit-in-function-with-descriptor": "f() { AB_A=S3CRET-v4lue; exit 0; }\nf 9>&2\n",
    "exit-in-function-with-stdout": "f() { AB_A=S3CRET-v4lue; exit 0; }\nf >&2\n",
    "exit-in-group": "{ AB_A=S3CRET-v4lue; exit 0; } 9>&2\n",
}


@pytest.mark.skipif(not shutil.which("flock"), reason="flock is not installed")
@pytest.mark.parametrize("shebang", EACH_SHELL)
@pytest.mark.parametrize("script", OWN_DESCRIPTORS.values(), ids=OWN_DESCRIPTORS.keys())
def test_p5_105_descriptors_of_the_script_get_nothing_of_the_environment(sourced, tmp_path: Path, script: str, shebang: str):
    """SPEC "What `prepare` sets" (No value is shown): the environment is written to the file Autobot reads
    and nowhere else, whatever the script does with its own descriptors (a lock file on 9, a log, its
    stderr) and with whatever redirection the command that ends it runs: the variables are read, and
    nothing of the environment is in a file of the script's or on stderr."""
    d = tmp_path / "own"
    d.mkdir()
    env, err = sourced(shebang + script.replace("@D@", str(d)))
    assert env.get("AB_A") == SECRET and env.get("AB_OS") == f"{SECRET}-os"
    assert [f.name for f in d.iterdir() if f.read_bytes()] == []
    assert SECRET not in err and "\0" not in err
    assert progress(err) == [">> prepare: running local script" if shebang else RUNNING, DONE]


@installed("bash")
def test_p5_105_what_a_startup_file_prints_is_shown_and_every_variable_is_read(sourced, tmp_path: Path):
    """SPEC "prepare" (Its environment): the shell's standard output is Autobot's standard error from its
    start, so what a `BASH_ENV` file prints before the script is shown, and is no part of what is read."""
    rc = tmp_path / "bash_env"
    rc.write_text("echo from-the-startup-file\n")
    env, err = sourced(f"#!{shutil.which('bash')}\nAB_A=a\n", BASH_ENV=str(rc))
    assert "from-the-startup-file\n" in err and SECRET not in err and "\0" not in err
    assert [env.get(name) for name in ("AB_A", "AB_OS", "BASH_ENV", "HOME")] == ["a", f"{SECRET}-os", str(rc), str(tmp_path)]
    assert env.get("PATH") == os.environ["PATH"] and "" not in env


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="needs /proc/<pid>/fd")
@pytest.mark.parametrize("shebang", EACH_SHELL)
def test_p5_105_file_of_the_environment_is_empty_once_it_is_read(sourced, tmp_path: Path, shebang: str):
    """SPEC "What `prepare` sets" (No value is shown): a process the script leaves running has the
    descriptor of the environment, and nothing else of Autobot's; the file is empty when `prepare` is done,
    with the variables read, one assigned after the process started among them."""
    pid = tmp_path / "pid"
    try:
        env, err = sourced(f"{shebang}sleep 60 > /dev/null 2>&1 < /dev/null &\necho $! > {pid}\nAB_A={SECRET}\n")
        assert env.get("AB_A") == SECRET and env.get("AB_OS") == f"{SECRET}-os"
        fds = f"/proc/{int(pid.read_text())}/fd"
        held = [fd for fd in os.listdir(fds) if int(fd) > 2]
        assert len(held) == 1 and int(held[0]) >= prepare._FD
        assert os.stat(f"{fds}/{held[0]}").st_size == 0
    finally:
        os.kill(int(pid.read_text()), signal.SIGKILL)
