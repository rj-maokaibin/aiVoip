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
             "backend/tests/test_candidate_decision_negative_control_v1.py",
             "backend/tests/test_candidate_decision_artifact_gate_v1.py",
             "backend/tests/test_media_candidate_decision_engine_v1.py",
             "backend/tests/test_pcm_candidate_decision_audit_v1.py",
             "backend/tests/test_evidence_report_authority_v1.py",
             "backend/tests/test_evidence_bundle_contract_v1.py",
             "backend/tests/test_evidence_bundle_profile_v1.py",
             "backend/tests/test_feishu_evidence_document_v1.py",
             "backend/tests/test_rtp_frame_evidence_v1.py",
             "backend/tests/test_dtmf_finding_v1.py",
             "backend/tests/test_evidence_retention_v1.py",
             "backend/tests/test_evidence_report_retention_expiry_v1.py",
             "backend/tests/test_evidence_permissions_v1.py",
             "backend/tests/test_evidence_report_metrics_v1.py"],
            category="REGRESSION", timeout=180,
        ))

    software_pass = all(x.status == "PASS" for x in gates)
    environment_gates = [
        {"key": "LIVE_FEISHU_TENANT", "status": "UNVERIFIED", "blocking_for_production": True,
         "detail": "需要真实飞书应用/Tenant 验证 Docx Block、PNG/WAV、权限和卡片更新。"},
        {"key": "REAL_DUT_END_TO_END", "status": "UNVERIFIED", "blocking_for_production": True,
         "detail": "需要真实 DUT 完成 PCAP+PCM RX/TX→Call→Analyzer→Report→Bundle→Cleanup。"},
        {"key": "REAL_GOLDEN_DATASET", "status": "UNVERIFIED", "blocking_for_production": True,
         "detail": "需要 Synthetic + Lab Real + Field Confirmed 的真实标注集验证最终 Recall/Precision/Boundary 指标。"},
    ]
    payload = {
        "schema_version": "preliminary-evidence-report-release-gate-v1",
        "software_status": "PASS" if software_pass else "FAIL",
        "production_status": "ENVIRONMENT_GATES_PENDING" if software_pass else "SOFTWARE_BLOCKED",
        "gates": [asdict(x) for x in gates],
        "environment_gates": environment_gates,
        "allowed_pending_environment_gates": [x["key"] for x in environment_gates],
        "claim": "When software_status=PASS, all machine-verifiable V1.0 software requirements are closed. Production acceptance still requires exactly the three listed environment gates.",
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"software_status": payload["software_status"], "production_status": payload["production_status"], "out": str(args.out)}, ensure_ascii=False, indent=2))
    return 0 if software_pass else 2


if __name__ == "__main__":
    raise SystemExit(main())
