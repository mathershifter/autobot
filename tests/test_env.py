"""P5-18..20, P5-31, P5-61..64, P5-94..97: the `env` section and `--env` (SPEC "Top-level fields",
"The environment in templates", "CLI")."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from conftest import SpawnLog, SpawnRecorded, make_config, make_doc, make_runner, run_cli, run_script

from autobot.runner import Runner

SECRET = "S3CRET-value"
REQUIRED = "^env.AB_A is required: give it with --env AB_A=VALUE or in the environment$"


def report(res) -> list[str]:
    """The CLI's stderr, without the line `python -m` prints about the package's own import."""
    return [line for line in res.stderr.splitlines() if "RuntimeWarning" not in line]


def undeclared(key: str) -> str:
    return f"template error: env has no key '{key}': the script's env section doesn't declare it"


def test_p5_18_spawn_env_is_the_inherited_one_with_a_plain_terminal(spawned: SpawnLog, monkeypatch: pytest.MonkeyPatch):
    """SPEC "The environment of the spawned process": the inherited environment with a plain terminal."""
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)
    spawned.stop = True
    with pytest.raises(SpawnRecorded):
        make_runner([]).run()
    _, kwargs = spawned[0]
    assert kwargs["env"] == {**os.environ, "TERM": "dumb", "NO_COLOR": "1"}


def test_p5_19_value_comes_from_env_option_then_the_environment_then_the_default(monkeypatch: pytest.MonkeyPatch):
    """SPEC "Top-level fields" (`env`): `--env` before the environment before the default."""
    monkeypatch.setenv("AB_X", "os")
    monkeypatch.setenv("AB_Y", "os")
    r = make_runner([], env={"AB_X": "yaml", "AB_Y": "yaml", "AB_A": "yaml", "AB_B": "yaml"}, given={"AB_X": "cli", "AB_A": "cli"})
    assert r.render("{{ env.AB_X }} {{ env.AB_Y }} {{ env.AB_A }} {{ env.AB_B }}") == "cli os cli yaml"


LITERALS = [
    "{{ env.AB_A }}-b", "{{ vars.v }}", "{{ env.AB_NOPE }}", "{{ nope }}", "{{ 1/0 }}", "{{", "{% if %}", "{% raw %}{{ x }}{% endraw %}",
    "{# never closed", "${#arr[@]}", "{{ env.AB_B }}",
]


@pytest.mark.parametrize("default", LITERALS)
def test_p5_20_string_default_is_a_literal(default: str):
    """SPEC "Top-level fields" (`env`), "The environment in templates" (No value is a template): a string
    default is the text as written: it reads no key and no `vars`, and what would be a template syntax
    error, an undefined name or a failing expression is plain text; a template that writes it out doesn't
    render it either."""
    Runner(make_config([], env={"AB_A": "a", "AB_B": default}, vars={"v": "V"}), {})  # no default is checked
    r = make_runner([], env={"AB_A": "a", "AB_B": default}, vars={"v": "V"})
    assert r._env == {"AB_A": "a", "AB_B": default}
    assert r.render("[{{ env.AB_B }}]") == f"[{default}]"


@pytest.mark.parametrize("source", ["environment", "option"])
def test_p5_31_given_value_is_verbatim(monkeypatch: pytest.MonkeyPatch, source: str):
    """SPEC "The environment in templates" (No value is a template): a value of the environment or of `--env`
    is never rendered."""
    value = "p{{w}}d{% x %}{{ env.AB_B }}"
    given = {"AB_A": value} if source == "option" else {}
    if source == "environment":
        monkeypatch.setenv("AB_A", value)
    r = make_runner([], env={"AB_A": "default", "AB_B": "b"}, given=given)
    assert r.render("{{ env.AB_A }}|{{ env.AB_B }}") == f"{value}|b"


@pytest.fixture
def environ(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """The whole environment the runner reads."""

    class Environ(dict):
        def pop(self, key: str, *default: str) -> str | None:  # pytest removes its PYTEST_CURRENT_TEST
            return super().pop(key, *(default or (None,)))

    fake = Environ({"AB_OS": "o", "AB_Z": "z z", "AB_B": "os-b", "AB_EMPTY": "", "HOME": "/home/ab", "TERM": "xterm"})
    monkeypatch.setattr(os, "environ", fake)
    return fake


@pytest.mark.parametrize("kw", [{}, {"env": {}}, {"env": {"AB_Y": "y"}}], ids=["no-section", "empty", "another-key"])
def test_p5_61_variable_the_section_does_not_declare_is_not_in_env(environ: dict[str, str], kw: dict):
    """SPEC "The environment in templates": `env` holds the declared keys and nothing else of the environment."""
    r = make_runner([], **kw)
    for name in ("AB_OS", "HOME", "TERM", "NO_COLOR"):
        with pytest.raises(ValueError) as ei:
            r.render(f"{{{{ env.{name} }}}}")
        assert str(ei.value) == undeclared(name)
    assert r.render("{{ env.get('AB_OS', 'd') }} {{ 'y' if 'HOME' in env else 'n' }} {{ 'y' if env.AB_Z is defined else 'n' }}") == "d n n"
    assert r.render("{{ env | length }}") == str(len(kw.get("env", {})))


def test_p5_61_declared_key_reads_the_environment(environ: dict[str, str]):
    """SPEC "Top-level fields" (`env`): a declared name takes whatever the environment has under it: the
    environment of the run, Autobot's own with `TERM=dumb`, the one a `prepare` is given."""
    r = make_runner([], env={"HOME": None, "TERM": "none", "AB_OS": "dflt"})
    assert r.render("{{ env.HOME }} {{ env.TERM }} http://{{ env.AB_OS }}{{ env.HOME }}") == "/home/ab dumb http://o/home/ab"


def test_p5_62_variable_that_is_set_empty_is_set(environ: dict[str, str]):
    """SPEC "Top-level fields" (`env`): an empty value is a value, from the environment and from `--env`: the
    default isn't used, and a required variable has what it needs. The environment is read once."""
    env = {"AB_B": "dflt", "AB_EMPTY": "dflt", "AB_Y": "dflt", "AB_A": None, "AB_C": "dflt"}
    r = make_runner([], env=env, given={"AB_A": "", "AB_C": ""})
    assert r.render("[{{ env.AB_B }}][{{ env.AB_EMPTY }}][{{ env.AB_Y }}][{{ env.AB_A }}][{{ env.AB_C }}]") == "[os-b][][dflt][][]"
    environ["AB_Y"] = "late"
    assert r.render("{{ env.AB_Y }}") == "dflt"


@pytest.mark.parametrize("template", ["{{ env.AB_NOPE }}", "{{ env['AB_NOPE'] }}", "x{{ env.AB_NOPE.y }}", "{{ env.AB_NOPE | upper }}"])
def test_p5_63_undeclared_key_is_undefined(template: str):
    """SPEC "The environment in templates": a key the section doesn't declare is an undefined variable, and
    the message says that it isn't declared; `default` and the like deal with it."""
    r = make_runner([], env={"AB_Y": "y"})
    for condition in (False, True):
        with pytest.raises(ValueError) as ei:
            r.render(template, condition=condition)
        assert str(ei.value) == undeclared("AB_NOPE")
    assert r.render("{{ env.AB_NOPE | default('d') }} {{ env.get('AB_NOPE', 'g') }}") == "d g"


def test_p5_63_undeclared_key_in_a_step_fails_the_step_and_sends_nothing(sent):
    """SPEC "Jinja2 Templating": in a step it is a template error when the step runs."""
    with pytest.raises(ValueError) as ei:
        run_script([{"line": "echo {{ env.HOME }}"}])
    assert str(ei.value) == undeclared("HOME")
    assert sent.lines() == []


def test_p5_64_mapping_holds_the_declared_keys_in_the_order_written(environ: dict[str, str]):
    """SPEC "The environment in templates": one mapping of the declared variables, typed, in the section's order."""
    r = make_runner([], env={"AB_Y": "oy", "AB_B": "unused", "AB_N": 3, "AB_F": True, "AB_R": 0.5})
    values = {"AB_Y": "oy", "AB_B": "os-b", "AB_N": 3, "AB_F": True, "AB_R": 0.5}
    assert r._env == values and list(r._env) == list(values)
    assert r.render("{{ env | length }} {{ env.keys() | join(',') }}") == "5 " + ",".join(values)
    assert r.render("{% for k, v in env | items %}{{ k }}={{ v | string }};{% endfor %}") == "".join(f"{k}={v};" for k, v in values.items())
    assert json.loads(r.render("{{ env | tojson }}")) == values
    assert r.render("{{ env }}") == str(values)


@pytest.mark.parametrize("name", ["items", "keys", "get", "values"])
def test_p5_64_key_named_like_a_method_wins(name: str):
    """SPEC "Jinja2 Templating": a declared key named like a mapping method is the key."""
    r = make_runner([], env={name: "declared"})
    assert r.render(f"{{{{ env.{name} }}}} {{{{ env['{name}'] }}}}") == "declared declared"


@pytest.mark.parametrize("name", ["declared", "_config", "why", "given", "__dict__"])
def test_p5_64_env_holds_nothing_but_its_keys(name: str):
    """SPEC "The environment in templates": a name that is neither a key nor a mapping method is undefined."""
    r = make_runner([], env={"AB_A": "a"})
    with pytest.raises(ValueError, match="^template error: "):
        r.render(f"{{{{ env.{name} }}}}")
    assert [a for a in dir(r._env) if not a.startswith("__")] == [a for a in dir({}) if not a.startswith("__")]


# -- P5-94: the type of a variable is the type of its default -------------------

ACCEPTED: dict[str, tuple[Any, list[tuple[str, Any]]]] = {
    "string": ("dflt", [("", ""), (" x ", " x "), ("true", "true"), ("7", "7"), ("1.5", "1.5")]),
    "required": (None, [("", ""), ("no", "no"), ("0", "0")]),
    "boolean": (
        False,
        [(t, True) for t in ("1", "true", "yes", "on", "TRUE", "Yes", " On\t", "\ntrue ")]
        + [(f, False) for f in ("0", "false", "no", "off", "", "FALSE", "No", " oFF ", "  ")],
    ),
    "integer": (3, [("7", 7), ("-7", -7), ("+7", 7), (" 42\n", 42), ("007", 7), ("0", 0), ("9" * 30, int("9" * 30))]),
    "number": (0.5, [("2.5", 2.5), ("-1", -1.0), (" 3 ", 3.0), ("1e3", 1000.0), (".5", 0.5), ("+0.25", 0.25)]),
}


@pytest.mark.parametrize("kind", ACCEPTED)
@pytest.mark.parametrize("source", ["environment", "option"])
def test_p5_94_given_text_becomes_a_value_of_the_type_of_the_default(monkeypatch, kind: str, source: str):
    """SPEC "Top-level fields" (`env`): text from the environment or `--env` is converted to the type of the
    key's default; a string, and a required variable, take it as it is."""
    default, cases = ACCEPTED[kind]
    for text, value in cases:
        if source == "environment":
            monkeypatch.setenv("AB_A", text)
        r = make_runner([], env={"AB_A": default}, given={"AB_A": text} if source == "option" else {})
        assert r._env["AB_A"] == value and type(r._env["AB_A"]) is type(value), text


REJECTED = {
    "a boolean": (True, ["2", "y", "n", "t", "enabled", "truee", "yes please", "1.0", "-", SECRET]),
    "an integer": (3, ["", " ", "1.5", "1e3", "0x10", "1_000", "--1", "+", "1 2", "٣", "9" * 5000, "seven", SECRET]),
    "a number": (0.5, ["", " ", "nan", "inf", "-inf", "Infinity", "1e999", "1,5", "0x1p3", "half", SECRET]),
}


@pytest.mark.parametrize("what", REJECTED)
@pytest.mark.parametrize("source", ["environment", "option"])
def test_p5_94_text_that_is_not_of_the_type_is_an_error_without_the_value(monkeypatch, what: str, source: str):
    """SPEC "Top-level fields" (`env`): a value that can't be converted is an error, of `--env` when the
    runner is built and of the environment when `env` is resolved, which names the variable, the type and the
    source, and never shows the value."""
    default, texts = REJECTED[what]
    where = "the environment" if source == "environment" else "--env"
    for text in texts:
        if source == "environment":
            monkeypatch.setenv("AB_A", text)
        with pytest.raises(ValueError) as ei:
            if source == "option":  # found without a run
                Runner(make_config([], env={"AB_A": default}), {"AB_A": text})
            make_runner([], env={"AB_A": default})
        assert str(ei.value) == f"env.AB_A: the value from {where} is not {what}", text
        # nothing that holds the value is chained to the error, so a traceback doesn't show it either
        assert ei.value.__cause__ is None and ei.value.__suppress_context__


def test_p5_94_default_is_not_converted():
    """SPEC "Top-level fields" (`env`): a default has the type YAML gave it: a quoted number is a string, and
    a boolean is not an integer."""
    r = make_runner([], env={"AB_S": "7", "AB_T": "true", "AB_I": 7, "AB_F": 7.0, "AB_B": True})
    assert r._env == {"AB_S": "7", "AB_T": "true", "AB_I": 7, "AB_F": 7.0, "AB_B": True}
    assert [type(v) for v in r._env.values()] == [str, str, int, float, bool]


def test_p5_94_cli_reports_no_value(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """SPEC "CLI", "The order of a run": the report of a value that can't be converted, with `--traceback`
    too, has no value in it. Of `--env` it is a script error, status 1, and `prepare` hasn't run; of the
    environment a failed run, status 3: `env` is resolved once `prepare` has run."""
    monkeypatch.setenv("AB_B", SECRET)
    marker = tmp_path / "prepared"
    doc = make_doc([], env={"AB_A": 1, "AB_B": False}, prepare=f"touch {marker}\n")
    for argv, status, line in [
        (("-e", f"AB_A={SECRET}"), 1, "Script error in {}: env.AB_A: the value from --env is not an integer"),
        (("-e", "AB_A=5"), 3, "Run failed in {}: env.AB_B: the value from the environment is not a boolean"),
    ]:
        res = run_cli(doc, tmp_path, *argv, "--traceback")
        assert res.returncode == status and res.stdout == ""
        assert report(res)[-1] == line.format(tmp_path / "script.autobot.yaml")
        assert SECRET not in res.stderr and marker.exists() == (status == 3)


# -- P5-95: a required variable -------------------------------------------------


@pytest.mark.parametrize("empty", ["AB_A:", "AB_A: ~", "AB_A: null"])
def test_p5_95_empty_value_declares_a_required_variable(tmp_path: Path, monkeypatch, empty: str):
    """SPEC "Top-level fields" (`env`): a key with an empty value has no default; without a value the run
    fails once `prepare` has run, with status 3, before anything is spawned, and the report says how to
    give it."""
    marker, spawned = tmp_path / "prepared", tmp_path / "spawned"
    raw = (
        f"autobot: 2026-10\nprepare: touch {marker}\nenv:\n  {empty}\n"
        f"attach:\n  spawn: sh -c 'touch {spawned}; echo {{{{ env.AB_A }}}}'\nscript: []\n"
    )
    res = run_cli(None, tmp_path, raw=raw)
    assert (res.returncode, res.stdout) == (3, "")
    assert report(res) == [
        ">> prepare: running local script (no shebang, using /bin/sh)",
        ">> prepare: done",
        f"Run failed in {tmp_path / 'script.autobot.yaml'}: env.AB_A is required: give it with --env AB_A=VALUE "
        "or in the environment",
    ]
    assert marker.exists() and not spawned.exists()
    assert "host-1" in run_cli(None, tmp_path, "-e", "AB_A=host-1", raw=raw).stdout
    monkeypatch.setenv("AB_A", "host-2")
    assert "host-2" in run_cli(None, tmp_path, raw=raw).stdout


def test_p5_95_required_variable_is_a_string_and_is_checked_in_order(monkeypatch: pytest.MonkeyPatch):
    """SPEC "Top-level fields" (`env`): the value is taken as text; the first variable without one is named,
    also when no template reads it."""
    with pytest.raises(ValueError, match=REQUIRED):
        make_runner([], env={"AB_Y": "y", "AB_A": None, "AB_B": None})
    with pytest.raises(ValueError, match=REQUIRED.replace("AB_A", "AB_B")):
        make_runner([], env={"AB_A": None, "AB_B": None}, given={"AB_A": "x"})
    monkeypatch.setenv("AB_B", " 0 ")
    r = make_runner([], env={"AB_A": None, "AB_B": None}, given={"AB_A": "true"})
    assert r._env == {"AB_A": "true", "AB_B": " 0 "}


# -- P5-96: --env ---------------------------------------------------------------


def cli(tmp_path: Path, *argv: str, **kw: Any):
    kw.setdefault("env", {"AB_A": "dflt", "AB_B": None})
    res = run_cli(make_doc([{"cmd": 'echo "<{{ env.AB_A }}><{{ env.AB_B }}>"'}], **kw), tmp_path, *argv)
    return res.returncode, res.stdout, "".join(line + "\n" for line in report(res))


def test_p5_96_env_option_sets_a_declared_variable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """SPEC "CLI": `-e KEY=VALUE` and `--env KEY=VALUE`, before or after the script; the value is all that
    follows the first `=`; of a key given twice the last value counts; `--env` comes before the environment."""
    monkeypatch.setenv("AB_A", "os")
    status, out, _ = cli(tmp_path, "-e", "AB_B=x=y z", "--env", "AB_A=first", "--env=AB_A=last")
    assert status == 0 and "\n<last><x=y z>\n" in out
    status, out, _ = cli(tmp_path, "-e", "AB_B=")
    assert status == 0 and "\n<os><>\n" in out


@pytest.mark.parametrize("item", ["AB_B", "", "AB_B:x", "=x", "="])
def test_p5_96_env_option_without_an_equals_sign(tmp_path: Path, item: str):
    """SPEC "CLI": a load error, status 1, before anything runs; an empty KEY (`=x`) is one too, since no `env` key is empty."""
    assert cli(tmp_path, "-e", "AB_B=x", "--env", item) == (1, "", "--env requires KEY=VALUE format\n")


@pytest.mark.parametrize("key", ["AB_NOPE", "ab_b", "AB_B ", "HOME"])
def test_p5_96_env_option_for_a_key_the_script_does_not_declare(tmp_path: Path, key: str):
    """SPEC "CLI": a key that isn't a key of the `env` section is a script error naming it, whatever its
    value and whether the environment has it; nothing runs."""
    marker = tmp_path / "prepared"
    status, out, err = cli(tmp_path, "-e", "AB_B=x", "-e", f"{key}={SECRET}", prepare=f"touch {marker}\n")
    path = tmp_path / "script.autobot.yaml"
    assert (status, out) == (1, "")
    assert err == f"Script error in {path}: --env {key}: the script's env section declares no variable {key!r}\n"
    assert not marker.exists()
    status, _, err = cli(tmp_path, "-e", "AB_A=x", env=None)
    assert status == 1 and err.endswith("--env AB_A: the script's env section declares no variable 'AB_A'\n")


@pytest.mark.parametrize("argv", [("-e",), ("--env",), ("-a", "k=v"), ("--arg", "k=v")], ids=["short", "long", "a", "arg"])
def test_p5_96_option_without_a_value_and_options_that_do_not_exist(tmp_path: Path, argv: tuple[str, ...]):
    """SPEC "CLI": usage errors of the argument parser, status 2."""
    status, out, err = cli(tmp_path, *argv)
    assert (status, out) == (2, "") and err.startswith("usage: ")


# -- P5-97: a variable that isn't a string, in templates --------------------------

BOOLEAN = (
    "template error: an expression gave a boolean ({}), which is never written as text; quote the value in "
    "the script, or say which text is meant, e.g. {{{{ value | string }}}} (True) or {{{{ value | tojson }}}} (true)"
)


@pytest.mark.parametrize(("text", "value"), [(None, False), ("yes", True), ("off", False), ("", False), ("1", True)])
def test_p5_97_boolean_variable_is_a_condition_and_not_text(sent, monkeypatch, text: str | None, value: bool):
    """SPEC "Booleans are not text": a boolean of `env` runs or skips a step in `when` and decides an
    `{% if %}`, and writing it out is a template error."""
    if text is not None:
        monkeypatch.setenv("AB_DEBUG", text)
    script = [
        {"line": "echo debug", "when": "{{ env.AB_DEBUG }}"},
        {"line": "echo quiet", "when": "{{ not env.AB_DEBUG }}"},
        {"line": "echo {% if env.AB_DEBUG %}on{% else %}off{% endif %} {{ env.AB_DEBUG | tojson }}"},
        {"line": "echo {{ env.AB_DEBUG }}"},
    ]
    with pytest.raises(ValueError) as ei:
        run_script(script, env={"AB_DEBUG": False})
    assert str(ei.value) == BOOLEAN.format(str(value).lower())
    assert sent.lines() == [("echo debug" if value else "echo quiet"), f"echo {'on true' if value else 'off false'}"]


def test_p5_97_number_is_a_number_in_an_expression_and_its_digits_as_text(monkeypatch: pytest.MonkeyPatch):
    """SPEC "Booleans are not text": a number renders as its digits, and counts and compares as a number."""
    monkeypatch.setenv("AB_N", "10")
    r = make_runner([], env={"AB_N": 3, "AB_R": 0.5, "AB_S": "3"}, given={"AB_R": "2"})
    assert r.render("{{ env.AB_N }} {{ env.AB_N + 1 }} {{ env.AB_R }} {{ env.AB_R * 2 }} {{ env.AB_S * 2 }}") == "10 11 2.0 4.0 33"
    assert r.render("{{ 'many' if env.AB_N > 9 else 'few' }} {{ range(env.AB_N) | length }}") == "many 10"
