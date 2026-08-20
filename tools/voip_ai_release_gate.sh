#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR="${VOIP_AI_GATE_VENV:-$ROOT_DIR/.venv-release-gate}"
PG_CONTAINER="voip-ai-gate-pg-$$"
REDIS_CONTAINER="voip-ai-gate-redis-$$"
PG_PORT=""
REDIS_PORT=""

log() { printf '\n==> %s\n' "$*"; }
fail() { printf '\n[FAIL] %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || fail "missing required command: $1"; }

cleanup() {
  docker rm -f "$PG_CONTAINER" "$REDIS_CONTAINER" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

need "$PYTHON_BIN"
need docker
need npm
need curl
if [[ -n "${VOIP_PR2_FIELD_PCAP:-}" ]]; then
  [[ -f "$VOIP_PR2_FIELD_PCAP" ]] || fail "VOIP_PR2_FIELD_PCAP does not exist: $VOIP_PR2_FIELD_PCAP"
  need tshark
fi

docker info >/dev/null 2>&1 || fail "Docker daemon is not available"

log "Preparing isolated Python environment"
if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi
source "$VENV_DIR/bin/activate"
python -m pip install --upgrade pip
pip install -r backend/requirements.txt

log "Starting ephemeral PostgreSQL 16 and Redis 7"
docker run -d --rm --name "$PG_CONTAINER" -e POSTGRES_DB=voip -e POSTGRES_USER=voip -e POSTGRES_PASSWORD=voip -p 127.0.0.1::5432 postgres:16 >/dev/null
docker run -d --rm --name "$REDIS_CONTAINER" -p 127.0.0.1::6379 redis:7-alpine >/dev/null
PG_PORT="$(docker port "$PG_CONTAINER" 5432/tcp | awk -F: 'END{print $NF}')"
REDIS_PORT="$(docker port "$REDIS_CONTAINER" 6379/tcp | awk -F: 'END{print $NF}')"
[[ -n "$PG_PORT" && -n "$REDIS_PORT" ]] || fail "failed to resolve ephemeral service ports"

export DATABASE_URL="postgresql+psycopg://voip:voip@127.0.0.1:${PG_PORT}/voip"
export REDIS_URL="redis://127.0.0.1:${REDIS_PORT}/0"
export REPRODUCTION_PLATFORM_MODE="real"
export PYTHONPATH="backend:."

# postgres's entrypoint runs a transient init server (CREATE DATABASE) before
# exec'ing the final server. A single ready probe can catch the init server and
# then fail during the init->final handoff, so require two consecutive ready
# states before declaring PostgreSQL ready.
pg_ready=0
for _ in $(seq 1 60); do
  if docker exec "$PG_CONTAINER" pg_isready -U voip -d voip >/dev/null 2>&1; then
    pg_ready=$((pg_ready + 1))
    [ "$pg_ready" -ge 2 ] && break
  else
    pg_ready=0
  fi
  sleep 1
done
if [ "$pg_ready" -lt 2 ]; then fail "PostgreSQL did not become ready"; fi
for _ in $(seq 1 30); do docker exec "$REDIS_CONTAINER" redis-cli ping 2>/dev/null | grep -q PONG && break; sleep 1; done
docker exec "$REDIS_CONTAINER" redis-cli ping 2>/dev/null | grep -q PONG || fail "Redis did not become ready"

log "1/11 Python compile"
python -m compileall -q backend/app backend/tests tools
log "2/11 AI contract coverage"
make ai-eval-gate
log "3/11 AI E1-E6 regression"
make ai-e1-e6-gate

if [[ -f backend/tests/test_ai1_semantic_router_v1.py ]]; then
  log "4/11 AI1 Semantic Router gate"
  pytest -q backend/tests/test_ai1_semantic_router_v1.py backend/tests/test_ai1_semantic_gateway_v1.py backend/tests/test_ai1_semantic_eval_gate_v1.py backend/tests/test_ai1_semantic_api_v1.py backend/tests/test_ai1_semantic_real_corpus_eval_v1.py
fi
if [[ -f backend/tests/test_ai3_case_copilot_v1.py ]]; then
  log "5/11 AI3 Case Copilot gate"
  pytest -q backend/tests/test_ai3_case_copilot_v1.py backend/tests/test_ai3_copilot_gateway_v1.py backend/tests/test_ai3_copilot_api_v1.py backend/tests/test_ai3_copilot_idempotency_isolation_v1.py backend/tests/test_ai3_feishu_copilot_v1.py backend/tests/test_ai3_feishu_tenant_idempotency_v1.py backend/tests/test_ai3_copilot_fail_closed_v1.py
fi
if [[ -f backend/tests/test_ai2_diagnostic_loop_v1.py ]]; then
  log "6/11 AI2 Diagnostic Loop SHADOW/SUGGEST gate"
  pytest -q backend/tests/test_ai2_diagnostic_loop_v1.py backend/tests/test_ai2_cycles_api_v1.py backend/tests/test_ai2_diagnosis_sidecar_v1.py backend/tests/test_ai2_cycle_concurrency_contract_v1.py backend/tests/test_ai2_reasoning_gateway_redaction_v1.py backend/tests/test_ai2_suggest_bridge_v1.py backend/tests/test_ai2_suggest_concurrency_contract_v1.py backend/tests/test_ai2_reproduction_publish_recovery_v1.py backend/tests/test_ai2_feishu_suggest_v1.py backend/tests/test_ai2_feishu_retry_card_v1.py backend/tests/test_ai2_feishu_dispatch_order_v1.py
fi

log "7/11 M7 acceptance contract"
pytest -q backend/tests/test_m7_acceptance_gate.py
log "8/11 PostgreSQL clean migration"
(cd backend && alembic upgrade head)
log "9/11 Full backend regression"
pytest -q backend/tests --tb=line
log "10/11 Preliminary Evidence Report software gate"
EVIDENCE_GATE_ARGS=(--skip-tests)
if [[ -n "${VOIP_PR2_FIELD_PCAP:-}" ]]; then
  log "PR2 Field Golden enabled: SHA-bound external PCAP"
  EVIDENCE_GATE_ARGS+=(--field-pcap "$VOIP_PR2_FIELD_PCAP")
fi
python tools/evidence_report_release_gate.py "${EVIDENCE_GATE_ARGS[@]}"
log "11/11 Frontend dependency audit and production build"
(cd frontend && npm ci && npm audit --audit-level=low && npm run build && test -s dist/index.html && test -s dist/evidence-report.html)

printf '\n=============================================\nVOIP AI SOFTWARE RELEASE GATE: PASS\n'
printf 'Branch: %s\n' "$(git branch --show-current 2>/dev/null || echo unknown)"
printf 'Commit: %s\n=============================================\n' "$(git rev-parse HEAD 2>/dev/null || echo unknown)"
