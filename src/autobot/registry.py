from __future__ import annotations

import importlib.metadata
from typing import TYPE_CHECKING, Any

import pydantic

from .models import _BUILTIN_KEYS, _COMMON_PROPS

if TYPE_CHECKING:
    from .protocols import StepExecutor


class StepRegistry:
    def __init__(self):
        self._executors: dict[str, StepExecutor] = {}
        self._model_keys: dict[type, str] = {}
        self._builtins: set[str] = set()
        self._discovered = False

    def register(self, executor: StepExecutor, *, builtin: bool = False):
        if not builtin:
            self._check_plugin(executor)
        self._executors[executor.key] = executor
        self._model_keys[executor.model] = executor.key
        if builtin:
            self._builtins.add(executor.key)

    def _check_plugin(self, executor: StepExecutor):
        # the step model strips the common props before the plugin's model sees the step, and the
        # discriminator never routes them, or a built-in key, to a plugin: such a name could never be set
        key, model = executor.key, executor.model
        who = f"{type(executor).__module__}.{type(executor).__qualname__}"
        if key in _COMMON_PROPS or key in _BUILTIN_KEYS or key in self._builtins:
            raise TypeError(
                f"plugin {who}: step key {key!r} is reserved (a built-in step or a common step property)"
            )
        clashes = [
            name if name == field else f"{name} (field {field!r})"
            for field, info in model.model_fields.items()
            for name in sorted(_input_names(field, info) & _COMMON_PROPS)
        ]
        if clashes:
            raise TypeError(
                f"plugin {who}, step key {key!r}: model {model.__name__} reuses common step property names, "
                f"which the runner handles and never passes to the plugin: {', '.join(clashes)}"
            )

    def get(self, key: str) -> StepExecutor:
        if not self.has(key):
            raise ValueError(f"unknown step type: {key}")
        return self._executors[key]

    def has(self, key: str) -> bool:
        if key not in self._executors:
            self.discover()
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
        if self._discovered:
            return
        self._discovered = True
        for ep in importlib.metadata.entry_points(group="autobot.steps"):
            obj: type[StepExecutor] = ep.load()
            executor = obj() if isinstance(obj, type) or callable(obj) else obj
            self.register(executor)

    def plugin_executors(self) -> list[StepExecutor]:
        return [e for k, e in self._executors.items() if k not in self._builtins]

    def keys(self) -> list[str]:
        return list(self._executors.keys())


def _input_names(field: str, info: pydantic.fields.FieldInfo) -> set[str]:
    """The keys a script could use to set a model field: its name and every alias."""
    alias = info.validation_alias
    choices = alias.choices if isinstance(alias, pydantic.AliasChoices) else [alias]
    names = {field, info.alias, *(c.path[0] if isinstance(c, pydantic.AliasPath) else c for c in choices)}
    return {n for n in names if isinstance(n, str)}


registry = StepRegistry()
