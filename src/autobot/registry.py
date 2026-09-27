from __future__ import annotations

import importlib.metadata
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .protocols import StepExecutor


class StepRegistry:
    def __init__(self):
        self._executors: dict[str, StepExecutor] = {}
        self._model_keys: dict[type, str] = {}
        self._builtins: set[str] = set()

    def register(self, executor: StepExecutor, *, builtin: bool = False):
        self._executors[executor.key] = executor
        self._model_keys[executor.model] = executor.key
        if builtin:
            self._builtins.add(executor.key)

    def get(self, key: str) -> StepExecutor:
        if key not in self._executors:
            raise ValueError(f"unknown step type: {key}")
        return self._executors[key]

    def has(self, key: str) -> bool:
        return key in self._executors

    def key_for_step(self, step) -> str:
        step_type = type(step)
        if step_type in self._model_keys:
            return self._model_keys[step_type]
        if hasattr(step, "plugin_key_") and step.plugin_key_:
            return step.plugin_key_
        raise ValueError(f"no executor for step: {step}")

    def validate_plugin_step(self, step) -> Any:
        key = step.plugin_key_
        if not key:
            raise ValueError(f"no plugin key on step: {step}")
        executor = self.get(key)
        raw = dict(step.model_extra) if step.model_extra else {}
        return executor.model.model_validate(raw)

    def discover(self):
        for ep in importlib.metadata.entry_points(group="autobot.steps"):
            obj = ep.load()
            executor = obj() if isinstance(obj, type) or callable(obj) else obj
            self.register(executor)

    def plugin_executors(self) -> list[StepExecutor]:
        return [e for k, e in self._executors.items() if k not in self._builtins]

    def keys(self) -> list[str]:
        return list(self._executors.keys())


registry = StepRegistry()
