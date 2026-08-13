# Phase F1 — V1.0 Release Readiness

## Goal

Phase F1 turns the frozen Engineering Contract into machine-enforced release gates. It does **not** declare the product production-ready while EC-02 or live integrations are pending.

## Implemented static gates

- Migration graph contract: one root, one head, connected graph, upgrade/downgrade present.
- Frozen OpenAPI V1 snapshot + SHA256 drift gate.
- Docker Compose static contract gate, including source-manifest and expected-Alembic-head propagation to full-stack E2E.
- Security boundary gate: unknown Action rejection, no high-level direct shell execution, explicit production blockers.
- Source manifest + aggregate SHA256 drift gate.
- Existing Profile / Rule / Golden / E2E / Workbench gates integrated into one F1 static gate.
- Runtime release-readiness endpoint: `GET /api/v1/system/release-readiness` (ADMIN permission).
- `make v1-release-readiness`: report current status without converting UNVERIFIED/BLOCKED into PASS.
- `make v1-release-gate`: strict production gate; expected to fail until all blocking evidence is complete.

## Exact-source runtime evidence

Phase F1 does not accept a successful artifact merely because a JSON file exists. Runtime evidence must include `source_manifest_aggregate_sha256` and it must equal `release/source_manifest.json` for the exact source under evaluation.

This rule is enforced for:

- Field Golden replay evidence.
- Docker full-stack E2E evidence.
- PostgreSQL Alembic runtime verification (recorded inside the full-stack artifact by querying `alembic_version`).
- Frontend production build evidence.

A stale artifact is `UNVERIFIED`, never `PASS`.

## Frontend reproducibility gate

A production frontend build requires `frontend/package-lock.json`, `npm ci`, and `npm run build`. `tools/frontend_build_gate.sh` records the resulting `dist/index.html` hash in an exact-source evidence artifact.

The current source does not contain a lockfile, and this execution environment cannot reach npm to create/verify one. Therefore the missing lockfile is an explicit **BLOCKED** release item rather than a fabricated success.

## Deliberately blocking production release

The following remain explicit blockers rather than hidden assumptions:

1. EC-02 real DUT autonomous reproduction contract is still pending.
2. A real (non-Mock) Reproduction Platform is not configured.
3. Release evidence has not been collected with `APP_ENV=production` and an immutable build revision.
4. Production credential provider is not configured.
5. Reproduction evidence storage is not configured/verified as production MinIO with non-default secrets.
6. Production authentication provider / trusted gateway contract is not implemented.
7. Anonymous development authentication must be disabled and production CORS restricted.
8. Feishu live send/update/callback transport is not configured.
9. Docker full-stack exact-source runtime evidence is required.
10. PostgreSQL `alembic upgrade head` + `alembic_version` exact-source runtime evidence is required.
11. `frontend/package-lock.json` is required.
12. Exact-source frontend `npm ci && npm run build` evidence is required.

Field Golden is no longer a blocker for the current source when the supplied APF1250 evidence is available: it is replayed and source-bound separately.

## Current environment limitations

This analysis environment has no Docker CLI/daemon. Node/npm are present, but npm package resolution cannot be verified because network/DNS access is unavailable and the source currently has no lockfile. These limitations remain `UNVERIFIED`/`BLOCKED`; they are not converted into success.

## Safety rule

`STATIC_PASS_PRODUCTION_BLOCKED` is a valid F1 result. It means the exact source satisfies deterministic static gates while production release remains correctly blocked by missing runtime/integration evidence. It must never be displayed as `PASS` or `READY`.
