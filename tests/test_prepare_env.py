"""P5-65..81: `attach.prepare` as an rc script and the spawned process's environment (SPEC "attach")."""

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

import pytest
import yaml
from conftest import BASH, PS1, AttachRecorded, Timeline, make_doc, make_runner, run_cli, run_vars

from autobot import prepare
from autobot.runner import Runner

NAMES = ("AB_A", "AB_B", "AB_C", "AB_OS", "AB_GONE", "AB_NEW", "AB_HOST", "AB_URL", "AB_SHELL")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in NAMES:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def prep_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Temp dir for the files of `prepare`, so a leftover is seen."""
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


def progress(err: str) -> list[str]:
    """Autobot's lines up to the spawn."""
    return [line for line in err.splitlines() if line.startswith(">> ") and not line.startswith(">> attach: ")]


# -- P5-65: precedence --------------------------------------------------------


def test_p5_65_prepare_beats_the_environment_and_the_default(prepared, monkeypatch: pytest.MonkeyPatch):
    """SPEC "The environment in templates": prepare > the environment Autobot was started with > a default."""
    monkeypatch.setenv("AB_OS", "os")
    monkeypatch.setenv("AB_B", "os-b")
    r = prepared(
        "export AB_OS=prep AB_A=prep-a AB_NEW=new\n",
        env={"AB_A": "dflt-a", "AB_B": "dflt-b", "AB_C": "dflt-c", "AB_OS": "dflt-os"},
    )
    assert r.render("{{ env.AB_OS }} {{ env.AB_A }} {{ env.AB_B }} {{ env.AB_C }} {{ env.AB_NEW }}") == (
        "prep prep-a os-b dflt-c new"
    )
    assert r.render("{{ env.keys() | list | first }} {{ 'y' if 'AB_NEW' in env else 'n' }}") == "AB_A y"


def test_p5_65_variable_unset_by_prepare(prepared, monkeypatch: pytest.MonkeyPatch):
    """SPEC "attach": a variable the script unsets is unset: its default applies, or it is undefined."""
    monkeypatch.setenv("AB_GONE", "os")
    monkeypatch.setenv("AB_OS", "os")
    r = prepared("unset AB_GONE AB_OS\n", env={"AB_OS": "dflt"})
    assert r.render("{{ env.AB_OS }} {{ env.AB_GONE | default('undefined') }}") == "dflt undefined"
    with pytest.raises(ValueError, match="^template error: env has no key 'AB_GONE'$"):
        r.render("{{ env.AB_GONE }}")


def test_p5_65_prepare_template_sees_the_environment_before_it(prepared, monkeypatch, tmp_path: Path):
    """SPEC "attach": `prepare` is rendered before it runs; a default it read is rendered again afterwards."""
    monkeypatch.setenv("AB_HOST", "before")
    out = tmp_path / "out"
    r = prepared(
        f"echo '{{{{ env.AB_HOST }}}} {{{{ env.AB_URL }}}} {{{{ env.AB_A }}}}' > {out}\nexport AB_HOST=after AB_A=prep\n",
        env={"AB_URL": "http://{{ env.AB_HOST }}/", "AB_A": "dflt"},
    )
    assert out.read_text() == "before http://before/ dflt\n"
    assert r.render("{{ env.AB_HOST }} {{ env.AB_URL }} {{ env.AB_A }}") == "after http://after/ prep"


# -- P5-66: values ------------------------------------------------------------

VALUES = {
    "newlines": "line1\nline2\n\nline4\n",
    "equals": "a=b==c=",
    "quotes": "it's \"q\" `x` $HOME \\ \\n",
    "spaces": "  two  words\t",
    "unicode": "é ✓ 日本 \U0001f600",
    "empty": "",
    "dump-like": "x\x01AB_B=injected",
}


@pytest.mark.parametrize("value", VALUES.values(), ids=VALUES.keys())
def test_p5_66_value_is_read_back_exactly(prepared, tmp_path: Path, value: str):
    """SPEC "attach": a value is read back byte for byte, whatever it holds."""
    data = tmp_path / "value"
    data.write_bytes(value.encode())
    # $(cat) drops trailing newlines: the x keeps them
    r = prepared(f'v=$(cat {data}; echo x)\nexport AB_A="${{v%x}}"\nexport AB_C=after\n')
    assert r._env["AB_A"] == value
    assert r._env["AB_C"] == "after"
    assert "AB_B" not in r._env


def test_p5_66_non_utf8_bytes_are_kept(prepared, monkeypatch: pytest.MonkeyPatch):
    """SPEC "The environment in templates": bytes that aren't UTF-8 are lone surrogates, as in `os.environ`."""
    monkeypatch.setenv("AB_OS", "caf\udce9")
    r = prepared("export AB_A=$(printf 'a\\377\\376b')\nexport AB_B=\"$AB_OS!\"\n")
    assert r._env["AB_A"] == "a\udcff\udcfeb"
    assert os.fsencode(r._env["AB_A"]) == b"a\xff\xfeb"
    assert r._env["AB_B"] == "caf\udce9!"
    assert r._env["AB_OS"] == "caf\udce9"


def test_p5_66_non_utf8_value_in_a_command_is_sent_with_replacement():
    """SPEC "The environment in templates": a byte that isn't UTF-8 goes to the session as `?`."""
    from conftest import run_vars

    out = run_vars(
        [{"cmd": "echo R=[{{ env.AB_A }}]", "register": "tmpl"}, {"cmd": 'printf %s "$AB_A" | od -An -tx1', "register": "raw"}],
        prepare="export AB_A=$(printf 'caf\\351')\n",
    )
    # the echo of the line isn't recognised (it has `?` where the sent text has the byte) and stays in front
    assert out["tmpl"].splitlines()[-1] == "R=[caf?]"
    assert out["raw"].split() == ["63", "61", "66", "e9"]


# -- P5-67: exit status -------------------------------------------------------

FAILING = {
    "exit": "export AB_A=1\nexit 3\n",
    "exit-in-function": "f() { exit 3; }\nexport AB_A=1\nf\n",
    "last-command": "export AB_A=1\n(exit 3)\n",
    "set-e": "set -e\nexport AB_A=1\n(exit 3)\nexport AB_B=2\n",
    "return": "export AB_A=1\nreturn 3\n",
    "bash-shebang": "#!/bin/bash\nexport AB_A=1\nexit 3\n",
    "sh-e-shebang": "#!/bin/sh -e\nexport AB_A=1\n(exit 3)\ntrue\n",
}


@pytest.mark.parametrize("script", FAILING.values(), ids=FAILING.keys())
def test_p5_67_failing_script_sets_nothing(timeline: Timeline, prep_tmp: Path, capsys, script: str):
    """SPEC "attach": the exit status is the script's, also from `exit N` in the sourced script; nothing is set."""
    r = make_runner([], prepare=script, env={"AB_A": "dflt"})
    with pytest.raises(RuntimeError, match="^prepare script failed with exit code 3$"):
        r.run()
    assert r.render("{{ env.AB_A }}") == "dflt"
    assert "AB_A" not in r._environ and "AB_B" not in r._environ
    assert "attach" not in timeline.names()
    assert list(prep_tmp.iterdir()) == []
    assert progress(capsys.readouterr().err) == [">> prepare: running local script" + (
        "" if script.startswith("#!") else " (no shebang, using /bin/sh)"
    )]


SUCCEEDING = {
    "exit-0": "export AB_A=1\nexit 0\nexport AB_B=2\n",
    "exit": "export AB_A=1\ntrue\nexit\n",
    "return": "export AB_A=1\nreturn 0\nexport AB_B=2\n",
    "own-exit-trap": "trap 'true' EXIT\nexport AB_A=1\n",
    "allexport": "set -a\nAB_A=1\nset -eu\n",
    "positional-parameters": "export AB_A=1\nset -- a b c\nshift\n",
    "background-process": "sleep 3 &\nexport AB_A=1\n",
    "path-and-python-home": "export PATH=/nonexistent PYTHONHOME=/nonexistent PYTHONPATH=/nonexistent AB_A=1\n",
}


@pytest.mark.parametrize("script", SUCCEEDING.values(), ids=SUCCEEDING.keys())
def test_p5_67_script_that_ends_early_still_sets_its_variables(prepared, script: str):
    """SPEC "attach": `exit 0`, `return`, the script's own EXIT trap, `set -eu`, a child left running, a new PATH."""
    r = prepared(script)
    assert r.render("{{ env.AB_A }} {{ env.AB_B | default('-') }}") == "1 -"


@pytest.mark.parametrize(
    "script",
    ["trap 'true' EXIT\nexport AB_A=1\nexit 0\n", "export AB_A=1\nexec true\n"],
    ids=["own-trap-and-exit", "exec"],
)
def test_p5_67_script_that_ends_its_shell_unseen_sets_nothing(prepared, capsys, script: str):
    """SPEC "attach": a shell that ends before it can report its environment: a warning, and the run goes on."""
    r = prepared(script)
    assert "AB_A" not in r._env
    assert progress(capsys.readouterr().err)[:3] == [
        ">> prepare: running local script (no shebang, using /bin/sh)",
        ">> prepare: environment not read: the script ended its shell before the shell could report it",
        ">> prepare: done",
    ]


# -- P5-68: output ------------------------------------------------------------


def test_p5_68_script_output_is_shown_and_no_value_is_printed(prepared, capfd, monkeypatch: pytest.MonkeyPatch):
    """SPEC "Output": the script's stdout and stderr are its own; Autobot prints counts, never names or values."""
    monkeypatch.setenv("AB_GONE", "S3CRET-os")
    r = prepared("echo to-stdout\necho to-stderr >&2\nexport AB_A=S3CRET-a AB_B=S3CRET-b\nunset AB_GONE\n")
    out, err = capfd.readouterr()
    assert out == "to-stdout\n"
    assert "to-stderr\n" in err
    assert progress(err) == [
        ">> prepare: running local script (no shebang, using /bin/sh)",
        ">> prepare: environment: 2 set, 1 unset",
        ">> prepare: done",
    ]
    assert "S3CRET" not in out + err and "AB_A" not in out + err
    assert r._env["AB_A"] == "S3CRET-a"


def test_p5_68_script_that_changes_nothing_prints_no_count(prepared, capfd):
    """SPEC "Output": no environment line when the script set and unset nothing."""
    prepared("x=1\ntrue\n")
    assert progress(capfd.readouterr().err) == [
        ">> prepare: running local script (no shebang, using /bin/sh)",
        ">> prepare: done",
    ]


@pytest.mark.parametrize(
    ("kw", "message"),
    [
        ({"prepare": "export AB_A=S3CRET\n", "spawn": "{{ env.AB_A }}{{ env.AB_NOPE }}"}, "template error: env has no key 'AB_NOPE'"),
        ({"prepare": "export AB_A=S3CRET\nexit 4\n"}, "prepare script failed with exit code 4"),
        ({"prepare": "export AB_A=S3CRET\n", "env": {"AB_B": "{{ env.AB_A }}{{ env.AB_NOPE }}"}}, "env.AB_B: template error: env has no key 'AB_NOPE'"),
    ],
    ids=["spawn-undefined", "failed", "default-undefined"],
)
def test_p5_68_cli_reports_no_value(tmp_path: Path, kw: dict, message: str):
    """SPEC "CLI": a failure after `prepare` is a failed run, and its report holds no value of the environment."""
    res = run_cli(make_doc([], **kw), tmp_path, "--traceback")
    assert res.returncode == 3
    assert res.stderr.splitlines()[-1] == f"Run failed in {tmp_path / 'script.autobot.yaml'}: {message}"
    assert "S3CRET" not in res.stdout + res.stderr


# -- P5-69: shells ------------------------------------------------------------


@pytest.mark.parametrize(
    ("shebang", "shell"),
    [
        ("#!/bin/sh", (["/bin/sh"], "")),
        ("#!/bin/bash", (["/bin/bash"], "")),
        ("#! /usr/bin/zsh ", (["/usr/bin/zsh"], "")),
        ("#!/usr/local/bin/dash", (["/usr/local/bin/dash"], "")),
        ("#!/bin/ksh", (["/bin/ksh"], "")),
        ("#!/bin/bash -eu", (["/bin/bash"], "eu")),
        ("#!/bin/sh -x", (["/bin/sh"], "x")),
        ("#!/bin/sh -", (["/bin/sh"], "")),
        ("#!/bin/bash --", (["/bin/bash"], "")),
        ("#!/usr/bin/env bash", (["/usr/bin/env", "bash"], "")),
        ("#!/usr/bin/env -S bash -eu", (["/usr/bin/env", "bash"], "eu")),
        ("#!/usr/bin/env -S bash -e -u --", (["/usr/bin/env", "bash"], "eu")),
        ("#!/usr/bin/env -S /bin/zsh", (["/usr/bin/env", "/bin/zsh"], "")),
        ("#!/bin/bash -e -u", None),
        ("#!/bin/bash -r", None),
        ("#!/bin/bash --posix", None),
        ("#!/bin/sh -n", None),
        ("#!/bin/sh -eo pipefail", None),
        ("#!/bin/bash -c", None),
        ("#!/bin/sh -s", None),
        ("#!/bin/bash +e", None),
        ("#!/usr/bin/env -S bash -l", None),
        ("#!/usr/bin/env python3", None),
        ("#!/usr/bin/env bash -e", None),
        ("#!/usr/bin/env -S python3 -u", None),
        ("#!/usr/bin/env", None),
        ("#!/usr/bin/python3", None),
        ("#!/bin/bash5", None),
        ("#!/usr/bin/fish", None),
        ("#!/usr/bin/perl -w", None),
        ("#!", None),
    ],
)
def test_p5_69_shell_of_a_shebang(shebang: str, shell: tuple[list[str], str] | None):
    """SPEC "attach": sh, bash, dash, ksh and zsh are shells, directly or through `env`, with `set` options
    or the end-of-options `-` or `--`; anything else isn't sourced."""
    assert prepare.shell_of(shebang) == shell


@pytest.mark.parametrize(
    "shebang", ["#!/bin/sh -", "#!/bin/bash --", "#!/usr/bin/env -S bash --", "#!/usr/bin/env -S sh -eu -"]
)
def test_p5_69_end_of_options_argument_is_sourced(prepared, tmp_path: Path, shebang: str):
    """SPEC "attach": `#!/bin/sh -` runs when executed, and is sourced like `#!/bin/sh`."""
    out = tmp_path / "out"
    r = prepared(f"{shebang}\necho \"$0 $#\" > {out}\nexport AB_A=1\n")
    assert out.read_text().split()[1] == "0" and "_autobot_" in out.read_text()
    assert r._env["AB_A"] == "1"


def test_p5_69_options_of_the_shebang_apply_to_the_script(timeline: Timeline, prep_tmp: Path):
    """SPEC "attach": the shebang's options are the script's: `-e` ends it at the first failure."""
    with pytest.raises(RuntimeError, match="^prepare script failed with exit code 9$"):
        make_runner([], prepare="#!/bin/sh -eu\n(exit 9)\nexit 0\n").run()
    with pytest.raises(RuntimeError, match="^prepare script failed with exit code [12]$"):
        make_runner([], prepare="#!/usr/bin/env -S bash -e -u\necho $AB_NOPE\nexit 0\n").run()


@pytest.mark.parametrize("shebang", ["#!/bin/bash -r", "#!/bin/bash --posix", "#!/bin/sh -ev"])
def test_p5_69_unsupported_argument_runs_the_script_without_reading_it(prepared, tmp_path: Path, capsys, shebang: str):
    """SPEC "attach": a shell with any other argument is executed as a program: it runs, and sets nothing."""
    out = tmp_path / "out"
    r = prepared(f"{shebang}\nexport AB_A=1\ntouch {out}\n", env={"AB_A": "dflt"})  # -r allows no redirection
    assert out.exists()
    assert r.render("{{ env.AB_A }}") == "dflt"
    assert progress(capsys.readouterr().err) == [">> prepare: running local script", ">> prepare: done"]


BASH_ONLY = '{{% raw %}}arr=(x y z)\n[[ ${{#arr[@]}} == 3 ]] && export AB_A="${{arr[*]:1}}"{{% endraw %}}\necho "$0" > {out}\n'


@pytest.mark.parametrize(
    "shebang", ["#!/bin/bash", "#!/usr/bin/env bash", "#!/bin/bash -eu", "#!/usr/bin/env -S bash -eu"]
)
def test_p5_69_bash_script_is_sourced_by_bash(prepared, prep_tmp: Path, tmp_path: Path, capsys, shebang: str):
    """SPEC "attach": a shebang that names a shell picks the shell that sources the script."""
    out = tmp_path / "out"
    r = prepared(f"\n{shebang}\r\n" + BASH_ONLY.format(out=out))
    assert r._env["AB_A"] == "y z"
    assert out.read_text().startswith(f"{prep_tmp}/_autobot_")
    assert progress(capsys.readouterr().err)[0] == ">> prepare: running local script"


@pytest.mark.skipif(not shutil.which("zsh"), reason="zsh not installed")
def test_p5_69_zsh_script_is_sourced_by_zsh(prepared):
    """SPEC "attach": zsh."""
    r = prepared("#!/usr/bin/env zsh\ntypeset -A m\nm[k]=v\nexport AB_A=${(k)m}-$m[k]\nexit 0\n")
    assert r._env["AB_A"] == "k-v"


# -- P5-70: other interpreters ------------------------------------------------


def test_p5_70_other_interpreter_runs_as_a_program_and_sets_nothing(prepared, prep_tmp, tmp_path: Path, capsys):
    """SPEC "attach": a script for any other interpreter is executed as it is; its environment isn't read."""
    out = tmp_path / "out"
    r = prepared(
        f"#!{sys.executable}\nimport os, sys\nos.environ['AB_A'] = '1'\nos.putenv('AB_B', '2')\n"
        f"open({str(out)!r}, 'w').write(sys.argv[0] + ' ' + str(len(os.listdir({str(prep_tmp)!r}))))\n",
        env={"AB_A": "dflt"},
    )
    script, files = out.read_text().split()
    assert script.startswith(f"{prep_tmp}/_autobot_") and files == "1"
    assert r.render("{{ env.AB_A }} {{ env.AB_B | default('-') }}") == "dflt -"
    assert progress(capsys.readouterr().err) == [">> prepare: running local script", ">> prepare: done"]


def test_p5_70_other_interpreter_failure(timeline: Timeline, prep_tmp: Path):
    """SPEC "attach": its exit status aborts the run as before."""
    with pytest.raises(RuntimeError, match="^prepare script failed with exit code 7$"):
        make_runner([], prepare=f"#!{sys.executable}\nraise SystemExit(7)\n").run()
    assert "attach" not in timeline.names()
    assert list(prep_tmp.iterdir()) == []


# -- P5-71: the shell's own variables ------------------------------------------


@pytest.mark.parametrize("shebang", ["", "#!/bin/bash\n"], ids=["sh", "bash"])
def test_p5_71_shell_bookkeeping_is_not_taken(prepared, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, shebang: str):
    """SPEC "attach": only what the script changed is taken, and never `_`, `SHLVL`, `PWD` or `OLDPWD`."""
    monkeypatch.setenv("SHLVL", "7")
    monkeypatch.setenv("PWD", "/before")
    monkeypatch.setenv("OLDPWD", "/older")
    monkeypatch.setenv("_", "/before/cmd")
    monkeypatch.setenv("AB_OS", "os")
    cwd = os.getcwd()
    r = prepared(f"{shebang}cd {tmp_path}\ncd /\n/bin/true\nexport SHLVL=42 PWD=/x OLDPWD=/y _=z AB_OS=os AB_A=1\n")
    assert os.getcwd() == cwd
    assert [r._environ[k] for k in ("SHLVL", "PWD", "OLDPWD", "_")] == ["7", "/before", "/older", "/before/cmd"]
    assert r._environ == {**os.environ, "TERM": "dumb", "NO_COLOR": "1", "AB_A": "1"}


# -- P5-72: temp files and quoting ---------------------------------------------


def test_p5_72_environment_is_never_in_a_named_file(prepared, prep_tmp: Path, tmp_path: Path, monkeypatch):
    """SPEC "attach": the environment is read from a file without a name; the temp dir holds the script only."""
    monkeypatch.setenv("AB_OS", "S3CRET-os")
    out = tmp_path / "out"
    # the needle is split so that the script's own file doesn't hold it
    r = prepared(f'export AB_A=1\nls {prep_tmp} > {out}\ngrep -rl "S3CRET""-os" {prep_tmp} /dev/null >> {out}\ntrue\n')
    (name,) = out.read_text().splitlines()
    assert name.startswith("_autobot_") and name.endswith(".sh")
    assert r._env["AB_A"] == "1" and r._env["AB_OS"] == "S3CRET-os"


def test_p5_72_script_file_removed_when_the_dump_file_fails(timeline, prep_tmp: Path, monkeypatch):
    """SPEC "attach": the temp file is removed whatever fails."""

    def temporary_file(*a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(tempfile, "TemporaryFile", temporary_file)
    with pytest.raises(OSError):
        make_runner([], prepare="true\n").run()
    assert list(prep_tmp.iterdir()) == []
    assert "attach" not in timeline.names()


def test_p5_72_dump_is_not_written_to_a_file_of_the_script(prepared, tmp_path: Path):
    """SPEC "attach": a script that reuses the descriptor's number for its own file gets no dump in it."""
    out = tmp_path / "out"
    reuse = "".join(f"exec {fd}>>{out}\n" for fd in range(prepare.DUMP_FD, prepare.DUMP_FD + 8))
    r = prepared(f"#!/bin/bash\nexport AB_A=S3CRET\n{reuse}")
    assert out.read_bytes() == b""
    assert "AB_A" not in r._env


def test_p5_72_takeover_of_the_descriptor_is_a_failed_read(prepared, tmp_path: Path, capfd):
    """SPEC "attach": the warning for a script that took the descriptor over."""
    reuse = "".join(f"exec {fd}>&-\n" for fd in range(prepare.DUMP_FD, prepare.DUMP_FD + 8))
    prepared(f"#!/bin/bash\nexport AB_A=1\n{reuse}")
    assert ">> prepare: environment not read: it could not be read after the script" in capfd.readouterr().err


def test_p5_72_no_descriptor_is_left_open(prepared):
    """The dump file and its copy for the shell are closed when `prepare` ends."""
    fds = "/proc/self/fd" if os.path.isdir("/proc/self/fd") else "/dev/fd"
    before = sorted(os.listdir(fds))
    prepared("export AB_A=1\n")
    assert sorted(os.listdir(fds)) == before


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT, signal.SIGKILL], ids=lambda s: s.name)
def test_p5_72_signal_while_prepare_runs(tmp_path: Path, sig: signal.Signals):
    """SPEC "attach": SIGTERM and an interrupt remove the script's temp file; SIGKILL leaves it, and nothing
    that holds a value of the environment."""
    tdir, started = tmp_path / "tmp", tmp_path / "started"
    tdir.mkdir()
    path = tmp_path / "script.autobot.yaml"
    path.write_text(yaml.safe_dump(make_doc([], prepare=f"export AB_A=1\ntouch {started}\nexec sleep 20 > /dev/null 2>&1\n")))
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


def test_p5_72_temp_path_is_quoted(timeline: Timeline, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """SPEC "attach": the temp files' paths are never read as shell code."""
    marker = tmp_path / "injected"
    tdir = tmp_path / f"we ird'\"; touch {marker.name}; $(touch {marker.name}) `touch {marker.name}` #"
    tdir.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tdir))
    monkeypatch.chdir(tmp_path)
    timeline.stop_attach = True
    for script in ("export AB_A=1\n", "#!/bin/bash\nexport AB_A=1\nexit 0\n"):
        r = make_runner([], prepare=script)
        with pytest.raises(AttachRecorded):
            r.run()
        assert r._env["AB_A"] == "1"
    assert not marker.exists()
    assert list(tdir.iterdir()) == []


def test_p5_72_truncated_dump_is_not_read():
    """A dump that is cut off, or missing, is no dump: nothing is taken from it."""
    full = b"A=1\0B=x=y\0\0A=2\0C=\0\0"
    assert prepare._changes(full) == prepare.Changes({"A": "2", "C": ""}, frozenset({"B"}))
    assert prepare._changes(b"\0\0") == prepare.Changes({}, frozenset())
    none = {}, frozenset()
    for cut in (b"", b"A=1", b"A=1\0", b"A=1\0B"):
        assert prepare._changes(cut) == prepare.Changes(*none, prepare.NOT_BEFORE)
    for cut in (b"A=1\0\0A=2", b"A=1\0\0A=2\0", b"A=1\0\0A=2\0C", b"\0A"):
        assert prepare._changes(cut) == prepare.Changes(*none, prepare.NOT_AFTER)
    for whole in (b"A=1\0\0", b"\0"):
        assert prepare._changes(whole) == prepare.Changes(*none, prepare.SHELL_ENDED)
        assert prepare._changes(whole, failed=True) == prepare.Changes(*none, prepare.NOT_AFTER)


# -- P5-73: order -------------------------------------------------------------


def test_p5_73_spawn_reads_a_variable_prepare_set(timeline: Timeline, prep_tmp: Path, capsys):
    """SPEC "attach" lifecycle: a `spawn` that reads `env` is rendered after `prepare`."""
    timeline.stop_attach = True
    r = make_runner([], prepare="export AB_HOST=sw1 AB_SHELL=ssh\n", spawn="{{ env.AB_SHELL }} admin@{{ env.AB_HOST }}")
    with pytest.raises(AttachRecorded, match="^ssh admin@sw1$"):
        r.run()
    assert capsys.readouterr().err.splitlines()[-1] == ">> attach: ssh admin@sw1"


@pytest.mark.parametrize("spawn", ["{{ env.AB_SHELL }}", " {{ env.AB_SHELL | default('') }} ", "{% if env.AB_A is defined %}ssh{% endif %}"])
def test_p5_73_spawn_reading_env_that_renders_empty_stops_after_prepare(timeline, tmp_path: Path, spawn: str):
    """SPEC "attach": it is checked once rendered: `prepare` has run, and nothing is spawned."""
    marker = tmp_path / "prepared"
    r = make_runner([], prepare=f"touch {marker}\nexport AB_SHELL=\n", spawn=spawn)
    with pytest.raises(ValueError, match=r"^attach\.spawn rendered to an empty command: "):
        r.run()
    assert marker.exists()
    assert "attach" not in timeline.names()


@pytest.mark.parametrize(
    ("spawn", "message"),
    [
        ("{{ args.cmd }}", r"attach\.spawn rendered to an empty command: '\{\{ args\.cmd \}\}'"),
        ("ssh {{ vars.nope }}", "template error: 'dict object' has no attribute 'nope'"),
        ("ssh {{ env.AB_HOST", "template error: unexpected end of template.*"),
        ("{% if env.X %}", "template error: Unexpected end of template.*"),
    ],
    ids=["empty", "undefined", "syntax", "syntax-block"],
)
def test_p5_73_spawn_that_cannot_depend_on_prepare_stops_before_it(timeline, tmp_path: Path, spawn: str, message: str):
    """SPEC "attach": a `spawn` that doesn't read `env`, or has a syntax error, is checked before `prepare`."""
    marker = tmp_path / "prepared"
    r = make_runner([], prepare=f"touch {marker}\nexport AB_HOST=h\n", spawn=spawn, args={"cmd": ""})
    with pytest.raises(ValueError, match=f"^{message}$"):
        r.run()
    assert not marker.exists()
    assert "attach" not in timeline.names()


# -- P5-74: defaults that wait for prepare --------------------------------------


def test_p5_74_default_that_needs_prepare_is_rendered_after_it(prepared, tmp_path: Path):
    """SPEC "Top-level fields": with `prepare`, a default that reads an unset variable waits for `prepare`."""
    r = prepared(
        "export AB_HOST=sw1\n",
        env={"AB_URL": "http://{{ env.AB_HOST }}/{{ env.AB_C }}", "AB_C": "{{ env.AB_HOST | upper }}", "AB_A": "{{ env.AB_NEW | default('none') }}"},
    )
    assert r.render("{{ env.AB_URL }} {{ env.AB_A }}") == "http://sw1/SW1 none"


def test_p5_74_default_that_needs_prepare_cannot_be_read_by_prepare(timeline: Timeline, tmp_path: Path):
    """SPEC "Top-level fields": until then it is undefined, and the error says why."""
    marker = tmp_path / "prepared"
    r = make_runner([], prepare=f"touch {marker}\necho {{{{ env.AB_URL }}}}\n", env={"AB_URL": "{{ env.AB_C }}", "AB_C": "{{ env.AB_HOST }}"})
    message = "template error: env.AB_URL can't be read before prepare has run: env has no key 'AB_HOST'"
    with pytest.raises(ValueError, match=f"^{message}$"):
        r.run()
    assert not marker.exists()
    assert r.render("{{ env.AB_URL | default('d') }} {{ 'y' if 'AB_URL' in env else 'n' }}") == "d n"


def test_p5_74_default_that_prepare_does_not_satisfy_stops_before_spawn(timeline: Timeline, prep_tmp: Path, tmp_path: Path):
    """SPEC "attach" lifecycle: the defaults are rendered after `prepare`; an error then stops the run."""
    marker = tmp_path / "prepared"
    r = make_runner([], prepare=f"touch {marker}\n", env={"AB_URL": "{{ env.AB_HOST }}"})
    with pytest.raises(ValueError, match="^env.AB_URL: template error: env has no key 'AB_HOST'$"):
        r.run()
    assert marker.exists()
    assert "attach" not in timeline.names()
    assert list(prep_tmp.iterdir()) == []


@pytest.mark.parametrize(
    ("env", "prepare_", "status", "line"),
    [
        ({"AB_URL": "{{ env.AB_HOST }}"}, None, 1, "Script error in {path}: env.AB_URL: template error: env has no key 'AB_HOST'"),
        ({"AB_URL": "{{ env.AB_HOST }}"}, "touch {marker}\n", 3, "Run failed in {path}: env.AB_URL: template error: env has no key 'AB_HOST'"),
        ({"AB_URL": "{{ 1/0 }}{{ env.AB_HOST }}"}, "touch {marker}\n", 1, "Script error in {path}: env.AB_URL: template error: ZeroDivisionError: division by zero"),
        ({"AB_URL": "{{ 1/0 }}"}, "touch {marker}\n", 1, "Script error in {path}: env.AB_URL: template error: ZeroDivisionError: division by zero"),
        ({"AB_URL": "{{ vars.nope }}"}, "touch {marker}\n", 1, "Script error in {path}: env.AB_URL: template error: 'dict object' has no attribute 'nope'"),
        ({"AB_URL": "{{ env.AB_HOST"}, "touch {marker}\n", 1, None),
        ({"AB_A": "{{ env.AB_B }}", "AB_B": "{{ env.AB_A }}"}, "export AB_A=1\ntouch {marker}\n", 1, "Script error in {path}: env cycle: AB_A -> AB_B -> AB_A"),
    ],
    ids=["no-prepare", "prepare-does-not-set-it", "error-then-undefined", "other-error", "undefined-vars", "syntax", "cycle"],
)
def test_p5_74_cli_env_errors_with_and_without_prepare(tmp_path: Path, env, prepare_, status: int, line: str | None):
    """SPEC "CLI": only an unset variable of `env` waits for `prepare`; every other `env` error is a load error."""
    marker = tmp_path / "prepared"
    kw = {"prepare": prepare_.format(marker=marker)} if prepare_ else {}
    res = run_cli(make_doc([], env=env, spawn="true", **kw), tmp_path)
    last = res.stderr.splitlines()[-1]
    assert res.returncode == status, res.stderr
    if line:
        assert last == line.format(path=tmp_path / "script.autobot.yaml")
    else:
        assert last.startswith(f"Script error in {tmp_path / 'script.autobot.yaml'}: env.AB_URL: template error: ")
    assert marker.exists() == (status == 3)

# -- P5-80: the wrapper stays out of the script's way ----------------------------


@pytest.mark.parametrize("end", ["", "exit 0\n"], ids=["end-of-file", "exit-0"])
@pytest.mark.parametrize("shebang", ["", "#!/bin/bash\n"], ids=["sh", "bash"])
def test_p5_80_script_that_changes_ifs_gets_no_error_from_the_wrapper(prepared, capfd, shebang: str, end: str):
    """The wrapper quotes what it expands: `IFS=0` used to make its `[ $? -ne 0 ]` an `Illegal number`."""
    r = prepared(f"{shebang}IFS=0\nexport AB_A=1\n{end}")
    assert r._env["AB_A"] == "1"
    assert [line for line in capfd.readouterr().err.splitlines() if not line.startswith(">> ")] == []


TRACED = ["#!/bin/sh -x\n", "#!/bin/bash -x\n", "#!/usr/bin/env -S bash -eux\n", "set -x\n", "#!/bin/bash\nset -x\n"]


@pytest.mark.parametrize("end", ["", "exit 0\n"], ids=["end-of-file", "exit-0"])
@pytest.mark.parametrize("start", TRACED, ids=["sh-x", "bash-x", "env-bash-eux", "set-x", "bash-set-x"])
def test_p5_80_tracing_shows_the_script_and_not_the_wrapper(prepared, capfd, start: str, end: str):
    """SPEC "prepare as an rc script": with `-x`, the trace is the script's; the dump command isn't in it."""
    r = prepared(f"{start}export AB_A=traced\n{end}")
    assert r._env["AB_A"] == "traced"
    err = capfd.readouterr().err
    assert "AB_A=traced" in err  # the script's own command is traced
    for part in ("-ISc", "fstat", "environ", "printf", "set --", "set +x", prepare._DUMPED):
        assert part not in err.replace("prepare: environment: 1 set", "")
    wrapper = [line for line in err.splitlines() if not line.startswith(">> ") and "AB_A" not in line and "set -x" not in line]
    assert all(line.split()[1:2] in (["."], ["exit"]) for line in wrapper), wrapper


@pytest.mark.skipif(not shutil.which("zsh"), reason="zsh not installed")
def test_p5_80_zsh_tracing(prepared, capfd):
    """The same under zsh, whose trace lines have their own form."""
    r = prepared("#!/usr/bin/env zsh\nset -x\nexport AB_A=traced\nexit 0\n")
    assert r._env["AB_A"] == "traced"
    err = capfd.readouterr().err
    assert "AB_A=traced" in err and "-ISc" not in err and "fstat" not in err


@pytest.mark.parametrize(
    "kw",
    [{"spawn": "ssh {{ env.AB_HOST }}"}, {"env": {"AB_URL": "http://{{ env.AB_HOST }}/"}}],
    ids=["spawn", "default"],
)
def test_p5_80_lost_variable_error_points_at_the_unread_environment(timeline: Timeline, prep_tmp: Path, capfd, kw):
    """SPEC "prepare as an rc script": after the warning, an unset variable's error says the environment wasn't read."""
    r = make_runner([], prepare="export AB_HOST=sw1\nexec true\n", **kw)
    message = (
        "template error: env has no key 'AB_HOST' (the environment prepare left was not read: "
        "the script ended its shell before the shell could report it)"
    )
    with pytest.raises(ValueError) as ei:
        r.run()
    assert str(ei.value) == ("env.AB_URL: " if "env" in kw else "") + message
    assert "attach" not in timeline.names()
    with pytest.raises(ValueError) as ei:
        r.render("{{ env.AB_NOPE }}")
    assert str(ei.value) == message.replace("AB_HOST", "AB_NOPE")
    assert r.render("{{ env.AB_NOPE | default('d') }}") == "d"


def test_p5_80_no_pointer_when_the_environment_was_read(prepared):
    """The pointer is only for a `prepare` whose environment wasn't read."""
    for script in ("export AB_A=1\n", f"#!{sys.executable}\n"):
        r = prepared(script)
        with pytest.raises(ValueError, match="^template error: env has no key 'AB_NOPE'$"):
            r.render("{{ env.AB_NOPE }}")

# -- P5-79: an environment that can't be read ------------------------------------

WARN = ">> prepare: environment not read: "


@pytest.fixture
def python_link(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """`sys.executable` as a link the script can remove, so the dumper stops working when it does."""
    link = tmp_path / "python"
    link.write_text(f'#!/bin/sh\nexec {sys.executable} "$@"\n')
    link.chmod(0o700)
    monkeypatch.setattr(sys, "executable", str(link))
    return link


@pytest.mark.parametrize("end", ["", "exit 0\n", "return\n"], ids=["end-of-file", "exit-0", "return"])
@pytest.mark.parametrize("shebang", ["", "#!/bin/bash\n", "#!/bin/sh -eu\n"], ids=["sh", "bash", "sh-eu"])
def test_p5_79_dump_that_fails_after_the_script_is_not_blamed_on_it(prepared, python_link, capfd, shebang, end):
    """SPEC "prepare as an rc script": the run goes on, nothing is set, and the warning says what failed."""
    r = prepared(f"{shebang}export AB_A=1\nrm {python_link}\n{end}", env={"AB_A": "dflt"})
    assert r.render("{{ env.AB_A }}") == "dflt"
    assert progress(capfd.readouterr().err)[1:] == [WARN + "it could not be read after the script", ">> prepare: done"]


@pytest.mark.skipif(sys.platform != "linux", reason="the size one argument may have is Linux's")
@pytest.mark.parametrize("shebang", ["", "#!/bin/bash\n"], ids=["sh", "bash"])
def test_p5_79_value_too_large_to_start_the_dumper(prepared, capfd, shebang: str):
    """SPEC "prepare as an rc script": with a value over 128 KB exported, the shell can start no command."""
    r = prepared(f"{shebang}export AB_A=1 AB_B=$(head -c 200000 /dev/zero | tr '\\0' x)\n")
    assert "AB_A" not in r._env and "AB_B" not in r._env
    assert progress(capfd.readouterr().err)[1:] == [WARN + "it could not be read after the script", ">> prepare: done"]


@pytest.mark.parametrize("status", [0, 3])
@pytest.mark.parametrize("shebang", ["", "#!/bin/bash\n", "#!/bin/sh -eu\n"], ids=["sh", "bash", "sh-eu"])
def test_p5_79_dump_that_fails_before_the_script_still_runs_it(timeline, prep_tmp, tmp_path, monkeypatch, capfd, shebang, status):
    """SPEC "prepare as an rc script": the script runs without the capture, and its exit status is its own."""
    monkeypatch.setattr(sys, "executable", str(tmp_path / "no-such-python"))
    ran = tmp_path / "ran"
    timeline.stop_attach = True
    r = make_runner([], prepare=f"{shebang}export AB_A=1\ntouch {ran}\n(exit {status})\n")
    if status:
        with pytest.raises(RuntimeError, match="^prepare script failed with exit code 3$"):
            r.run()
    else:
        with pytest.raises(AttachRecorded):
            r.run()
        assert progress(capfd.readouterr().err)[1:] == [
            WARN + "it could not be read before the script, which ran without that",
            ">> prepare: done",
        ]
    assert ran.exists() and "AB_A" not in r._env
    assert list(prep_tmp.iterdir()) == []


def test_p5_79_no_python_interpreter_is_a_warning(prepared, tmp_path: Path, monkeypatch, capfd):
    """SPEC "prepare as an rc script": without `sys.executable` the script is run as before, and a warning says so."""
    monkeypatch.setattr(sys, "executable", "")
    out = tmp_path / "out"
    for shebang in ("", "#!/bin/bash\n"):
        r = prepared(f'{shebang}export AB_A=1\necho "$0" > {out}\n')
        assert "_autobot_" in out.read_text() and "AB_A" not in r._env
        assert progress(capfd.readouterr().err)[1:] == [
            WARN + "Autobot doesn't know the Python interpreter it runs in (sys.executable is empty)",
            ">> prepare: done",
        ]
    prepared(f"#!{shutil.which('true')}\n")  # not a shell: there is nothing to read, and no warning
    assert WARN not in capfd.readouterr().err

# -- P5-78: the dump is the shell's environment, exactly -------------------------

LOCALE = {
    "lang-to-c": ({"LANG": "C.UTF-8"}, "export LANG=C\n", {"LANG": "C"}, set()),
    "lc-ctype-c": ({"LANG": "C.UTF-8"}, "export LC_CTYPE=C\n", {"LC_CTYPE": "C"}, set()),
    "lc-ctype-posix": ({}, "export LC_CTYPE=POSIX\n", {"LC_CTYPE": "POSIX"}, set()),
    "unset-lang": ({"LANG": "C.UTF-8"}, "unset LANG\n", {}, {"LANG"}),
    "c-to-utf8": ({"LANG": "C"}, "export LANG=C.UTF-8\n", {"LANG": "C.UTF-8"}, set()),
    "c-untouched": ({"LANG": "C"}, "export AB_A=1\n", {"AB_A": "1"}, set()),
    "lc-ctype-kept": ({"LC_CTYPE": "POSIX"}, "export LANG=C\n", {"LANG": "C"}, set()),
    "lc-ctype-unset": ({"LC_CTYPE": "C.UTF-8"}, "unset LC_CTYPE\n", {}, {"LC_CTYPE"}),
    "lc-ctype-not-exported": ({}, "LC_CTYPE=C\nLANG=C\nAB_A=1\n", {}, set()),
    "lc-ctype-empty": ({"LANG": "C"}, "export LC_CTYPE=\n", {"LC_CTYPE": ""}, set()),
    "lc-ctype-odd": ({}, "export LC_CTYPE='s. x\n'\n", {"LC_CTYPE": "s. x\n"}, set()),
    "lc-all": ({"LANG": "C"}, "export LC_ALL=C\n", {"LC_ALL": "C"}, set()),
    "lc-all-empty": ({"LC_ALL": "", "LANG": "C"}, "unset LANG\n", {}, {"LANG"}),
}


@pytest.fixture(params=["proc", "environ"])
def dump_source(request, monkeypatch: pytest.MonkeyPatch) -> str:
    """Both ways the dumper reads its environment: `/proc/self/environ`, and `os.environb` where there is none."""
    if request.param == "environ":
        monkeypatch.setattr(prepare, "PROC", None, raising=False)
    elif not os.path.exists("/proc/self/environ"):
        pytest.skip("no /proc/self/environ")
    return request.param


@pytest.mark.parametrize("shebang", ["", "#!/bin/bash\n"], ids=["sh", "bash"])
@pytest.mark.parametrize(("os_env", "script", "set_", "unset"), LOCALE.values(), ids=LOCALE.keys())
def test_p5_78_locale_variables_are_read_exactly(dump_source, prep_tmp, capsys, shebang, os_env, script, set_, unset):
    """SPEC "prepare as an rc script": the helper's own locale handling adds, changes and removes nothing."""
    changes = prepare.run(shebang + script, {"PATH": os.environ["PATH"], **os_env})
    assert (changes.set, changes.unset) == (set_, unset)
    counts = [line for line in capsys.readouterr().err.splitlines() if "environment:" in line]
    assert counts == ([f">> prepare: environment: {len(set_)} set, {len(unset)} unset"] if set_ or unset else [])
    assert list(prep_tmp.iterdir()) == []


def test_p5_78_values_and_shell_bookkeeping_with_either_source(dump_source, prep_tmp):
    """SPEC "prepare as an rc script": the same differences whichever way the environment is read."""
    environ = {"PATH": os.environ["PATH"], "AB_OS": "caf\udce9", "AB_GONE": "g", "LANG": "C"}
    changes = prepare.run("cd /\nexport AB_A=$(printf 'l1\\nl2=\\377')\nunset AB_GONE\nexport AB_OS\nexit 0\n", environ)
    assert (changes.set, changes.unset) == ({"AB_A": "l1\nl2=\udcff"}, {"AB_GONE"})


def test_p5_78_spawned_process_gets_no_invented_locale(spawn_env, monkeypatch: pytest.MonkeyPatch):
    """SPEC "The environment of the spawned process": `export LANG=C` gives the process no `LC_CTYPE`."""
    for name in ("LC_ALL", "LC_CTYPE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LANG", "C.UTF-8")
    env = spawn_env(prepare="export LANG=C\n")
    assert env["LANG"] == "C" and "LC_CTYPE" not in env


@pytest.mark.parametrize("name", ["later", "_later", "why", "raw", "environ", "__dict__"])
def test_p5_74_env_holds_nothing_but_variables(timeline: Timeline, name: str):
    """SPEC "The environment in templates": what Autobot knows about the defaults isn't readable as `env.<name>`."""
    for kw in ({}, {"prepare": "true\n", "env": {"AB_URL": "{{ env.AB_HOST }}"}}):
        r = make_runner([], **kw)
        assert r.render(f"{{{{ env.{name} | default('undefined') }}}} {{{{ 'y' if env.{name} is defined else 'n' }}}}") == "undefined n"
        with pytest.raises(ValueError, match=f"^template error: env has no key '{name}'$"):
            r.render(f"{{{{ env.{name} }}}}")
        assert [a for a in dir(r._env) if not a.startswith("__")] == [a for a in dir({}) if not a.startswith("__")]


# -- P5-75..77: the environment of the spawned process --------------------------


@pytest.fixture
def spawn_env(spawned, prep_tmp: Path):
    """Run a script up to `pexpect.spawn`, which is recorded and not made; returns the env it was given."""
    spawned.stop = True

    def run(**kw) -> dict[str, str]:
        from conftest import SpawnRecorded

        with pytest.raises(SpawnRecorded):
            make_runner([], **kw).run()
        assert list(prep_tmp.iterdir()) == []
        return spawned[-1][1]["env"]

    return run


def test_p5_75_spawned_process_inherits_the_environment_and_prepare(monkeypatch: pytest.MonkeyPatch):
    """SPEC "The environment of the spawned process": checked from inside the spawned shell."""
    monkeypatch.setenv("AB_OS", "from os")
    monkeypatch.setenv("AB_B", "os-b")
    monkeypatch.setenv("AB_GONE", "os")
    show = 'echo "[$AB_OS][$AB_A][$AB_B][${AB_GONE-unset}][${AB_C-unset}][${AB_NEW-unset}][$TERM][$NO_COLOR]"'
    out = run_vars(
        [{"cmd": show, "register": "out"}, {"cmd": "echo \"[{{ env.AB_B }}][{{ env.AB_C }}]\"", "register": "tmpl"}],
        prepare="export AB_A='it'\\''s  \"a\" = $x' AB_B=prep-b\nunset AB_GONE\nAB_NEW=not-exported\n",
        env={"AB_C": "dflt", "AB_A": "dflt"},
    )
    assert out["out"] == "[from os][it's  \"a\" = $x][prep-b][unset][unset][unset][dumb][1]"
    assert out["tmpl"] == "[prep-b][dflt]"  # a default is for templates: AB_C is unset in the process


def test_p5_75_spawned_process_gets_a_multiline_and_a_non_utf8_value(spawn_env):
    """SPEC "prepare as an rc script": what the script exported reaches the process byte for byte."""
    env = spawn_env(prepare="export AB_A='l1\nl2\n' AB_B=$(printf 'a\\377b')\n")
    assert env["AB_A"] == "l1\nl2\n"
    assert os.fsencode(env["AB_B"]) == b"a\xffb"


def test_p5_76_plain_terminal_over_the_inherited_environment(spawn_env, monkeypatch):
    """SPEC "The environment of the spawned process": lines 1 and 2, and nothing else."""
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("AB_OS", "os")
    expected = {**os.environ, "TERM": "dumb", "NO_COLOR": "1"}
    assert spawn_env(env={"AB_C": "template-only"}) == expected
    assert spawn_env(prepare="true\n") == expected
    assert spawn_env(prepare=f"#!{sys.executable}\nimport os\nos.environ['TERM'] = 'x'\n") == expected


@pytest.mark.parametrize(
    ("script", "term", "no_color"),
    [
        ("export TERM=vt100\n", "vt100", "1"),
        ("export TERM=xterm-256color\n", "xterm-256color", "1"),
        ("unset TERM\nexport NO_COLOR=\n", None, ""),
        ("unset NO_COLOR\n", "dumb", None),
        ("export NO_COLOR=0\n", "dumb", "0"),
        ("export TERM=dumb NO_COLOR=1 AB_A=1\n", "dumb", "1"),
        ("export AB_A=1\n", "dumb", "1"),
    ],
    ids=["term-set", "term-set-to-a-terminal's", "term-unset", "no-color-unset", "no-color-set", "set-to-the-defaults", "untouched"],
)
@pytest.mark.parametrize("os_env", [{}, {"TERM": "xterm-256color", "NO_COLOR": "0"}, {"TERM": "dumb", "NO_COLOR": "1"}], ids=["os-unset", "os-set", "os-plain"])
def test_p5_76_prepare_decides_term_and_no_color(spawn_env, monkeypatch, tmp_path, os_env, script: str, term, no_color):
    """SPEC "The environment of the spawned process": `prepare` runs with the plain terminal set, so whatever
    it does to `TERM` or `NO_COLOR` is a change, whatever Autobot was started with."""
    for name in ("TERM", "NO_COLOR"):
        monkeypatch.delenv(name, raising=False)
    for name, value in os_env.items():
        monkeypatch.setenv(name, value)
    seen = tmp_path / "seen"
    env = spawn_env(prepare=f'echo "$TERM/$NO_COLOR/{{{{ env.TERM }}}}/{{{{ env.NO_COLOR }}}}" > {seen}\n' + script)
    assert seen.read_text() == "dumb/1/dumb/1\n"
    assert (env.get("TERM"), env.get("NO_COLOR")) == (term, no_color)


@pytest.mark.parametrize("os_env", [{}, {"TERM": "xterm-256color", "NO_COLOR": "0"}], ids=["os-unset", "os-set"])
def test_p5_76_templates_read_the_environment_the_process_gets(prepared, monkeypatch, os_env):
    """SPEC "The environment in templates": `env.TERM` and `env.NO_COLOR` are the run's, before and after."""
    for name in ("TERM", "NO_COLOR"):
        monkeypatch.delenv(name, raising=False)
    for name, value in os_env.items():
        monkeypatch.setenv(name, value)
    assert make_runner([]).render("{{ env.TERM }}/{{ env.NO_COLOR }}") == "dumb/1"
    r = prepared("export TERM=vt100\nunset NO_COLOR\n", env={"NO_COLOR": "dflt"})
    assert r.render("{{ env.TERM }}/{{ env.NO_COLOR }}") == "vt100/dflt"


def test_p5_76_prepare_is_the_last_layer(spawn_env, monkeypatch: pytest.MonkeyPatch):
    """SPEC "The environment of the spawned process": line 3 over lines 1 and 2, and nothing after it."""
    monkeypatch.setenv("AB_OS", "os")
    monkeypatch.setenv("AB_B", "os")
    monkeypatch.setenv("AB_GONE", "os")
    env = spawn_env(prepare="export TERM=vt100 AB_A=prep AB_B=prep\nunset AB_GONE\n", env={"AB_C": "template-only"})
    expected = {**os.environ, "TERM": "vt100", "NO_COLOR": "1", "AB_A": "prep", "AB_B": "prep"}
    del expected["AB_GONE"]
    assert env == expected


def test_p5_77_spawn_is_looked_up_in_the_path_prepare_set(tmp_path: Path, monkeypatch, capfd):
    """SPEC "The environment of the spawned process": `spawn` is found in the `PATH` of that environment."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    cmd = bindir / "ab-device"
    cmd.write_text('#!/bin/sh\necho "device says $AB_A"\n')
    cmd.chmod(cmd.stat().st_mode | stat.S_IXUSR)
    kw = {"prompts": []}
    make_runner([], spawn="ab-device", prepare=f'export PATH="{bindir}:$PATH" AB_A=hi\n', **kw).run()
    assert "device says hi" in capfd.readouterr().out
    # on the spawn line, `env` is the command that is looked up, and it looks up the rest in the `PATH` it sets
    make_runner([], spawn=f"env PATH={bindir}:/usr/bin:/bin AB_A=there ab-device", **kw).run()
    assert "device says there" in capfd.readouterr().out
    import pexpect

    with pytest.raises(pexpect.ExceptionPexpect, match="The command was not found or was not executable: ab-device"):
        make_runner([], spawn="ab-device", **kw).run()
    with pytest.raises(pexpect.ExceptionPexpect, match="The command was not found or was not executable: sh"):
        make_runner([], spawn="sh", prepare="export PATH=/nonexistent\n", **kw).run()
    # without a `PATH`, the system default path
    make_runner([], spawn="sh -c 'echo no-path'", prepare="unset PATH\n", **kw).run()
    assert "no-path" in capfd.readouterr().out


# -- P5-81: the three layers, and the spawn line, seen from inside the spawned shell --

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
    """SPEC "The environment of the spawned process": lines 1 and 2."""
    assert in_shell() == "from os|_|os|dumb|1"
    # a default of the `env` section is for templates only
    assert in_shell(env={"AB_A": "dflt"}) == "from os|_|os|dumb|1"


def test_p5_81_shell_has_what_prepare_set_and_unset(os_env):
    """SPEC "The environment of the spawned process": line 3 wins, for `TERM` and `NO_COLOR` too."""
    prepare = "export AB_OS=prep AB_A=new TERM=vt100\nunset AB_GONE NO_COLOR\n"
    assert in_shell(prepare=prepare) == "prep|new|_|vt100|_"


def test_p5_81_variable_on_the_spawn_line(os_env):
    """SPEC "The environment of the spawned process": `env X=... cmd` on the `spawn` line sets a variable
    for the command, from a template too; it is the command's own line, so it has the last word."""
    assert in_shell(spawn=f"env AB_A='on the line' AB_OS={{{{ args.v }}}} {BASH}", args={"v": "tmpl"}) == (
        "tmpl|on the line|os|dumb|1"
    )
    out = in_shell(spawn=f"env -u AB_GONE AB_A={{{{ env.AB_A }}}}-line TERM=vt100 {BASH}", prepare="export AB_A=prep\n")
    assert out == "from os|prep-line|_|vt100|1"
    # the way to hand a default of the `env` section to the command
    assert in_shell(spawn=f"env AB_A={{{{ env.AB_A }}}} {BASH}", env={"AB_A": "dflt"}) == "from os|dflt|os|dumb|1"


def test_p5_81_env_i_on_the_spawn_line_gives_a_minimal_environment(os_env):
    """SPEC "The environment of the spawned process": `spawn: env -i ...` inherits nothing."""
    bash = shutil.which("bash")
    out = run_vars(
        [{"cmd": "compgen -e", "register": "names"}, {"cmd": SHOW, "register": "out"}],
        spawn=f"env -i PS1={shlex.quote(PS1)} PATH=/usr/bin:/bin {bash} --norc --noprofile -i",
        prepare="export AB_A=prep\n",
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
        spawn=f'env "AB_A={{{{ args.v }}}}" {BASH}', args={"v": value},
    )
    assert out["out"] == f"<{value}>"


def test_p5_81_entry_without_a_name_is_not_in_the_run_environment(tmp_path: Path):
    """SPEC "The environment of the spawned process": an entry with an empty name, which a program can be
    started with, is left out: the process is spawned, without it."""
    out = tmp_path / "out"
    doc = make_doc([{"cmd": f"env | grep -c '^=' > {out}; echo {{{{ env | length }}}}", "ignore_error": True}])
    path = tmp_path / "script.autobot.yaml"
    path.write_text(yaml.safe_dump(doc))
    env = {**os.environ, "": "nameless"}
    res = subprocess.run(
        [sys.executable, "-m", "autobot.cli", str(path)], check=False, capture_output=True, text=True, env=env, timeout=60
    )
    assert res.returncode == 0 and res.stderr.splitlines()[-1] == ">> run completed"
    assert "Traceback" not in res.stdout + res.stderr and "nameless" not in res.stdout
    assert out.read_text() == "0\n"
