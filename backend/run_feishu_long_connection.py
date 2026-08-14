"""Standalone entrypoint for the Feishu WebSocket long-connection listener.

Runs as its own small container/service (outbound-only -- works from an intranet
with no public callback URL). Keeps the long connection alive with auto-reconnect
and dispatches events through the same handler as the HTTP callback.

Usage:
    python run_feishu_long_connection.py
"""
from __future__ import annotations

import asyncio

from app.core.config import settings
from app.integrations.feishu.long_connection import run_long_connection


def main() -> None:
    if not settings.feishu_live_enabled:
        print("FEISHU_LIVE_DISABLED: set FEISHU_LIVE_ENABLED=true to start the listener")
        return
    print("Starting Feishu long-connection listener (Ctrl+C to stop)...")
    try:
        reconnects = asyncio.run(run_long_connection())
        print(f"listener stopped after {reconnects} reconnects")
    except KeyboardInterrupt:
        print("listener stopped by user")


if __name__ == "__main__":
    main()
