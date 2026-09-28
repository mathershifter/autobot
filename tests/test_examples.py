"""P6-19/20: every example validates against the models and the schema."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import ROOT

from autobot.models import Config

EXAMPLES = sorted((ROOT / "examples").glob("*.yaml"))


def load(path: Path) -> Any:
    return yaml.safe_load(path.read_text())


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_p6_19_examples_validate_model(path: Path):
    """SPEC.md:12: examples are valid scripts."""
    Config.model_validate(load(path))


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_p6_20_examples_validate_schema(schema_validator: Any, path: Path):
    """SPEC.md:12: examples are valid against the JSON schema."""
    errors = [e.message for e in schema_validator.iter_errors(load(path))]
    assert errors == []
