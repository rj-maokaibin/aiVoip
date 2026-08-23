from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolate_live_feishu_side_effects(monkeypatch):
    """Keep unit tests deterministic even when the runner loads a real .env.

    The self-hosted validation runner intentionally loads production-like settings
    such as REPRODUCTION_PLATFORM_MODE=real.  Unit tests must not, however, send
    real Feishu replies or enqueue Celery tasks merely because FEISHU_LIVE_ENABLED
    is true in that runner environment.  Tests that explicitly verify live Feishu
    behaviour can opt in by setting settings.feishu_live_enabled=True themselves.
    """
    from app.core.config import settings

    monkeypatch.setattr(settings, "feishu_live_enabled", False)
