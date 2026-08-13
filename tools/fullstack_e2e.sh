#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PROJECT="${E2E_PROJECT_NAME:-voip-ai-e2e}"
COMPOSE=(docker compose -p "$PROJECT" -f docker-compose.e2e.yml)
RESULT_DIR="$ROOT/e2e_runtime/results"
LOG_DIR="$ROOT/e2e_runtime/logs"
EVID_DIR="$ROOT/e2e_runtime/evidence"
mkdir -p "$RESULT_DIR" "$LOG_DIR" "$EVID_DIR"
rm -f "$RESULT_DIR/fullstack_result.json"

if [[ ! -f "$ROOT/release/source_manifest.json" ]]; then
  echo "ERROR: release/source_manifest.json is required for source-bound runtime evidence" >&2
  exit 2
fi
export SOURCE_MANIFEST_SHA256="$(python - <<'PY'
import json
from pathlib import Path
p=json.loads(Path('release/source_manifest.json').read_text())
print(p['aggregate_sha256'])
PY
)"
export EXPECTED_ALEMBIC_HEAD="$(python tools/migration_contract_gate.py | python -c 'import json,sys; print(json.load(sys.stdin)["heads"][0])')"
[[ ${#SOURCE_MANIFEST_SHA256} -eq 64 ]] || { echo "ERROR: invalid source manifest hash" >&2; exit 2; }
[[ -n "$EXPECTED_ALEMBIC_HEAD" ]] || { echo "ERROR: expected Alembic head unavailable" >&2; exit 2; }

if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: docker CLI is required for M6.1 full-stack E2E" >&2
  exit 127
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "ERROR: docker compose plugin is required" >&2
  exit 127
fi
if ! docker info >/dev/null 2>&1; then
  echo "ERROR: Docker daemon is not reachable" >&2
  exit 126
fi

FIELD_COPY=0
if [[ -n "${FIELD_PCAP:-}" ]]; then
  if [[ ! -f "$FIELD_PCAP" ]]; then
    echo "ERROR: FIELD_PCAP does not exist: $FIELD_PCAP" >&2
    exit 2
  fi
  cp "$FIELD_PCAP" "$EVID_DIR/input.pcap"
  FIELD_COPY=1
  export E2E_EVIDENCE_PATH=/e2e/evidence/input.pcap
  echo "V1.0 full-stack mode: FIELD PCAP ($FIELD_PCAP)"
else
  rm -f "$EVID_DIR/input.pcap"
  unset E2E_EVIDENCE_PATH || true
  export E2E_FIXTURE_MODE="${E2E_FIXTURE_MODE:-synthetic-periodic}"
  echo "V1.0 full-stack mode: SYNTHETIC FULL-STACK FIXTURE ($E2E_FIXTURE_MODE)"
fi

capture_diagnostics() {
  local code=$?
  set +e
  "${COMPOSE[@]}" ps -a > "$LOG_DIR/compose-ps.txt" 2>&1
  "${COMPOSE[@]}" logs --no-color --timestamps backend media-worker diagnosis-worker postgres redis minio > "$LOG_DIR/stack.log" 2>&1
  if [[ $code -ne 0 ]]; then
    echo "Full-stack E2E failed. Diagnostics: $LOG_DIR" >&2
    tail -n 120 "$LOG_DIR/stack.log" >&2 || true
  fi
  if [[ "${KEEP_E2E_STACK:-0}" != "1" ]]; then
    "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  else
    echo "KEEP_E2E_STACK=1: stack kept running under project $PROJECT"
  fi
  if [[ $FIELD_COPY -eq 1 && "${KEEP_E2E_EVIDENCE:-0}" != "1" ]]; then rm -f "$EVID_DIR/input.pcap"; fi
  exit $code
}
trap capture_diagnostics EXIT

"${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
"${COMPOSE[@]}" up -d --build postgres redis minio backend media-worker diagnosis-worker
"${COMPOSE[@]}" run --rm --no-deps e2e-runner

python - <<'PY'
import json
from pathlib import Path
p=Path('e2e_runtime/results/fullstack_result.json')
if not p.exists():
    raise SystemExit('fullstack_result.json missing')
r=json.loads(p.read_text())
print('\nV1.0 Full-stack E2E summary')
print(json.dumps({k:r.get(k) for k in ['passed','mode','checks_passed','checks_total','duration_seconds']},ensure_ascii=False,indent=2))
if not r.get('passed'):
    raise SystemExit(1)
Path('validation').mkdir(exist_ok=True)
Path('validation/fullstack_result.json').write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
PY
