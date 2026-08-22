# Capture V2 Remote Validation Control

This directory is the Git-mediated command channel for Capture Engine V2.1.1 real-device validation.

**It is not a production remote shell.** The local runner only executes action types registered in `app.capture_v2.control.policy.ControlPolicy`. Arbitrary shell/code from `next_action.json` is rejected.

Every action is fail-closed unless all are true:

- `CAPTURE_ENGINE_VERSION=V1`
- `CAPTURE_V2_PRODUCTION_ENABLED=false`
- product worktree is clean (generated `validation/control/*` and `.capture-v2-control/*` are excluded)
- `safety.expected_head` identifies the immutable product/release-candidate commit
- commits on top of `expected_head` touch only `validation/control/`
- action sequence is monotonic and not expired
- reused `action_id` has the exact same SHA256

Additional guards:

- worker `kill` requires `allow_worker_kill=true` and `confirm_owned_worker=true`
- Server-copy loss uses quarantine/restore only and requires `allow_server_quarantine=true`
- no action can flip Production V2 switches

## Registered action types

Current `ControlActionType` values are:

- `SOFTWARE_REGRESSION`
- `GATE_LEASE_RACE`
- `GATE_LEASE_FENCING`
- `GATE_OWNERSHIP`
- `GATE_OWNERSHIP_ADOPT`
- `GATE_SEGMENT`
- `GATE_READINESS_FXS`
- `GATE_COLLECT`
- `GATE_EVALUATE`
- `GOLDEN_ARCHIVE_RECOVER`
- `FAULT_WORKER_SIGNAL`
- `FAULT_QUARANTINE_COPY`
- `FAULT_RESTORE_COPY`
- `HUMAN_STEP`

All commands are constructed internally and executed with `shell=False`. Action JSON never carries executable shell text.

## Start the runner

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

The controller writes `validation/control/next_action.json`.
The runner publishes `validation/control/status.json` and `validation/control/results/<action_id>/result.json`.
Raw stdout/stderr remain local under `.capture-v2-control/logs/<action_id>/`; only hashes are pushed by default.

For a `HUMAN_STEP`, perform the requested physical action and acknowledge it with:

```bash
cd backend
PYTHONPATH=. python -m app.capture_v2.control_cli ack \
  --repo-root .. --action-id <ACTION_ID> --token <ACK_TOKEN>
```

## Current RC25 provenance state

The immutable software-regressed product/validation-tooling head is:

`9395bb97ebd8cdaafc700c0701482a960a514bf5`

`RC25-FINAL-SW-001` completed successfully with return code 0 while V1 remained authoritative and Production V2 remained disabled.

A final compare from that product head to the RC25 control-evidence head before status synchronization found only `validation/control/` action/status/result changes and no Production Capture runtime changes. The earlier dirty-worktree provenance issue is therefore no longer the current release blocker.

Current remaining blockers are documented in:

- `validation/capture_v2/VALIDATION_STATUS.json`
- `validation/capture_v2/FINAL_BLOCKER_AUDIT_RC25.json`

They are limited to physical handset Gates and operations requiring explicit V2 activation/rollback/cutover authorization.
