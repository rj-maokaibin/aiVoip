from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from app.core.config import settings
from app.core.logging import configure_logging
from app.api.v1.cases import router as cases_router
from app.api.v1.jobs import router as jobs_router
from app.api.v1.evidences import router as evidence_router
from app.api.v1.analyzers import router as analyzers_router
from app.api.v1.uploads import router as uploads_router
from app.api.v1.artifacts import router as artifacts_router
from app.api.v1.diagnosis import router as diagnosis_router
from app.api.v1.rules import router as rules_router
from app.api.v1.knowledge import router as knowledge_router
from app.api.v1.reports import router as reports_router
from app.api.v1.evidence_reports import router as evidence_reports_router
from app.api.v1.evidence_retention import router as evidence_retention_router
from app.api.v1.evidence_report_metrics import router as evidence_report_metrics_router
from app.api.health import router as health_router
from app.api.v1.audit import router as audit_router
from app.api.v1.events import router as events_router
from app.api.v1.reproduction import router as reproduction_router
from app.api.v1.experiments import router as experiments_router
from app.api.v1.feishu import router as feishu_router
from app.api.v1.feishu_callback import router as feishu_callback_router
from app.api.v1.feishu_governance import router as feishu_governance_router
from app.api.v1.feishu_document_acl import router as feishu_document_acl_router
from app.api.v1.ai_semantic import router as ai_semantic_router
from app.api.v1.ai_copilot import router as ai_copilot_router
from app.api.v1.ai_cycles import router as ai_cycles_router
from app.api.v1.system import router as system_router
from app.api.v1.golden_candidates import router as golden_candidates_router
from app.api.deps import get_identity
from app.core.errors import AppError
from app.core.http_contract import trace_id_middleware, app_error_handler, http_exception_handler, request_validation_error_handler, unhandled_exception_handler
from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
configure_logging(settings.log_level)
app=FastAPI(title='VOIP AI Fault Assistant', version=settings.app_version)
_cors_origins=[x.strip() for x in settings.cors_allow_origins.split(',') if x.strip()]
if settings.app_env.lower() == 'production':
    if settings.auth_allow_anonymous_dev:
        raise RuntimeError('PRODUCTION_ANONYMOUS_AUTH_FORBIDDEN')
    if '*' in _cors_origins or not _cors_origins:
        raise RuntimeError('PRODUCTION_CORS_WILDCARD_FORBIDDEN')
    if str(settings.production_auth_provider).lower() in {'', 'pending', 'dev_headers', 'trusted_headers_only'}:
        raise RuntimeError('PRODUCTION_AUTH_PROVIDER_REQUIRED')
    if settings.feishu_live_enabled and not settings.feishu_identity_rbac_enabled:
        raise RuntimeError('PRODUCTION_FEISHU_RBAC_REQUIRED')
app.add_middleware(CORSMiddleware, allow_origins=_cors_origins or [], allow_methods=['*'], allow_headers=['*'])
app.middleware('http')(trace_id_middleware)
app.add_exception_handler(AppError, app_error_handler)
app.add_exception_handler(HTTPException, http_exception_handler)
app.add_exception_handler(StarletteHTTPException, http_exception_handler)
app.add_exception_handler(RequestValidationError, request_validation_error_handler)
app.add_exception_handler(Exception, unhandled_exception_handler)
@app.get('/health')
def health(): return {'status':'ok','version':settings.app_version,'build_revision':settings.build_revision}
app.include_router(health_router)
app.include_router(cases_router, prefix='/api/v1', dependencies=[Depends(get_identity)])
app.include_router(jobs_router, prefix='/api/v1', dependencies=[Depends(get_identity)])
app.include_router(evidence_router, prefix='/api/v1', dependencies=[Depends(get_identity)])
app.include_router(analyzers_router, prefix='/api/v1', dependencies=[Depends(get_identity)])
app.include_router(uploads_router, prefix='/api/v1', dependencies=[Depends(get_identity)])
app.include_router(artifacts_router, prefix='/api/v1', dependencies=[Depends(get_identity)])
app.include_router(diagnosis_router, prefix='/api/v1', dependencies=[Depends(get_identity)])
app.include_router(rules_router, prefix='/api/v1', dependencies=[Depends(get_identity)])
app.include_router(knowledge_router, prefix='/api/v1', dependencies=[Depends(get_identity)])
app.include_router(reports_router, prefix='/api/v1', dependencies=[Depends(get_identity)])
app.include_router(evidence_reports_router, prefix='/api/v1', dependencies=[Depends(get_identity)])
app.include_router(evidence_retention_router, prefix='/api/v1', dependencies=[Depends(get_identity)])
app.include_router(evidence_report_metrics_router, prefix='/api/v1', dependencies=[Depends(get_identity)])
app.include_router(audit_router, prefix='/api/v1', dependencies=[Depends(get_identity)])
app.include_router(events_router, prefix='/api/v1', dependencies=[Depends(get_identity)])
app.include_router(reproduction_router, prefix='/api/v1', dependencies=[Depends(get_identity)])
app.include_router(experiments_router, prefix='/api/v1', dependencies=[Depends(get_identity)])
app.include_router(golden_candidates_router, prefix='/api/v1', dependencies=[Depends(get_identity)])
app.include_router(feishu_router, prefix='/api/v1', dependencies=[Depends(get_identity)])
app.include_router(feishu_governance_router, prefix='/api/v1', dependencies=[Depends(get_identity)])
app.include_router(feishu_document_acl_router, prefix='/api/v1', dependencies=[Depends(get_identity)])
app.include_router(ai_semantic_router, prefix='/api/v1', dependencies=[Depends(get_identity)])
app.include_router(ai_copilot_router, prefix='/api/v1', dependencies=[Depends(get_identity)])
app.include_router(ai_cycles_router, prefix='/api/v1', dependencies=[Depends(get_identity)])
# Feishu callbacks are authenticated by Feishu signature/token, not by user auth headers.
app.include_router(feishu_callback_router, prefix='/api/v1')
app.include_router(system_router, prefix='/api/v1', dependencies=[Depends(get_identity)])