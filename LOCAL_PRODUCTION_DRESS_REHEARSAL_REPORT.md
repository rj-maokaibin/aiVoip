# aiVoip / Capture Engine V2.1.1 Local Production Dress Rehearsal Report

Date: 2026-08-23  
Scope: local Linux + Docker only; no production server, real DUT, real Feishu, real credential service, or production secrets were accessed.  
Compose project: `voip-ai-local-v2`  
Local runtime root: `.local-runtime/`

## Executive Verdict

`LOCAL_PRODUCTION_DRESS_REHEARSAL = PASS`

`PRODUCTION_RETRY_READINESS = READY`

`PRODUCTION_CUTOVER = NOT EXECUTED`

The complete local production-like deployment flow passed after fixing defects found on the latest remote `master`. The final deployment used the normal production CLI, attempted the online `--pull` build first, authorized the offline fallback only after proving a registry network failure and complete local image inventory, applied migrations, started all required services, and passed the production runtime verifier twice with `9/9` checks.

## 1. Source Identity

- Repository: `rj-maokaibin/aiVoip`
- Actual latest `origin/master` used as baseline: `311c14eb2b56ea1e4c407f431b27864c12c8dd1f`
- Baseline commit: `311c14eb Merge PR #40: audit cutover wrapper and preserved base`
- Isolated worktree: `/home/dev/workspace/aiVoip-local-rehearsal-311c14e`
- Test/fix branch: `fix/production-offline-image-fallback`
- Final locally tested code revision: `f488bfec8c0f113e5aa7035e7fb79065022f40d2`
- Original user workspace changes were not overwritten or committed.
- Final fix branch status before this report was clean; `.local-runtime/` is locally excluded and retained as evidence.

Commits added on top of `origin/master`:

1. `7636574878e40f55f6e2c8fabe6c7e7babee0b39` — `fix(deploy): add fail-closed offline image fallback`
2. `cbebe23c88a746f7d8c9c88bf899fb830dff7ee3` — `fix(deploy): enforce production Feishu RBAC preflight`
3. `f488bfec8c0f113e5aa7035e7fb79065022f40d2` — `fix(deploy): align runtime production verification`

No push or merge was performed.

## 2. Docker Environment

- Docker client/server: `29.1.3`
- Docker API: `1.52`
- Docker Compose: `2.40.3+ds1-0ubuntu1~24.04.1`
- Platform: Linux `amd64`
- Docker daemon: available
- Disk and memory checks: sufficient for the rehearsal

Local ports were moved away from occupied defaults without changing production semantics:

- Backend: `127.0.0.1:28000`
- Frontend: `127.0.0.1:28080`
- MinIO console: `127.0.0.1:19001`

## 3. Image Build

Status: `PASS WITH AUDITED OFFLINE FALLBACK`

Every direct pull failed at the configured Docker registry mirror with the same network error:

`mirror.ruijie.com.cn:8090` / `172.18.34.132:8090` / `connect: no route to host`

The six required local base/runtime images were present and inspected successfully:

| Image | Local image ID |
|---|---|
| `python:3.12-slim` | `sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36` |
| `node:22-alpine` | `sha256:c610fcdfb1d5b4740dd70c284ed3cb16bb857e0f7166196e36a5501df7a3aa32` |
| `nginx:1.27-alpine` | `sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10` |
| `postgres:16` | `sha256:95206741a5b214807675e14165369d05b93a9cf692223b616d07cca227e74b0b` |
| `redis:7-alpine` | `sha256:e7723ff73d963f5cc6d9c4643ea3d989527a402a319239054e9472a7fb9219a2` |
| `minio/minio:RELEASE.2025-04-22T22-12-26Z` | `sha256:a1ea29fa28355559ef137d71fc570e508a214ec84ff8083e39bc5428980b015e` |

The production CLI then built all 12 application targets successfully with `--pull=false`:

- `backend`
- `collector-worker`
- `packet-worker`
- `pcm-worker`
- `media-worker`
- `diagnosis-worker`
- `reproduction-worker`
- `reproduction-control-high-worker`
- `reproduction-watch-worker`
- `beat`
- `frontend`
- `release-runner`

Final fallback decision:

- Status: `ALLOWED`
- Reason: `REGISTRY_NETWORK_FAILURE_AND_LOCAL_IMAGES_COMPLETE`
- Online pull remained the preferred first attempt.
- Missing any required image, registry authentication failure, dependency download failure, Dockerfile failure, or an unrecognized error remains fail-closed.
- Audit evidence: `.local-runtime/evidence/final/offline-build-fallback.json`

## 4. Capture V2 Regression

Status: `PASS`

- Command scope: current `tests/test_capture_v2_*.py`
- Result: `237 passed, 0 failed`
- Duration: `8.44s`
- Final log: `.local-runtime/logs/capture-v2-regression-post-runtime-contract-fix.log`

## 5. Frozen Gate

Status: `PASS`

- Result: `39 passed, 0 failed`
- Gate verdict: `PRELIMINARY_EVIDENCE_V1_GATE=PASS`
- Final log: `.local-runtime/logs/preliminary-evidence-v1-gate-post-runtime-contract-fix.log`

No real-device R1-R7 procedure was rerun, as required by the handoff. Existing frozen evidence was validated by the software gate.

## 6. Full Software Gate

Status: `PASS`

- Backend regression: `1004 passed, 0 failed`
- Frontend dependency audit: `0 vulnerabilities`
- Frontend production build: `PASS`
- Overall verdict: `VOIP AI SOFTWARE RELEASE GATE: PASS`
- Final log: `.local-runtime/logs/voip-ai-release-gate-post-runtime-contract-fix.log`

The gate was run on the exact working-tree content subsequently committed as `f488bfec8c0f113e5aa7035e7fb79065022f40d2`; the gate banner shows the prior HEAD because the validation run intentionally preceded the commit.

## 7. Golden #001

Status: `142/142 PASS`

- Case: `OFFLINE_ANALYSIS_20260814_001`
- PCAP: `/home/dev/workspace/tcpdump-2026-08-14.pcap`
- PCAP SHA-256: `b038aa7c9a0644581f2815f654fcdee4620860796382265b178823fccba2e3f0`
- Manifest SHA-256 expectation: exact match
- TShark: `4.2.2`
- Fixture mode: real external PCAP required; no synthetic/fixture-only pass
- Evidence: `.local-runtime/evidence/golden-001-post-runtime-contract-fix/`

## 8. Production Preflight

Status: `PASS`

- `deployment_status = PASS`
- `release_status = PASS`
- `deploy_blocking_keys = []`
- `release_blocking_keys = []`
- Build revision pinned to `f488bfec8c0f113e5aa7035e7fb79065022f40d2`
- `FEISHU_IDENTITY_RBAC = PASS`
- `EC02_REAL_PLATFORM = PASS`
- Source manifest: `PASS`
- Final source manifest file count: `594`
- Final source manifest aggregate SHA-256: `eb2fb2b2a36d140c59cf726322c49d026f865b9fc4194e41908c7fac56b5ecd9`
- Evidence: `.local-runtime/evidence/release-preflight-final.json`

All integration credentials and URLs used in this rehearsal were local-only test values. No real Feishu or credential service was called.

## 9. Production-like Deploy

Status: `PASS`

The formal command `deploy/voip-ai ... deploy` completed:

1. deployment preflight
2. source manifest validation
3. online `--pull` build attempt
4. fail-closed offline eligibility and image inventory
5. offline application image build
6. PostgreSQL, Redis, and MinIO start
7. explicit Alembic migration
8. backend start
9. all workers and beat start
10. frontend start
11. backend/frontend wait checks
12. production runtime verification

Deployment runtime result: `9/9 PASS`.

Final log: `.local-runtime/logs/production-deploy-final.log`

## 10. Database Migration

Status: `PASS`

- `alembic current`: `0031_capture_v2_quality_report (head)`
- `alembic heads`: `0031_capture_v2_quality_report (head)`
- Direct database query returned the same revision.
- PostgreSQL connectivity query `SELECT 1` returned `1`.

Evidence:

- `.local-runtime/evidence/final/alembic-current.txt`
- `.local-runtime/evidence/final/alembic-heads.txt`

## 11. Runtime Services

Status before cleanup: `PASS`

All required services were running:

| Service | Result |
|---|---|
| `backend` | Up |
| `collector-worker` | Up |
| `packet-worker` | Up |
| `pcm-worker` | Up |
| `media-worker` | Up |
| `diagnosis-worker` | Up |
| `reproduction-worker` | Up |
| `reproduction-control-high-worker` | Up |
| `reproduction-watch-worker` | Up |
| `beat` | Up |
| `frontend` | Up |
| `postgres` | Up, healthy |
| `redis` | Up, healthy |
| `minio` | Up |

Evidence: `.local-runtime/evidence/final/runtime-ps.txt`

After evidence capture, `docker compose ... down --remove-orphans` was executed for project `voip-ai-local-v2`. Containers and its network were removed. Volumes were not deleted, images were not deleted, and local evidence/data remains available.

## 12. Celery Queues

Status: `PASS`

Eight workers responded and the verifier observed every required queue:

- `collector`
- `packet`
- `pcm`
- `media`
- `diagnosis`
- `reproduction-control`
- `reproduction-control-high`
- `reproduction-watch`

This proves queue subscription through Celery inspect, not merely container `Up` state.

## 13. Capture Authority

Status: `PASS`

Exact backend-container probe output:

```text
CAPTURE_ENGINE_VERSION=V2
CAPTURE_V2_PRODUCTION_ENABLED=true
REPRODUCTION_PLATFORM_MODE=real
CAPTURE_AUTHORITY=V2
```

Evidence: `.local-runtime/evidence/final/capture-authority.txt`

## 14. Smoke Test

Status: `PASS`

- Backend `/health/ready`: `status=ok`
- PostgreSQL dependency: `ok`
- Redis dependency: `ok`; direct `PING` returned `PONG`
- MinIO dependency: `ok`; direct read/write probe returned `read_write=true`
- Frontend: HTTP `200`, content type `text/html`
- Same-origin `/api/v1/cases`: HTTP `401`, proving the proxy reached protected backend auth instead of falling back to SPA HTML
- Celery workers/queues: `PASS`
- Capture V2 authority: `PASS`

Evidence: `.local-runtime/evidence/final/smoke.txt`

No real reproduction session was created and no DUT action was attempted.

## 15. Offline Docker Strategy

The deploy script did need a change. Unconditional `compose build --pull` made an otherwise buildable release impossible whenever the configured registry mirror was unreachable, even if every exact base image already existed locally.

Implemented strategy:

1. Always try the normal online pull/build first.
2. Analyze the captured failure and authorize fallback only for recognized registry metadata network/transport failures.
3. Inspect all six exact required images locally.
4. Emit a machine-readable audit artifact.
5. Continue with `compose build --pull=false` only when the network classification and inventory both pass.
6. Fail closed for incomplete inventory and non-network build failures.

This behavior is appropriate for a controlled production retry because it preserves online freshness preference while making the fallback explicit, bounded, and auditable.

## 16. Differences vs RC83

RC83's observed failure is conclusively reproduced as a Docker registry network problem: all pulls and production-equivalent `--pull` builds failed against `172.18.34.132:8090` with `no route to host`, while the same source built successfully from the complete local image inventory.

However, it is not correct to conclude that the unmodified latest `master` had no other deployment defects. RC83 stopped at image metadata resolution and therefore never reached later stages. This rehearsal found additional issues that would have blocked or falsely failed a subsequent deployment stage:

1. `release/source_manifest.json` was stale relative to `origin/master` and failed the source manifest gate.
2. `deploy/voip-ai` had no safe offline build fallback.
3. Production startup required Feishu identity RBAC, but the production env template and preflight did not require it; preflight could PASS before backend restart-looped.
4. The runtime verifier rejected the shipped minimal Vite HTML entrypoint despite a valid `text/html` response.
5. `release-runner` did not mount the Capture V2 gate artifact at its configured `/app/validation` path.
6. Production storage readiness judged development default fields instead of the resolved file-secret values, despite successful MinIO read/write operation.

All six issues are fixed on the local branch and covered by tests. Therefore:

- RC83 failure point: proven registry network failure.
- Unpatched `origin/master`: not sufficient for a full production retry.
- This fix branch: local production dress rehearsal fully PASS and suitable for PR review.

## 17. Remaining Production-only Work

### Proven locally

- Current source builds with the exact locally cached base images.
- The fallback is fail-closed and audited.
- Capture V2 and frozen software regressions pass.
- Full software gate and frontend production build pass.
- Real Offline Golden #001 passes `142/142` with TShark `4.2.2`.
- Strict production preflight passes with no blockers.
- Production-like Compose deployment, migration, services, queues, storage, auth proxy behavior, real-platform configuration, and V2 authority all pass.
- Deployment cleanup is safe and does not require deleting volumes/images.

### Still required on the real production server

1. Review and merge the three fix commits through a PR.
2. Confirm the real server's exact base-image inventory if registry access remains unavailable, or repair its registry route/mirror.
3. Prepare and validate the real `/etc/voip-ai/production.env`, including `FEISHU_IDENTITY_RBAC_ENABLED=true`, real secret-file mounts, real URLs, and the merged build revision.
4. Run strict preflight on the production server.
5. Execute the guarded production deployment/cutover on the production server.
6. Verify real production health, queues, migration, MinIO, Feishu/credential integrations, and rollback evidence.

The prior real-device R1-R7 evidence remains accepted from the handoff; this local rehearsal does not claim to have rerun those DUT procedures.

## Recommendation

Open a PR from `fix/production-offline-image-fallback` and review all three commits together. After merge, retry production using the guarded deployment flow. Do not retry directly from unpatched `origin/master`.

Final verdict:

`LOCAL_PRODUCTION_DRESS_REHEARSAL = PASS`

`PRODUCTION_RETRY_READINESS = READY`

`PRODUCTION_CUTOVER = NOT EXECUTED`
