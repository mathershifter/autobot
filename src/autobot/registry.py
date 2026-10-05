from __future__ import annotations

import importlib.metadata
from typing import TYPE_CHECKING, Any

import pydantic

from .models import _BUILTIN_KEYS, _COMMON_PROPS, PluginStep

if TYPE_CHECKING:
    from .protocols import StepExecutor


class PluginError(TypeError):
    """A plugin that can't be loaded or registered; the CLI reports it without a traceback."""


class StepRegistry:
    def __init__(self):
        self._executors: dict[str, StepExecutor] = {}
        self._model_keys: dict[type, str] = {}
        self._builtins: set[str] = set()
        self._origins: dict[str, str] = {}
        self._discovered = False
        self._discover_error: PluginError | None = None

    def register(self, executor: StepExecutor, *, builtin: bool = False, origin: str = ""):
        if not builtin:
            _check_shape(executor, origin)
            self._check_plugin(executor, origin)
            self._check_unique(executor, origin)
            self._origins[executor.key] = origin
        self._executors[executor.key] = executor
        if builtin:
            # only a built-in step is dispatched by its model: a plugin step arrives as a PluginStep
            self._model_keys[executor.model] = executor.key
            self._builtins.add(executor.key)

    def _check_plugin(self, executor: StepExecutor, origin: str):
        # the step model strips the common props before the plugin's model sees the step, and the
        # discriminator never routes them, or a built-in key, to a plugin: such a name could never be set
        key, model = executor.key, executor.model
        who = _describe(executor, origin)
        if key in _COMMON_PROPS or key in _BUILTIN_KEYS or key in self._builtins:
            raise PluginError(
                f"plugin {who}: step key {key!r} is reserved (a built-in step or a common step property)"
            )
        if key == "plugin":
            # `autobot schema` names a plugin's definition `<key>Step`, and `pluginStep` is the catch-all
            raise PluginError(
                f"plugin {who}: step key {key!r} is reserved (the schema's pluginStep definition "
                "and the plugin step type in validation errors use the name)"
            )
        # a built-in step is dispatched by its model's class: a plugin with that model would take its steps.
        # A plugin step is dispatched by its key, so plugins may share a model with each other
        if model is PluginStep:
            raise PluginError(
                f"plugin {who}, step key {key!r}: model PluginStep is the runner's own model of every plugin step; "
                "a plugin needs a model of its own"
            )
        if model in self._model_keys:
            raise PluginError(
                f"plugin {who}, step key {key!r}: model {model.__name__} is already the model of "
                f"the built-in step {self._model_keys[model]!r}; a plugin needs a model of its own"
            )
        clashes = [
            name if name == field else f"{name} (field {field!r})"
            for field, info in model.model_fields.items()
            for name in sorted(_input_names(field, info) & _COMMON_PROPS)
        ]
        if clashes:
            raise PluginError(
                f"plugin {who}, step key {key!r}: model {model.__name__} reuses common step property names, "
                f"which the runner handles and never passes to the plugin: {', '.join(clashes)}"
            )

    def _check_unique(self, executor: StepExecutor, origin: str):
        # the same plugin again (the instance, or another instance of its class and model) replaces itself;
        # any other plugin with a registered key is an error rather than a silent last-one-wins
        key = executor.key
        prev = self._executors.get(key)
        if prev is None or prev is executor or (type(prev) is type(executor) and prev.model is executor.model):
            return
        raise PluginError(
            f"plugin {_describe(executor, origin)}: step key {key!r} is already registered by plugin "
            f"{_describe(prev, self._origins.get(key, ''))}"
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
        if self._discover_error is not None:
            # a broken plugin is never skipped: every later attempt fails the same way
            raise PluginError(str(self._discover_error)) from self._discover_error
        if self._discovered:
            return
        try:
            self._load_entry_points()
        except PluginError as e:
            self._discover_error = e
            raise
        self._discovered = True

    def _load_entry_points(self):
        for ep in importlib.metadata.entry_points(group="autobot.steps"):
            try:
                obj: type[StepExecutor] = ep.load()
                executor = obj() if isinstance(obj, type) or callable(obj) else obj
            except Exception as e:
                dist = f" (distribution {ep.dist.name})" if ep.dist else ""
                why = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
                raise PluginError(f"entry point {ep.name!r}{dist} failed to load: {why}") from e
            dist = f"distribution {ep.dist.name}, " if ep.dist else ""
            self.register(executor, origin=f"{dist}entry point {ep.name!r}")

    def plugin_executors(self) -> list[StepExecutor]:
        return [e for k, e in self._executors.items() if k not in self._builtins]

    def keys(self) -> list[str]:
        return list(self._executors.keys())


def _check_shape(executor: StepExecutor, origin: str):
    # what the registry and the runner use: a usable key, a pydantic model class and a callable `execute`
    missing = object()

    def attr(name: str) -> Any:
        try:
            return getattr(executor, name, missing)
        except Exception as e:
            why = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
            who = _describe(executor, origin)
            raise PluginError(f"plugin {who}: executor's {name!r} attribute raised {why}") from e

    key = attr("key")
    model = attr("model")
    execute = attr("execute")
    if key is missing:
        why = "executor has no 'key' attribute (a non-empty string)"
    elif not isinstance(key, str) or not key:
        why = f"executor's 'key' must be a non-empty string, got {key!r}"
    elif model is missing:
        why = "executor has no 'model' attribute (a pydantic model class)"
    elif not (isinstance(model, type) and issubclass(model, pydantic.BaseModel)):
        why = f"executor's 'model' must be a pydantic model class (a pydantic.BaseModel subclass), got {model!r}"
    elif execute is missing:
        why = "executor has no 'execute' method"
    elif not callable(execute):
        why = f"executor's 'execute' must be callable, got {execute!r}"
    else:
        return
    raise PluginError(f"plugin {_describe(executor, origin)}: {why}")


def _describe(executor: StepExecutor, origin: str) -> str:
    who = f"{type(executor).__module__}.{type(executor).__qualname__}"
    return f"{who} ({origin})" if origin else who


def _input_names(field: str, info: pydantic.fields.FieldInfo) -> set[str]:
    """The keys a script could use to set a model field: its name and every alias."""
    alias = info.validation_alias
    choices = alias.choices if isinstance(alias, pydantic.AliasChoices) else [alias]
    names = {field, info.alias, *(c.path[0] if isinstance(c, pydantic.AliasPath) else c for c in choices)}
    return {n for n in names if isinstance(n, str)}


registry = StepRegistry()


def __getattr__(name: str) -> Any:
    # `from autobot import registry` gives this module: the instance's public methods are reachable on
    # it, so `registry.register(...)` registers on the instance
    if not name.startswith("_") and hasattr(StepRegistry, name):
        return getattr(registry, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
