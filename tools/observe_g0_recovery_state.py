#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone

from app.automation.gates.golden_cfg_config import G0_MODULE, safe_readback
from app.capture_v2.db_models import CaptureLease
from app.capture_v2.gate.context import build_asyncssh_adapter
from app.capture_v2.gate.models import GateDeviceSpec
from app.db.session import SessionLocal
from app.infrastructure.config_framework.executor import ConfigFrameworkExecutor
from app.infrastructure.transport.ssh import SharedSshTransport


def _aware(value):
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


async def _observe(args) -> dict:
    spec = GateDeviceSpec(
        device_id=args.device_id,
        model=args.model,
        host=args.host,
        port=args.port,
        username=args.username,
        platform_id=args.platform_id,
    )
    adapter = build_asyncssh_adapter(spec, password_env=args.password_env)
    config = ConfigFrameworkExecutor(
        SharedSshTransport(adapter),
        allowed_modules=(G0_MODULE,),
    )
    await adapter.connect()
    try:
        result = await config.get(G0_MODULE, timeout=args.command_timeout)
    finally:
        await adapter.disconnect()
    if not result.success:
        raise RuntimeError(f"G0_RECOVERY_READ_FAILED:{result.rcode}")

    sanitized = safe_readback(result)
    rows = sanitized.get("data") if isinstance(sanitized, dict) else None
    first = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else {}
    current_disname = str(first.get("disName") or "")

    with SessionLocal() as db:
        lease = db.get(CaptureLease, args.device_id)
        if lease is None:
            lease_state = None
            lease_epoch = None
            lease_expired = None
        else:
            lease_state = lease.state
            lease_epoch = int(lease.lease_epoch)
            expires_at = _aware(lease.expires_at)
            lease_expired = bool(expires_at is None or expires_at <= datetime.now(timezone.utc))

    return {
        "schema": "g0-recovery-observe-v1",
        "read_only": True,
        "mutation_executed": False,
        "device_id": args.device_id,
        "module": G0_MODULE,
        "current_disname": current_disname,
        "matches_failed_probe_marker": current_disname == args.failed_probe_marker,
        "lease": {
            "state": lease_state,
            "epoch": lease_epoch,
            "expired": lease_expired,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only G0 recovery state observer")
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--username", default="root")
    parser.add_argument("--platform-id", default=None)
    parser.add_argument("--password-env", required=True)
    parser.add_argument("--command-timeout", type=float, default=20.0)
    parser.add_argument("--failed-probe-marker", default="G0")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    payload = asyncio.run(_observe(args))
    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
