"""The step keys the models and the registry both know: a leaf, so neither imports the other for them."""

from __future__ import annotations

# the built-in step types, in the order a step's key is looked for
BUILTIN_KEYS = ("cmd", "sleep", "call", "block", "line", "return", "control", "set")
# what the runner handles on every step, a plugin step included, and never passes to a plugin's model
COMMON_PROPS = frozenset({"after", "when", "delay_before", "delay_after", "timeout", "plugin_key_"})
