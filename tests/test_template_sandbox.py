"""Templates are sandboxed: SPEC "Jinja2 Templating", "Templates are sandboxed".

A template reads what it is given and uses Jinja2's own features. It reaches nothing of Python and changes
nothing it is given, in `run` and in `validate` alike.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import jinja2.sandbox
import pytest
import yaml
from conftest import make_doc, make_runner, plugin_dist

from autobot import types
from autobot.runner import Runner
from autobot.types import ScriptError

VARS = {"a": 1, "ports": [3, 1, 2], "values": "V", "_tmp": "under", "__x": "dunder", "site": {"name": "lab", "get": "G"}}
NOT_ALLOWED = "^template error: not allowed in a template: access to attribute "


def runner(**kw: Any) -> Runner:
    kw.setdefault("vars", {k: (list(v) if isinstance(v, list) else dict(v) if isinstance(v, dict) else v) for k, v in VARS.items()})
    kw.setdefault("env", {"AB_E": "e", "AB_F": "{{ env.AB_E }}-{{ env.get('AB_NOPE', 'd') }}"})
    kw.setdefault("args", {"k": "v"})
    return make_runner([], **kw)


# -- what the documented features still do ------------------------------------------------


def test_p3_23_both_environments_are_the_immutable_sandbox():
    """One engine for `run` and `validate`, for text and for conditions."""
    for env in (types._jinja_env, types._condition_env):
        assert isinstance(env, jinja2.sandbox.ImmutableSandboxedEnvironment)
        assert env.sandboxed and env.undefined is jinja2.StrictUndefined
        assert {"contains", "search"} <= env.filters.keys()


def test_p3_23_globals_are_the_documented_ones():
    """SPEC: `range`, `dict` and `namespace`; nothing like `lipsum`, `cycler`, `joiner`, `request` or `config`."""
    r = runner()
    for env in (types._jinja_env, types._condition_env):
        assert sorted(env.globals) == ["dict", "namespace", "range"]
    for name in ("lipsum", "cycler", "joiner", "request", "config", "g", "self.nope", "os", "open", "__builtins__"):
        with pytest.raises(ScriptError, match="^template error: "):
            r.render(f"{{{{ {name} }}}}")


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        # a mapping key wins over a method, and a method works while no key has its name
        ("{{ vars.values }} {{ vars.site.get }} {{ vars.site.name }}", "V G lab"),
        ("{{ vars.get('nope', 'd') }} {{ env.get('AB_NOPE', 'd') }} {{ args.get('k') }}", "d d v"),
        ("{% for k, v in vars.site.items() %}{{ k }}={{ v }};{% endfor %}", "name=lab;get=G;"),
        ("{{ vars.keys() | list | length }} {{ vars | length }}", "6 6"),
        # a key that starts with an underscore is a key, not an attribute
        ("{{ vars._tmp }} {{ vars['_tmp'] }} {{ vars.__x }} {{ vars['__x'] }}", "under under dunder dunder"),
        # the context
        ("{{ env.AB_E }} {{ env['AB_F'] }} {{ args.k }} {{ session.before }}|", "e e-d v |"),
        # filters and tests
        ("{{ vars.nope | default('D') }} {{ vars.site | tojson }}", 'D {"get": "G", "name": "lab"}'),
        ("{{ vars.a | string }} {{ vars.ports | sort | join(',') }} {{ vars | items | first | first }}", "1 1,2,3 a"),
        ("{{ vars.ports | map('string') | list | length }} {{ vars.ports | select('odd') | list }}", "3 [3, 1]"),
        ("{{ [vars.site] | map(attribute='name') | first }} {{ vars.site | attr('nope') | default('x') }}", "lab x"),
        ("{{ 'on' if vars.values | contains('V') else 'off' }} {{ 'y' if 'v1.2' | search('\\\\d') else 'n' }}", "on y"),
        ("{{ 'y' if vars.nope is defined else 'n' }} {{ 'y' if 'a' in vars else 'n' }}", "n y"),
        # statements
        ("{% set x = vars.a + 1 %}{{ x }}", "2"),
        ("{% for i in range(3) %}{{ i }}{% endfor %} {{ range(2, 8, 2) | list }}", "012 [2, 4, 6]"),
        ("{% for p in vars.ports %}{{ loop.index }}:{{ p }}{{ ',' if not loop.last }}{% endfor %}", "1:3,2:1,3:2"),
        ("{% macro port(n) %}Ethernet{{ n }}{% endmacro %}{{ port(4) }}", "Ethernet4"),
        ("{% set ns = namespace(n=0) %}{% for p in vars.ports %}{% set ns.n = ns.n + p %}{% endfor %}{{ ns.n }}", "6"),
        (
            "{% set ns = namespace(got=[]) %}{% for p in vars.ports %}{% set ns.got = ns.got + [p * 2] %}{% endfor %}"
            "{{ ns.got }}",
            "[6, 2, 4]",
        ),
        ("{{ dict(a=1) }} {{ dict(vars.site, extra=1) | length }}", "{'a': 1} 3"),
        ("{% raw %}${#arr[@]}{% endraw %} {{ '{#' }}", "${#arr[@]} {#"),
        # methods of strings, and text an expression builds itself
        ("{{ 'a,b'.split(',') }} {{ 'x'.upper() }} {{ vars.values.lower() }}", "['a', 'b'] X v"),
        ("{{ '%s-%s' | format(vars.a, 'b') }} {{ '{}-{n}'.format(vars.a, n=2) }} {{ vars.a ~ '' }}", "1-b 1-2 1"),
        ("{{ '{0[name]}'.format(vars.site) }}", "lab"),
    ],
)
def test_p3_23_documented_features_render_in_the_sandbox(template: str, expected: str):
    r = runner()
    assert r.render(template) == expected
    assert r.render(template, condition=True) == expected


@pytest.mark.parametrize(
    ("template", "message"),
    [
        ("{{ vars.nope }}", "template error: 'dict object' has no attribute 'nope'"),
        ("{{ nope }}", "template error: 'nope' is undefined"),
        ("{{ env.AB_NOPE }}", "template error: env has no key 'AB_NOPE'"),
        ("{{ 1/0 }}", "template error: ZeroDivisionError: division by zero"),
        ("{{ 'a' + 1 }}", 'template error: TypeError: can only concatenate str (not "int") to str'),
        ("{{ x ", "template error: unexpected end of template, expected 'end of print statement'."),
        ("{{ 'a' | search('(') }}", "template error: search: invalid regex '(': missing ), unterminated subpattern at position 0"),
        (
            "{{ vars.a == 1 }}",
            "template error: an expression gave a boolean (true), which is never written as text; quote the value in "
            "the script, or say which text is meant, e.g. {{ value | string }} (True) or {{ value | tojson }} (true)",
        ),
    ],
)
def test_p3_23_error_messages_are_the_documented_ones(template: str, message: str):
    with pytest.raises(ScriptError) as ei:
        runner().render(template)
    assert str(ei.value) == message


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"A": "{{ env.B }}", "B": "{{ env.A }}"}, "env cycle: A -> B -> A"),
        ({f"K{i}": f"{{{{ env.K{i + 1} }}}}" for i in range(60)} | {"K60": "x"}, "env nesting deeper than 50 levels: K0 -> ... -> K50"),
    ],
    ids=["cycle", "depth"],
)
def test_p3_23_env_cycle_and_depth_errors_are_kept(env: dict[str, str], message: str):
    with pytest.raises(ScriptError) as ei:
        runner(env=env)
    assert str(ei.value) == message


# -- the ways out are closed ----------------------------------------------------------------

ESCAPES = [
    "{{ lipsum.__globals__.os.system('touch MARKER') }}",
    "{{ lipsum.__globals__['os'].popen('touch MARKER').read() }}",
    "{{ cycler.__init__.__globals__.os.system('touch MARKER') }}",
    "{{ joiner.__init__.__globals__.os.system('touch MARKER') }}",
    "{{ namespace.__init__.__globals__.os.system('touch MARKER') }}",
    "{{ range.__globals__ }}",
    "{{ dict.__subclasses__() }}",
    "{{ dict.mro() }}",
    "{{ ''.__class__.__mro__[1].__subclasses__() }}",
    "{{ ().__class__.__base__.__subclasses__() }}",
    "{{ [].__class__ }}",
    "{{ vars.__class__ }}",
    "{{ vars.site.__class__ }}",
    "{{ env.__class__.__mro__ }}",
    "{{ args.__init__ }}",
    "{{ session.__class__ }}",
    "{{ self._TemplateReference__context }}",
    "{{ self.__init__.__globals__ }}",
    "{{ fn.__globals__ }}",
    "{{ fn.__builtins__ }}",
    "{{ fn.__code__ }}",
    "{{ fn.__call__('x') }}",
    "{{ obj.__dict__ }}",
    "{{ obj._hidden }}",
    "{{ obj.method.__self__ }}",
    "{{ obj.method.__func__.__globals__ }}",
    "{{ vars.items.__self__ }}",
    "{{ ''['__class__'] }}",
    "{{ vars['__class__'] }}",
    "{{ '' | attr('__class__') }}",
    "{{ obj | attr('_hidden') }}",
    "{{ [obj] | map(attribute='_hidden') | first }}",
    "{{ [obj] | map('attr', '__dict__') | first }}",
    "{{ '{0.__class__}'.format(vars.a) }}",
    "{{ '{0.__class__.__mro__}'.format(vars) }}",
    "{{ '{x._hidden}'.format(x=obj) }}",
    "{{ '{x.__class__}'.format_map({'x': 1}) }}",
    "{{ (vars.ports | map('string')).gi_frame }}",
    "{% set x = ''.__class__ %}{{ x }}",
    "{% for c in ''.__class__.__mro__ %}{{ c }}{% endfor %}",
    "{% if ''.__class__.__mro__ %}yes{% endif %}",
]


class Obj:
    """What a plugin might pass in: a public attribute and a method are readable, the rest is not."""

    public = "pub"
    _hidden = "MARKER-secret"

    def method(self) -> str:
        return "called"


@pytest.mark.parametrize("condition", [False, True], ids=["text", "condition"])
@pytest.mark.parametrize("template", ESCAPES)
def test_p3_24_escape_routes_are_closed(template: str, condition: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Each known way from a template to Python's internals is a template error, and nothing happened."""
    monkeypatch.chdir(tmp_path)
    r = runner()
    with pytest.raises(ScriptError, match="^template error: ") as ei:
        r.render(template, {"fn": str.upper, "obj": Obj()}, condition=condition)
    assert "MARKER-secret" not in str(ei.value)
    assert list(tmp_path.iterdir()) == []
    assert r.config.vars == VARS


@pytest.mark.parametrize(
    ("template", "what"),
    [
        ("{{ ''.__class__ }}", "'__class__' of 'str' object is unsafe."),
        ("{{ double.__globals__ }}", "'__globals__' of 'function' object is unsafe."),
        ("{{ obj._hidden }}", "'_hidden' of 'Obj' object is unsafe."),
        ("{{ '{0.__class__}'.format(1) }}", "'__class__' of 'int' object is unsafe."),
        ("{{ vars.clear() }}", "'clear' of 'dict' object is unsafe."),
    ],
)
def test_p3_24_the_message_says_what_was_not_allowed(template: str, what: str):
    with pytest.raises(ScriptError) as ei:
        runner().render(template, {"double": lambda n: n * 2, "obj": Obj()})
    assert str(ei.value) == f"template error: not allowed in a template: access to attribute {what}"
    assert isinstance(ei.value.__cause__, jinja2.sandbox.SecurityError)


def test_p3_24_an_unreachable_attribute_is_undefined():
    """It counts as undefined where a template asks: no error, and no value."""
    r = runner()
    assert r.render("{{ 'y' if ''.__class__ is defined else 'n' }} {{ ''.__class__ | default('d') }}") == "n d"


@pytest.mark.parametrize(
    "template",
    [
        "{{ vars.update({'a': 2}) }}",
        "{{ vars.clear() }}",
        "{{ vars.pop('a') }}",
        "{{ vars.setdefault('z', 1) }}",
        "{{ vars.site.update(name='x') }}",
        "{{ vars.ports.append(9) }}",
        "{{ vars.ports.sort() }}",
        "{{ vars.ports.clear() }}",
        "{{ env.update({'AB_E': 'x'}) }}",
        "{{ env.pop('AB_E') }}",
        "{{ env.clear() }}",
        "{{ args.update(k='x') }}",
        "{{ args.clear() }}",
        "{{ session.update(before='x') }}",
        "{% set _ = vars.update(a=2) %}",
        "{% set l = [] %}{% set _ = l.append(1) %}{{ l }}",
    ],
)
def test_p3_24_a_template_changes_nothing_it_is_given(template: str):
    r = runner()
    before = (dict(r._env), dict(r._cli_args), dict(r.session.ctx))
    with pytest.raises(ScriptError, match=NOT_ALLOWED):
        r.render(template)
    assert r.config.vars == VARS
    assert (dict(r._env), dict(r._cli_args), dict(r.session.ctx)) == before


def test_p3_24_range_is_bounded():
    """Jinja2's sandbox refuses a range of more than 100000 items."""
    r = runner()
    assert r.render("{{ range(100000) | length }}") == "100000"
    with pytest.raises(ScriptError, match="^template error: OverflowError: Range too big"):
        r.render("{{ range(100001) | length }}")


# -- every place a template is rendered ----------------------------------------------------

INJECT = "{{ lipsum.__globals__.os.system('touch PWNED') }}"
CLASS = "{{ ''.__class__.__mro__[1].__subclasses__() | length }}"


def _cli(*argv: Any, cwd: Path, **extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-W", "ignore", "-m", "autobot.cli", *map(str, argv)],
        check=False, capture_output=True, text=True, timeout=120, cwd=cwd, env={**os.environ, **extra},
    )


@pytest.mark.parametrize("command", ["validate", "run"])
@pytest.mark.parametrize("template", [INJECT, CLASS], ids=["lipsum", "class"])
def test_p3_25_env_default_cannot_run_code_when_the_script_is_loaded(tmp_path: Path, command: str, template: str):
    """The reported case: an `env` default is rendered at load, by `validate` as by `run`."""
    path = tmp_path / "s.yaml"
    path.write_text(yaml.safe_dump(make_doc([], env={"A": template}, spawn=f"touch {tmp_path / 'spawned'}")))
    res = _cli(command, path, cwd=tmp_path)
    assert res.returncode == 1 and res.stdout == ""
    assert res.stderr.startswith(f"Script error in {path}: ") and "template error: " in res.stderr.splitlines()[0]
    assert "Traceback" not in res.stderr
    assert sorted(p.name for p in tmp_path.iterdir()) == ["s.yaml"]


def test_p3_25_one_script_cannot_change_the_result_of_the_next(tmp_path: Path):
    """A template can't set a variable of the environment that the next script's default would read."""
    first, second = tmp_path / "a.yaml", tmp_path / "b.yaml"
    first.write_text(yaml.safe_dump(make_doc([], env={"A": "{{ lipsum.__globals__.os.environ.update({'AB_LEAK': '1'}) }}"})))
    second.write_text(yaml.safe_dump(make_doc([], env={"B": "{{ env.AB_LEAK }}"})))
    res = _cli("validate", first, second, cwd=tmp_path)
    assert res.returncode == 1
    lines = res.stderr.splitlines()
    assert lines[1] == f"{first}: invalid" and lines[-1] == f"{second}: invalid"
    assert lines[2].startswith(f"Script error in {second}: ") and lines[2].endswith("env has no key 'AB_LEAK'")


RUN_FIELDS = {
    "cmd": lambda t: {"script": [{"cmd": f"echo {t}"}]},
    "embedded-script": lambda t: {"script": [{"cmd": f"#!/bin/sh\necho {t}\n"}]},
    "assert": lambda t: {"script": [{"cmd": "echo ok", "assert": t}]},
    "after": lambda t: {"script": [{"line": "echo ok", "after": t}]},
    "when": lambda t: {"script": [{"cmd": "echo ok", "when": t}]},
    "line": lambda t: {"script": [{"line": f"echo {t}"}]},
    "spawn": lambda t: {"script": [], "spawn": f"bash {t}"},
    "prepare": lambda t: {"script": [], "prepare": f"#!/bin/sh\necho {t}\n"},
}


@pytest.mark.parametrize("field", RUN_FIELDS)
def test_p3_25_every_templated_field_is_rendered_in_the_sandbox(field: str, tmp_path: Path):
    """Whatever renders the value, a step, `when`, `spawn` or `prepare`, it is the one sandboxed engine: the
    run fails with the template error (status 3), and the file is not made."""
    kw = RUN_FIELDS[field](INJECT)
    path = tmp_path / "s.yaml"
    path.write_text(yaml.safe_dump(make_doc(kw.pop("script"), **kw)))
    res = _cli("run", path, cwd=tmp_path)
    assert res.returncode == 3, res.stderr
    assert f"Run failed in {path}: template error: 'lipsum' is undefined" in res.stderr
    assert not (tmp_path / "PWNED").exists()


def test_p3_25_prompt_send_is_rendered_in_the_sandbox():
    from autobot.models import Prompt

    r = runner()
    handler = r.build_handler(Prompt.model_validate({"name": "p", "expect": ["x"], "send": "{{ ''.__class__ }}"}))
    with pytest.raises(ScriptError, match="^prompt 'p': template error: not allowed in a template: "):
        handler.respond(0)


PLUGIN = '''
from __future__ import annotations

import pydantic


class TellStep(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    tell: str


class Device:
    name = "sw1"
    _token = "s3cret"

    def ports(self):
        return [1, 2]


class TellExecutor:
    key = "tell"
    model = TellStep

    def execute(self, step, ctx, timeout):
        text = ctx.render(step.tell, {"double": lambda n: n * 2, "device": Device(), "up": str.upper})
        ctx.config.vars["told"] = text
        print("TOLD " + text, flush=True)
'''


@pytest.fixture
def plugins(tmp_path: Path) -> Path:
    root = tmp_path / "plugins"
    root.mkdir()
    return plugin_dist(root, "tell", PLUGIN, "TellExecutor")


def _tell(tmp_path: Path, plugins: Path, template: str) -> subprocess.CompletedProcess[str]:
    path = tmp_path / "s.yaml"
    path.write_text(yaml.safe_dump(make_doc([{"tell": template}], spawn="bash --norc --noprofile -i")))
    return _cli("run", path, cwd=tmp_path, PYTHONPATH=str(plugins))


def test_p3_25_plugin_render_calls_what_the_plugin_passes_in(tmp_path: Path, plugins: Path):
    """`ctx.render(template, extra)`: a function, a method and a public attribute of what the plugin passes
    in work as in any Jinja2 template."""
    res = _tell(tmp_path, plugins, "{{ double(21) }} {{ up('x') }} {{ device.name }} {{ device.ports() | length }}")
    assert res.returncode == 0, res.stderr
    assert "TOLD 42 X sw1 2\n" in res.stdout


@pytest.mark.parametrize(
    "template",
    ["{{ device._token }}", "{{ double.__globals__ }}", "{{ device.ports.__func__.__globals__ }}", "{{ vars.update(a=1) }}"],
)
def test_p3_25_plugin_render_is_sandboxed(tmp_path: Path, plugins: Path, template: str):
    res = _tell(tmp_path, plugins, template)
    assert res.returncode == 3, res.stderr
    assert "template error: not allowed in a template: access to attribute " in res.stderr
    assert "s3cret" not in res.stderr + res.stdout and "Unexpected error" not in res.stderr
