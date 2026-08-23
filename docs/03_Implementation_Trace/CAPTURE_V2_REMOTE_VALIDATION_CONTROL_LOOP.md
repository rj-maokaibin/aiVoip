# Capture Engine V2.1.1 — Remote Validation Control Loop

## Purpose

Provide a safe indirect-control plane between a remote reviewer/controller and the local real-device validation host.

```text
Remote controller (GitHub)
        |
        | next_action.json
        v
Local RemoteValidationRunner
        |
        +-- policy + provenance fence
        +-- existing capture_v2 Gate CLIs
        +-- PostgreSQL / server evidence
        +-- SSH -> DUT
        |
        v
status.json + results/<action_id>/result.json
        |
        +-- commit/push (structured result only)
        v
Remote reviewer decides next action
```

## Non-goals

- no arbitrary remote shell
- no production configuration mutation
- no automatic PR merge/cutover
- no irreversible storage deletion as fault injection
- no bypass for Git provenance

## State machine

```text
PENDING -> CLAIMED -> RUNNING -> SUCCEEDED | FAILED | INCONCLUSIVE
HUMAN_STEP: PENDING -> WAITING_HUMAN -> SUCCEEDED
Validation failure: PENDING -> REJECTED | EXPIRED
```

`action_id` is immutable/idempotent; reuse with different content is rejected. `sequence` must strictly increase.

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

## Provenance fence

`expected_head` identifies the immutable product/release-candidate commit. Branch tip may contain later control-channel commits only; every changed path between `expected_head..HEAD` must remain under `validation/control/` for remote actions. If any application/config/test file changed after `expected_head`, the action is rejected until a new reviewed product head is selected.

The worktree must be clean except generated `validation/control/*` and `.capture-v2-control/*` paths. This prevents the earlier failure mode where primary manifests were produced from an unbounded dirty worktree.

For RC25, the software-regressed product/validation-tooling head is:

`9395bb97ebd8cdaafc700c0701482a960a514bf5`

The final compare from that head to the control-evidence head before status synchronization contained only `validation/control/` action/status/result changes and no Production Capture runtime changes.

## Git synchronization

With `--git-sync` the runner:

1. fetches the configured validation branch;
2. performs ff-only merge (never reset/force);
3. validates action and provenance;
4. re-execs itself when pulled control Python sources changed, preserving `python -m app.capture_v2.control_cli` module semantics;
5. executes one allowlisted action;
6. writes structured result;
7. stages only explicit status/result paths via `git add -- <paths>`;
8. commits and pushes.

A concurrent remote update causes an ordinary push failure. The local result is retained and is never converted to a false PASS. Persistent runner errors are published as distinct `RUNNER_ERROR` evidence rather than remaining local-only.

## Human step

For physical handset actions the controller posts `HUMAN_STEP` with an instruction and one-time acknowledgement token. The runner enters `WAITING_HUMAN`; a person performs the physical step and runs `control_cli ack`. No background capture command is required from the person.

## Secret handling

Action JSON stores a credential source reference, for example `DB:<SN>` or an allowed password environment-variable name. Password values must never be committed. Raw stdout/stderr remain local under `.capture-v2-control/`; only SHA256 hashes and structured evidence are published by default.

## RC25 completion boundary

`RC25-FINAL-SW-001` succeeded with return code 0 while:

- `CAPTURE_ENGINE_VERSION=V1`
- `CAPTURE_V2_PRODUCTION_ENABLED=false`
- release approval remains `approved=false`

At RC25 there are no remaining autonomous non-physical validation Gates that can be executed without changing the V1/V2 activation state.

Remaining blockers are only:

1. physical handset evidence: fresh dual-platform FXS behavior, Hook Flash/short-rebound calibration, live Coverage Ledger finalization on a fresh call, and the real first-digit-loss abnormal Product E2E Golden;
2. explicitly authorized activation work: actual V1/V2 shadow or `V2_ACTIVE`, real `PRE_V1 -> V2_ACTIVE -> ROLLED_BACK_V1`, Production V2 enable, approval, PR ready/merge/cutover.

Authoritative status artifacts:

- `validation/capture_v2/VALIDATION_STATUS.json`
- `validation/capture_v2/FINAL_BLOCKER_AUDIT_RC25.json`
- `validation/control/status.json`
