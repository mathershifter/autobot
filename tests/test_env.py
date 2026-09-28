"""P5-17..22: environment handling (SPEC.md:25, 75)."""

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


def test_p5_22_env_cycle_too_deep():
    """SPEC.md:25: a reference cycle fails instead of looping forever."""
    with pytest.raises(ValueError, match="nesting too deep"):
        make_runner([], env={"AB_A": "{{ env.AB_B }}x", "AB_B": "{{ env.AB_A }}"})
