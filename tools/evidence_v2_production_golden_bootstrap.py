from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path

from sqlalchemy import select

from app.contracts.enums import EvidenceCompleteness, EvidenceKind, EvidenceLevel, EvidenceScope
from app.db.evidence_report_models import FeishuEvidenceDocumentBinding, PreliminaryEvidenceReport
from app.db.models import AnalyzerRun, Case, Evidence
from app.db.session import SessionLocal
from app.integrations.feishu.evidence_document import FeishuEvidenceDocumentService
from app.integrations.storage import ObjectStorage
from app.reports.v2.migration import rollout_from_env
from app.services.analysis import create_media_analysis_job, create_packet_analysis_job, create_pcm_analysis_job
from app.services.audit import audit
from app.services.cases import create_case
from app.services.evidence import create_evidence
from app.services.evidence_report import generate_evidence_report
from app.workers.media_tasks import analyze_media_evidence
from app.workers.packet_tasks import analyze_evidence as analyze_packet_evidence
from app.workers.pcm_tasks import analyze_pcm_evidence
from human_evidence_feishu_live_acceptance import (
    GOLDEN_PCM_PROFILE_ID,
    REAL_GOLDEN_001_SHA256,
    _block_text,
    _case_has_exact_golden,
    _case_has_required_analyzers,
    _list_document_blocks,
    _select_bound_golden,
)

ACTOR = "evidence-v2-production-golden-bootstrap"
CASE_SUMMARY = "[SYSTEM ACCEPTANCE] Real Golden #001 Evidence V2 production baseline"
CASE_SN = "SYSTEM-EVIDENCE-V2-GOLDEN-001"
GOLDEN_CASE_ID = "OFFLINE_ANALYSIS_20260814_001"
GOLDEN_FILENAME = "tcpdump-2026-08-14(2).pcap"
CONTRACT = "evidence-v2-production-golden-bootstrap-v1"
REQUIRED_ANALYZERS = ("packet_intelligence", "pcm_intelligence", "media_intelligence")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fingerprint(value: str | None) -> str | None:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:12] if value else None


def _persist(path: Path, payload: dict) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    shared = Path("/validation/evidence_v2_production_golden_bootstrap.json")
    if shared != path and shared.parent.is_dir():
        shared.write_text(text, encoding="utf-8")


def _dedicated_case(db) -> Case | None:
    rows = list(db.scalars(
        select(Case)
        .where(Case.created_by == ACTOR, Case.summary == CASE_SUMMARY)
        .order_by(Case.created_at.asc())
        .limit(2)
    ))
    if len(rows) > 1:
        raise RuntimeError("EVIDENCE_V2_GOLDEN_BOOTSTRAP_DUPLICATE_CASES")
    return rows[0] if rows else None


def _exact_successful_analyzers(db, *, case_id: str, evidence_id: str) -> set[str]:
    rows = list(db.scalars(select(AnalyzerRun).where(
        AnalyzerRun.case_id == case_id,
        AnalyzerRun.status == "SUCCESS",
        AnalyzerRun.analyzer_name.in_(REQUIRED_ANALYZERS),
    )))
    return {
        str(row.analyzer_name)
        for row in rows
        if str(evidence_id) in {str(item) for item in (row.input_evidence_ids or [])}
    }


def _existing_binding(
    db,
    *,
    case_id: str,
    evidence_id: str,
) -> tuple[FeishuEvidenceDocumentBinding, PreliminaryEvidenceReport] | None:
    binding = db.scalar(select(FeishuEvidenceDocumentBinding).where(
        FeishuEvidenceDocumentBinding.case_id == case_id,
        FeishuEvidenceDocumentBinding.document_id.is_not(None),
        FeishuEvidenceDocumentBinding.projected_report_id.is_not(None),
    ).limit(1))
    if binding is None:
        return None
    report = db.get(PreliminaryEvidenceReport, binding.projected_report_id)
    if report is None or str(report.case_id) != str(case_id):
        raise RuntimeError("EVIDENCE_V2_GOLDEN_BASELINE_REPORT_BINDING_INVALID")

    # A prior failed bootstrap can legitimately leave a projected document binding
    # while the exact Golden evidence or one of its required analyzer runs is still
    # incomplete. That is a recoverable partial bootstrap state, not a valid final
    # baseline and not a structural binding corruption. Return None so run() repairs
    # the missing evidence/analyzers, regenerates the report, reprojects the same
    # case, and performs a fresh remote read-back.
    if not _case_has_exact_golden(db, case_id):
        return None
    exact_analyzers = _exact_successful_analyzers(db, case_id=case_id, evidence_id=evidence_id)
    if set(REQUIRED_ANALYZERS) - exact_analyzers:
        return None
    return binding, report


def _ensure_evidence(db, *, case: Case, pcap: Path) -> tuple[Evidence, bool]:
    rows = list(db.scalars(select(Evidence).where(
        Evidence.case_id == case.id,
        Evidence.sha256 == REAL_GOLDEN_001_SHA256,
    ).order_by(Evidence.created_at.asc()).limit(2)))
    if len(rows) > 1:
        raise RuntimeError("EVIDENCE_V2_GOLDEN_BOOTSTRAP_DUPLICATE_EVIDENCE")
    if rows:
        return rows[0], False

    object_key = f"cases/{case.id}/evidence/real-golden-001/{GOLDEN_FILENAME}"
    ObjectStorage().put_file(object_key, pcap, "application/vnd.tcpdump.pcap")
    evidence = create_evidence(
        db,
        case_id=case.id,
        evidence_type="PCAP",
        source="USER_UPLOAD",
        filename=GOLDEN_FILENAME,
        object_key=object_key,
        size_bytes=pcap.stat().st_size,
        sha256=REAL_GOLDEN_001_SHA256,
        content_type="application/vnd.tcpdump.pcap",
        kind=EvidenceKind.RAW,
        scope=EvidenceScope.CASE,
        level=EvidenceLevel.L1,
        completeness=EvidenceCompleteness.COMPLETE,
        producer_type="SYSTEM_ACCEPTANCE",
        producer_id=ACTOR,
        producer_version=CONTRACT,
        metadata={
            "golden_case": GOLDEN_CASE_ID,
            "purpose": "PRODUCTION_EVIDENCE_V2_ACCEPTANCE_BASELINE",
            "human_reviewed_raw_evidence": True,
            "ground_truth_injected_into_analyzers": False,
        },
        actor=ACTOR,
    )
    audit(
        db,
        case_id=case.id,
        actor=ACTOR,
        event_type="PRODUCTION_GOLDEN_EVIDENCE_BOOTSTRAPPED",
        target_type="evidence",
        target_id=evidence.id,
        detail={"golden_case": GOLDEN_CASE_ID, "sha256": REAL_GOLDEN_001_SHA256},
    )
    db.commit()
    db.refresh(evidence)
    return evidence, True


def _ensure_analyzers(db, *, case_id: str, evidence_id: str) -> list[str]:
    existing = _exact_successful_analyzers(db, case_id=case_id, evidence_id=evidence_id)
    performed: list[str] = []

    if "packet_intelligence" not in existing:
        job = create_packet_analysis_job(db, case_id=case_id, evidence_id=evidence_id)
        result = analyze_packet_evidence.run(str(job.id), str(evidence_id))
        if str((result or {}).get("status") or "") != "SUCCESS":
            raise RuntimeError("EVIDENCE_V2_GOLDEN_PACKET_ANALYSIS_FAILED")
        performed.append("packet_intelligence")
        db.expire_all()

    existing = _exact_successful_analyzers(db, case_id=case_id, evidence_id=evidence_id)
    if "pcm_intelligence" not in existing:
        job = create_pcm_analysis_job(db, case_id=case_id, evidence_id=evidence_id, profile_id=GOLDEN_PCM_PROFILE_ID)
        result = analyze_pcm_evidence.run(str(job.id), str(evidence_id), GOLDEN_PCM_PROFILE_ID, False)
        if str((result or {}).get("status") or "") != "SUCCESS":
            raise RuntimeError("EVIDENCE_V2_GOLDEN_PCM_ANALYSIS_FAILED")
        performed.append("pcm_intelligence")
        db.expire_all()

    existing = _exact_successful_analyzers(db, case_id=case_id, evidence_id=evidence_id)
    if "media_intelligence" not in existing:
        job = create_media_analysis_job(db, case_id=case_id, evidence_id=evidence_id, profile_id=GOLDEN_PCM_PROFILE_ID)
        result = analyze_media_evidence.run(str(job.id), str(evidence_id), GOLDEN_PCM_PROFILE_ID, False)
        if str((result or {}).get("status") or "") != "SUCCESS":
            raise RuntimeError("EVIDENCE_V2_GOLDEN_MEDIA_ANALYSIS_FAILED")
        performed.append("media_intelligence")
        db.expire_all()

    exact = _exact_successful_analyzers(db, case_id=case_id, evidence_id=evidence_id)
    if set(REQUIRED_ANALYZERS) - exact:
        raise RuntimeError("EVIDENCE_V2_GOLDEN_REQUIRED_ANALYZERS_INCOMPLETE")
    return performed


async def run(*, pcap: Path, expected_revision: str) -> dict:
    observed_revision = str(os.getenv("BUILD_REVISION") or "").strip()
    if observed_revision != expected_revision:
        raise RuntimeError(
            f"EVIDENCE_V2_GOLDEN_BOOTSTRAP_REVISION_MISMATCH:{observed_revision}:{expected_revision}"
        )
    rollout = rollout_from_env()
    if rollout.mode != "SHADOW" or rollout.strict_validator is not True:
        raise RuntimeError(
            f"EVIDENCE_V2_GOLDEN_BOOTSTRAP_NOT_SHADOW:mode={rollout.mode}:strict={rollout.strict_validator}"
        )
    if not pcap.is_file():
        raise RuntimeError("EVIDENCE_V2_GOLDEN_FIXTURE_MISSING")
    actual_sha = _sha256_file(pcap)
    if actual_sha != REAL_GOLDEN_001_SHA256:
        raise RuntimeError(
            f"EVIDENCE_V2_GOLDEN_FIXTURE_SHA256_MISMATCH:{actual_sha}:{REAL_GOLDEN_001_SHA256}"
        )

    db = SessionLocal()
    try:
        case = _dedicated_case(db)
        case_created = False
        if case is None:
            case = create_case(
                db,
                summary=CASE_SUMMARY,
                ip="127.0.0.1",
                ssh_port=22,
                sn=CASE_SN,
                created_by=ACTOR,
            )
            case_created = True

        evidence, evidence_created = _ensure_evidence(db, case=case, pcap=pcap)
        existing = _existing_binding(
            db,
            case_id=str(case.id),
            evidence_id=str(evidence.id),
        )
        if existing is not None:
            binding, report = existing
            selected_binding, selected_report = _select_bound_golden(db)
            if str(selected_binding.id) != str(binding.id) or str(selected_report.id) != str(report.id):
                raise RuntimeError("EVIDENCE_V2_GOLDEN_STRICT_SELECTOR_DID_NOT_RESOLVE_BASELINE")
            return {
                "status": "PASS",
                "contract": CONTRACT,
                "source_revision": observed_revision,
                "stage": "SHADOW",
                "golden_case": GOLDEN_CASE_ID,
                "golden_sha256": REAL_GOLDEN_001_SHA256,
                "case_id": case.id,
                "case_created": False,
                "evidence_id": evidence.id,
                "evidence_created": evidence_created,
                "analyzer_refresh_components": [],
                "report_id": report.id,
                "report_version": report.version,
                "document_fingerprint": _fingerprint(binding.document_id),
                "document_reused": True,
                "projection_version": binding.projection_version,
                "strict_selector_verified": True,
                "baseline_remote_readback": "PREVIOUSLY_VERIFIED",
            }

        analyzer_components = _ensure_analyzers(db, case_id=str(case.id), evidence_id=str(evidence.id))
        db.expire_all()
        exact_analyzers = _exact_successful_analyzers(
            db,
            case_id=str(case.id),
            evidence_id=str(evidence.id),
        )
        if not _case_has_exact_golden(db, str(case.id)) or set(REQUIRED_ANALYZERS) - exact_analyzers:
            raise RuntimeError("EVIDENCE_V2_GOLDEN_BASELINE_PRE_REPORT_CONTRACT_FAILED")

        report, payload, _ = generate_evidence_report(
            db,
            scope_type="CASE",
            scope_id=str(case.id),
            actor=ACTOR,
            force=True,
        )
        if payload.get("active_projection") != "V1":
            raise RuntimeError("EVIDENCE_V2_GOLDEN_BOOTSTRAP_CHANGED_ACTIVE_PROJECTION")
        v2 = payload.get("v2_shadow") or {}
        if (v2.get("semantic_validation") or {}).get("status") != "PASS" or v2.get("publishable") is not True:
            raise RuntimeError("EVIDENCE_V2_GOLDEN_BOOTSTRAP_SHADOW_V2_NOT_PUBLISHABLE")

        service = FeishuEvidenceDocumentService()
        binding = await service.project(db, case_id=str(case.id), report_id=str(report.id))
        if not binding.document_id or str(binding.projected_report_id) != str(report.id):
            raise RuntimeError("EVIDENCE_V2_GOLDEN_BASELINE_FEISHU_BINDING_INVALID")
        db.commit()

        blocks = await _list_document_blocks(service, str(binding.document_id))
        remote_text = "\n".join(_block_text(block) for block in blocks)
        if not blocks or str(report.id) not in remote_text:
            raise RuntimeError("EVIDENCE_V2_GOLDEN_BASELINE_REMOTE_READBACK_FAILED")

        selected_binding, selected_report = _select_bound_golden(db)
        if str(selected_binding.id) != str(binding.id) or str(selected_report.id) != str(report.id):
            raise RuntimeError("EVIDENCE_V2_GOLDEN_STRICT_SELECTOR_DID_NOT_RESOLVE_BASELINE")

        return {
            "status": "PASS",
            "contract": CONTRACT,
            "source_revision": observed_revision,
            "stage": "SHADOW",
            "golden_case": GOLDEN_CASE_ID,
            "golden_sha256": REAL_GOLDEN_001_SHA256,
            "case_id": case.id,
            "case_created": case_created,
            "evidence_id": evidence.id,
            "evidence_created": evidence_created,
            "analyzer_refresh_components": analyzer_components,
            "required_analyzers": list(REQUIRED_ANALYZERS),
            "report_id": report.id,
            "report_version": report.version,
            "active_projection": payload.get("active_projection"),
            "v2_semantic_status": (v2.get("semantic_validation") or {}).get("status"),
            "v2_publishable": v2.get("publishable"),
            "document_fingerprint": _fingerprint(binding.document_id),
            "document_reused": False,
            "projection_version": binding.projection_version,
            "strict_selector_verified": True,
            "remote_block_count": len(blocks),
            "baseline_remote_readback": "PASS",
        }
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pcap", required=True, type=Path)
    parser.add_argument("--expected-revision", required=True)
    parser.add_argument("--result", required=True, type=Path)
    args = parser.parse_args()
    try:
        payload = asyncio.run(run(pcap=args.pcap, expected_revision=args.expected_revision))
    except Exception as exc:
        payload = {
            "status": "FAIL",
            "contract": CONTRACT,
            "source_revision": str(os.getenv("BUILD_REVISION") or ""),
            "stage": "SHADOW",
            "golden_case": GOLDEN_CASE_ID,
            "golden_sha256": REAL_GOLDEN_001_SHA256,
            "error_code": type(exc).__name__,
            "error_message": str(exc)[:700],
        }
    _persist(args.result, payload)
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if payload.get("status") == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
