#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from app.capture_v2.gate.context import build_asyncssh_adapter
from app.capture_v2.gate.models import GateDeviceSpec
from app.infrastructure.config_framework.executor import ConfigFrameworkExecutor
from app.infrastructure.transport.ssh import SharedSshTransport

MODULES = (
    "voice_vlan",
    "voipServInfo",
    "voipUserInfo",
    "voipFxsTbl",
    "voipAdvanced",
)


def _shape(value: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"root_type": type(value).__name__}
    current = value
    if isinstance(current, Mapping):
        result["root_keys"] = sorted(str(key) for key in current.keys())
        if "data" in current:
            current = current["data"]
    result["payload_type"] = type(current).__name__
    if isinstance(current, list):
        result["row_count"] = len(current)
        if current and isinstance(current[0], Mapping):
            result["row_keys"] = sorted(str(key) for key in current[0].keys())
    elif isinstance(current, Mapping):
        result["payload_keys"] = sorted(str(key) for key in current.keys())
        gain = current.get("dspGain")
        if isinstance(gain, Mapping):
            result["dsp_gain_keys"] = sorted(str(key) for key in gain.keys())
    return result


async def run(args: argparse.Namespace) -> dict[str, Any]:
    spec = GateDeviceSpec(
        device_id=args.device_id,
        model=args.model,
        host=args.host,
        port=args.port,
        username=args.username,
        platform_id=args.platform_id,
    )
    adapter = build_asyncssh_adapter(spec, password_env=args.password_env)
    transport = SharedSshTransport(adapter)
    shapes: dict[str, Any] = {}
    await transport.connect()
    try:
        executor = ConfigFrameworkExecutor(transport, allowed_modules=MODULES)
        for module in MODULES:
            result = await executor.get(module, timeout=20.0)
            if not result.success:
                raise RuntimeError(f"VOIP_CONFIG_SHAPE_GET_FAILED:{module}:{result.rcode}")
            raw = result.raw if isinstance(result.raw, Mapping) else result.data
            shapes[module] = _shape(raw)
    finally:
        await transport.disconnect()
    return {
        "schema": "voip-config-shape-v1",
        "mutation_executed": False,
        "secret_values_emitted": False,
        "transport": "ssh",
        "backend": "config_framework",
        "operation": "get",
        "modules": shapes,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only current VOIP config shape probe")
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--username", default="root")
    parser.add_argument("--platform-id", default=None)
    parser.add_argument("--password-env", default="ENV:SIP_ABA_SSH_PASSWORD")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    payload = asyncio.run(run(args))
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "VOIP_CONFIG_SHAPE_PROBE": "PASS",
        "module_count": len(payload["modules"]),
        "mutation_executed": False,
        "secret_values_emitted": False,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
