#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# shellcheck source=tools/ci_dependency_runtime.sh
source tools/ci_dependency_runtime.sh

PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_BASE="${RUNNER_TEMP:-/tmp}"
VENV_DIR="${PRELIMINARY_EVIDENCE_V1_VENV:-${VOIP_AI_GATE_VENV:-$VENV_BASE/voip-ai-acceptance-runtime}}"

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

command -v "$PYTHON_BIN" >/dev/null 2>&1 || { echo "missing python: $PYTHON_BIN" >&2; exit 1; }

printf 'PRELIMINARY_EVIDENCE_V1_GATE_HEAD=%s\n' "$(git rev-parse HEAD)"
printf 'PRELIMINARY_EVIDENCE_V1_GATE_PYTHON=%s\n' "$($PYTHON_BIN --version 2>&1)"
printf 'PRELIMINARY_EVIDENCE_V1_GATE_VENV=%s\n' "$VENV_DIR"

ci_prepare_python_runtime "$VENV_DIR" backend/requirements.txt
# shellcheck disable=SC1090
source "$VENV_DIR/bin/activate"

export PYTHONPATH="backend:${PYTHONPATH:-}"
ci_run_timed frozen_contract_compile python -m compileall -q backend/app "${TESTS[@]}"
ci_run_timed frozen_contract_tests python -m pytest -q "${TESTS[@]}"
printf 'PRELIMINARY_EVIDENCE_V1_GATE=PASS\n'
