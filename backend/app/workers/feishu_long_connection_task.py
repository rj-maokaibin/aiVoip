"""Hard-disabled compatibility shim for the removed Feishu Celery listener.

The production Feishu WebSocket consumer is exclusively the standalone
``feishu-long-connection`` compose service.  This module intentionally does not
register a Celery task and must never import or call ``run_long_connection``.

It exists only so historical imports fail closed instead of crashing while old
callers/tests are phased out.  Calling ``feishu_long_connection.apply()`` can
never start a WebSocket consumer.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core.config import settings


@dataclass(frozen=True)
class _ImmediateResult:
    value: dict[str, Any]

    def get(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.value


class _RemovedFeishuLongConnectionTask:
    """Fail-closed compatibility surface; deliberately not a Celery task."""

    name = "feishu.long_connection.removed"

    def apply(self, *args: Any, **kwargs: Any) -> _ImmediateResult:
        if not settings.feishu_live_enabled:
            return _ImmediateResult({"status": "SKIPPED", "reason": "FEISHU_LIVE_DISABLED"})
        return _ImmediateResult({
            "status": "FAILED",
            "reason": "LEGACY_FEISHU_LONG_CONNECTION_REMOVED",
        })


feishu_long_connection = _RemovedFeishuLongConnectionTask()
