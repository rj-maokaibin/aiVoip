#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx
from redis import Redis
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
TOOLS = ROOT / "tools"
for p in (BACKEND, TOOLS):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from app.capture_v2.runtime import capture_authority_mode  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.integrations.storage import ObjectStorage  # noqa: E402
from app.production_config import production_config_readiness  # noqa: E402
from app.workers.celery_app import celery_app  # noqa: E402
from release_evidence import evidence_envelope  # noqa: E402


def check(name: str, fn):
    try:
        detail = fn()
        return {"key": name, "status": "PASS", "detail": detail}
    except Exception as exc:
        return {"key": name, "status": "FAIL", "detail": f"{type(exc).__name__}: {exc}"}


def backend_health() -> dict[str, Any]:
    with httpx.Client(timeout=8.0) as client:
        live = client.get("http://backend:8000/health/live")
        live.raise_for_status()
        ready = client.get("http://backend:8000/health/ready")
        ready.raise_for_status()
        lp, rp = live.json(), ready.json()
    expected = str(settings.build_revision)
    if lp.get("build_revision") != expected or rp.get("build_revision") != expected:
        raise RuntimeError(f"build revision mismatch: live={lp.get('build_revision')} ready={rp.get('build_revision')} expected={expected}")
    return {"live": lp, "ready": rp}


def frontend_health() -> dict[str, Any]:
    with httpx.Client(timeout=8.0) as client:
        r = client.get("http://frontend/")
        r.raise_for_status()
        if "<html" not in r.text.lower():
            raise RuntimeError("frontend response is not HTML")
        # Verify same-origin nginx proxy reaches the protected backend. An unauthenticated
        # request must be rejected by backend auth rather than falling back to SPA HTML.
        api = client.get("http://frontend/api/v1/cases")
        if api.status_code not in {401, 403}:
            raise RuntimeError(f"same-origin API proxy did not reach protected backend: status={api.status_code}")
    return {"status_code": r.status_code, "bytes": len(r.content), "api_proxy_status": api.status_code}


def postgres_migration() -> dict[str, Any]:
    # Importing the migration gate here computes the contract head from the exact mounted source.
    import subprocess
    cp = subprocess.run([sys.executable, "tools/migration_contract_gate.py"], cwd=ROOT, text=True, capture_output=True, check=True)
    expected = json.loads(cp.stdout)["heads"][0]
    engine = create_engine(settings.database_url)
    with engine.connect() as conn:
        actual = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        conn.execute(text("SELECT 1"))
    engine.dispose()
    if actual != expected:
        raise RuntimeError(f"alembic head mismatch actual={actual} expected={expected}")
    return {"actual_head": actual, "expected_head": expected}


def redis_health() -> dict[str, Any]:
    r = Redis.from_url(settings.redis_url, socket_timeout=3, socket_connect_timeout=3)
    try:
        if not r.ping():
            raise RuntimeError("redis PING returned false")
        return {"ping": True}
    finally:
        r.close()


def minio_probe() -> dict[str, Any]:
    storage = ObjectStorage()
    storage.ensure_bucket()
    result = storage.probe(read_write=True)
    if result.get("status") != "ok" or result.get("read_write") is not True:
        raise RuntimeError(str(result))
    return result


def celery_queues() -> dict[str, Any]:
    # Capture V2 requires three distinct serialized reproduction queues. The old
    # verifier checked a non-existent generic "reproduction" queue and therefore
    # could not prove that cancel/recovery/watch paths were actually deployable.
    required = {
        "collector",
        "packet",
        "pcm",
        "media",
        "diagnosis",
        "reproduction-control",
        "reproduction-control-high",
        "reproduction-watch",
    }
    insp = celery_app.control.inspect(timeout=8)
    pings = insp.ping() or {}
    queues = insp.active_queues() or {}
    if not pings:
        raise RuntimeError("no Celery workers responded to ping")
    seen: set[str] = set()
    for rows in queues.values():
        for row in rows or []:
            if row.get("name"):
                seen.add(str(row["name"]))
    missing = sorted(required - seen)
    if missing:
        raise RuntimeError(f"required queues unavailable: {missing}; seen={sorted(seen)}")
    return {"workers": sorted(pings), "queues": sorted(seen), "required": sorted(required)}


def production_config() -> dict[str, Any]:
    payload = production_config_readiness()
    if payload.get("status") != "PASS":
        blockers = [x["key"] for x in payload.get("items", []) if x.get("status") != "PASS"]
        raise RuntimeError(f"production config blockers: {blockers}")
    return payload


def reproduction_platform() -> dict[str, Any]:
    mode = str(settings.reproduction_platform_mode or "mock").strip().lower()
    if mode in {"", "mock", "pending"}:
        raise RuntimeError(f"real production reproduction platform required; observed={mode}")
    return {"mode": mode}


def capture_authority() -> dict[str, Any]:
    configured = str(settings.capture_engine_version or "V1").strip().upper()
    observed = capture_authority_mode()
    expected = "V2" if configured == "V2" else "V1"
    if observed != expected:
        raise RuntimeError(f"capture authority mismatch configured={configured} observed={observed}")
    if configured == "V2" and not bool(settings.capture_v2_production_enabled):
        raise RuntimeError("V2 selected without production enable")
    return {
        "configured_engine": configured,
        "production_enabled": bool(settings.capture_v2_production_enabled),
        "authority_mode": observed,
    }


def main() -> int:
    out = Path(os.getenv("PRODUCTION_RUNTIME_EVIDENCE", str(ROOT / "validation" / "production_runtime_result.json")))
    checks = [
        check("BACKEND_HEALTH", backend_health),
        check("FRONTEND_AND_API_PROXY", frontend_health),
        check("POSTGRES_MIGRATION", postgres_migration),
        check("REDIS", redis_health),
        check("MINIO_READ_WRITE", minio_probe),
        check("CELERY_QUEUES", celery_queues),
        check("PRODUCTION_CONFIG", production_config),
        check("REPRODUCTION_PLATFORM", reproduction_platform),
        check("CAPTURE_AUTHORITY", capture_authority),
    ]
    passed = all(x["status"] == "PASS" for x in checks)
    payload = evidence_envelope(evidence_type="PRODUCTION_DEPLOYMENT_RUNTIME", payload={
        "passed": passed,
        "build_revision": settings.build_revision,
        "checks_passed": sum(x["status"] == "PASS" for x in checks),
        "checks_total": len(checks),
        "checks": checks,
    }, root=ROOT)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
