"""P8-01..04: type helpers (SPEC.md:292, 307-334)."""

from __future__ import annotations

from typing import Any

import pytest

from autobot.types import (
    _jinja_env,
    check_template,
    ensure_list,
    parse_duration,
    render,
)


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
    ],
)
def test_p8_01_parse_duration(value: Any, seconds: float):
    """SPEC.md:307-313: duration parsing."""
    assert parse_duration(value) == seconds


@pytest.mark.parametrize("value", ["5", "5x", "", "ms", "-1s", "1.5.5s", None])
def test_p8_01_parse_duration_invalid(value: str | None):
    """SPEC.md:307-313: strings without a valid unit, and null, are rejected."""
    with pytest.raises(ValueError, match="invalid duration"):
        parse_duration(value)


def test_p8_01_parse_duration_null_message():
    """A null is reported with its YAML name, not Python's ``None``."""
    with pytest.raises(ValueError, match="^invalid duration: null$"):
        parse_duration(None)


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


@pytest.mark.parametrize("template", ["{{ vars.nope }}", "{{ oops(", "{% if %}", "${#arr[@]}"])
def test_every_template_error_is_a_value_error(template: str):
    """SPEC "Jinja2 Templating": syntax and undefined-variable errors read the same."""
    with pytest.raises(ValueError, match="^template error: "):
        render(template, {"vars": {}})


@pytest.mark.parametrize("template", ["{{ oops(", "{% if %}", "${#arr[@]}"])
def test_check_template_reports_syntax_errors_like_render(template: str):
    with pytest.raises(ValueError, match="^template error: "):
        check_template(template)
    check_template("{{ vars.nope }}")  # undefined names are only known when rendered


# -- P8-17..19: non-finite durations, ASCII digits, invalid search regex -----


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), 10**400, "9" * 400 + "h"])
def test_p8_17_parse_duration_nonfinite(value: Any):
    """SPEC "Duration Format": a duration must be finite (a NaN timeout never expired)."""
    with pytest.raises(ValueError, match=r"^invalid duration: .* \(not a finite number\)$"):
        parse_duration(value)


@pytest.mark.parametrize("value", ["٥s", "5٥ms", "1.٥s", "５s"])
def test_p8_18_parse_duration_ascii_digits_only(value: str):
    """SPEC "Duration Format": only ASCII digits, as in the schema's pattern."""
    with pytest.raises(ValueError, match="^invalid duration: "):
        parse_duration(value)


def test_p8_19_search_invalid_regex_is_template_error():
    """SPEC "Jinja2 Templating": an invalid ``search`` regex is a template error."""
    with pytest.raises(ValueError, match=r"^template error: search: invalid regex '\(': "):
        render("{{ 'x' | search('(') }}", {})
    assert render("{{ 'abc' | search('b+') }}", {}) == "True"
