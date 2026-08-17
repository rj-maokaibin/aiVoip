#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from app.contracts.enums import CausalConclusionState, FixVerificationStatus, HypothesisState
from app.db.models import (
    AuditLog,
    Case,
    CausalAssessment,
    DiagnosisRun,
    Evidence,
    FixVerificationRun,
    Hypothesis,
)
from app.db.session import SessionLocal
from app.diagnosis.discriminating_planner import infer_symptom
from app.diagnosis.snapshot import CaseEvidenceSnapshotBuilder
from app.golden.service import GoldenCandidateService


_CATEGORY_MAP = {
    "REGISTER_FAILURE": "REGISTER_FAILURE",
    "CALL_SETUP_FAILURE": "INVITE_FAILURE",
    "ONE_WAY_AUDIO": "ONE_WAY_AUDIO",
    "AUDIO_STUTTER": "RTP_LOSS_JITTER_STUTTER",
    "AUDIO_NOISE": "NOISE_INTERFERENCE",
    "DTMF_LOSS": "DTMF_FIRST_DIGIT_LOSS",
    "ECHO": "ECHO",
}


def _category(summary: str) -> str:
    symptom = infer_symptom(summary)
    return _CATEGORY_MAP.get(symptom or "", symptom or "FIELD_OTHER")


def _latest_baseline(db, case_id: str) -> dict:
    row = db.scalar(
        select(DiagnosisRun)
        .where(DiagnosisRun.case_id == case_id)
        .order_by(DiagnosisRun.created_at.desc())
        .limit(1)
    )
    return dict(row.decision_json or {}) if row else {}


def _verification_status(db, case_id: str) -> str | None:
    verified = db.scalar(
        select(FixVerificationRun.id)
        .where(
            FixVerificationRun.case_id == case_id,
            FixVerificationRun.status == FixVerificationStatus.FIX_VERIFIED.value,
        )
        .limit(1)
    )
    if verified:
        return "FIX_VERIFIED"
    causal = db.scalar(
        select(CausalAssessment.id)
        .where(
            CausalAssessment.case_id == case_id,
            CausalAssessment.state == CausalConclusionState.ROOT_CAUSE_CONFIRMED.value,
        )
        .limit(1)
    )
    if causal:
        return "ROOT_CAUSE_CONFIRMED"
    confirmed = db.scalar(
        select(Hypothesis.id)
        .where(
            Hypothesis.case_id == case_id,
            Hypothesis.status == HypothesisState.CONFIRMED.value,
        )
        .limit(1)
    )
    return "ROOT_CAUSE_CONFIRMED" if confirmed else None


def _confirmed_hypotheses(db, case_id: str) -> list[Hypothesis]:
    return list(db.scalars(
        select(Hypothesis)
        .where(
            Hypothesis.case_id == case_id,
            Hypothesis.status == HypothesisState.CONFIRMED.value,
        )
        .order_by(Hypothesis.confidence.desc(), Hypothesis.created_at.asc())
    ))


def _audit_rows(db, case_ids: list[str]) -> list[dict]:
    if not case_ids:
        return []
    rows = list(db.scalars(
        select(AuditLog)
        .where(AuditLog.case_id.in_(case_ids))
        .order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
    ))
    return [
        {
            "id": row.id,
            "case_id": row.case_id,
            "actor": row.actor,
            "actor_type": row.actor_type,
            "event_type": row.event_type,
            "action": row.action,
            "target_type": row.target_type,
            "target_id": row.target_id,
            "detail": row.detail or {},
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in rows
    ]


def export_dataset(*, limit: int = 100, case_nos: list[str] | None = None,
                   require_golden_ready: bool = True) -> dict:
    db = SessionLocal()
    try:
        query = select(Case).order_by(Case.updated_at.desc()).limit(limit)
        if case_nos:
            query = select(Case).where(Case.case_no.in_(case_nos)).order_by(Case.updated_at.desc()).limit(limit)
        cases = list(db.scalars(query))
        builder = CaseEvidenceSnapshotBuilder()
        golden = GoldenCandidateService()
        exported = []
        selected_case_ids: list[str] = []
        skipped = []
        for case in cases:
            assessment = golden.assess(db, case.id)
            if require_golden_ready and assessment["status"] != "GOLDEN_READY":
                skipped.append({
                    "case_no": case.case_no,
                    "reason": f"GOLDEN_NOT_READY:{assessment['status']}",
                    "blocker_codes": assessment["blocker_codes"],
                    "gap_codes": assessment["gap_codes"],
                    "next_steps": assessment["next_steps"],
                })
                continue

            verification = _verification_status(db, case.id)
            hypotheses = _confirmed_hypotheses(db, case.id)
            if not verification or not hypotheses:
                skipped.append({
                    "case_no": case.case_no,
                    "reason": "NO_MACHINE_CONFIRMED_ROOT_CAUSE",
                })
                continue
            evidence_ids = list(db.scalars(select(Evidence.id).where(Evidence.case_id == case.id)))
            try:
                snapshot = builder.build(db, case.id)
            except Exception as exc:
                skipped.append({
                    "case_no": case.case_no,
                    "reason": f"SNAPSHOT_BUILD_FAILED:{type(exc).__name__}",
                })
                continue
            selected_case_ids.append(case.id)
            exported.append({
                "ground_truth": {
                    "case_id": case.id,
                    "category": _category(case.summary),
                    "source_kind": "REAL",
                    "verification_status": verification,
                    "verification_tier": assessment["verification_tier"],
                    "golden_status": assessment["status"],
                    "golden_assessment_version": assessment["schema_version"],
                    "expected_hypothesis_codes": [row.code for row in hypotheses],
                    "expected_fault_domains": sorted({row.fault_domain for row in hypotheses}),
                    "allowed_evidence_ids": evidence_ids,
                    "expected_question_keys": [],
                    "expected_profile_ids": [],
                    "required_behavior": ["NO_FALSE_ROOT_CAUSE"],
                    "notes": f"exported_from_case:{case.case_no}",
                },
                "snapshot": snapshot,
                "deterministic_baseline": _latest_baseline(db, case.id),
            })

        return {
            "schema_version": "ai-model-eval-dataset-v2",
            "dataset_id": f"field-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source": "VOIP_CASE_DATABASE",
            "audit_coverage_complete": True,
            "thresholds": {},
            "cases": exported,
            "audit_events": _audit_rows(db, selected_case_ids),
            "export_summary": {
                "selected_count": len(exported),
                "skipped_count": len(skipped),
                "skipped": skipped,
                "quality_rule": "GOLDEN_READY + REAL + CONFIRMED hypothesis + ROOT_CAUSE_CONFIRMED/FIX_VERIFIED",
                "require_golden_ready": require_golden_ready,
            },
        }
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Export verified VOIP field Cases for AI model Eval")
    parser.add_argument("--out", default="validation/ai_eval_field_dataset_v2.json")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--case-no", action="append", dest="case_nos")
    parser.add_argument("--require-minimum", type=int, default=0)
    parser.add_argument(
        "--allow-non-ready",
        action="store_true",
        help="Debug-only compatibility mode. Production Eval should export GOLDEN_READY only.",
    )
    args = parser.parse_args()

    payload = export_dataset(
        limit=max(1, args.limit), case_nos=args.case_nos,
        require_golden_ready=not args.allow_non_ready,
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload["export_summary"], ensure_ascii=False, indent=2))
    if len(payload["cases"]) < args.require_minimum:
        print(f"REAL_EVAL_SAMPLE_INSUFFICIENT:{len(payload['cases'])}<{args.require_minimum}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
