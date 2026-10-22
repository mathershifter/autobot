"""Autobot: YAML scripts run against remote consoles.

The names below are imported when first used, so `import autobot` loads neither the runner nor the CLI;
`autobot.cli:main` is the console script.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from . import registry
    from .models import Config
    from .protocols import PluginLookup, Reporter, RunnerContext, SessionLike, StepExecutor
    from .runner import Runner

__all__ = [
    "Config",
    "PluginLookup",
    "Reporter",
    "Runner",
    "RunnerContext",
    "SessionLike",
    "StepExecutor",
    "registry",
]

_HOME = {
    "Config": ".models",
    "PluginLookup": ".protocols",
    "Reporter": ".protocols",
    "Runner": ".runner",
    "RunnerContext": ".protocols",
    "SessionLike": ".protocols",
    "StepExecutor": ".protocols",
}


def __getattr__(name: str) -> Any:
    if name == "registry":
        return importlib.import_module(".registry", __name__)
    if name in _HOME:
        return getattr(importlib.import_module(_HOME[name], __name__), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
