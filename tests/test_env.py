"""P5-17..22, P5-27..31: environment handling (SPEC.md:25, 75)."""

from __future__ import annotations

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
