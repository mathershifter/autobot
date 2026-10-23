"""Durations as the script writes them: a number of seconds, or `5s`, `500ms`, `2m`, `1h` (see SPEC, Duration
Format)."""

from __future__ import annotations

import math
import re
import sys
from typing import Any

DURATION_RE = re.compile(r"^([0-9]+(?:\.[0-9]+)?)(ms|s|m|h)$")
DURATION_MULT = {"ms": 0.001, "s": 1, "m": 60, "h": 3600}
DURATION_MAX = sys.float_info.max


def parse_duration(value: Any) -> float:
    if value is None:
        raise ValueError("invalid duration: null")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # compared before the conversion, which rounds: the schema's `maximum` is the largest double
        if value != value or abs(value) > DURATION_MAX:
            raise ValueError(f"invalid duration: {value} (not a finite number)")
        seconds = float(value)
        if seconds < 0:
            raise ValueError(f"invalid duration: {value}")
        return seconds
    m = DURATION_RE.fullmatch(str(value))
    if m:
        seconds = float(m.group(1)) * DURATION_MULT[m.group(2)]
        if not math.isfinite(seconds):
            raise ValueError(f"invalid duration: {value} (not a finite number)")
        return seconds
    # a boolean as YAML writes it (`types.text`, which can't be imported here: `types` builds `Duration` from this)
    raise ValueError(f"invalid duration: {str(value).lower() if isinstance(value, bool) else value}")
