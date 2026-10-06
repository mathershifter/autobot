"""P5-65..76: `attach.prepare` as an rc script (SPEC "attach", "The environment in templates")."""

from __future__ import annotations

import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

import pytest
from conftest import AttachRecorded, Timeline, make_doc, make_runner, run_cli

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
        ({"prepare": "export AB_A=S3CRET\n", "env": {"AB_B": "{{ env.AB_A }}{{ env.AB_NOPE }}"}}, "template error: env has no key 'AB_NOPE'"),
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
        ("#!/bin/sh", ["/bin/sh"]),
        ("#!/bin/bash", ["/bin/bash"]),
        ("#! /usr/bin/zsh ", ["/usr/bin/zsh"]),
        ("#!/usr/local/bin/dash", ["/usr/local/bin/dash"]),
        ("#!/bin/ksh", ["/bin/ksh"]),
        ("#!/bin/bash -eu", ["/bin/bash", "-eu"]),
        ("#!/bin/bash -e -u", ["/bin/bash", "-e -u"]),
        ("#!/usr/bin/env bash", ["/usr/bin/env", "bash"]),
        ("#!/usr/bin/env -S bash -eu", ["/usr/bin/env", "bash", "-eu"]),
        ("#!/usr/bin/env -S /bin/zsh", ["/usr/bin/env", "/bin/zsh"]),
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
def test_p5_69_shell_of_a_shebang(shebang: str, shell: list[str] | None):
    """SPEC "attach": sh, bash, dash, ksh and zsh are shells, directly or through `env`; nothing else is."""
    assert prepare.shell_of(shebang) == shell


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
    assert script.startswith(f"{prep_tmp}/_autobot_") and files == "1"  # no dump file either
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
    assert r._environ == {**os.environ, "AB_A": "1"}


# -- P5-72: temp files and quoting ---------------------------------------------


def test_p5_72_dump_file_is_private_and_removed(prepared, prep_tmp: Path, tmp_path: Path):
    """SPEC "attach": the file the environment is read from is mode 0600 and removed with the script."""
    out = tmp_path / "out"
    prepared(f"ls -l {prep_tmp} > {out}\n")  # `prepared` checks that nothing is left
    lines = [line.split() for line in out.read_text().splitlines() if "_autobot_" in line]
    assert sorted((line[0][:10], line[-1].rsplit(".", 1)[-1]) for line in lines) == [
        ("-rw-------", "env"),
        ("-rwx------", "sh"),
    ]


@pytest.mark.parametrize("fails", ["mkstemp", "open"])
def test_p5_72_files_removed_when_the_dump_fails(timeline, prep_tmp: Path, monkeypatch, fails: str):
    """SPEC "attach": every temp file is removed in every case."""
    if fails == "mkstemp":
        def mkstemp(*a, **k):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(tempfile, "mkstemp", mkstemp)
        script = "true\n"
    else:
        script = f"chmod 000 {prep_tmp}/_autobot_*.env\n"
        if os.geteuid() == 0:
            pytest.skip("root reads a file of mode 000")
    with pytest.raises(OSError):
        make_runner([], prepare=script).run()
    assert list(prep_tmp.iterdir()) == []
    assert "attach" not in timeline.names()


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
    for cut in (b"", b"A=1\0\0", b"A=1\0\0A=2", b"A=1\0\0A=2\0", b"A=1\0\0A=2\0C"):
        assert prepare._changes(cut) is None


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
    with pytest.raises(ValueError, match="^template error: env has no key 'AB_HOST'$"):
        r.run()
    assert marker.exists()
    assert "attach" not in timeline.names()
    assert list(prep_tmp.iterdir()) == []


@pytest.mark.parametrize(
    ("env", "prepare_", "status", "line"),
    [
        ({"AB_URL": "{{ env.AB_HOST }}"}, None, 1, "Script error in {path}: template error: env has no key 'AB_HOST'"),
        ({"AB_URL": "{{ env.AB_HOST }}"}, "touch {marker}\n", 3, "Run failed in {path}: template error: env has no key 'AB_HOST'"),
        ({"AB_URL": "{{ 1/0 }}{{ env.AB_HOST }}"}, "touch {marker}\n", 1, "Script error in {path}: template error: ZeroDivisionError: division by zero"),
        ({"AB_URL": "{{ 1/0 }}"}, "touch {marker}\n", 1, "Script error in {path}: template error: ZeroDivisionError: division by zero"),
        ({"AB_URL": "{{ vars.nope }}"}, "touch {marker}\n", 1, "Script error in {path}: template error: 'dict object' has no attribute 'nope'"),
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
        assert last.startswith(f"Script error in {tmp_path / 'script.autobot.yaml'}: template error: ")
    assert marker.exists() == (status == 3)
