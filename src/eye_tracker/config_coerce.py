"""Per-type coercion of raw settings values (TD-CPLX-012).

Extracted from :mod:`eye_tracker.config`: one small function per default type (bool, number,
string, list) selected by the setting's default. The module keeps the settings logger name, so
warnings still appear as ``eye_tracker.config`` records.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from typing import Any

# The settings logger: the split must not move the log records.
log = logging.getLogger("eye_tracker.config")


def _coerce(key: str, value: Any, default: Any, meta: Any) -> Any:
    """Convert one raw settings value; the default's type decides the rules."""
    for expected, coerce in _COERCERS:
        if isinstance(default, expected):
            return coerce(key, value, default, meta)
    if isinstance(default, (int, float)):
        return _coerce_number(key, value, default, meta)
    return value


def _coerce_bool(key: str, value: Any, default: Any, meta: Any) -> Any:
    if isinstance(value, bool):
        return value
    log.warning("Setting %s expects true/false; using default %r", key, default)
    return default


def _coerce_number(key: str, value: Any, default: Any, meta: Any) -> Any:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        log.warning("Setting %s expects a number; using default %r", key, default)
        return default
    num: float = float(value)
    if math.isnan(num) or math.isinf(num):
        log.warning("Setting %s must be finite; using default %r", key, default)
        return default
    lo, hi = meta.get("lo"), meta.get("hi")
    if lo is not None and num < lo:
        log.warning("Setting %s=%r below minimum %r; clamped", key, value, lo)
        num = float(lo)
    if hi is not None and num > hi:
        log.warning("Setting %s=%r above maximum %r; clamped", key, value, hi)
        num = float(hi)
    return round(num) if isinstance(default, int) else num


def _coerce_str(key: str, value: Any, default: Any, meta: Any) -> Any:
    if not isinstance(value, str):
        log.warning("Setting %s expects a string; using default %r", key, default)
        return default
    choices = meta.get("choices")
    if choices and value not in choices:
        log.warning("Setting %s=%r not one of %s; using default", key, value, choices)
        return default
    return value


def _coerce_list(key: str, value: Any, default: Any, meta: Any) -> Any:
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return [v for v in value if v.strip()]
    log.warning("Setting %s expects a list of strings; using default", key)
    return list(default)


#: Coercers for non-numeric setting types; the default's type selects one in
#: :func:`_coerce`. Numbers are handled separately (``bool`` is not a number here).
_COERCERS: tuple[tuple[type[Any], Callable[..., Any]], ...] = (
    (bool, _coerce_bool),
    (str, _coerce_str),
    (list, _coerce_list),
)
