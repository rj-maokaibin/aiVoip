#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import socket
import subprocess
from pathlib import Path
from typing import Any

from sqlalchemy.engine import make_url


def _docker_containers() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    ps = subprocess.check_output(
        ["docker", "ps", "--format", "{{.ID}}\t{{.Image}}\t{{.Names}}"], text=True
    )
    for line in ps.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        cid, image, name = parts
        data = json.loads(subprocess.check_output(["docker", "inspect", cid], text=True))[0]
        env: dict[str, str] = {}
        for item in ((data.get("Config") or {}).get("Env") or []):
            key, sep, value = str(item).partition("=")
            if sep:
                env[key] = value
        networks = (data.get("NetworkSettings") or {}).get("Networks") or {}
        rows.append({"id": cid, "image": image, "name": name, "env": env, "networks": networks})
    return rows


def _resolve_database(containers: list[dict[str, Any]], output_root: Path, runtime_root: Path) -> str:
    raw_url = os.getenv("DATABASE_URL", "").strip()
    db_source = "runner-env" if raw_url else ""
    source_containers: list[dict[str, Any]] = []

    if raw_url:
        source_containers = [c for c in containers if c["env"].get("DATABASE_URL") == raw_url]
    else:
        db_containers = [c for c in containers if str(c["env"].get("DATABASE_URL") or "").strip()]
        unique_urls = {str(c["env"]["DATABASE_URL"]).strip() for c in db_containers}
        sanitized = [
            {
                "name": c["name"],
                "image": c["image"],
                "has_database_url": bool(c["env"].get("DATABASE_URL")),
                "reproduction_mode": c["env"].get("REPRODUCTION_PLATFORM_MODE"),
                "app_env": c["env"].get("APP_ENV"),
            }
            for c in containers
        ]
        (output_root / "runtime_container_inventory.json").write_text(
            json.dumps({"containers": sanitized}, indent=2) + "\n", encoding="utf-8"
        )
        if len(unique_urls) != 1:
            raise SystemExit(
                f"SIP_ABA_DATABASE_SOURCE_NOT_UNIQUE containers_with_db={len(db_containers)} "
                f"unique_urls={len(unique_urls)}"
            )
        raw_url = next(iter(unique_urls))
        source_containers = [
            c for c in db_containers if str(c["env"]["DATABASE_URL"]).strip() == raw_url
        ]
        db_source = "docker-env-consensus"

    url = make_url(raw_url)
    db_host = str(url.host or "").strip()
    db_port = int(url.port or 5432)
    if not db_host:
        raise SystemExit("SIP_ABA_DATABASE_HOST_MISSING")

    resolved_via = "native-dns"
    resolved_host = db_host
    target_name = None
    target_network = None
    try:
        socket.getaddrinfo(db_host, db_port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        source_networks = {
            network
            for c in source_containers
            for network in (c.get("networks") or {}).keys()
        }
        matches: list[tuple[str, str, str]] = []
        for container in containers:
            for network, info in (container.get("networks") or {}).items():
                if source_networks and network not in source_networks:
                    continue
                aliases = set((info or {}).get("Aliases") or [])
                ip = str((info or {}).get("IPAddress") or "").strip()
                if ip and (db_host == container["name"] or db_host in aliases):
                    matches.append((container["name"], network, ip))
        unique_matches = set(matches)
        if len(unique_matches) != 1:
            raise SystemExit(
                f"SIP_ABA_DATABASE_TARGET_NOT_UNIQUE db_host={db_host} "
                f"match_count={len(unique_matches)}"
            )
        target_name, target_network, resolved_host = next(iter(unique_matches))
        url = url.set(host=resolved_host)
        resolved_via = "docker-shared-network-ip"

    runtime_url = url.render_as_string(hide_password=False)
    db_runtime = runtime_root / "db_runtime.env"
    db_runtime.write_text(f"DATABASE_URL={shlex.quote(runtime_url)}\n", encoding="utf-8")
    db_runtime.chmod(0o600)
    os.environ["DATABASE_URL"] = runtime_url

    (output_root / "db_resolution.json").write_text(
        json.dumps(
            {
                "source": db_source,
                "source_container_count": len(source_containers),
                "source_containers": [c["name"] for c in source_containers],
                "original_host": db_host,
                "port": db_port,
                "resolved_via": resolved_via,
                "target_container": target_name,
                "target_network": target_network,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return db_source


def _resolve_dut(output_root: Path, runtime_root: Path, db_source: str) -> None:
    # Import after DATABASE_URL is made host-reachable.
    from sqlalchemy import select

    from app.db.models import CaseDevice, DeviceCredential, ReproductionSession
    from app.db.session import SessionLocal

    selector_id = next(
        (
            os.getenv(key, "").strip()
            for key in ("SIP_ABA_DEVICE_ID", "REAL_DUT_DEVICE_ID", "CAPTURE_GATE_DEVICE_ID")
            if os.getenv(key, "").strip()
        ),
        "",
    )
    selector_sn = next(
        (
            os.getenv(key, "").strip()
            for key in ("SIP_ABA_DEVICE_SN", "REAL_DUT_DEVICE_SN", "CAPTURE_GATE_DEVICE_SN")
            if os.getenv(key, "").strip()
        ),
        "",
    )

    with SessionLocal() as db:
        devices = list(db.scalars(select(CaseDevice).order_by(CaseDevice.created_at.desc())).all())
        eligible: list[tuple[Any, Any, str]] = []
        for device in devices:
            template = db.scalar(
                select(ReproductionSession)
                .where(ReproductionSession.device_id == device.id)
                .order_by(ReproductionSession.created_at.desc())
                .limit(1)
            )
            credential = db.scalar(select(DeviceCredential).where(DeviceCredential.sn == device.sn))
            info = dict(device.device_info or {})
            model = str(info.get("model") or info.get("product_model") or device.platform_id or "").strip()
            if (
                template is not None
                and credential is not None
                and str(credential.password or "")
                and str(device.ip or "").strip()
                and str(device.sn or "").strip()
                and model
            ):
                eligible.append((device, template, model))

        if selector_id:
            selected = [row for row in eligible if str(row[0].id) == selector_id]
        elif selector_sn:
            selected = [row for row in eligible if str(row[0].sn) == selector_sn]
        else:
            selected = eligible

        inventory = []
        for device, template, model in selected:
            inventory.append(
                {
                    "device_id": str(device.id),
                    "sn_sha256_12": hashlib.sha256(str(device.sn).encode()).hexdigest()[:12],
                    "model": model,
                    "platform_id": device.platform_id,
                    "device_created_at": device.created_at.isoformat() if device.created_at else None,
                    "latest_reproduction_session_id": str(template.id),
                    "latest_reproduction_created_at": (
                        template.created_at.isoformat() if template.created_at else None
                    ),
                }
            )
        (output_root / "dut_candidates.json").write_text(
            json.dumps(
                {
                    "selector_id_present": bool(selector_id),
                    "selector_sn_present": bool(selector_sn),
                    "candidate_count": len(selected),
                    "candidates": inventory,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        if len(selected) != 1:
            raise SystemExit(f"SIP_ABA_DUT_SELECTION_NOT_UNIQUE count={len(selected)}")

        device, template, model = selected[0]
        values = {
            "DEVICE_ID": str(device.id),
            "DEVICE_SN": str(device.sn),
            "DEVICE_HOST": str(device.ip),
            "DEVICE_PORT": str(int(device.ssh_port or 22)),
            "DEVICE_USER": str(device.username or "admin"),
            "DEVICE_MODEL": model,
            "DEVICE_PLATFORM": str(device.platform_id or ""),
        }
        dut_runtime = runtime_root / "resolved_dut.env"
        dut_runtime.write_text(
            "".join(f"{key}={shlex.quote(value)}\n" for key, value in values.items()), encoding="utf-8"
        )
        dut_runtime.chmod(0o600)
        print(
            json.dumps(
                {
                    "database_source": db_source,
                    "device_id": str(device.id),
                    "model": model,
                    "platform_id": device.platform_id,
                    "reproduction_template": str(template.id),
                },
                sort_keys=True,
            )
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--runtime-root", required=True)
    args = parser.parse_args()

    output_root = Path(args.output_root)
    runtime_root = Path(args.runtime_root)
    output_root.mkdir(parents=True, exist_ok=True)
    runtime_root.mkdir(parents=True, exist_ok=True)
    runtime_root.chmod(0o700)

    containers = _docker_containers()
    db_source = _resolve_database(containers, output_root, runtime_root)
    _resolve_dut(output_root, runtime_root, db_source)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
