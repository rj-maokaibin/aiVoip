from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import quote

from sqlalchemy import select

from app.db.evidence_report_models import (
    EvidenceReportArtifactLink,
    FeishuEvidenceDocumentBinding,
    PreliminaryEvidenceReport,
)
from app.db.models import AnalyzerRun, Artifact, Evidence
from app.db.session import SessionLocal
from app.integrations.feishu.evidence_document_human_v2 import HumanFeishuEvidenceDocumentService
from app.services.evidence_report import generate_evidence_report


REAL_GOLDEN_001_SHA256 = "b038aa7c9a0644581f2815f654fcdee4620860796382265b178823fccba2e3f0"
HUMAN_CONTRACT = "feishu-evidence-living-document-human-v2"
PREFLIGHT_CONTRACT = "voip-live-acceptance-preflight-v1"
REQUIRED_VISUAL_KINDS = {"DTMF_INSPECTOR", "SPECTRUM", "SPECTROGRAM"}
REQUIRED_GOLDEN_ANALYZERS = {"packet_intelligence", "media_intelligence", "pcm_intelligence"}
EXPLANATION_KEYS = ("what_to_look_at", "meaning", "evidence_boundary", "plain_language_summary")
LIVE_LABELS = (
    "📖 这张图怎么看：",
    "🔎 图中发现：",
    "💡 这意味着：",
    "⚠️ 证据边界：",
    "✅ 一句话结论：",
)


def _fingerprint(value: str | None) -> str | None:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:12] if value else None


def _snapshot_contains_golden(report: PreliminaryEvidenceReport) -> bool:
    material = json.dumps(report.snapshot_json or {}, ensure_ascii=False, sort_keys=True)
    return REAL_GOLDEN_001_SHA256 in material


def _case_has_exact_golden(db, case_id: str) -> bool:
    return db.scalar(
        select(Evidence.id)
        .where(Evidence.case_id == case_id, Evidence.sha256 == REAL_GOLDEN_001_SHA256)
        .limit(1)
    ) is not None


def _case_has_required_analyzers(db, case_id: str) -> bool:
    successful = set(db.scalars(
        select(AnalyzerRun.analyzer_name).where(
            AnalyzerRun.case_id == case_id,
            AnalyzerRun.status == "SUCCESS",
            AnalyzerRun.analyzer_name.in_(REQUIRED_GOLDEN_ANALYZERS),
        )
    ))
    return REQUIRED_GOLDEN_ANALYZERS.issubset({str(x) for x in successful})


def _ready_human(meta: dict | None) -> bool:
    meta = meta or {}
    explanation = meta.get("human_explanation")
    if str(meta.get("renderer_family") or "").upper() != "HUMAN":
        return False
    if meta.get("annotation_complete") is not True or not isinstance(explanation, dict):
        return False
    if any(not str(explanation.get(key) or "").strip() for key in EXPLANATION_KEYS):
        return False
    return str(explanation.get("diagnostic_authority") or "NONE").upper() == "NONE"


def _require_preflight(path: Path) -> dict:
    if not path.is_file():
        raise RuntimeError("LIVE_ACCEPTANCE_PREFLIGHT_RESULT_MISSING")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("contract") != PREFLIGHT_CONTRACT:
        raise RuntimeError("LIVE_ACCEPTANCE_PREFLIGHT_CONTRACT_INVALID")
    if payload.get("status") != "PASS" or payload.get("mutation_allowed") is not True:
        blockers = ",".join(str(x) for x in (payload.get("blocking_keys") or [])[:12])
        raise RuntimeError("LIVE_ACCEPTANCE_PREFLIGHT_BLOCKED:" + blockers)
    expected_revision = str(os.getenv("LIVE_ACCEPTANCE_SOURCE_REVISION") or "").strip()
    actual_revision = str(payload.get("source_revision") or "").strip()
    if not expected_revision or actual_revision != expected_revision:
        raise RuntimeError("LIVE_ACCEPTANCE_PREFLIGHT_REVISION_MISMATCH")
    if not str(payload.get("runtime_fingerprint") or "").strip():
        raise RuntimeError("LIVE_ACCEPTANCE_PREFLIGHT_RUNTIME_FINGERPRINT_MISSING")
    return payload


def _select_bound_golden(db):
    bindings = list(db.scalars(select(FeishuEvidenceDocumentBinding).where(
        FeishuEvidenceDocumentBinding.document_id.is_not(None),
        FeishuEvidenceDocumentBinding.projected_report_id.is_not(None),
    ).order_by(FeishuEvidenceDocumentBinding.updated_at.desc())))
    for binding in bindings:
        report = db.get(PreliminaryEvidenceReport, binding.projected_report_id)
        if report is None:
            continue
        if str(report.case_id) != str(binding.case_id):
            continue
        if not _case_has_exact_golden(db, str(binding.case_id)):
            continue
        if not _case_has_required_analyzers(db, str(binding.case_id)):
            continue
        return binding, report
    raise RuntimeError("NO_BOUND_REAL_GOLDEN_001_CASE_EVIDENCE")


def _ready_human_artifacts(db, report_id: str) -> list[Artifact]:
    rows = list(db.scalars(
        select(Artifact)
        .join(EvidenceReportArtifactLink, EvidenceReportArtifactLink.artifact_id == Artifact.id)
        .where(EvidenceReportArtifactLink.report_id == report_id)
        .order_by(Artifact.created_at.asc())
    ))
    return [row for row in rows if _ready_human(row.metadata_json)]


def _block_text(block: dict) -> str:
    parts: list[str] = []

    def walk(value):
        if isinstance(value, dict):
            text_run = value.get("text_run")
            if isinstance(text_run, dict) and text_run.get("content") is not None:
                parts.append(str(text_run.get("content")))
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(block)
    return "".join(parts)


async def _list_document_blocks(service: HumanFeishuEvidenceDocumentService, document_id: str) -> list[dict]:
    rows: list[dict] = []
    token: str | None = None
    for _ in range(20):
        params = {"page_size": "500"}
        if token:
            params["page_token"] = token
        response = await service.transport._request(
            "GET",
            f"/docx/v1/documents/{quote(document_id, safe='')}/blocks",
            params=params,
        )
        data = response.get("data") or {}
        rows.extend(data.get("items") or [])
        if not data.get("has_more"):
            break
        token = str(data.get("page_token") or "") or None
        if not token:
            raise RuntimeError("FEISHU_BLOCK_LIST_PAGINATION_TOKEN_MISSING")
    return rows


def _human_sequence_count(blocks: list[dict]) -> int:
    count = 0
    for index, block in enumerate(blocks):
        if int(block.get("block_type") or 0) != 27:
            continue
        cursor = index + 1
        matched = True
        for label in LIVE_LABELS:
            found = None
            for pos in range(cursor, min(len(blocks), index + 18)):
                if label in _block_text(blocks[pos]):
                    found = pos
                    break
            if found is None:
                matched = False
                break
            cursor = found + 1
        if matched:
            count += 1
    return count


async def run(result_path: Path, preflight_path: Path) -> dict:
    try:
        preflight = _require_preflight(preflight_path)
    except Exception as exc:
        return {
            "status": "FAIL",
            "contract": "human-evidence-feishu-live-acceptance-v1",
            "error_code": type(exc).__name__,
            "error_message": str(exc)[:300],
            "feishu_projection_attempted": False,
        }

    db = SessionLocal()
    projected = False
    try:
        binding, previous_report = _select_bound_golden(db)
        original_document_id = str(binding.document_id)
        original_projection_version = int(binding.projection_version or 0)

        report, payload, _reused = generate_evidence_report(
            db,
            scope_type=previous_report.scope_type,
            scope_id=previous_report.scope_id,
            actor="human-evidence-feishu-live-acceptance",
            force=True,
        )
        if not _snapshot_contains_golden(report):
            raise RuntimeError("REBUILT_REPORT_LOST_REAL_GOLDEN_001_BINDING")

        ready = _ready_human_artifacts(db, report.id)
        kinds = sorted({str((a.metadata_json or {}).get("visual_kind") or "") for a in ready if (a.metadata_json or {}).get("visual_kind")})
        missing = sorted(REQUIRED_VISUAL_KINDS - set(kinds))
        if not ready:
            raise RuntimeError("NO_READY_HUMAN_VISUALS")
        if missing:
            raise RuntimeError("MISSING_REQUIRED_HUMAN_VISUALS:" + ",".join(missing))

        service = HumanFeishuEvidenceDocumentService()
        projected_binding = await service.project(db, case_id=report.case_id, report_id=report.id)
        projected = True
        if str(projected_binding.document_id) != original_document_id:
            raise RuntimeError("FEISHU_DOCUMENT_ID_CHANGED")
        if int(projected_binding.projection_version or 0) <= original_projection_version:
            raise RuntimeError("FEISHU_PROJECTION_VERSION_NOT_ADVANCED")
        metadata = projected_binding.metadata_json or {}
        if metadata.get("living_document_contract") != HUMAN_CONTRACT:
            raise RuntimeError("FEISHU_HUMAN_V2_CONTRACT_NOT_PERSISTED")
        db.commit()

        blocks = await _list_document_blocks(service, original_document_id)
        image_count = sum(1 for block in blocks if int(block.get("block_type") or 0) == 27)
        human_sequences = _human_sequence_count(blocks)
        if image_count < 1:
            raise RuntimeError("FEISHU_LIVE_IMAGE_BLOCK_MISSING")
        if human_sequences < 1:
            raise RuntimeError("FEISHU_LIVE_HUMAN_EXPLANATION_SEQUENCE_MISSING")

        return {
            "status": "PASS",
            "contract": "human-evidence-feishu-live-acceptance-v1",
            "preflight_contract": preflight.get("contract"),
            "runtime_fingerprint": preflight.get("runtime_fingerprint"),
            "source_revision": preflight.get("source_revision"),
            "golden_case": "OFFLINE_ANALYSIS_20260814_001",
            "golden_identity_source": "BOUND_CASE_EVIDENCE_SHA256",
            "golden_sha256_verified": True,
            "document_fingerprint": _fingerprint(original_document_id),
            "document_reused": True,
            "previous_report_version": previous_report.version,
            "projected_report_version": report.version,
            "projection_version": projected_binding.projection_version,
            "living_document_contract": metadata.get("living_document_contract"),
            "ready_human_visual_count": len(ready),
            "ready_human_visual_kinds": kinds,
            "required_visual_kinds_present": not missing,
            "remote_image_block_count": image_count,
            "remote_human_sequence_count": human_sequences,
            "image_then_five_explanations_verified": True,
            "diagnostic_authority_escalation": False,
        }
    except Exception as exc:
        if not projected:
            db.rollback()
        return {
            "status": "FAIL",
            "contract": "human-evidence-feishu-live-acceptance-v1",
            "preflight_contract": preflight.get("contract"),
            "runtime_fingerprint": preflight.get("runtime_fingerprint"),
            "source_revision": preflight.get("source_revision"),
            "error_code": type(exc).__name__,
            "error_message": str(exc)[:300],
            "feishu_projection_attempted": projected,
        }
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--result", default="validation/human_evidence_feishu_live_acceptance.json")
    parser.add_argument("--preflight-result", required=True)
    args = parser.parse_args()
    path = Path(args.result)
    path.parent.mkdir(parents=True, exist_ok=True)
    result = asyncio.run(run(path, Path(args.preflight_result)))
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
