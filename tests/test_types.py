"""P8-01..04: type helpers (SPEC.md:292, 307-334)."""

from __future__ import annotations

from typing import Any

import pytest

from autobot.types import _jinja_env, ensure_list, parse_duration, render


@pytest.mark.parametrize(
    ("value", "seconds"),
    [
        (5, 5.0),
        (5.5, 5.5),
        ("5s", 5.0),
        ("500ms", 0.5),
        ("2m", 120.0),
        ("1h", 3600.0),
        ("1.5s", 1.5),
        (None, 0),
    ],
)
def test_p8_01_parse_duration(value: Any, seconds: float):
    """SPEC.md:307-313: duration parsing."""
    assert parse_duration(value) == seconds


@pytest.mark.parametrize("value", ["5", "5x", "", "ms", "-1s", "1.5.5s"])
def test_p8_01_parse_duration_invalid(value: str):
    """SPEC.md:307-313: strings without a valid unit are rejected."""
    with pytest.raises(ValueError, match="invalid duration"):
        parse_duration(value)


def test_p8_02_ensure_list():
    assert ensure_list(None) == []
    assert ensure_list("a") == ["a"]
    assert ensure_list(["a"]) == ["a"]


def test_p8_03_render_passthrough_and_filters():
    """SPEC.md:315-334: non-strings pass through; custom filters are registered."""
    assert render(5, {}) == 5
    assert render(None, {}) is None
    assert render(["{{ x }}"], {}) == ["{{ x }}"]
    assert {"contains", "search"} <= set(_jinja_env.filters)


def test_p8_04_render_trailing_newline_dropped():
    """SPEC.md:292: Jinja drops one trailing newline, so ``"false\\n"`` is falsy."""
    assert render("x\n", {}) == "x"
