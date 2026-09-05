#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import re
from datetime import datetime, timezone
from typing import Any

from app.automation.gates.golden_cfg_config import G0_MODULE, safe_readback
from app.capture_v2.db_models import CaptureLease
from app.capture_v2.gate.context import build_asyncssh_adapter
from app.capture_v2.gate.models import GateDeviceSpec
from app.db.session import SessionLocal
from app.infrastructure.config_framework.executor import ConfigFrameworkExecutor
from app.infrastructure.transport.ssh import SharedSshTransport


_HISTORY_FILES = ("/tmp/voip_ipc_cli_log.txt", "/tmp/voip_log.txt")
_HISTORY_RE = re.compile(r"^(?P<path>/tmp/[^:]+):(?P<line>\d+):.*?\"disName\"\s*:\s*\"(?P<value>[^\"]*)\"")


def _aware(value):
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _history_command() -> str:
    # Read only the two documented VOIP runtime logs and emit only the disName
    # JSON fragment plus line number. The command never prints passwd/authId or
    # an original log line, so the runner cannot accidentally persist secrets.
    files = " ".join(_HISTORY_FILES)
    return (
        "for f in " + files + "; do "
        "[ -r \"$f\" ] || continue; "
        "grep -n -o -E '\"disName\"[[:space:]]*:[[:space:]]*\"[^\"]*\"' \"$f\" 2>/dev/null "
        "| tail -n 80 | sed \"s#^#$f:#\"; "
        "done"
    )


def parse_historical_disnames(stdout: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in stdout.splitlines():
        match = _HISTORY_RE.match(raw.strip())
        if not match:
            continue
        path = match.group("path")
        if path not in _HISTORY_FILES:
            continue
        rows.append({
            "source": path,
            "line": int(match.group("line")),
            "disName": match.group("value"),
        })
    return rows


def derive_recovery_candidate(
    rows: list[dict[str, Any]],
    *,
    failed_probe_marker: str,
) -> dict[str, Any]:
    per_source: dict[str, str] = {}
    for source in _HISTORY_FILES:
        source_rows = [row for row in rows if row.get("source") == source]
        if not source_rows:
            continue
        source_rows.sort(key=lambda row: int(row.get("line") or 0))
        marker_indexes = [
            index for index, row in enumerate(source_rows)
            if str(row.get("disName") or "") == failed_probe_marker
        ]
        if not marker_indexes:
            continue
        last_marker = marker_indexes[-1]
        prior = [
            str(row.get("disName") or "")
            for row in source_rows[:last_marker]
            if str(row.get("disName") or "") != failed_probe_marker
        ]
        if prior:
            per_source[source] = prior[-1]

    values = set(per_source.values())
    consensus = next(iter(values)) if len(values) == 1 and per_source else None
    return {
        "sources_with_prior": len(per_source),
        "per_source": per_source,
        "consensus": consensus,
        "confidence": (
            "HIGH" if consensus is not None and len(per_source) == len(_HISTORY_FILES)
            else "MEDIUM" if consensus is not None
            else "NONE"
        ),
    }


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
        history_result = await adapter.execute_shell(
            _history_command(),
            timeout=args.command_timeout,
            retries=1,
        )
    finally:
        await adapter.disconnect()
    if not result.success:
        raise RuntimeError(f"G0_RECOVERY_READ_FAILED:{result.rcode}")
    if history_result.exit_status not in (0, 1):
        raise RuntimeError(f"G0_RECOVERY_HISTORY_READ_FAILED:{history_result.exit_status}")

    sanitized = safe_readback(result)
    rows = sanitized.get("data") if isinstance(sanitized, dict) else None
    first = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else {}
    current_disname = str(first.get("disName") or "")
    history = parse_historical_disnames(history_result.stdout or "")
    candidate = derive_recovery_candidate(
        history,
        failed_probe_marker=args.failed_probe_marker,
    )

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
        "schema": "g0-recovery-observe-v2",
        "read_only": True,
        "mutation_executed": False,
        "credential_value_persisted": False,
        "raw_log_lines_persisted": False,
        "device_id": args.device_id,
        "module": G0_MODULE,
        "current_disname": current_disname,
        "matches_failed_probe_marker": current_disname == args.failed_probe_marker,
        "history": {
            "documented_sources": list(_HISTORY_FILES),
            "entries": history,
            "recovery_candidate": candidate,
        },
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
