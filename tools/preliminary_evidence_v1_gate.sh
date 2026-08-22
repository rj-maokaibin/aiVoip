#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
export PYTHONPATH="backend:${PYTHONPATH:-}"

TESTS=(
  backend/tests/test_prd_spec_v1_alignment.py
  backend/tests/test_evidence_visuals_spec_v1.py
  backend/tests/test_evidence_report_interrupted_call_v1.py
  backend/tests/test_evidence_report_no_valid_call_v1.py
  backend/tests/test_evidence_bundle_frozen_v1.py
  backend/tests/test_evidence_report_web_drilldown_v1.py
  backend/tests/test_evidence_retention_download_v1.py
  backend/tests/test_evidence_audit_contract_v1.py
  backend/tests/test_feishu_case_card_update_contract_v1.py
  backend/tests/test_feishu_living_document_v1.py
)

printf 'PRELIMINARY_EVIDENCE_V1_GATE_HEAD=%s\n' "$(git rev-parse HEAD)"
python3 -m compileall -q backend/app "${TESTS[@]}"
pytest -q "${TESTS[@]}"
printf 'PRELIMINARY_EVIDENCE_V1_GATE=PASS\n'
