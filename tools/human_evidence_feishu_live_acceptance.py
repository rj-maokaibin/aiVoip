from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import quote

from sqlalchemy import select

from app.analyzers.pcm import PcmIntelligenceEngine
from app.db.evidence_report_models import (
    EvidenceReportArtifactLink,
    FeishuEvidenceDocumentBinding,
    PreliminaryEvidenceReport,
)
from app.db.models import AnalyzerRun, Artifact, Evidence
from app.db.session import SessionLocal
from app.integrations.feishu.evidence_document_human_v2 import HumanFeishuEvidenceDocumentService
from app.integrations.storage import ObjectStorage
from app.services.analysis import create_media_analysis_job, create_pcm_analysis_job
from app.services.evidence_report import generate_evidence_report
from app.workers.media_tasks import MEDIA_GATED_ANALYZER_VERSION, analyze_media_evidence
from app.workers.pcm_tasks import analyze_pcm_evidence


REAL_GOLDEN_001_SHA256 = "b038aa7c9a0644581f2815f654fcdee4620860796382265b178823fccba2e3f0"
GOLDEN_PCM_PROFILE_ID = "ruijie_aim_diag_v1"
GOLDEN_DTMF_DIGITS = "601"
EXPECTED_PCM_ANALYZER_VERSION = PcmIntelligenceEngine.analyzer_version
EXPECTED_MEDIA_ANALYZER_VERSION = MEDIA_GATED_ANALYZER_VERSION
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


def _golden_evidence(db, case_id: str) -> Evidence:
    evidence = db.scalar(
        select(Evidence)
        .where(Evidence.case_id == case_id, Evidence.sha256 == REAL_GOLDEN_001_SHA256)
        .order_by(Evidence.created_at.asc())
        .limit(1)
    )
    if evidence is None:
        raise RuntimeError("REAL_GOLDEN_001_EVIDENCE_MISSING")
    return evidence


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


def _latest_report_run(db, *, case_id: str, analyzer_name: str) -> AnalyzerRun | None:
    """Mirror CASE report run selection: newest run wins, before status/content checks."""
    return db.scalar(
        select(AnalyzerRun)
        .where(
            AnalyzerRun.case_id == case_id,
            AnalyzerRun.analyzer_name == analyzer_name,
        )
        .order_by(AnalyzerRun.created_at.desc())
        .limit(1)
    )


def _run_uses_evidence(run: AnalyzerRun | None, evidence_id: str) -> bool:
    if run is None:
        return False
    return str(evidence_id) in {str(x) for x in (run.input_evidence_ids or [])}


def _load_run_result(storage: ObjectStorage, run: AnalyzerRun | None) -> dict:
    if run is None or not str(run.result_object_key or "").strip():
        return {}
    try:
        return json.loads(storage.get_bytes(run.result_object_key).decode("utf-8"))
    except Exception:
        return {}


def _pcm_601_candidates(pcm: dict) -> set[tuple[str, int]]:
    candidates: set[tuple[str, int]] = set()
    for stream in pcm.get("streams", []) or []:
        tap = stream.get("tap") or {}
        if str(tap.get("direction") or "").upper() != "RX":
            continue
        tap_name = str(tap.get("name") or "")
        if not tap_name:
            continue
        for session in stream.get("sessions", []) or []:
            sequences = [str(x.get("digits") or "") for x in (session.get("dtmf_sequences") or [])]
            if GOLDEN_DTMF_DIGITS not in sequences:
                continue
            events = list(session.get("dtmf_events") or [])
            if not any(str(x.get("digit") or "") == GOLDEN_DTMF_DIGITS[0] for x in events):
                continue
            candidates.add((tap_name, int(session.get("session_index") or 0)))
    return candidates


def _media_601_matches(media: dict) -> set[tuple[str, int]]:
    matches: set[tuple[str, int]] = set()
    for event in media.get("cross_layer_events", []) or []:
        if str(event.get("type") or "") != "DTMF_SIP_DIAL_MATCH":
            continue
        details = event.get("details") or {}
        if str(details.get("pcm_digits") or "") != GOLDEN_DTMF_DIGITS:
            continue
        if str(details.get("sip_target") or "") != GOLDEN_DTMF_DIGITS:
            continue
        scope = event.get("scope") or {}
        tap = str(scope.get("pcm_tap") or details.get("pcm_tap") or "")
        idx = scope.get("pcm_session_index", details.get("pcm_session_index"))
        if not tap or idx is None:
            continue
        matches.add((tap, int(idx)))
    return matches


def _media_pcm_wav_scopes(db, media_run: AnalyzerRun | None) -> set[tuple[str, int]]:
    if media_run is None:
        return set()
    rows = list(db.scalars(select(Artifact).where(
        Artifact.analyzer_run_id == media_run.id,
        Artifact.type == "PCM_WAV",
    )))
    scopes: set[tuple[str, int]] = set()
    for row in rows:
        meta = row.metadata_json or {}
        tap = str(meta.get("pcm_tap") or "")
        if tap:
            scopes.add((tap, int(meta.get("session_index") or 0)))
    return scopes


def _dtmf_source_readiness(db, storage: ObjectStorage, *, case_id: str, evidence: Evidence) -> dict:
    pcm_run = _latest_report_run(db, case_id=case_id, analyzer_name="pcm_intelligence")
    media_run = _latest_report_run(db, case_id=case_id, analyzer_name="media_intelligence")

    pcm_status = str(pcm_run.status or "") if pcm_run is not None else None
    media_status = str(media_run.status or "") if media_run is not None else None
    pcm_exact = _run_uses_evidence(pcm_run, str(evidence.id))
    media_exact = _run_uses_evidence(media_run, str(evidence.id))
    pcm_version = str(pcm_run.analyzer_version or "") if pcm_run is not None else None
    media_version = str(media_run.analyzer_version or "") if media_run is not None else None
    pcm_version_current = pcm_version == EXPECTED_PCM_ANALYZER_VERSION
    media_version_current = media_version == EXPECTED_MEDIA_ANALYZER_VERSION

    pcm = _load_run_result(storage, pcm_run) if pcm_status == "SUCCESS" and pcm_exact else {}
    media = _load_run_result(storage, media_run) if media_status == "SUCCESS" and media_exact else {}
    pcm_candidates = _pcm_601_candidates(pcm)
    media_matches = _media_601_matches(media)
    media_wavs = _media_pcm_wav_scopes(db, media_run) if media_exact else set()
    coherent = pcm_candidates & media_matches & media_wavs

    reasons: list[str] = []
    if pcm_run is None:
        reasons.append("PCM_REPORT_RUN_MISSING")
    elif pcm_status != "SUCCESS":
        reasons.append("PCM_REPORT_RUN_NOT_SUCCESS")
    elif not pcm_exact:
        reasons.append("PCM_REPORT_RUN_NOT_GOLDEN_EVIDENCE")
    elif not pcm_version_current:
        reasons.append("PCM_ANALYZER_VERSION_STALE")
    elif not pcm_candidates:
        reasons.append("PCM_601_ACCEPTED_EVENT_MISSING")

    if media_run is None:
        reasons.append("MEDIA_REPORT_RUN_MISSING")
    elif media_status != "SUCCESS":
        reasons.append("MEDIA_REPORT_RUN_NOT_SUCCESS")
    elif not media_exact:
        reasons.append("MEDIA_REPORT_RUN_NOT_GOLDEN_EVIDENCE")
    elif not media_version_current:
        reasons.append("MEDIA_ANALYZER_VERSION_STALE")
    elif not media_matches:
        reasons.append("MEDIA_601_SIP_MATCH_MISSING")
    elif not (media_matches & media_wavs):
        reasons.append("MEDIA_601_PCM_WAV_MISSING")

    if pcm_candidates and media_matches and media_wavs and not coherent:
        reasons.append("DTMF_SCOPE_COHERENCE_MISSING")

    pcm_ready = bool(
        pcm_run is not None
        and pcm_status == "SUCCESS"
        and pcm_exact
        and pcm_version_current
        and pcm_candidates
    )
    media_ready = bool(
        media_run is not None
        and media_status == "SUCCESS"
        and media_exact
        and media_version_current
        and (media_matches & media_wavs)
    )
    return {
        "ready": pcm_ready and media_ready and bool(coherent) and not reasons,
        "pcm_ready": pcm_ready,
        "media_ready": media_ready,
        "scope_coherent": bool(coherent),
        "reason_codes": reasons,
        "pcm_content_ready": bool(pcm_candidates),
        "media_content_ready": bool(media_matches & media_wavs),
        "pcm_report_run_status": pcm_status,
        "media_report_run_status": media_status,
        "pcm_report_run_exact_golden": pcm_exact,
        "media_report_run_exact_golden": media_exact,
        "pcm_analyzer_version": pcm_version,
        "media_analyzer_version": media_version,
        "expected_pcm_analyzer_version": EXPECTED_PCM_ANALYZER_VERSION,
        "expected_media_analyzer_version": EXPECTED_MEDIA_ANALYZER_VERSION,
    }


def _refresh_stale_golden_dtmf(db, *, case_id: str) -> dict:
    evidence = _golden_evidence(db, case_id)
    storage = ObjectStorage()
    before = _dtmf_source_readiness(db, storage, case_id=case_id, evidence=evidence)
    components: list[str] = []

    if not before["pcm_ready"]:
        job = create_pcm_analysis_job(
            db, case_id=case_id, evidence_id=str(evidence.id), profile_id=GOLDEN_PCM_PROFILE_ID
        )
        result = analyze_pcm_evidence.run(str(job.id), str(evidence.id), GOLDEN_PCM_PROFILE_ID, False)
        if str((result or {}).get("status") or "") != "SUCCESS":
            raise RuntimeError("GOLDEN_PCM_REFRESH_FAILED")
        components.append("pcm_intelligence")
        db.expire_all()

    intermediate = _dtmf_source_readiness(db, storage, case_id=case_id, evidence=evidence)
    if not intermediate["media_ready"] or not intermediate["scope_coherent"]:
        job = create_media_analysis_job(
            db, case_id=case_id, evidence_id=str(evidence.id), profile_id=GOLDEN_PCM_PROFILE_ID
        )
        result = analyze_media_evidence.run(str(job.id), str(evidence.id), GOLDEN_PCM_PROFILE_ID, False)
        if str((result or {}).get("status") or "") != "SUCCESS":
            raise RuntimeError("GOLDEN_MEDIA_REFRESH_FAILED")
        components.append("media_intelligence")
        db.expire_all()

    after = _dtmf_source_readiness(db, storage, case_id=case_id, evidence=evidence)
    if not after["ready"]:
        reasons = ",".join(after.get("reason_codes") or ["UNKNOWN"])
        raise RuntimeError("GOLDEN_DTMF_SOURCE_NOT_READY:" + reasons)
    return {
        "performed": bool(components),
        "components": components,
        "before": before,
        "after": after,
    }


def _verify_rebuilt_golden_identity(db, *, binding, previous_report, report, payload: dict) -> None:
    if str(previous_report.case_id) != str(binding.case_id):
        raise RuntimeError("PREVIOUS_REPORT_CASE_BINDING_MISMATCH")
    if str(report.case_id) != str(binding.case_id):
        raise RuntimeError("REBUILT_REPORT_CASE_CHANGED")
    if str(report.scope_type) != str(previous_report.scope_type) or str(report.scope_id) != str(previous_report.scope_id):
        raise RuntimeError("REBUILT_REPORT_SCOPE_CHANGED")
    if int(report.version or 0) <= int(previous_report.version or 0):
        raise RuntimeError("REBUILT_REPORT_VERSION_NOT_ADVANCED")
    if not _case_has_exact_golden(db, str(report.case_id)):
        raise RuntimeError("REBUILT_REPORT_GOLDEN_EVIDENCE_MISSING")
    if not _case_has_required_analyzers(db, str(report.case_id)):
        raise RuntimeError("REBUILT_REPORT_GOLDEN_ANALYZERS_MISSING")
    scope = payload.get("scope") or {}
    if str(scope.get("type") or "") != str(report.scope_type) or str(scope.get("id") or "") != str(report.scope_id):
        raise RuntimeError("REBUILT_REPORT_PAYLOAD_SCOPE_MISMATCH")
    if not str(payload.get("input_snapshot_hash") or "").strip():
        raise RuntimeError("REBUILT_REPORT_INPUT_SNAPSHOT_HASH_MISSING")


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
    refresh = {"performed": False, "components": [], "before": None, "after": None}
    try:
        binding, previous_report = _select_bound_golden(db)
        original_document_id = str(binding.document_id)
        original_projection_version = int(binding.projection_version or 0)

        refresh = _refresh_stale_golden_dtmf(db, case_id=str(binding.case_id))

        report, payload, _reused = generate_evidence_report(
            db,
            scope_type=previous_report.scope_type,
            scope_id=previous_report.scope_id,
            actor="human-evidence-feishu-live-acceptance",
            force=True,
        )
        _verify_rebuilt_golden_identity(
            db,
            binding=binding,
            previous_report=previous_report,
            report=report,
            payload=payload,
        )

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
            "rebuilt_golden_identity_verified": True,
            "analyzer_refresh_performed": refresh["performed"],
            "analyzer_refresh_components": refresh["components"],
            "dtmf_source_readiness_before": refresh["before"],
            "dtmf_source_readiness_after": refresh["after"],
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
            "analyzer_refresh_performed": refresh.get("performed", False),
            "analyzer_refresh_components": refresh.get("components") or [],
            "dtmf_source_readiness_before": refresh.get("before"),
            "dtmf_source_readiness_after": refresh.get("after"),
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
