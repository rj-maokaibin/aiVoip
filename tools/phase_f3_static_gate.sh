#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"
export PYTHONPATH="$ROOT/backend"
mkdir -p validation

python -m compileall -q backend/app backend/tests deploy tools
bash -n deploy/voip-ai
pytest -q backend/tests | tee validation/phase_f3_pytest.txt
python tools/migration_contract_gate.py | tee validation/phase_f3_migration_gate.json
python tools/openapi_contract_gate.py | tee validation/phase_f3_openapi_gate.json
python tools/compose_contract_gate.py | tee validation/phase_f3_compose_gate.json
python tools/security_release_gate.py | tee validation/phase_f3_security_gate.json
python tools/production_hardening_gate.py | tee validation/phase_f3_production_hardening_gate.json
python tools/deployment_contract_gate.py | tee validation/phase_f3_deployment_contract_gate.json
python tools/check_profiles.py > validation/phase_f3_profiles.txt
python tools/platform_contract_gate.py | tee validation/phase_f3_platform_contract.json
python tools/reproduction_profile_gate.py | tee validation/phase_f3_reproduction_profiles.json
python tools/phase_c3_profile_gate.py | tee validation/phase_f3_c3_profiles.json
python tools/reproduction_mock_e2e.py | tee validation/phase_f3_reproduction_mock_e2e.json
python tools/reproduction_evidence_e2e.py | tee validation/phase_f3_reproduction_evidence_e2e.json
python tools/reproduction_c3_e2e.py | tee validation/phase_f3_reproduction_c3_e2e.json
python tools/rule_validate.py > validation/phase_f3_rules.txt
python tools/golden_synthetic_replay.py --out-dir .f3-golden-artifacts --result validation/phase_f3_synthetic_golden.json
python tools/e2e_replay.py --result validation/phase_f3_synthetic_e2e.json
python tools/e2e_diff.py e2e_baselines/v1.json validation/phase_f3_synthetic_e2e.json --out validation/phase_f3_e2e_diff.md | tee validation/phase_f3_e2e_diff_result.json
python tools/workbench_contract_gate.py | tee validation/phase_f3_workbench_gate.json
python tools/source_manifest_gate.py | tee validation/phase_f3_source_manifest_gate.json

python - <<'PY'
import json,re
from pathlib import Path
root=Path('.')
manifest=json.loads((root/'release/source_manifest.json').read_text())
pytest_text=(root/'validation/phase_f3_pytest.txt').read_text()
m=re.search(r'(\d+) passed',pytest_text)
backend_tests=int(m.group(1)) if m else None
payload={
 'schema_version':1,'status':'PASS','source_manifest_aggregate_sha256':manifest['aggregate_sha256'],
 'backend_tests':{'passed':backend_tests},
 'gates':['PYTHON_COMPILE','SHELL_SYNTAX','BACKEND_TESTS','MIGRATION_CONTRACT','OPENAPI_CONTRACT','COMPOSE_CONTRACT','SECURITY_CONTRACT','PRODUCTION_HARDENING','DEPLOYMENT_CONTRACT','PROFILES','PLATFORM_CONTRACT','REPRODUCTION_PROFILES','DIAGNOSTIC_EXPERIMENT_PROFILES','REPRODUCTION_MOCK_E2E','REPRODUCTION_EVIDENCE_E2E','REPRODUCTION_C3_E2E','RULES','SYNTHETIC_GOLDEN','SYNTHETIC_E2E','BASELINE_DIFF','WORKBENCH_CONTRACT','SOURCE_MANIFEST']
}
(root/'validation/phase_f3_static_gate.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'status':'PASS','backend_tests':backend_tests,'static_gates':len(payload['gates']),'source_manifest':manifest['aggregate_sha256']},ensure_ascii=False))
PY
