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

### Status freshness contract

`status.json` is **only the last transport/runner status written by the remote-control loop**. It is not the authoritative current product, production-runtime, M7, or release status.

Consumers must check `updated_at` before using it. A historical `RUNNER_ERROR` remains valid evidence that a particular sync attempt failed, but it must not override a newer immutable action result, a newer production-runtime artifact, or a newer M7 acceptance report. In particular, after the Capture V2.1.1 real-DUT work was merged to `master` and `validation/m7_acceptance_report.json` recorded 20/20 PASS, the older `status.json` GitSync timeout is a **superseded runner-transport observation**, not a current product blocker.

For current state, use evidence in this order:

1. exact-SHA product/release artifacts for the capability being judged;
2. the newest immutable `validation/control/results/<action_id>/result.json` for a remote-control action;
3. dedicated acceptance artifacts such as `validation/m7_acceptance_report.json`;
4. `status.json` only for the latest remote-control runner heartbeat/transport state.

Never infer Production authority or M7 status from `status.json` alone.

For a `HUMAN_STEP`, perform the requested physical action and acknowledge it with:

```bash
cd backend
PYTHONPATH=. python -m app.capture_v2.control_cli ack \
  --repo-root .. --action-id <ACTION_ID> --token <ACK_TOKEN>
```

## Historical RC25 provenance state

The immutable software-regressed product/validation-tooling head for RC25 was:

`9395bb97ebd8cdaafc700c0701482a960a514bf5`

`RC25-FINAL-SW-001` completed successfully with return code 0 while V1 remained authoritative and Production V2 remained disabled.

A final compare from that product head to the RC25 control-evidence head before status synchronization found only `validation/control/` action/status/result changes and no Production Capture runtime changes. The earlier dirty-worktree provenance issue was therefore no longer the release blocker at that stage.

The RC25 blocker references below are historical snapshots, not a current-state pointer:

- `validation/capture_v2/VALIDATION_STATUS.json`
- `validation/capture_v2/FINAL_BLOCKER_AUDIT_RC25.json`

Current production/M7 state must be determined from newer exact-SHA artifacts as described in the freshness contract above.
