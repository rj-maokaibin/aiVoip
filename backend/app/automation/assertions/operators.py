from __future__ import annotations

from datetime import datetime
import re
from typing import Any, Mapping

from app.automation.assertions.resolver import MISSING


class AssertionOperatorError(ValueError):
    pass


def _in_range(actual: Any, expected: Any) -> bool:
    if isinstance(expected, (list, tuple)) and len(expected) == 2:
        lower, upper = expected
    elif isinstance(expected, Mapping) and {"min", "max"} <= set(expected):
        lower, upper = expected["min"], expected["max"]
    else:
        raise AssertionOperatorError("RANGE_EXPECTS_MIN_MAX")
    return lower <= actual <= upper


def _duration_seconds(actual: Any) -> float:
    if isinstance(actual, (int, float)) and not isinstance(actual, bool):
        return float(actual)
    if isinstance(actual, Mapping):
        if "duration_seconds" in actual:
            return float(actual["duration_seconds"])
        if "start" in actual and "end" in actual:
            start = actual["start"]
            end = actual["end"]
            if isinstance(start, str):
                start = datetime.fromisoformat(start.replace("Z", "+00:00"))
            if isinstance(end, str):
                end = datetime.fromisoformat(end.replace("Z", "+00:00"))
            if isinstance(start, datetime) and isinstance(end, datetime):
                return (end - start).total_seconds()
    raise AssertionOperatorError("DURATION_ACTUAL_UNSUPPORTED")


def evaluate_operator(operator: str, actual: Any, expected: Any = None) -> bool:
    if operator == "exists":
        return actual is not MISSING
    if operator == "not_exists":
        return actual is MISSING
    if actual is MISSING:
        raise AssertionOperatorError("ACTUAL_MISSING")
    try:
        if operator == "eq":
            return actual == expected
        if operator == "ne":
            return actual != expected
        if operator == "contains":
            return expected in actual
        if operator == "regex":
            return re.search(str(expected), str(actual)) is not None
        if operator == "gt":
            return actual > expected
        if operator == "lt":
            return actual < expected
        if operator == "range":
            return _in_range(actual, expected)
        if operator == "count":
            count = len(actual) if not isinstance(actual, (int, float)) else actual
            if isinstance(expected, (list, tuple, Mapping)):
                return _in_range(count, expected)
            return count == expected
        if operator == "duration":
            duration = _duration_seconds(actual)
            if isinstance(expected, (list, tuple, Mapping)):
                return _in_range(duration, expected)
            return duration == float(expected)
    except AssertionOperatorError:
        raise
    except Exception as exc:
        raise AssertionOperatorError(
            f"OPERATOR_EVALUATION_ERROR:{operator}:{type(exc).__name__}"
        ) from exc
    raise AssertionOperatorError(f"UNKNOWN_OPERATOR:{operator}")
