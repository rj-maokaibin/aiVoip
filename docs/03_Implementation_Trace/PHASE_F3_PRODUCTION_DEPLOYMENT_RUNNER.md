# Phase F3 — Production Deployment & Full-stack Release Runner

## Goal

Turn the F2 production-hardening contracts into a reproducible, fail-closed Linux/Docker deployment
workflow without bypassing EC-02 or manufacturing runtime evidence.

## Delivered

- `deploy/voip-ai` one-command production CLI.
- Host/env/secret preflight using only Python stdlib before Docker promotion.
- Protected production Compose override with Docker secret mounts and restart policies.
- Same-origin Nginx `/api/` proxy so browser API/SSE traffic does not depend on localhost/CORS tricks.
- Explicit Alembic migration before application promotion.
- Automatic PostgreSQL logical backup before upgrade when an existing database is running.
- Live runtime verification across backend, frontend proxy, PostgreSQL, Redis, MinIO read/write,
  Celery queues, and production configuration.
- Source-bound `PRODUCTION_DEPLOYMENT_RUNTIME` evidence.
- Docker-based frontend build evidence path (still requires the committed `package-lock.json`).
- Strict release orchestration: deploy → runtime verify → Docker full-stack E2E → frontend build
  evidence → optional Field Golden → V1.0 strict Release Gate.
- `deployment_contract_gate.py` and `phase_f3_static_gate.sh`.

## Safety rules

- Production `down` does not use `-v` and never deletes data directories.
- No script can turn EC-02/mock into production-ready status.
- Source-bound runtime artifacts are rejected after source drift.
- Secret source files and the production env file must not be group/world accessible.
- Placeholder/default credentials are rejected before image promotion.
- The release command remains non-zero until all V1.0 production blockers are truly satisfied.

## Explicit remaining external blockers

- EC-02 real DUT Platform Contract and real reproduction adapter.
- A real Docker daemon for runtime execution.
- Source-controlled frontend `package-lock.json` and a successful Docker frontend build.
- Real production auth/credential/MinIO/Feishu values.
- Final real-DUT Field Reproduction/cleanup validation.
