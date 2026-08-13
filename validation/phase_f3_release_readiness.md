# VOIP AI V1.0 Release Readiness

- Overall: **STATIC_PASS_PRODUCTION_BLOCKED**
- Static gates: **PASS**
- Production readiness: **BLOCKED**
- PASS: 27 / BLOCKED: 12 / UNVERIFIED: 4 / FAIL: 0

| Gate | Status | Category | Blocking | Detail |
|---|---|---|---:|---|
| PYTHON_COMPILE | PASS | STATIC | yes | Validated by the exact-source Phase F2 static-gate artifact. |
| SHELL_SYNTAX | PASS | STATIC | yes | Validated by the exact-source Phase F2 static-gate artifact. |
| BACKEND_TESTS | PASS | STATIC | yes | Validated by the exact-source Phase F2 static-gate artifact. |
| MIGRATION_CONTRACT | PASS | STATIC | yes | Validated by the exact-source Phase F2 static-gate artifact. |
| OPENAPI_CONTRACT | PASS | STATIC | yes | Validated by the exact-source Phase F2 static-gate artifact. |
| COMPOSE_CONTRACT | PASS | STATIC | yes | Validated by the exact-source Phase F2 static-gate artifact. |
| SECURITY_CONTRACT | PASS | STATIC | yes | Validated by the exact-source Phase F2 static-gate artifact. |
| PRODUCTION_HARDENING | PASS | STATIC | yes | Validated by the exact-source Phase F2 static-gate artifact. |
| DEPLOYMENT_CONTRACT | PASS | STATIC | yes | Validated by the exact-source Phase F2 static-gate artifact. |
| PROFILES | PASS | STATIC | yes | Validated by the exact-source Phase F2 static-gate artifact. |
| PLATFORM_CONTRACT | PASS | STATIC | yes | Validated by the exact-source Phase F2 static-gate artifact. |
| REPRODUCTION_PROFILES | PASS | STATIC | yes | Validated by the exact-source Phase F2 static-gate artifact. |
| DIAGNOSTIC_EXPERIMENT_PROFILES | PASS | STATIC | yes | Validated by the exact-source Phase F2 static-gate artifact. |
| REPRODUCTION_MOCK_E2E | PASS | STATIC | yes | Validated by the exact-source Phase F2 static-gate artifact. |
| REPRODUCTION_EVIDENCE_E2E | PASS | STATIC | yes | Validated by the exact-source Phase F2 static-gate artifact. |
| REPRODUCTION_C3_E2E | PASS | STATIC | yes | Validated by the exact-source Phase F2 static-gate artifact. |
| RULES | PASS | STATIC | yes | Validated by the exact-source Phase F2 static-gate artifact. |
| SYNTHETIC_GOLDEN | PASS | STATIC | yes | Validated by the exact-source Phase F2 static-gate artifact. |
| SYNTHETIC_E2E | PASS | STATIC | yes | Validated by the exact-source Phase F2 static-gate artifact. |
| BASELINE_DIFF | PASS | STATIC | yes | Validated by the exact-source Phase F2 static-gate artifact. |
| WORKBENCH_CONTRACT | PASS | STATIC | yes | Validated by the exact-source Phase F2 static-gate artifact. |
| SOURCE_MANIFEST | PASS | STATIC | yes | Validated by the exact-source Phase F2 static-gate artifact. |
| PRODUCTION_AUTH_IMPLEMENTATION | PASS | SECURITY | yes | gateway_hmac provider validates signed actor/role/timestamp assertions and rejects unsigned production headers. |
| SECRET_PROVIDER_IMPLEMENTATION | PASS | SECURITY | yes | SecretResolver supports mounted file, named environment and direct dev/e2e values without logging resolved secrets. |
| PRODUCTION_STORAGE_IMPLEMENTATION | PASS | STORAGE | yes | MinIO evidence backend supports immutable writes plus read/write probe and cleanup. |
| FEISHU_TRANSPORT_IMPLEMENTATION | PASS | INTEGRATION | yes | Feishu tenant-token send/update transport, persistent Case message binding and callback verification/handlers are implemented. |
| EC02_PLATFORM_PRODUCTION_READY | BLOCKED | PLATFORM | yes | EC-02 remains partial; real DUT write/cleanup/event contracts are not fully confirmed. |
| REAL_REPRODUCTION_PLATFORM | BLOCKED | PLATFORM | yes | Production release cannot run with REPRODUCTION_PLATFORM_MODE=mock. |
| PRODUCTION_ENVIRONMENT | BLOCKED | RUNTIME | yes | APP_ENV=development; production release evidence must be collected with APP_ENV=production. |
| BUILD_REVISION_PINNED | BLOCKED | RUNTIME | yes | BUILD_REVISION must identify the immutable source/build revision; 'dev' is not release evidence. |
| PRODUCTION_CREDENTIAL_PROVIDER | BLOCKED | SECURITY | yes | CREDENTIAL_PROVIDER=api and CREDENTIAL_API_URL are required for production DUT access. |
| PRODUCTION_REPRODUCTION_STORAGE | BLOCKED | STORAGE | yes | REPRODUCTION_STORAGE_MODE must be minio for production; local storage is mock/dev only. |
| PRODUCTION_DEFAULT_SECRETS_REPLACED | BLOCKED | SECURITY | yes | Default/example MinIO credentials must not be used for production release. |
| PRODUCTION_AUTH_PROVIDER | BLOCKED | SECURITY | yes | Set PRODUCTION_AUTH_PROVIDER=gateway_hmac and configure its secret before production release. |
| ANONYMOUS_DEV_AUTH_DISABLED | BLOCKED | SECURITY | yes | AUTH_ALLOW_ANONYMOUS_DEV must be false for production release. |
| PRODUCTION_CORS_RESTRICTED | BLOCKED | SECURITY | yes | Wildcard/empty CORS is forbidden in production. |
| FEISHU_LIVE_TRANSPORT | BLOCKED | INTEGRATION | yes | Feishu implementation is present; live app credentials, target and callback security still require production configuration. |
| DOCKER_FULLSTACK_RUNTIME | UNVERIFIED | RUNTIME | yes | Docker full-stack runtime is not verified for this source: evidence artifact missing: /mnt/data/voip-ai-v1-deployment-f3/validation/fullstack_result.json |
| POSTGRES_MIGRATION_RUNTIME | UNVERIFIED | RUNTIME | yes | Real PostgreSQL migration-to-head has not been verified for the exact current source. |
| PRODUCTION_DEPLOYMENT_RUNTIME | UNVERIFIED | RUNTIME | yes | Production deployment runtime is not verified for this source: evidence artifact missing: /mnt/data/voip-ai-v1-deployment-f3/validation/production_runtime_result.json |
| FRONTEND_LOCKFILE | BLOCKED | BUILD | yes | frontend/package-lock.json is missing; a production frontend build is not reproducible and must not be promoted. |
| FRONTEND_PRODUCTION_BUILD | UNVERIFIED | BUILD | yes | Frontend production build is not verified for this source: evidence artifact missing: /mnt/data/voip-ai-v1-deployment-f3/validation/frontend_build_runtime.json |
| FIELD_GOLDEN | PASS | FIELD | yes | Exact-source Field Golden passed. |

## Blocking items

- **EC02_PLATFORM_PRODUCTION_READY** — BLOCKED: EC-02 remains partial; real DUT write/cleanup/event contracts are not fully confirmed.
- **REAL_REPRODUCTION_PLATFORM** — BLOCKED: Production release cannot run with REPRODUCTION_PLATFORM_MODE=mock.
- **PRODUCTION_ENVIRONMENT** — BLOCKED: APP_ENV=development; production release evidence must be collected with APP_ENV=production.
- **BUILD_REVISION_PINNED** — BLOCKED: BUILD_REVISION must identify the immutable source/build revision; 'dev' is not release evidence.
- **PRODUCTION_CREDENTIAL_PROVIDER** — BLOCKED: CREDENTIAL_PROVIDER=api and CREDENTIAL_API_URL are required for production DUT access.
- **PRODUCTION_REPRODUCTION_STORAGE** — BLOCKED: REPRODUCTION_STORAGE_MODE must be minio for production; local storage is mock/dev only.
- **PRODUCTION_DEFAULT_SECRETS_REPLACED** — BLOCKED: Default/example MinIO credentials must not be used for production release.
- **PRODUCTION_AUTH_PROVIDER** — BLOCKED: Set PRODUCTION_AUTH_PROVIDER=gateway_hmac and configure its secret before production release.
- **ANONYMOUS_DEV_AUTH_DISABLED** — BLOCKED: AUTH_ALLOW_ANONYMOUS_DEV must be false for production release.
- **PRODUCTION_CORS_RESTRICTED** — BLOCKED: Wildcard/empty CORS is forbidden in production.
- **FEISHU_LIVE_TRANSPORT** — BLOCKED: Feishu implementation is present; live app credentials, target and callback security still require production configuration.
- **DOCKER_FULLSTACK_RUNTIME** — UNVERIFIED: Docker full-stack runtime is not verified for this source: evidence artifact missing: /mnt/data/voip-ai-v1-deployment-f3/validation/fullstack_result.json
- **POSTGRES_MIGRATION_RUNTIME** — UNVERIFIED: Real PostgreSQL migration-to-head has not been verified for the exact current source.
- **PRODUCTION_DEPLOYMENT_RUNTIME** — UNVERIFIED: Production deployment runtime is not verified for this source: evidence artifact missing: /mnt/data/voip-ai-v1-deployment-f3/validation/production_runtime_result.json
- **FRONTEND_LOCKFILE** — BLOCKED: frontend/package-lock.json is missing; a production frontend build is not reproducible and must not be promoted.
- **FRONTEND_PRODUCTION_BUILD** — UNVERIFIED: Frontend production build is not verified for this source: evidence artifact missing: /mnt/data/voip-ai-v1-deployment-f3/validation/frontend_build_runtime.json
