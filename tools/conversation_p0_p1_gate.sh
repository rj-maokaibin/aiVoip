#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

export PYTHONPATH="backend:."

printf 'CONVERSATION_P0_P1_GATE_HEAD=%s\n' "$(git rev-parse HEAD 2>/dev/null || echo unknown)"

pytest -q \
  backend/tests/test_conversation_interpreter_v1.py \
  backend/tests/test_conversation_state_v1.py \
  backend/tests/test_feishu_conversation_cycle_decoupling.py \
  backend/tests/test_conversation_question_planner_v1.py \
  backend/tests/test_conversation_progress_push_v1.py \
  backend/tests/test_conversation_orchestrator_v1.py \
  backend/tests/test_product_fact_v1.py \
  backend/tests/test_product_fact_importer_v1.py \
  backend/tests/test_knowledge_hybrid_retrieval_v1.py

python - <<'PY'
from pathlib import Path
required = [
    'backend/app/conversation/contracts.py',
    'backend/app/conversation/interpreter.py',
    'backend/app/conversation/state_service.py',
    'backend/app/conversation/orchestrator.py',
    'backend/app/conversation/planner.py',
    'backend/app/conversation/progress.py',
    'backend/app/conversation/response.py',
    'backend/app/knowledge/product_facts.py',
    'backend/app/knowledge/importer.py',
    'backend/app/knowledge/retrieval.py',
    'backend/migrations/versions/0027_conversation_knowledge_v1.py',
]
missing = [path for path in required if not Path(path).is_file()]
assert not missing, missing

feedback = Path('backend/app/integrations/feishu/feedback.py').read_text(encoding='utf-8')
events = Path('backend/app/integrations/feishu/events.py').read_text(encoding='utf-8')
worker = Path('backend/app/workers/device_provision_task.py').read_text(encoding='utf-8')
factory = Path('backend/app/diagnosis/factory.py').read_text(encoding='utf-8')
assert 'ConversationStateService' in feedback
assert '_dispatch_case_conversation' in events
assert '_dispatch_knowledge_conversation' in events
assert 'diagnosis_resumed' in worker
assert 'DeterministicDiagnosisReasoner' in factory
print('CONVERSATION_P0_P1_STATIC_CONTRACT=PASS')
PY

printf 'CONVERSATION_P0_P1_GATE=PASS\n'
