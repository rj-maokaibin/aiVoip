"""Standalone entrypoint for the Feishu WebSocket long-connection listener.

Runs as its own small container/service (outbound-only -- works from an intranet
with no public callback URL). Uses the official lark-oapi SDK to keep the
connection alive with auto-reconnect and dispatches events through the same
handler as the HTTP callback.

Usage:
    python run_feishu_long_connection.py
"""
from __future__ import annotations

import time

from app.core.config import settings
from app.integrations.feishu.long_connection import (
    FeishuLongConnectionError,
    run_long_connection,
)


def main() -> None:
    if not settings.feishu_live_enabled:
        print("FEISHU_LIVE_DISABLED: set FEISHU_LIVE_ENABLED=true to start the listener")
        return
    print("Starting Feishu long-connection listener (Ctrl+C to stop)...")
    try:
        handle = run_long_connection()
        print("listener started; keep process alive")
        while True:
            time.sleep(1)
            if not handle.is_alive():
                print("listener thread exited unexpectedly")
                break
    except FeishuLongConnectionError as exc:
        print(f"listener failed to start: {exc}")
    except KeyboardInterrupt:
        print("listener stopped by user")


if __name__ == "__main__":
    main()
