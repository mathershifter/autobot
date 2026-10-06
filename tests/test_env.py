"""P5-17..22, P5-27..31, P5-61..64: environment handling (SPEC "Top-level fields", "The environment in templates")."""

from __future__ import annotations

import json
import os

import pytest
from conftest import SpawnLog, SpawnRecorded, make_runner, run_vars

NESTED = {"AB_A": "a", "AB_B": "{{ env.AB_A }}-b", "AB_C": "{{ env.AB_B }}-c"}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("AB_A", "AB_B", "AB_C", "AB_X", "AB_Y"):
        monkeypatch.delenv(key, raising=False)


def test_p5_17_attach_env_replaces_parent_env(monkeypatch: pytest.MonkeyPatch):
    """SPEC.md:75: attach.env replaces the process environment (no merge)."""
    monkeypatch.setenv("AUTOBOT_LEAK", "1")
    out = run_vars([{"cmd": 'echo "leak=${AUTOBOT_LEAK:-unset}"', "register": "out"}])
    assert out["out"] == "leak=unset"


def test_p5_18_attach_env_default_when_omitted(spawned: SpawnLog):
    """SPEC.md:75: without attach.env the child gets TERM=dumb and NO_COLOR=1."""
    spawned.stop = True
    with pytest.raises(SpawnRecorded):
        make_runner([], attach_env=None).run()
    _, kwargs = spawned[0]
    assert kwargs["env"] == {"TERM": "dumb", "NO_COLOR": "1"}


def test_p5_19_yaml_env_overridden_by_os_env(monkeypatch: pytest.MonkeyPatch):
    """SPEC.md:25: OS environment variables override YAML env defaults."""
    monkeypatch.setenv("AB_X", "os")
    r = make_runner([], env={"AB_X": "yaml", "AB_Y": "keep"})
    assert r.render("{{ env.AB_X }}-{{ env.AB_Y }}") == "os-keep"


def test_p5_20_env_nesting():
    """SPEC.md:25: env values may reference other env values."""
    r = make_runner([], env=NESTED)
    assert r.render("{{ env.AB_C }}") == "a-b-c"


def test_p5_21_env_nesting_uses_os_override(monkeypatch: pytest.MonkeyPatch):
    """SPEC.md:25: nesting sees the OS-overridden value."""
    monkeypatch.setenv("AB_A", "os")
    r = make_runner([], env=NESTED)
    assert r.render("{{ env.AB_C }}") == "os-b-c"


@pytest.mark.parametrize(
    ("env", "cycle"),
    [
        ({"AB_A": "{{ env.AB_A }}x"}, "AB_A -> AB_A"),
        ({"AB_A": "{{ env.AB_B }}", "AB_B": "{{ env.AB_A }}"}, "AB_A -> AB_B -> AB_A"),
        ({"AB_A": "{{ env.AB_B }}x", "AB_B": "{{ env.AB_A }}"}, "AB_A -> AB_B -> AB_A"),
        ({"AB_A": "{{ env.AB_B }}", "AB_B": "{{ env.AB_C }}", "AB_C": "{{ env.AB_A }}"}, "AB_A -> AB_B -> AB_C -> AB_A"),
        ({"AB_X": "{{ env.AB_A }}", "AB_A": "{{ env.AB_B }}", "AB_B": "{{ env.AB_A }}"}, "AB_A -> AB_B -> AB_A"),
        ({"AB_A": "{{ env['AB_B'] | upper }}", "AB_B": "{{ env.get('AB_A') }}"}, "AB_A -> AB_B -> AB_A"),
    ],
    ids=["self", "mutual", "mutual-growing", "three", "lead-in", "item-get-filter"],
)
def test_p5_22_env_cycle_detected(env: dict[str, str], cycle: str):
    """SPEC.md:25: a reference cycle among env keys is an error naming the cycle (only the cycle, not a lead-in)."""
    with pytest.raises(ValueError, match=f"^env cycle: {cycle}$"):
        make_runner([], env=env)


def test_p5_27_env_nesting_any_order_and_forms():
    """SPEC.md:25: references resolve whatever the key order; `env['X']`, `env.get`, filters, `args` and `vars` work."""
    env = {
        "AB_C": "{{ env['AB_B'] | upper }}-c",
        "AB_B": "{{ env.get('AB_A') }}-b-{{ args.a }}",
        "AB_A": "{{ vars.v }}",
        "AB_X": "{{ env.AB_NOPE | default('dflt') }}-{{ 'in' if 'AB_A' in env else 'out' }}",
    }
    r = make_runner([], env=env, vars={"v": "a"}, args={"a": "arg"})
    assert r.render("{{ env.AB_C }} {{ env.AB_X }}") == "A-B-ARG-c dflt-in"


def test_p5_28_env_os_override_breaks_cycle(monkeypatch: pytest.MonkeyPatch):
    """SPEC.md:25: an OS variable replaces the default, so the default's reference is never followed."""
    monkeypatch.setenv("AB_A", "os")
    r = make_runner([], env={"AB_A": "{{ env.AB_B }}", "AB_B": "{{ env.AB_A }}-b"})
    assert r.render("{{ env.AB_A }} {{ env.AB_B }}") == "os os-b"


def test_p5_29_env_value_rendered_once():
    """SPEC.md:25: a value is rendered once; text it renders to is not rendered again."""
    r = make_runner([], env={"AB_A": "{% raw %}{{ lit }}{% endraw %}", "AB_B": "{{ env.AB_A }}"})
    assert r.render("{{ env.AB_B }}") == "{{ lit }}"


def test_p5_30_env_undefined_key_and_depth_limit():
    """SPEC.md:25: a reference to a missing key names it; nesting deeper than 50 levels is an error, not a crash."""
    with pytest.raises(ValueError, match="^template error: env has no key 'AB_NOPE'$"):
        make_runner([], env={"AB_A": "{{ env.AB_NOPE }}"})

    def chain(n: int) -> dict[str, str]:
        return {f"AB_K{i}": f"{{{{ env.AB_K{i + 1} }}}}" if i < n - 1 else "end" for i in range(n)}

    assert make_runner([], env=chain(50)).render("{{ env.AB_K0 }}") == "end"
    with pytest.raises(ValueError, match=r"^env nesting deeper than 50 levels: AB_K0 -> \.\.\. -> AB_K50$"):
        make_runner([], env=chain(51))


def test_p5_31_env_os_value_is_verbatim(monkeypatch: pytest.MonkeyPatch):
    """SPEC.md:25: an OS value is used as written, never rendered, and can't form a cycle."""
    monkeypatch.setenv("AB_A", "p{{w}}d{% x %}")
    r = make_runner([], env={"AB_A": "default", "AB_B": "{{ env.AB_A }}-b"})
    assert r.render("{{ env.AB_A }}") == "p{{w}}d{% x %}"
    assert r.render("{{ env.AB_B }}") == "p{{w}}d{% x %}-b"

    monkeypatch.setenv("AB_A", "{{ env.AB_B }}")
    r = make_runner([], env={"AB_A": "default", "AB_B": "{{ env.AB_A }}-b"})
    assert r.render("{{ env.AB_A }}|{{ env.AB_B }}") == "{{ env.AB_B }}|{{ env.AB_B }}-b"


@pytest.fixture
def environ(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """The whole environment the runner reads, so the merged view can be compared exactly."""


    class Environ(dict):
        # pytest keeps PYTEST_CURRENT_TEST in os.environ while a test runs
        def __setitem__(self, key: str, value: str) -> None:
            if key != "PYTEST_CURRENT_TEST":
                super().__setitem__(key, value)

        def pop(self, key: str, *default: str) -> str | None:
            return super().pop(key, *(default or (None,)))

    fake = Environ({"AB_OS": "o", "AB_Z": "z z", "AB_B": "os-b", "AB_EMPTY": ""})
    monkeypatch.setattr(os, "environ", fake)
    return fake


def test_p5_61_undeclared_variable_is_readable(environ: dict[str, str]):
    """SPEC "The environment in templates": `env` reads any variable, whether `env:` declares it or not."""
    for kw in ({}, {"env": {}}, {"env": {"AB_Y": "y"}}):
        r = make_runner([], **kw)
        assert r.render("{{ env.AB_OS }}|{{ env['AB_Z'] }}|{{ env.get('AB_B') }}|{{ env.get('AB_NOPE', 'd') }}") == "o|z z|os-b|d"
        assert r.render("{{ 'y' if 'AB_OS' in env else 'n' }}{{ 'y' if 'AB_NOPE' in env else 'n' }}") == "yn"
        assert r.render("{{ 'y' if env.AB_OS is defined else 'n' }}{{ 'y' if env.AB_NOPE is defined else 'n' }}") == "yn"


def test_p5_61_default_may_reference_an_undeclared_variable(environ: dict[str, str]):
    """SPEC "Top-level fields": a default references variables of the environment like keys of `env`."""
    r = make_runner([], env={"AB_URL": "http://{{ env.AB_OS }}/{{ env.AB_Y }}", "AB_Y": "{{ env['AB_Z'] | upper }}"})
    assert r.render("{{ env.AB_URL }}") == "http://o/Z Z"


def test_p5_62_default_is_used_only_when_the_variable_is_unset(environ: dict[str, str]):
    """SPEC "Top-level fields": a set variable keeps its value, even an empty one; the default isn't rendered."""
    r = make_runner([], env={"AB_B": "{{ 1/0 }}", "AB_EMPTY": "{{ env.AB_NOPE }}", "AB_Y": "dflt"})
    assert r.render("[{{ env.AB_B }}][{{ env.AB_EMPTY }}][{{ env.AB_Y }}]") == "[os-b][][dflt]"
    environ["AB_Y"] = "late"  # the environment is read once, when the run starts
    assert r.render("{{ env.AB_Y }}") == "dflt"


@pytest.mark.parametrize("template", ["{{ env.AB_NOPE }}", "{{ env['AB_NOPE'] }}", "x{{ env.AB_NOPE.y }}"])
def test_p5_63_variable_set_nowhere_is_undefined(environ: dict[str, str], template: str):
    """SPEC "The environment in templates": neither set nor a default is an undefined variable, with its name."""
    r = make_runner([], env={"AB_Y": "y"})
    with pytest.raises(ValueError, match="^template error: env has no key 'AB_NOPE'$"):
        r.render(template)
    assert r.render("{{ env.AB_NOPE | default('d') }}") == "d"
    with pytest.raises(ValueError, match="^template error: env has no key 'AB_NOPE'$"):
        make_runner([], env={"AB_Y": template})


def test_p5_64_mapping_methods_see_the_merged_view(environ: dict[str, str]):
    """SPEC "The environment in templates": one mapping; `env:` keys first as written, then the rest by name."""
    r = make_runner([], env={"AB_Y": "{{ env.AB_OS }}y", "AB_B": "unused", "AB_A": "a"})
    merged = {"AB_Y": "oy", "AB_B": "os-b", "AB_A": "a", "AB_EMPTY": "", "AB_OS": "o", "AB_Z": "z z"}
    assert r.render("{{ env | length }}") == "6"
    assert r.render("{{ env.keys() | join(',') }}") == ",".join(merged)
    assert r.render("{% for k in env %}{{ k }},{% endfor %}") == ",".join(merged) + ","
    assert r.render("{% for k, v in env | items %}{{ k }}={{ v }};{% endfor %}") == "".join(f"{k}={v};" for k, v in merged.items())
    assert r.render("{{ env.items() | list | length }} {{ env.values() | list | first }}") == "6 oy"
    assert json.loads(r.render("{{ env | tojson }}")) == merged
    assert r.render("{{ env }}") == str(merged)


@pytest.mark.parametrize("name", ["items", "keys", "get", "values"])
def test_p5_64_variable_named_like_a_method_wins(environ: dict[str, str], name: str):
    """SPEC "Jinja2 Templating": a variable of the environment named like a mapping method is the variable."""
    environ[name] = "from-os"
    r = make_runner([], env={"AB_REF": f"{{{{ env.{name} }}}}!"})
    assert r.render(f"{{{{ env.{name} }}}} {{{{ env['{name}'] }}}} {{{{ env.AB_REF }}}}") == "from-os from-os from-os!"
    assert r.render("{{ env | length }}") == "6"


def test_p5_64_env_cycle_and_depth_with_the_whole_environment(environ: dict[str, str]):
    """SPEC "Top-level fields": cycles and depth are still reported; a set variable still breaks a cycle."""
    with pytest.raises(ValueError, match="^env cycle: AB_A -> AB_C -> AB_A$"):
        make_runner([], env={"AB_A": "{{ env.AB_OS }}{{ env.AB_C }}", "AB_C": "{{ env.AB_A }}"})
    with pytest.raises(ValueError, match="^env cycle: AB_A -> AB_A$"):
        make_runner([], env={"AB_A": "{{ env.items() | list }}"})
    r = make_runner([], env={"AB_B": "{{ env.AB_C }}", "AB_C": "{{ env.AB_B }}-c"})
    assert r.render("{{ env.AB_C }}") == "os-b-c"
    chain = {f"AB_K{i}": f"{{{{ env.AB_K{i + 1} }}}}" for i in range(51)}
    with pytest.raises(ValueError, match=r"^env nesting deeper than 50 levels: AB_K0 -> \.\.\. -> AB_K50$"):
        make_runner([], env=chain)
    environ["AB_K50"] = "set"  # a variable of the environment ends the chain: it isn't rendered
    assert make_runner([], env=chain).render("{{ env.AB_K0 }}") == "set"
