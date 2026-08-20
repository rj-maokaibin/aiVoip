#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class Gate:
    key: str
    status: str
    category: str
    blocking: bool
    detail: str


def run(key: str, command: list[str], *, category: str, timeout: int = 120) -> Gate:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "backend") + os.pathsep + str(ROOT)
    try:
        cp = subprocess.run(command, cwd=ROOT, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
        detail = cp.stdout[-3000:].strip()
        return Gate(key, "PASS" if cp.returncode == 0 else "FAIL", category, True, detail)
    except subprocess.TimeoutExpired:
        return Gate(key, "FAIL", category, True, f"timeout after {timeout}s")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=ROOT / "validation" / "evidence_report_release_gate.json")
    ap.add_argument("--skip-tests", action="store_true")
    args = ap.parse_args()

    gates: list[Gate] = []
    gates.append(run("GOLDEN_SYNTHETIC", [sys.executable, "tools/evidence_report_golden_gate.py"], category="ACCURACY"))
    gates.append(run("PERFORMANCE_SOFTWARE_CORE", [sys.executable, "tools/evidence_report_performance_gate.py", "--iterations", "4"], category="PERFORMANCE", timeout=180))
    if not args.skip_tests:
        gates.append(run(
            "EVIDENCE_REPORT_REGRESSION",
            [sys.executable, "-m", "pytest", "-q",
             "backend/tests/test_preliminary_evidence_report_v1.py",
             "backend/tests/test_evidence_report_offline_context_v1.py",
             "backend/tests/test_evidence_report_context_provenance_v1.py",
             "backend/tests/test_offline_subject_call_selection_v1.py",
             "backend/tests/test_pcm_source_provenance_v1.py",
             "backend/tests/test_dtmf_subject_call_correlation_v1.py",
             "backend/tests/test_rtp_high_delta_semantics_v1.py",
             "backend/tests/test_rtp_high_delta_report_v1.py",
             "backend/tests/test_rtp_stream_call_binding_v1.py",
             "backend/tests/test_candidate_decision_negative_control_v1.py",
             "backend/tests/test_candidate_decision_rtp_activity_v1.py",
             "backend/tests/test_candidate_decision_field_regression_v1.py",
             "backend/tests/test_candidate_audio_artifacts_v1.py",
             "backend/tests/test_candidate_decision_profile_v1.py",
             "backend/tests/test_candidate_decision_diagnosis_boundary_v1.py",
             "backend/tests/test_offline_analysis_golden_e2e_v1.py",
             "backend/tests/test_offline_analysis_golden_extended_v1.py",
             "backend/tests/test_offline_analysis_golden_manifest_v1.py",
             "backend/tests/test_evidence_report_authority_v1.py",
             "backend/tests/test_evidence_bundle_contract_v1.py",
             "backend/tests/test_evidence_bundle_profile_v1.py",
             "backend/tests/test_feishu_evidence_document_v1.py",
             "backend/tests/test_rtp_frame_evidence_v1.py",
             "backend/tests/test_dtmf_finding_v1.py",
             "backend/tests/test_evidence_card_readability_v1.py",
             "backend/tests/test_evidence_artifact_binding_v1.py",
             "backend/tests/test_visual_annotation_contract_v1.py",
             "backend/tests/test_sip_flow_visual_v1.py",
             "backend/tests/test_evidence_card_artifact_permissions_v1.py",
             "backend/tests/test_report_grounding_validator_v1.py",
             "backend/tests/test_report_grounding_replay_boundary_v1.py",
             "backend/tests/test_evidence_retention_v1.py",
             "backend/tests/test_evidence_report_retention_expiry_v1.py",
             "backend/tests/test_evidence_permissions_v1.py",
             "backend/tests/test_evidence_report_metrics_v1.py"],
            category="REGRESSION", timeout=180,
        ))

    software_pass = all(x.status == "PASS" for x in gates)
    environment_gates = [
        {"key": "LIVE_FEISHU_TENANT", "status": "UNVERIFIED", "blocking_for_production": True,
         "detail": "需要真实飞书应用/Tenant 验证 Evidence Card 内联 PNG/WAV、权限、媒体替换、Grounding 状态投影和卡片更新。"},
        {"key": "REAL_DUT_END_TO_END", "status": "UNVERIFIED", "blocking_for_production": True,
         "detail": "需要真实 DUT 完成 PCAP+PCM RX/TX→Call→Analyzer→Report→Bundle→Cleanup；Offline Golden 不覆盖采集可靠性。"},
    ]

    offline_fixture = os.getenv("VOIP_OFFLINE_GOLDEN_001_PCAP")
    if offline_fixture:
        offline_gate = run(
            "OFFLINE_ANALYSIS_GOLDEN_001",
            [sys.executable, "tools/offline_analysis_golden_replay.py",
             "--pcap", offline_fixture,
             "--require-fixture",
             "--result", str(ROOT / "validation" / "offline_analysis_golden_001.json"),
             "--artifacts", str(ROOT / "validation" / "offline_analysis_golden_001_artifacts")],
            category="REAL_GOLDEN", timeout=600,
        )
        environment_gates.append({"key": offline_gate.key, "status": offline_gate.status, "blocking_for_production": True, "detail": offline_gate.detail})
    else:
        environment_gates.append({
            "key": "OFFLINE_ANALYSIS_GOLDEN_001",
            "status": "UNVERIFIED",
            "blocking_for_production": True,
            "detail": "设置 VOIP_OFFLINE_GOLDEN_001_PCAP 指向 SHA256=b038aa7c...e3f0 的外部 PCAP fixture 后，执行真实 Imported-Evidence Golden E2E，并验证 Evidence Card 图/音频/Frame 与 Grounding 语义。",
        })

    environment_gates.append({
        "key": "BROADER_REAL_GOLDEN_DATASET",
        "status": "UNVERIFIED",
        "blocking_for_production": True,
        "detail": "单个 Offline Golden 不能代表最终 Recall/Precision、所有 Evidence Card 可读性或所有 Grounding 规则；仍需 Synthetic + Lab Real + 更多 Field/Imported Confirmed 样本覆盖正常、负控及各故障族。",
    })
    environment_fail = any(x.get("blocking_for_production") and x.get("status") == "FAIL" for x in environment_gates)
    environment_pending = any(x.get("blocking_for_production") and x.get("status") == "UNVERIFIED" for x in environment_gates)
    if not software_pass:
        production_status = "SOFTWARE_BLOCKED"
    elif environment_fail:
        production_status = "ENVIRONMENT_GATE_FAILED"
    elif environment_pending:
        production_status = "ENVIRONMENT_GATES_PENDING"
    else:
        production_status = "PASS"

    payload = {
        "schema_version": "preliminary-evidence-report-release-gate-v1",
        "software_status": "PASS" if software_pass else "FAIL",
        "production_status": production_status,
        "gates": [asdict(x) for x in gates],
        "environment_gates": environment_gates,
        "allowed_pending_environment_gates": [x["key"] for x in environment_gates if x.get("status") == "UNVERIFIED"],
        "claim": "software_status covers machine-verifiable software gates, including PR5 Evidence Card traceability/renderer/permission boundaries and PR6 Structural/Semantic/Evidence/Explainability Grounding rules, in-memory replay/publication boundary separation, and deterministic Claim Manifest. production_status also evaluates configured real/offline Golden fixtures; missing external fixtures remain explicit UNVERIFIED gates rather than being silently treated as PASS.",
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"software_status": payload["software_status"], "production_status": payload["production_status"], "out": str(args.out)}, ensure_ascii=False, indent=2))
    return 0 if software_pass and not environment_fail else 2


if __name__ == "__main__":
    raise SystemExit(main())
