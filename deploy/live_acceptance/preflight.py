#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib
import importlib.metadata
import json
import os
import socket
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import quote, urlparse

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONTRACT = ROOT / "deploy/live_acceptance/runtime_contract.json"


@dataclass
class Check:
    key: str
    category: str
    status: str
    detail: str
    blocks_mutation: bool = True


class Collector:
    def __init__(self) -> None:
        self.checks: list[Check] = []

    def pass_(self, key: str, category: str, detail: str) -> None:
        self.checks.append(Check(key, category, "PASS", detail, True))

    def block(self, key: str, category: str, detail: str) -> None:
        self.checks.append(Check(key, category, "BLOCKED", detail, True))

    @property
    def blocking_keys(self) -> list[str]:
        return [item.key for item in self.checks if item.blocks_mutation and item.status != "PASS"]


def _load_contract(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("contract") != "voip-live-acceptance-runtime-v1":
        raise RuntimeError("LIVE_ACCEPTANCE_RUNTIME_CONTRACT_INVALID")
    return data


def _fingerprint(value: str | None) -> str | None:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:12] if value else None


def _pinned_requirements(path: Path) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "==" not in line:
            continue
        name, version = line.split("==", 1)
        rows.append((name.split("[", 1)[0].strip(), version.strip()))
    return rows


def _check_runtime(contract: dict, collector: Collector) -> None:
    expected_python = str(contract.get("python_major_minor") or "")
    actual_python = f"{os.sys.version_info.major}.{os.sys.version_info.minor}"
    if actual_python == expected_python:
        collector.pass_("PYTHON_ABI", "RUNTIME", f"python={actual_python}")
    else:
        collector.block("PYTHON_ABI", "RUNTIME", f"expected {expected_python}, got {actual_python}")

    requirements_path = ROOT / str(contract.get("requirements_file") or "backend/requirements.txt")
    mismatches: list[str] = []
    for name, expected in _pinned_requirements(requirements_path):
        try:
            actual = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            mismatches.append(f"{name}=MISSING(expected {expected})")
            continue
        if actual != expected:
            mismatches.append(f"{name}={actual}(expected {expected})")
    if mismatches:
        collector.block("PINNED_PYTHON_DEPENDENCIES", "RUNTIME", "; ".join(mismatches[:20]))
    else:
        collector.pass_("PINNED_PYTHON_DEPENDENCIES", "RUNTIME", "all pinned backend requirements match")

    failed_imports: list[str] = []
    for module in contract.get("required_imports") or []:
        try:
            importlib.import_module(str(module))
        except Exception as exc:
            failed_imports.append(f"{module}:{type(exc).__name__}")
    if failed_imports:
        collector.block("REQUIRED_IMPORTS", "RUNTIME", "; ".join(failed_imports))
    else:
        collector.pass_("REQUIRED_IMPORTS", "RUNTIME", "all required live modules import")

    try:
        result = subprocess.run(["fc-list", ":", "family"], text=True, capture_output=True, timeout=10, check=True)
        families = result.stdout
        markers = [str(x) for x in contract.get("font_family_markers") or []]
        found = next((marker for marker in markers if marker.lower() in families.lower()), None)
        if found:
            collector.pass_("CJK_FONT_RUNTIME", "RUNTIME", f"CJK family available: {found}")
        else:
            collector.block("CJK_FONT_RUNTIME", "RUNTIME", "no approved CJK font family found")
    except Exception as exc:
        collector.block("CJK_FONT_RUNTIME", "RUNTIME", f"font discovery failed: {type(exc).__name__}")

    contract_env = os.getenv("LIVE_ACCEPTANCE_RUNTIME_CONTRACT", "")
    fingerprint = os.getenv("LIVE_ACCEPTANCE_RUNTIME_FINGERPRINT", "")
    source_revision = os.getenv("LIVE_ACCEPTANCE_SOURCE_REVISION", "")
    if contract_env == contract.get("contract") and fingerprint and source_revision:
        collector.pass_("RUNTIME_IDENTITY", "RUNTIME", f"contract={contract_env}; fingerprint={fingerprint[:16]}; revision={source_revision[:12]}")
    else:
        collector.block("RUNTIME_IDENTITY", "RUNTIME", "runtime contract/fingerprint/source revision is incomplete")
    try:
        git_head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        if source_revision and git_head == source_revision:
            collector.pass_("SOURCE_REVISION_EXACT", "RUNTIME", git_head)
        else:
            collector.block("SOURCE_REVISION_EXACT", "RUNTIME", f"runtime={source_revision}; workspace={git_head}")
    except Exception as exc:
        collector.block("SOURCE_REVISION_EXACT", "RUNTIME", f"git revision check failed: {type(exc).__name__}")


def _service_host(value: str, default_port: int | None = None) -> tuple[str, int | None]:
    raw = str(value or "").strip()
    parsed = urlparse(raw if "://" in raw else f"dummy://{raw}")
    return str(parsed.hostname or ""), parsed.port or default_port


def _dns_check(name: str, value: str, default_port: int | None, collector: Collector) -> None:
    host, port = _service_host(value, default_port)
    if not host:
        collector.block(f"DNS_{name.upper()}", "NETWORK", "hostname missing")
        return
    try:
        rows = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        addresses = sorted({str(row[4][0]) for row in rows})
        collector.pass_(f"DNS_{name.upper()}", "NETWORK", f"{host} -> {','.join(addresses[:3])}")
    except Exception as exc:
        collector.block(f"DNS_{name.upper()}", "NETWORK", f"{host}: {type(exc).__name__}")


def _resolve_secret(value: str, file_path: str, env_name: str) -> str:
    # Production secret indirection is authoritative. Plain Settings values are
    # only the final fallback because several fields intentionally have dev defaults.
    if str(file_path or "").strip():
        return Path(file_path).read_text(encoding="utf-8").strip()
    if str(env_name or "").strip():
        resolved = os.getenv(str(env_name).strip(), "").strip()
        if resolved:
            return resolved
    return str(value or "").strip()


def _check_database(settings, collector: Collector) -> None:
    try:
        from sqlalchemy import text
        from alembic.config import Config
        from alembic.script import ScriptDirectory
        from app.db.session import SessionLocal

        db = SessionLocal()
        try:
            value = db.execute(text("SELECT 1")).scalar_one()
            versions = set(str(v) for v in db.execute(text("SELECT version_num FROM alembic_version")).scalars().all())
        finally:
            db.close()
        if value != 1:
            raise RuntimeError("DB_SELECT_ONE_INVALID")
        cfg = Config(str(ROOT / "backend/alembic.ini"))
        cfg.set_main_option("script_location", str(ROOT / "backend/migrations"))
        expected_heads = set(ScriptDirectory.from_config(cfg).get_heads())
        collector.pass_("DATABASE_CONNECTIVITY", "DATABASE", "SELECT 1 succeeded")
        if versions == expected_heads:
            collector.pass_("DATABASE_MIGRATION_HEAD", "DATABASE", ",".join(sorted(versions)))
        else:
            collector.block("DATABASE_MIGRATION_HEAD", "DATABASE", f"db={sorted(versions)} expected={sorted(expected_heads)}")
    except Exception as exc:
        collector.block("DATABASE_CONNECTIVITY", "DATABASE", f"{type(exc).__name__}: {str(exc)[:180]}")


def _check_redis(settings, collector: Collector) -> None:
    try:
        import redis
        client = redis.Redis.from_url(settings.redis_url, socket_connect_timeout=5, socket_timeout=5)
        if client.ping() is not True:
            raise RuntimeError("REDIS_PING_FALSE")
        collector.pass_("REDIS_CONNECTIVITY", "REDIS", "PING=PONG")
    except Exception as exc:
        collector.block("REDIS_CONNECTIVITY", "REDIS", f"{type(exc).__name__}: {str(exc)[:180]}")


def _check_minio(settings, collector: Collector) -> None:
    try:
        from minio import Minio
        access_key = _resolve_secret(settings.minio_access_key, settings.minio_access_key_file, settings.minio_access_key_env)
        secret_key = _resolve_secret(settings.minio_secret_key, settings.minio_secret_key_file, settings.minio_secret_key_env)
        if not access_key or not secret_key:
            raise RuntimeError("MINIO_CREDENTIAL_MISSING")
        client = Minio(settings.minio_endpoint, access_key=access_key, secret_key=secret_key, secure=bool(settings.minio_secure))
        if not client.bucket_exists(settings.minio_bucket):
            raise RuntimeError("MINIO_BUCKET_MISSING")
        collector.pass_("MINIO_CONNECTIVITY", "MINIO", f"bucket={settings.minio_bucket} readable")
    except Exception as exc:
        collector.block("MINIO_CONNECTIVITY", "MINIO", f"{type(exc).__name__}: {str(exc)[:180]}")


def _bound_golden(profile: dict, collector: Collector) -> tuple[str | None, str | None]:
    golden_sha = str(profile.get("golden_sha256") or "")
    try:
        from sqlalchemy import func, select
        from app.db.evidence_report_models import EvidenceReportArtifactLink, FeishuEvidenceDocumentBinding, PreliminaryEvidenceReport
        from app.db.models import Evidence
        from app.db.session import SessionLocal

        db = SessionLocal()
        try:
            bindings = list(db.scalars(select(FeishuEvidenceDocumentBinding).where(
                FeishuEvidenceDocumentBinding.document_id.is_not(None),
                FeishuEvidenceDocumentBinding.projected_report_id.is_not(None),
            ).order_by(FeishuEvidenceDocumentBinding.updated_at.desc())))
            selected = None
            report = None
            for binding in bindings:
                candidate = db.get(PreliminaryEvidenceReport, binding.projected_report_id)
                material = json.dumps(candidate.snapshot_json or {}, ensure_ascii=False, sort_keys=True) if candidate else ""
                if candidate is not None and golden_sha and golden_sha in material:
                    selected, report = binding, candidate
                    break
            if selected is None or report is None:
                raise RuntimeError("NO_BOUND_REAL_GOLDEN_REPORT")
            link_count = int(db.scalar(select(func.count()).select_from(EvidenceReportArtifactLink).where(EvidenceReportArtifactLink.report_id == report.id)) or 0)
            evidence_count = int(db.scalar(select(func.count()).select_from(Evidence).where(Evidence.case_id == report.case_id)) or 0)
            if link_count <= 0 or evidence_count <= 0:
                raise RuntimeError(f"GOLDEN_SOURCE_EMPTY:links={link_count},evidence={evidence_count}")
            collector.pass_("GOLDEN_DOCUMENT_BINDING", "PROFILE", f"document={_fingerprint(selected.document_id)}; report_version={report.version}")
            collector.pass_("GOLDEN_SOURCE_EVIDENCE", "PROFILE", f"report_artifacts={link_count}; case_evidence={evidence_count}")
            return str(selected.document_id), str(report.id)
        finally:
            db.close()
    except Exception as exc:
        collector.block("GOLDEN_DOCUMENT_BINDING", "PROFILE", f"{type(exc).__name__}: {str(exc)[:180]}")
        return None, None


async def _check_feishu(document_id: str | None, collector: Collector) -> None:
    if not document_id:
        collector.block("FEISHU_READ_ONLY", "FEISHU", "document binding unavailable")
        return
    try:
        from app.integrations.feishu.evidence_document_human_v2 import HumanFeishuEvidenceDocumentService
        service = HumanFeishuEvidenceDocumentService()
        response = await service.transport._request(
            "GET",
            f"/docx/v1/documents/{quote(document_id, safe='')}/blocks",
            params={"page_size": "1"},
        )
        data = response.get("data") or {}
        collector.pass_("FEISHU_READ_ONLY", "FEISHU", f"document={_fingerprint(document_id)}; blocks_read={len(data.get('items') or [])}")
    except Exception as exc:
        collector.block("FEISHU_READ_ONLY", "FEISHU", f"{type(exc).__name__}: {str(exc)[:180]}")


async def run(contract: dict, profile_name: str) -> dict:
    collector = Collector()
    profile = (contract.get("profiles") or {}).get(profile_name)
    if not isinstance(profile, dict):
        collector.block("PROFILE", "CONFIG", f"unknown profile: {profile_name}")
        profile = {"services": []}
    else:
        collector.pass_("PROFILE", "CONFIG", profile_name)

    _check_runtime(contract, collector)

    settings = None
    try:
        from app.core.config import settings as app_settings
        settings = app_settings
        collector.pass_("APP_SETTINGS", "CONFIG", "production settings loaded")
    except Exception as exc:
        collector.block("APP_SETTINGS", "CONFIG", f"{type(exc).__name__}: {str(exc)[:180]}")

    if settings is not None:
        for key, expected in (contract.get("required_environment") or {}).items():
            actual = os.getenv(str(key), "")
            if actual.lower() == str(expected).lower():
                collector.pass_(f"ENV_{key}", "CONFIG", f"{key}={actual}")
            else:
                collector.block(f"ENV_{key}", "CONFIG", f"expected {expected}, got {actual}")
        services = set(profile.get("services") or [])
        if "database" in services:
            _dns_check("database", settings.database_url, 5432, collector)
            _check_database(settings, collector)
        if "redis" in services:
            _dns_check("redis", settings.redis_url, 6379, collector)
            _check_redis(settings, collector)
        if "minio" in services:
            _dns_check("minio", settings.minio_endpoint, 9000, collector)
            _check_minio(settings, collector)

        document_id = None
        if profile_name == "human-feishu-golden-001":
            document_id, _ = _bound_golden(profile, collector)
        if "feishu" in services:
            if not bool(settings.feishu_live_enabled) or not str(settings.feishu_app_id or "").strip():
                collector.block("FEISHU_CONFIG", "FEISHU", "live Feishu app configuration is incomplete")
            else:
                collector.pass_("FEISHU_CONFIG", "FEISHU", "live Feishu configuration enabled")
            await _check_feishu(document_id, collector)

    blockers = collector.blocking_keys
    return {
        "schema_version": 1,
        "contract": "voip-live-acceptance-preflight-v1",
        "runtime_contract": contract.get("contract"),
        "runtime_version": contract.get("runtime_version"),
        "runtime_fingerprint": os.getenv("LIVE_ACCEPTANCE_RUNTIME_FINGERPRINT", "")[:16],
        "source_revision": os.getenv("LIVE_ACCEPTANCE_SOURCE_REVISION", ""),
        "profile": profile_name,
        "status": "PASS" if not blockers else "BLOCKED",
        "mutation_allowed": not blockers,
        "blocking_keys": blockers,
        "checks": [asdict(item) for item in collector.checks],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Aggregated read-only preflight for VOIP AI live acceptance")
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument("--profile", default="base")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    contract = _load_contract(args.contract.resolve())
    payload = asyncio.run(run(contract, args.profile))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
