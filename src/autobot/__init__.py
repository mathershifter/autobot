"""Autobot: YAML scripts run against remote consoles.

The names below are imported when first used, so `import autobot` loads neither the runner nor the CLI;
`autobot.cli:main` is the console script.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from . import errors, registry
    from .log import OutputLost
    from .models import Config, Prompt
    from .protocols import PluginLookup, Reporter, RunnerContext, SessionLike, StepExecutor
    from .report import left, trail
    from .runner import Runner

__all__ = [
    "Config",
    "OutputLost",
    "PluginLookup",
    "Prompt",
    "Reporter",
    "Runner",
    "RunnerContext",
    "SessionLike",
    "StepExecutor",
    "errors",
    "left",
    "registry",
    "trail",
]

_HOME = {
    "Config": ".models",
    "OutputLost": ".log",
    "PluginLookup": ".protocols",
    "Prompt": ".models",
    "Reporter": ".protocols",
    "Runner": ".runner",
    "RunnerContext": ".protocols",
    "SessionLike": ".protocols",
    "StepExecutor": ".protocols",
    "left": ".report",
    "trail": ".report",
}
_MODULES = ("errors", "registry")  # `autobot.errors` and `autobot.registry` are the modules


def __getattr__(name: str) -> Any:
    if name in _MODULES:
        return importlib.import_module(f".{name}", __name__)
    if name in _HOME:
        return getattr(importlib.import_module(_HOME[name], __name__), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    # the lazy names of `__all__` as well as what is already a module attribute, so `dir(autobot)` lists them
    return sorted(set(globals()) | set(__all__))
