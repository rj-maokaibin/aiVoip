from __future__ import annotations

from fastapi import APIRouter, HTTPException
from redis import Redis
from sqlalchemy import text

from app.core.config import settings
from app.db.session import engine
from app.integrations.storage import ObjectStorage

router = APIRouter(tags=["health"])


def _check_db() -> dict:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"status": "ok"}
    except Exception as exc:  # pragma: no cover - exercised in full-stack test
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}


def _check_redis() -> dict:
    client = Redis.from_url(settings.redis_url, socket_connect_timeout=2, socket_timeout=2)
    try:
        pong = client.ping()
        return {"status": "ok" if pong else "error"}
    except Exception as exc:  # pragma: no cover
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
    finally:
        try:
            client.close()
        except Exception:
            pass


def _check_minio() -> dict:
    try:
        storage = ObjectStorage()
        storage.ensure_bucket()
        return {"status": "ok", "bucket": storage.bucket}
    except Exception as exc:  # pragma: no cover
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}


@router.get("/health/live")
def live():
    return {"status": "ok", "service": "backend", "version": settings.app_version, "build_revision": settings.build_revision}


@router.get("/health/ready")
def ready():
    checks = {
        "postgres": _check_db(),
        "redis": _check_redis(),
        "minio": _check_minio(),
    }
    ok = all(item.get("status") == "ok" for item in checks.values())
    payload = {"status": "ok" if ok else "not_ready", "version": settings.app_version, "build_revision": settings.build_revision, "checks": checks}
    if not ok:
        raise HTTPException(status_code=503, detail=payload)
    return payload
