# VOIP AI V1.0 Release Readiness

- Overall: **FAIL**
- Static gates: **UNVERIFIED**
- Production readiness: **BLOCKED**
- PASS: 10 / BLOCKED: 6 / UNVERIFIED: 6 / FAIL: 0

| Gate | Status | Category | Blocking | Detail |
|---|---|---|---:|---|
| PHASE_F3_STATIC_GATE | UNVERIFIED | STATIC | yes | No current static-gate artifact matches the exact source manifest; run tools/phase_f3_static_gate.sh. |
| PRODUCTION_AUTH_IMPLEMENTATION | PASS | SECURITY | yes | gateway_hmac provider validates signed actor/role/timestamp assertions and rejects unsigned production headers. |
| SECRET_PROVIDER_IMPLEMENTATION | PASS | SECURITY | yes | SecretResolver supports mounted file, named environment and direct dev/e2e values without logging resolved secrets. |
| PRODUCTION_STORAGE_IMPLEMENTATION | PASS | STORAGE | yes | MinIO evidence backend supports immutable writes plus read/write probe and cleanup. |
| FEISHU_TRANSPORT_IMPLEMENTATION | PASS | INTEGRATION | yes | Feishu tenant-token send/update transport, persistent Case message binding and callback verification/handlers are implemented. |
| EC02_PLATFORM_PRODUCTION_READY | PASS | PLATFORM | yes | RUIJIE_VOIP_AIM_V1 autonomous reproduction contract is production-ready. |
| REAL_REPRODUCTION_PLATFORM | PASS | PLATFORM | yes | A non-mock reproduction platform is configured. |
| PRODUCTION_ENVIRONMENT | BLOCKED | RUNTIME | yes | APP_ENV=development; production release evidence must be collected with APP_ENV=production. |
| BUILD_REVISION_PINNED | PASS | RUNTIME | yes | Build revision is pinned to workspace. |
| PRODUCTION_CREDENTIAL_PROVIDER | BLOCKED | SECURITY | yes | CREDENTIAL_PROVIDER=api and CREDENTIAL_API_URL are required for production DUT access. |
| PRODUCTION_REPRODUCTION_STORAGE | BLOCKED | STORAGE | yes | REPRODUCTION_STORAGE_MODE must be minio for production; local storage is mock/dev only. |
| PRODUCTION_DEFAULT_SECRETS_REPLACED | BLOCKED | SECURITY | yes | Default/example MinIO credentials must not be used for production release. |
| PRODUCTION_AUTH_PROVIDER | BLOCKED | SECURITY | yes | Set PRODUCTION_AUTH_PROVIDER=gateway_hmac and configure its secret before production release. |
| ANONYMOUS_DEV_AUTH_DISABLED | PASS | SECURITY | yes | Anonymous development fallback is disabled. |
| PRODUCTION_CORS_RESTRICTED | BLOCKED | SECURITY | yes | Wildcard/empty CORS is forbidden in production. |
| FEISHU_LIVE_TRANSPORT | PASS | INTEGRATION | yes | Live Feishu send/update and callback security are configured. |
| DOCKER_FULLSTACK_RUNTIME | UNVERIFIED | RUNTIME | yes | Docker full-stack runtime is not verified for this source: evidence artifact missing: /workspace/validation/fullstack_result.json |
| POSTGRES_MIGRATION_RUNTIME | UNVERIFIED | RUNTIME | yes | Real PostgreSQL migration-to-head has not been verified for the exact current source. |
| PRODUCTION_DEPLOYMENT_RUNTIME | UNVERIFIED | RUNTIME | yes | Production deployment runtime is not verified for this source: evidence artifact missing: /workspace/validation/production_runtime_result.json |
| FRONTEND_LOCKFILE | PASS | BUILD | yes | frontend/package-lock.json is source-controlled for reproducible npm ci builds. |
| FRONTEND_PRODUCTION_BUILD | UNVERIFIED | BUILD | yes | Frontend production build is not verified for this source: evidence artifact missing: /workspace/validation/frontend_build_runtime.json |
| FIELD_GOLDEN | UNVERIFIED | FIELD | yes | Field Golden is not verified for this source: stale evidence: source hash 8c4f6e503e3a9ce8d1aa28bac3ab9659b319ae789642dda22401e613a76289db != current cc96f3ed8dabe29abbb0958c308a683241717569b0bc24359de7cfcfcf2288c7 |

## Blocking items

- **PHASE_F3_STATIC_GATE** — UNVERIFIED: No current static-gate artifact matches the exact source manifest; run tools/phase_f3_static_gate.sh.
- **PRODUCTION_ENVIRONMENT** — BLOCKED: APP_ENV=development; production release evidence must be collected with APP_ENV=production.
- **PRODUCTION_CREDENTIAL_PROVIDER** — BLOCKED: CREDENTIAL_PROVIDER=api and CREDENTIAL_API_URL are required for production DUT access.
- **PRODUCTION_REPRODUCTION_STORAGE** — BLOCKED: REPRODUCTION_STORAGE_MODE must be minio for production; local storage is mock/dev only.
- **PRODUCTION_DEFAULT_SECRETS_REPLACED** — BLOCKED: Default/example MinIO credentials must not be used for production release.
- **PRODUCTION_AUTH_PROVIDER** — BLOCKED: Set PRODUCTION_AUTH_PROVIDER=gateway_hmac and configure its secret before production release.
- **PRODUCTION_CORS_RESTRICTED** — BLOCKED: Wildcard/empty CORS is forbidden in production.
- **DOCKER_FULLSTACK_RUNTIME** — UNVERIFIED: Docker full-stack runtime is not verified for this source: evidence artifact missing: /workspace/validation/fullstack_result.json
- **POSTGRES_MIGRATION_RUNTIME** — UNVERIFIED: Real PostgreSQL migration-to-head has not been verified for the exact current source.
- **PRODUCTION_DEPLOYMENT_RUNTIME** — UNVERIFIED: Production deployment runtime is not verified for this source: evidence artifact missing: /workspace/validation/production_runtime_result.json
- **FRONTEND_PRODUCTION_BUILD** — UNVERIFIED: Frontend production build is not verified for this source: evidence artifact missing: /workspace/validation/frontend_build_runtime.json
- **FIELD_GOLDEN** — UNVERIFIED: Field Golden is not verified for this source: stale evidence: source hash 8c4f6e503e3a9ce8d1aa28bac3ab9659b319ae789642dda22401e613a76289db != current cc96f3ed8dabe29abbb0958c308a683241717569b0bc24359de7cfcfcf2288c7
