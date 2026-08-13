#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"
export PYTHONPATH="$ROOT/backend"
mkdir -p validation

python -m compileall -q backend/app backend/tests
pytest -q backend/tests | tee validation/phase_f1_pytest.txt
python tools/migration_contract_gate.py | tee validation/phase_f1_migration_gate.json
python tools/openapi_contract_gate.py | tee validation/phase_f1_openapi_gate.json
python tools/compose_contract_gate.py | tee validation/phase_f1_compose_gate.json
python tools/security_release_gate.py | tee validation/phase_f1_security_gate.json
python tools/source_manifest_gate.py | tee validation/phase_f1_source_manifest_gate.json
python tools/check_profiles.py > validation/phase_f1_profiles.txt
python tools/platform_contract_gate.py | tee validation/phase_f1_platform_contract.json
python tools/reproduction_profile_gate.py | tee validation/phase_f1_reproduction_profiles.json
python tools/phase_c3_profile_gate.py | tee validation/phase_f1_c3_profiles.json
python tools/reproduction_mock_e2e.py | tee validation/phase_f1_reproduction_mock_e2e.json
python tools/reproduction_evidence_e2e.py | tee validation/phase_f1_reproduction_evidence_e2e.json
python tools/reproduction_c3_e2e.py | tee validation/phase_f1_reproduction_c3_e2e.json
python tools/rule_validate.py > validation/phase_f1_rules.txt
python tools/golden_synthetic_replay.py --out-dir .f1-golden-artifacts --result validation/phase_f1_synthetic_golden.json
python tools/e2e_replay.py --result validation/phase_f1_synthetic_e2e.json
python tools/e2e_diff.py e2e_baselines/v1.json validation/phase_f1_synthetic_e2e.json --out validation/phase_f1_e2e_diff.md | tee validation/phase_f1_e2e_diff_result.json
python tools/workbench_contract_gate.py | tee validation/phase_f1_workbench_gate.json

python - <<'PY'
import json,re
from pathlib import Path
root=Path('.')
manifest=json.loads((root/'release/source_manifest.json').read_text())
pytest_text=(root/'validation/phase_f1_pytest.txt').read_text()
m=re.search(r'(\d+) passed',pytest_text)
backend_tests=int(m.group(1)) if m else None
mock=json.loads((root/'validation/phase_f1_reproduction_mock_e2e.json').read_text())
evidence=json.loads((root/'validation/phase_f1_reproduction_evidence_e2e.json').read_text())
c3=json.loads((root/'validation/phase_f1_reproduction_c3_e2e.json').read_text())
golden=json.loads((root/'validation/phase_f1_synthetic_golden.json').read_text())
e2e=json.loads((root/'validation/phase_f1_synthetic_e2e.json').read_text())
diff=json.loads((root/'validation/phase_f1_e2e_diff_result.json').read_text())
payload={
 'schema_version':1,'status':'PASS','source_manifest_aggregate_sha256':manifest['aggregate_sha256'],
 'backend_tests':{'passed':backend_tests},
 'reproduction_mock_e2e':{'passed':mock['passed'],'total':mock['total']},
 'reproduction_evidence_e2e':{'passed':evidence['passed'],'total':evidence['total']},
 'reproduction_c3_e2e':{'passed':c3['passed'],'total':c3['total']},
 'synthetic_golden':{'passed':golden['checks_passed'],'total':golden['checks_total']},
 'synthetic_e2e':{'passed':e2e['checks_passed'],'total':e2e['checks_total']},
 'baseline_diff':{'regressions':diff['regressions'],'changes':diff['changes']},
 'gates':['PYTHON_COMPILE','BACKEND_TESTS','MIGRATION_CONTRACT','OPENAPI_CONTRACT','COMPOSE_CONTRACT','SECURITY_CONTRACT','SOURCE_MANIFEST','PROFILES','PLATFORM_CONTRACT','REPRODUCTION_PROFILES','DIAGNOSTIC_EXPERIMENT_PROFILES','REPRODUCTION_MOCK_E2E','REPRODUCTION_EVIDENCE_E2E','REPRODUCTION_C3_E2E','RULES','SYNTHETIC_GOLDEN','SYNTHETIC_E2E','BASELINE_DIFF','WORKBENCH_CONTRACT']
}
(root/'validation/phase_f1_static_gate.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'status':'PASS','backend_tests':backend_tests,'static_gates':len(payload['gates']),'source_manifest':manifest['aggregate_sha256']},ensure_ascii=False))
PY
