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

## Release prerequisite

Historical Real Gate evidence was captured from a dirty worktree. Before release-grade revalidation, materialize all A-F source and validation-time fixes into one clean immutable release-candidate commit and use that SHA as `safety.expected_head`.
