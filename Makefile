.PHONY: up down logs migrate test lint profiles ai-eval-gate ai-model-eval ai-promotion-gate ai-e1-e6-gate phase-e1-workbench-gate platform-contract-gate platform-production-gate rules golden-synthetic e2e-synthetic e2e-diff golden-field quality-gate fullstack-smoke fullstack-field release-gate field-release-gate reproduction-profile-gate reproduction-e2e reproduction-evidence-e2e phase-c3-profile-gate diagnostic-question-gate experiment-profile-gate reproduction-c3-e2e m62-core-gate m62-c2-gate m62-c3-gate migration-contract-gate openapi-contract-gate compose-contract-gate security-release-gate source-manifest-gate frontend-build-gate phase-f1-static-gate phase-f2-static-gate production-hardening-gate v1-release-readiness v1-release-gate

up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f backend collector-worker

migrate:
	docker compose run --rm backend alembic upgrade head

test:
	PYTHONPATH=backend pytest -q backend/tests

lint:
	cd backend && python -m compileall -q app tests
	rm -rf backend/app/**/__pycache__ backend/tests/__pycache__ 2>/dev/null || true

profiles:
	PYTHONPATH=backend python tools/check_profiles.py

platform-contract-gate:
	PYTHONPATH=backend python tools/platform_contract_gate.py

platform-production-gate:
	PYTHONPATH=backend python tools/platform_contract_gate.py --require-production-ready

rules:
	PYTHONPATH=backend python tools/rule_validate.py

golden-synthetic:
	PYTHONPATH=backend python tools/golden_synthetic_replay.py --out-dir .golden-artifacts --result .golden-result.json

e2e-synthetic:
	PYTHONPATH=backend python tools/e2e_replay.py --result .e2e-result.json

e2e-diff: e2e-synthetic
	PYTHONPATH=backend python tools/e2e_diff.py e2e_baselines/v1.json .e2e-result.json --out .e2e-diff.md

golden-field:
	@test -n "$(EVIDENCE_DIR)" || (echo "EVIDENCE_DIR is required" && exit 2)
	PYTHONPATH=backend python tools/field_golden_batch.py --evidence-dir "$(EVIDENCE_DIR)" --out .field-golden-result.json $(FIELD_REQUIRE_ALL)

reproduction-profile-gate:
	PYTHONPATH=backend python tools/reproduction_profile_gate.py

reproduction-e2e:
	PYTHONPATH=backend python tools/reproduction_mock_e2e.py

m62-core-gate: reproduction-profile-gate test reproduction-e2e

reproduction-evidence-e2e:
	PYTHONPATH=backend python tools/reproduction_evidence_e2e.py

phase-c3-profile-gate:
	PYTHONPATH=backend python tools/phase_c3_profile_gate.py

diagnostic-question-gate: phase-c3-profile-gate

experiment-profile-gate: phase-c3-profile-gate

reproduction-c3-e2e:
	PYTHONPATH=backend python tools/reproduction_c3_e2e.py

m62-c2-gate: reproduction-profile-gate test reproduction-e2e reproduction-evidence-e2e rules golden-synthetic e2e-diff

m62-c3-gate: reproduction-profile-gate diagnostic-question-gate experiment-profile-gate test reproduction-e2e reproduction-evidence-e2e reproduction-c3-e2e rules golden-synthetic e2e-diff

quality-gate: lint profiles reproduction-profile-gate test rules golden-synthetic e2e-diff

# Contract coverage only. This target never promotes a model.
ai-eval-gate:
	PYTHONPATH=backend:. python tools/ai_eval_gate.py --out validation/ai_eval_gate.json

# Real model/fixture replay against ai-model-eval-dataset-v2.
# Example: make ai-model-eval AI_EVAL_DATASET=/data/ai_eval_real_v2.json AI_EVAL_MODE=gateway
ai-model-eval:
	@test -n "$(AI_EVAL_DATASET)" || (echo "AI_EVAL_DATASET is required" && exit 2)
	PYTHONPATH=backend:. python tools/ai_eval_runner.py --dataset "$(AI_EVAL_DATASET)" --mode "$(or $(AI_EVAL_MODE),fixture)" --out validation/ai_model_eval.json

# Strict promotion gate: requires contract PASS + real verified quality PASS + complete audit coverage.
ai-promotion-gate: ai-eval-gate
	@test -f validation/ai_model_eval.json || (echo "validation/ai_model_eval.json is required; run ai-model-eval first" && exit 2)
	PYTHONPATH=backend:. python tools/ai_promotion_gate.py --quality-report validation/ai_model_eval.json --out validation/ai_promotion_gate.json

# Source-level E1-E6 contract/regression gate. Does not imply production model promotion.
ai-e1-e6-gate:
	PYTHONPATH=backend:. pytest -q backend/tests/test_ai_eval_gate.py backend/tests/test_ai_proposal_shadow.py backend/tests/test_ai_readonly_workbench.py backend/tests/test_gateway_safety.py backend/tests/test_knowledge_similarity.py backend/tests/test_ai_e1_e6.py

# M6.1: real PostgreSQL + Redis + MinIO + Celery + HTTP API full-stack smoke.
# Uses a generated periodic-audio PCAP so it can run in CI without field evidence.
fullstack-smoke:
	./tools/fullstack_e2e.sh

# Example: make fullstack-field FIELD_PCAP=/data/voip-golden/8b729....pcap
fullstack-field:
	@test -n "$(FIELD_PCAP)" || (echo "FIELD_PCAP is required" && exit 2)
	FIELD_PCAP="$(FIELD_PCAP)" ./tools/fullstack_e2e.sh

# Release gate that is self-contained in source + Docker.
release-gate: quality-gate fullstack-smoke

# Strongest gate: deterministic + full-stack + real field evidence.
field-release-gate: quality-gate
	@test -n "$(FIELD_PCAP)" || (echo "FIELD_PCAP is required" && exit 2)
	FIELD_PCAP="$(FIELD_PCAP)" ./tools/fullstack_e2e.sh

phase-e1-workbench-gate:
	PYTHONPATH=backend python tools/workbench_contract_gate.py
	PYTHONPATH=backend pytest -q backend/tests/test_phase_e1_workbench_feishu.py

# Phase F1: machine-checkable V1.0 release-readiness contracts.
migration-contract-gate:
	PYTHONPATH=backend python tools/migration_contract_gate.py

openapi-contract-gate:
	PYTHONPATH=backend python tools/openapi_contract_gate.py

compose-contract-gate:
	PYTHONPATH=backend python tools/compose_contract_gate.py

security-release-gate:
	PYTHONPATH=backend python tools/security_release_gate.py

source-manifest-gate:
	PYTHONPATH=backend python tools/source_manifest_gate.py

frontend-build-gate:
	./tools/frontend_build_gate.sh

phase-f1-static-gate:
	./tools/phase_f1_static_gate.sh

# Reports current readiness without converting UNVERIFIED/BLOCKED into PASS.
v1-release-readiness:
	PYTHONPATH=backend python tools/release_readiness_gate.py $(if $(FIELD_EVIDENCE_DIR),--field-evidence-dir "$(FIELD_EVIDENCE_DIR)",)

# Strict V1.0 production gate. It MUST remain non-zero while EC-02/live integration/runtime evidence is pending.
v1-release-gate: phase-f3-static-gate
	PYTHONPATH=backend python tools/release_readiness_gate.py --strict $(if $(FIELD_EVIDENCE_DIR),--field-evidence-dir "$(FIELD_EVIDENCE_DIR)",)

production-hardening-gate:
	PYTHONPATH=backend python tools/production_hardening_gate.py

phase-f2-static-gate:
	./tools/phase_f2_static_gate.sh

production-config-gate:
	PYTHONPATH=backend python tools/production_config_gate.py

# Phase F3: production deployment + runtime verification contracts.
.PHONY: deployment-contract-gate phase-f3-static-gate production-preflight production-deploy production-verify production-release

deployment-contract-gate:
	PYTHONPATH=backend python tools/deployment_contract_gate.py

phase-f3-static-gate:
	./tools/phase_f3_static_gate.sh

production-preflight:
	@test -n "$(PRODUCTION_ENV)" || (echo "PRODUCTION_ENV is required" && exit 2)
	./deploy/voip-ai --env "$(PRODUCTION_ENV)" preflight

production-deploy:
	@test -n "$(PRODUCTION_ENV)" || (echo "PRODUCTION_ENV is required" && exit 2)
	./deploy/voip-ai --env "$(PRODUCTION_ENV)" deploy

production-verify:
	@test -n "$(PRODUCTION_ENV)" || (echo "PRODUCTION_ENV is required" && exit 2)
	./deploy/voip-ai --env "$(PRODUCTION_ENV)" verify

production-release:
	@test -n "$(PRODUCTION_ENV)" || (echo "PRODUCTION_ENV is required" && exit 2)
	./deploy/voip-ai --env "$(PRODUCTION_ENV)" $(if $(FIELD_PCAP),--field-pcap "$(FIELD_PCAP)",) release
