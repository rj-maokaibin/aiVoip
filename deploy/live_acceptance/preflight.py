#!/usr/bin/env python3
from __future__ import annotations
import argparse, asyncio, hashlib, importlib, importlib.metadata, json, os, socket, subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import quote, urlparse
ROOT=Path(__file__).resolve().parents[2]
DEFAULT_CONTRACT=ROOT/"deploy/live_acceptance/runtime_contract.json"
REQUIRED_GOLDEN_ANALYZERS={"packet_intelligence","media_intelligence","pcm_intelligence"}

@dataclass
class Check:
    key:str; category:str; status:str; detail:str; blocks_mutation:bool=True

class Collector:
    def __init__(self): self.checks:list[Check]=[]
    def pass_(self,key,category,detail): self.checks.append(Check(key,category,"PASS",detail,True))
    def block(self,key,category,detail): self.checks.append(Check(key,category,"BLOCKED",detail,True))
    @property
    def blocking_keys(self): return [x.key for x in self.checks if x.blocks_mutation and x.status!="PASS"]

def _load_contract(path:Path)->dict:
    data=json.loads(path.read_text(encoding="utf-8"))
    if data.get("contract")!="voip-live-acceptance-runtime-v1": raise RuntimeError("LIVE_ACCEPTANCE_RUNTIME_CONTRACT_INVALID")
    return data

def _fingerprint(value): return hashlib.sha256(str(value or "").encode()).hexdigest()[:12] if value else None

def _pinned_requirements(path):
    rows=[]
    for raw in path.read_text(encoding="utf-8").splitlines():
        line=raw.strip()
        if not line or line.startswith("#") or "==" not in line: continue
        name,version=line.split("==",1); rows.append((name.split("[",1)[0].strip(),version.strip()))
    return rows

def _check_runtime(contract,collector):
    expected=str(contract.get("python_major_minor") or ""); actual=f"{os.sys.version_info.major}.{os.sys.version_info.minor}"
    collector.pass_("PYTHON_ABI","RUNTIME",f"python={actual}") if actual==expected else collector.block("PYTHON_ABI","RUNTIME",f"expected {expected}, got {actual}")
    mismatches=[]
    for name,expected_v in _pinned_requirements(ROOT/str(contract.get("requirements_file") or "backend/requirements.txt")):
        try: actual_v=importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError: mismatches.append(f"{name}=MISSING(expected {expected_v})"); continue
        if actual_v!=expected_v: mismatches.append(f"{name}={actual_v}(expected {expected_v})")
    collector.block("PINNED_PYTHON_DEPENDENCIES","RUNTIME","; ".join(mismatches[:20])) if mismatches else collector.pass_("PINNED_PYTHON_DEPENDENCIES","RUNTIME","all pinned backend requirements match")
    failed=[]
    for module in contract.get("required_imports") or []:
        try: importlib.import_module(str(module))
        except Exception as exc: failed.append(f"{module}:{type(exc).__name__}")
    collector.block("REQUIRED_IMPORTS","RUNTIME","; ".join(failed)) if failed else collector.pass_("REQUIRED_IMPORTS","RUNTIME","all required live modules import")
    try:
        families=subprocess.run(["fc-list",":","family"],text=True,capture_output=True,timeout=10,check=True).stdout
        found=next((str(m) for m in contract.get("font_family_markers") or [] if str(m).lower() in families.lower()),None)
        collector.pass_("CJK_FONT_RUNTIME","RUNTIME",f"CJK family available: {found}") if found else collector.block("CJK_FONT_RUNTIME","RUNTIME","no approved CJK font family found")
    except Exception as exc: collector.block("CJK_FONT_RUNTIME","RUNTIME",f"font discovery failed: {type(exc).__name__}")
    contract_env=os.getenv("LIVE_ACCEPTANCE_RUNTIME_CONTRACT",""); fp=os.getenv("LIVE_ACCEPTANCE_RUNTIME_FINGERPRINT",""); source=os.getenv("LIVE_ACCEPTANCE_SOURCE_REVISION",""); workspace=os.getenv("LIVE_ACCEPTANCE_WORKSPACE_REVISION",""); orchestrator=os.getenv("LIVE_ACCEPTANCE_ORCHESTRATOR_VERSION","")
    collector.pass_("RUNTIME_IDENTITY","RUNTIME",f"contract={contract_env}; fingerprint={fp[:16]}; revision={source[:12]}") if contract_env==contract.get("contract") and fp and source else collector.block("RUNTIME_IDENTITY","RUNTIME","runtime contract/fingerprint/source revision is incomplete")
    collector.pass_("ORCHESTRATOR_IDENTITY","RUNTIME",f"orchestrator={orchestrator}") if orchestrator else collector.block("ORCHESTRATOR_IDENTITY","RUNTIME","orchestrator version missing")
    collector.pass_("SOURCE_REVISION_EXACT","RUNTIME",source) if source and workspace and source==workspace else collector.block("SOURCE_REVISION_EXACT","RUNTIME",f"runtime={source}; workspace={workspace}")

def _service_host(value,default_port=None):
    raw=str(value or "").strip(); parsed=urlparse(raw if "://" in raw else f"dummy://{raw}"); return str(parsed.hostname or ""), parsed.port or default_port

def _dns_check(name,value,default_port,collector):
    host,port=_service_host(value,default_port)
    if not host: collector.block(f"DNS_{name.upper()}","NETWORK","hostname missing"); return
    try:
        rows=socket.getaddrinfo(host,port,type=socket.SOCK_STREAM); addresses=sorted({str(r[4][0]) for r in rows}); collector.pass_(f"DNS_{name.upper()}","NETWORK",f"{host} -> {','.join(addresses[:3])}")
    except Exception as exc: collector.block(f"DNS_{name.upper()}","NETWORK",f"{host}: {type(exc).__name__}")

def _check_database_route(collector):
    status=os.getenv("LIVE_ACCEPTANCE_DATABASE_ROUTE_STATUS","").strip()
    allowed={"backend_dns","candidate_same_network","candidate_cross_network"}
    collector.pass_("DATABASE_ROUTE","NETWORK",status) if status in allowed else collector.block("DATABASE_ROUTE","NETWORK",status or "route status missing")

def _resolve_secret(value,file_path,env_name):
    if str(file_path or "").strip(): return Path(file_path).read_text(encoding="utf-8").strip()
    if str(env_name or "").strip():
        resolved=os.getenv(str(env_name).strip(),"").strip()
        if resolved: return resolved
    return str(value or "").strip()

def _check_database(settings,collector):
    try:
        from sqlalchemy import text
        from alembic.config import Config
        from alembic.script import ScriptDirectory
        from app.db.session import SessionLocal
        db=SessionLocal()
        try: value=db.execute(text("SELECT 1")).scalar_one(); versions=set(str(v) for v in db.execute(text("SELECT version_num FROM alembic_version")).scalars().all())
        finally: db.close()
        if value!=1: raise RuntimeError("DB_SELECT_ONE_INVALID")
        cfg=Config(str(ROOT/"backend/alembic.ini")); cfg.set_main_option("script_location",str(ROOT/"backend/migrations")); expected=set(ScriptDirectory.from_config(cfg).get_heads())
        collector.pass_("DATABASE_CONNECTIVITY","DATABASE","SELECT 1 succeeded")
        collector.pass_("DATABASE_MIGRATION_HEAD","DATABASE",",".join(sorted(versions))) if versions==expected else collector.block("DATABASE_MIGRATION_HEAD","DATABASE",f"db={sorted(versions)} expected={sorted(expected)}")
    except Exception as exc: collector.block("DATABASE_CONNECTIVITY","DATABASE",f"{type(exc).__name__}: {str(exc)[:180]}")

def _check_redis(settings,collector):
    try:
        import redis
        if redis.Redis.from_url(settings.redis_url,socket_connect_timeout=5,socket_timeout=5).ping() is not True: raise RuntimeError("REDIS_PING_FALSE")
        collector.pass_("REDIS_CONNECTIVITY","REDIS","PING=PONG")
    except Exception as exc: collector.block("REDIS_CONNECTIVITY","REDIS",f"{type(exc).__name__}: {str(exc)[:180]}")

def _check_minio(settings,collector):
    try:
        from minio import Minio
        ak=_resolve_secret(settings.minio_access_key,settings.minio_access_key_file,settings.minio_access_key_env); sk=_resolve_secret(settings.minio_secret_key,settings.minio_secret_key_file,settings.minio_secret_key_env)
        if not ak or not sk: raise RuntimeError("MINIO_CREDENTIAL_MISSING")
        client=Minio(settings.minio_endpoint,access_key=ak,secret_key=sk,secure=bool(settings.minio_secure))
        if not client.bucket_exists(settings.minio_bucket): raise RuntimeError("MINIO_BUCKET_MISSING")
        collector.pass_("MINIO_CONNECTIVITY","MINIO",f"bucket={settings.minio_bucket} readable")
    except Exception as exc: collector.block("MINIO_CONNECTIVITY","MINIO",f"{type(exc).__name__}: {str(exc)[:180]}")

def _bound_golden(profile,collector):
    golden_sha=str(profile.get("golden_sha256") or "")
    try:
        from sqlalchemy import func,select
        from app.db.evidence_report_models import EvidenceReportArtifactLink,FeishuEvidenceDocumentBinding,PreliminaryEvidenceReport
        from app.db.models import AnalyzerRun,Evidence
        from app.db.session import SessionLocal
        db=SessionLocal()
        try:
            bindings=list(db.scalars(select(FeishuEvidenceDocumentBinding).where(FeishuEvidenceDocumentBinding.document_id.is_not(None),FeishuEvidenceDocumentBinding.projected_report_id.is_not(None)).order_by(FeishuEvidenceDocumentBinding.updated_at.desc())))
            selected=report=None; report_snapshot_has_sha=False
            for binding in bindings:
                candidate=db.get(PreliminaryEvidenceReport,binding.projected_report_id)
                exact=int(db.scalar(select(func.count()).select_from(Evidence).where(Evidence.case_id==binding.case_id,Evidence.sha256==golden_sha)) or 0)
                if candidate is not None and golden_sha and exact>0:
                    selected,report=binding,candidate
                    report_snapshot_has_sha=golden_sha in json.dumps(candidate.snapshot_json or {},ensure_ascii=False,sort_keys=True)
                    break
            if selected is None or report is None: raise RuntimeError("NO_BOUND_REAL_GOLDEN_CASE_EVIDENCE")
            successful=set(str(x) for x in db.scalars(select(AnalyzerRun.analyzer_name).where(AnalyzerRun.case_id==report.case_id,AnalyzerRun.status=="SUCCESS",AnalyzerRun.analyzer_name.in_(REQUIRED_GOLDEN_ANALYZERS))))
            missing=sorted(REQUIRED_GOLDEN_ANALYZERS-successful)
            if missing: raise RuntimeError("GOLDEN_REQUIRED_ANALYZERS_MISSING:"+",".join(missing))
            links=int(db.scalar(select(func.count()).select_from(EvidenceReportArtifactLink).where(EvidenceReportArtifactLink.report_id==report.id)) or 0); evidence=int(db.scalar(select(func.count()).select_from(Evidence).where(Evidence.case_id==report.case_id)) or 0)
            if links<=0 or evidence<=0: raise RuntimeError(f"GOLDEN_SOURCE_EMPTY:links={links},evidence={evidence}")
            collector.pass_("GOLDEN_DOCUMENT_BINDING","PROFILE",f"document={_fingerprint(selected.document_id)}; report_version={report.version}; identity=CASE_EVIDENCE_SHA; snapshot_sha={report_snapshot_has_sha}")
            collector.pass_("GOLDEN_SOURCE_EVIDENCE","PROFILE",f"exact_sha=1+; report_artifacts={links}; case_evidence={evidence}; analyzers={','.join(sorted(successful))}")
            return str(selected.document_id),str(report.id)
        finally: db.close()
    except Exception as exc: collector.block("GOLDEN_DOCUMENT_BINDING","PROFILE",f"{type(exc).__name__}: {str(exc)[:180]}"); return None,None

async def _check_feishu(document_id,collector):
    if not document_id: collector.block("FEISHU_READ_ONLY","FEISHU","document binding unavailable"); return
    try:
        from app.integrations.feishu.evidence_document_human_v2 import HumanFeishuEvidenceDocumentService
        service=HumanFeishuEvidenceDocumentService(); response=await service.transport._request("GET",f"/docx/v1/documents/{quote(document_id,safe='')}/blocks",params={"page_size":"1"}); data=response.get("data") or {}; collector.pass_("FEISHU_READ_ONLY","FEISHU",f"document={_fingerprint(document_id)}; blocks_read={len(data.get('items') or [])}")
    except Exception as exc: collector.block("FEISHU_READ_ONLY","FEISHU",f"{type(exc).__name__}: {str(exc)[:180]}")

async def run(contract,profile_name):
    collector=Collector(); profile=(contract.get("profiles") or {}).get(profile_name)
    if not isinstance(profile,dict): collector.block("PROFILE","CONFIG",f"unknown profile: {profile_name}"); profile={"services":[]}
    else: collector.pass_("PROFILE","CONFIG",profile_name)
    _check_runtime(contract,collector)
    settings=None
    try:
        from app.core.config import settings as app_settings
        settings=app_settings; collector.pass_("APP_SETTINGS","CONFIG","production settings loaded")
    except Exception as exc: collector.block("APP_SETTINGS","CONFIG",f"{type(exc).__name__}: {str(exc)[:180]}")
    if settings is not None:
        for key,expected in (contract.get("required_environment") or {}).items():
            actual=os.getenv(str(key),""); collector.pass_(f"ENV_{key}","CONFIG",f"{key}={actual}") if actual.lower()==str(expected).lower() else collector.block(f"ENV_{key}","CONFIG",f"expected {expected}, got {actual}")
        services=set(profile.get("services") or [])
        if "database" in services: _check_database_route(collector); _dns_check("database",settings.database_url,5432,collector); _check_database(settings,collector)
        if "redis" in services: _dns_check("redis",settings.redis_url,6379,collector); _check_redis(settings,collector)
        if "minio" in services: _dns_check("minio",settings.minio_endpoint,9000,collector); _check_minio(settings,collector)
        document_id=None
        if profile_name=="human-feishu-golden-001": document_id,_=_bound_golden(profile,collector)
        if "feishu" in services:
            collector.pass_("FEISHU_CONFIG","FEISHU","live Feishu configuration enabled") if bool(settings.feishu_live_enabled) and str(settings.feishu_app_id or "").strip() else collector.block("FEISHU_CONFIG","FEISHU","live Feishu app configuration is incomplete")
            await _check_feishu(document_id,collector)
    blockers=collector.blocking_keys
    return {"schema_version":1,"contract":"voip-live-acceptance-preflight-v1","runtime_contract":contract.get("contract"),"runtime_version":contract.get("runtime_version"),"orchestrator_version":os.getenv("LIVE_ACCEPTANCE_ORCHESTRATOR_VERSION",""),"runtime_fingerprint":os.getenv("LIVE_ACCEPTANCE_RUNTIME_FINGERPRINT","")[:16],"source_revision":os.getenv("LIVE_ACCEPTANCE_SOURCE_REVISION",""),"profile":profile_name,"status":"PASS" if not blockers else "BLOCKED","mutation_allowed":not blockers,"blocking_keys":blockers,"checks":[asdict(x) for x in collector.checks]}

def main():
    p=argparse.ArgumentParser(description="Aggregated read-only preflight for VOIP AI live acceptance"); p.add_argument("--contract",type=Path,default=DEFAULT_CONTRACT); p.add_argument("--profile",default="base"); p.add_argument("--out",type=Path,required=True); args=p.parse_args(); payload=asyncio.run(run(_load_contract(args.contract.resolve()),args.profile)); args.out.parent.mkdir(parents=True,exist_ok=True); args.out.write_text(json.dumps(payload,ensure_ascii=False,indent=2)+"\n",encoding="utf-8"); print(json.dumps(payload,ensure_ascii=False,indent=2)); return 0 if payload["status"]=="PASS" else 2

if __name__=="__main__": raise SystemExit(main())
