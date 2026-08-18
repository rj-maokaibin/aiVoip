from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from math import ceil

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.evidence_report_models import FeishuEvidenceDocumentBinding, EvidenceReportArtifactLink, PreliminaryEvidenceReport
from app.db.models import AnalyzerRun, Artifact


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    xs = sorted(values)
    idx = max(0, min(len(xs) - 1, ceil(q * len(xs)) - 1))
    return round(float(xs[idx]), 3)


def _redis_queue_depth() -> dict:
    try:
        from redis import Redis
        client = Redis.from_url(settings.redis_url, socket_connect_timeout=1, socket_timeout=1)
        queues = ["celery", "reproduction-control-high"]
        return {q: int(client.llen(q)) for q in queues}
    except Exception as exc:
        return {"status": "UNAVAILABLE", "reason": f"{type(exc).__name__}:{exc}"}


def evidence_report_pipeline_metrics(db: Session, *, window_days: int | None = None) -> dict:
    days = int(window_days or settings.evidence_report_metrics_window_days)
    since = utcnow() - timedelta(days=days)
    reports = list(db.scalars(select(PreliminaryEvidenceReport).where(PreliminaryEvidenceReport.created_at >= since)))
    analyzer_runs = list(db.scalars(select(AnalyzerRun).where(AnalyzerRun.created_at >= since)))
    report_ids = [r.id for r in reports]
    links = list(db.scalars(select(EvidenceReportArtifactLink).where(EvidenceReportArtifactLink.report_id.in_(report_ids)))) if report_ids else []
    artifact_ids = [x.artifact_id for x in links]
    artifacts = list(db.scalars(select(Artifact).where(Artifact.id.in_(artifact_ids)))) if artifact_ids else []
    # Feishu binding has immutable created_at plus last_synced_at; there is no
    # generic updated_at column. Count documents created or synchronized in the
    # observation window so long-lived Case documents remain observable.
    feishu = list(db.scalars(select(FeishuEvidenceDocumentBinding).where(or_(
        FeishuEvidenceDocumentBinding.created_at >= since,
        FeishuEvidenceDocumentBinding.last_synced_at >= since,
    ))))

    report_latency = [
        (r.completed_at - r.created_at).total_seconds()
        for r in reports if r.completed_at is not None and r.completed_at >= r.created_at
    ]
    report_status = Counter(str(r.status) for r in reports)
    analyzer_status = Counter(str(r.status) for r in analyzer_runs)
    analyzer_by_name: dict[str, Counter] = {}
    for run in analyzer_runs:
        analyzer_by_name.setdefault(run.analyzer_name, Counter())[str(run.status)] += 1
    artifact_types = Counter(str(a.type) for a in artifacts)
    feishu_status = Counter(str(x.status) for x in feishu)

    p95 = _percentile(report_latency, 0.95)
    sla = {
        "full_report_p95_seconds": p95,
        "target_p95_seconds": settings.evidence_report_full_p95_seconds,
        "pass": p95 is None or p95 <= settings.evidence_report_full_p95_seconds,
        "sample_count": len(report_latency),
        "note": "真实 Call End→Report SLA 仍需真实 DUT 环境验收；此指标为服务端 Report created→completed。",
    }
    return {
        "schema_version": "evidence-report-pipeline-metrics-v1",
        "window_days": days,
        "generated_at": utcnow().isoformat(),
        "reports": {
            "total": len(reports),
            "status": dict(report_status),
            "scope": dict(Counter(str(r.scope_type) for r in reports)),
            "latency_seconds": {
                "p50": _percentile(report_latency, 0.50),
                "p95": p95,
                "max": round(max(report_latency), 3) if report_latency else None,
                "samples": len(report_latency),
            },
        },
        "analyzers": {
            "total": len(analyzer_runs),
            "status": dict(analyzer_status),
            "by_name": {name: dict(counts) for name, counts in sorted(analyzer_by_name.items())},
        },
        "artifacts": {
            "total": len(artifacts),
            "by_type": dict(artifact_types),
            "bundle_count": sum(v for k, v in artifact_types.items() if k == "EVIDENCE_BUNDLE"),
        },
        "feishu_projection": {"total": len(feishu), "status": dict(feishu_status)},
        "queue_depth": _redis_queue_depth(),
        "sla": sla,
    }
