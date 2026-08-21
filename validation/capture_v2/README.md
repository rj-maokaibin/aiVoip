# Capture V2.1.1 Real-Gates Validation Branch

**VALIDATION ONLY / NOT MERGE READY**

Base commit: `a805e2dfefdc8ca62fae90bc403166bfeea61827`.

This branch carries:

- Capture V2 real-gate tooling;
- SCP adaptation for Dropbear DUTs without SFTP subsystem;
- validation-time fixes committed to the branch where available;
- Git-mediated Remote Validation Control Loop under `backend/app/capture_v2/control/`;
- real-gate evidence summaries and bootstrap helper for the historical A-F Software Baseline.

## Current release status

The historical real-device run executed substantial R1-R7 validation, but final evidence audit does **not** accept the earlier blanket `R1-R7 all PASS` statement as release-grade proof.

Current audited state:

- R1 PostgreSQL: `PASS_WITH_PROVENANCE_GAP`
- R2 Ownership/Recovery: `PASS_WITH_PROVENANCE_GAP`
- R3 Segment/Transfer/ACK: `PARTIAL`
- R4 Readiness/FXS: `PARTIAL`
- R5 Coverage: `PARTIAL`
- R6 Evidence-first Report E2E: `NOT_PASS_FOR_PRODUCT_E2E`
- R7 Shadow/Long-run/Rollback: `PARTIAL`
- Production V2: **OFF / RELEASE BLOCKED**

The key provenance issue is that historical primary manifests were captured from a dirty worktree. Release-grade revalidation must run from one clean immutable release-candidate commit.

## Remote Validation Control Loop

Start the local runner only after materializing a clean RC commit:

```bash
export CAPTURE_ENGINE_VERSION=V1
export CAPTURE_V2_PRODUCTION_ENABLED=false
export CAPTURE_GATE_SSH_PASSWORD='...'

cd backend
PYTHONPATH=. python -m app.capture_v2.control_cli run \
  --repo-root .. \
  --branch feat/capture-v2.1.1-real-gates \
  --git-sync \
  --poll-seconds 10
```

The remote controller writes `validation/control/next_action.json`; the runner executes only registered allowlisted Gate operations and publishes structured status/results back through Git.

See:

- `validation/control/README.md`
- `docs/03_Implementation_Trace/CAPTURE_V2_REMOTE_VALIDATION_CONTROL_LOOP.md`

## Safety

- keep `CAPTURE_ENGINE_VERSION=V1`;
- keep `CAPTURE_V2_PRODUCTION_ENABLED=false`;
- no arbitrary shell through control JSON;
- clean Git + immutable `expected_head` required;
- PR #27 remains Draft and must not be merged for Production cutover.
