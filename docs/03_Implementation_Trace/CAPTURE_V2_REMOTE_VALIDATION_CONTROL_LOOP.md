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
        +-- existing capture_v2.gate_cli
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
- no bypass for dirty Git provenance

## State machine

```text
PENDING -> RUNNING -> SUCCEEDED | FAILED | INCONCLUSIVE
HUMAN_STEP: PENDING -> WAITING_HUMAN -> SUCCEEDED
Validation failure: PENDING -> REJECTED | EXPIRED
```

`action_id` is immutable/idempotent; reuse with different content is rejected. `sequence` must strictly increase.

## Registered action types

- `SOFTWARE_REGRESSION`
- `GATE_LEASE_RACE`
- `GATE_OWNERSHIP`
- `GATE_OWNERSHIP_ADOPT`
- `GATE_SEGMENT`
- `GATE_COLLECT`
- `GATE_EVALUATE`
- `FAULT_WORKER_SIGNAL`
- `FAULT_QUARANTINE_COPY`
- `FAULT_RESTORE_COPY`
- `HUMAN_STEP`

All commands are constructed internally and executed with `shell=False`. Action JSON never carries executable shell text.

## Provenance fence

`expected_head` identifies the immutable product/release-candidate commit. Branch tip may contain later control-channel commits only; every changed path between `expected_head..HEAD` must remain under `validation/control/`. If any application/config/test file changed after `expected_head`, the action is rejected.

The worktree must be clean except generated `validation/control/*` and `.capture-v2-control/*`. This directly prevents repeating the prior real-gate problem where primary manifests were produced with `dirty=true`.

## Git synchronization

With `--git-sync` the runner:

1. fetches the configured validation branch;
2. performs ff-only merge (never reset/force);
3. validates action and provenance;
4. executes one allowlisted action;
5. writes structured result;
6. stages only explicit status/result paths via `git add -- <paths>`;
7. commits and pushes.

A concurrent remote update causes an ordinary push failure. The local result is retained and is never converted to a false PASS.

## Human step

For physical handset actions the controller posts `HUMAN_STEP` with an instruction and one-time acknowledgement token. The runner enters `WAITING_HUMAN`; a person performs the physical step and runs `control_cli ack`. No background capture command is required from the person.

## Secret handling

Action JSON stores only the password environment-variable name (`CAPTURE_GATE_SSH_PASSWORD` by default). Password values must never be committed. Raw stdout/stderr remain local under `.capture-v2-control/`; only SHA256 hashes are published by default.

## Release prerequisite

Before release-grade R1-R7 revalidation, create one clean release-candidate commit containing the complete A-F source, validation-time fixes, transport adaptation, regression tests and this control loop. Only that immutable SHA may be used as `expected_head`.
