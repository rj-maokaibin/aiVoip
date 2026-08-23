# Capture Engine V2.1.1 Gate Tooling Runbook

## Scope

This tooling is for real-gate execution only. It does **not** enable Production V2 and does not weaken the V1/V2 cutover guard.

## Commands

Run from the backend environment with the real `DATABASE_URL` and profiles mounted at `/app/profiles` (or pass `--profile-root`).

```bash
export CAPTURE_GATE_SSH_PASSWORD='<DUT password>'
cd backend
python -m app.capture_v2.gate_cli --help
```

### R1 PostgreSQL first-acquire race

Both Capture Session IDs must already exist and belong to the same existing CaseDevice used by `--device-id`.

```bash
python -m app.capture_v2.gate_cli lease-race \
  --device-id <case_devices.id> \
  --capture-session-a <capture_session_A> \
  --capture-session-b <capture_session_B> \
  --output-root /var/tmp/capture-v2-gates
```

PASS requires exactly one winner, exactly one `LEASE_BUSY` loser, and one device lease row.

### R2 ownership establish / pre-crash state

```bash
python -m app.capture_v2.gate_cli ownership \
  --device-id <case_devices.id> \
  --model APF1250 \
  --host <DUT_IP> --port 22 --username admin \
  --reproduction-session-id <existing_reproduction_session_id> \
  --worker-id gate-A \
  --state-file /tmp/r2-before.json \
  --hold-seconds 3600
```

From another shell, explicitly inject the worker crash:

```bash
python -m app.capture_v2.gate_cli fault kill --pid <gate-worker-pid>
```

Do **not** kill tcpdump. Wait longer than the persisted lease TTL, then run takeover:

```bash
python -m app.capture_v2.gate_cli ownership-adopt \
  --device-id <case_devices.id> \
  --model APF1250 \
  --host <DUT_IP> \
  --reproduction-session-id <same_reproduction_session_id> \
  --worker-id gate-B \
  --before-state /tmp/r2-before.json \
  --gate-id R2-01
```

The evaluator checks PID, process starttime and CaptureEpoch are unchanged while `lease_epoch` increases.

### R3 normal Reliable Segment / SFTP / ACK

```bash
python -m app.capture_v2.gate_cli segment \
  --device-id <case_devices.id> \
  --model APF1250 \
  --host <DUT_IP> \
  --reproduction-session-id <existing_reproduction_session_id> \
  --worker-id gate-C \
  --duration 60 \
  --gate-id R3-01
```

The command collects DB, DUT and server-store evidence and runs the deterministic R3 evaluator.

### R3 deterministic Gate-only failpoints

SFTP failure before exact GET:

```bash
python -m app.capture_v2.gate_cli segment ... \
  --gate-id R3-02 \
  --fault-plan validation/capture_v2/fault_plans/sftp_fail_once.json
```

Server durable-store failure:

```bash
python -m app.capture_v2.gate_cli segment ... \
  --gate-id R3-03 \
  --fault-plan validation/capture_v2/fault_plans/server_store_fail_once.json
```

These failpoints exist only in the Gate composition path. Production factories do not read them.

### Reversible server-copy loss injection

Quarantine instead of deleting evidence:

```bash
python -m app.capture_v2.gate_cli fault quarantine-copy \
  --store-root /tmp/voip-reproduction-objects \
  --path /tmp/voip-reproduction-objects/capture-v2/<device>/<epoch>/seg_....pcap
```

The command returns a restore token. Restore with:

```bash
python -m app.capture_v2.gate_cli fault restore-copy \
  --store-root /tmp/voip-reproduction-objects \
  --token <token>
```

Paths outside `--store-root` are refused.

## Evidence bundle

Every Gate command writes a timestamped bundle containing:

- `manifest.json` with gate facts and git revision;
- DB JSON snapshots for Capture Session/Lease/Epoch/Event/Gap/Segment/Readiness/Attempt/Coverage/Quality/Evidence;
- DUT boot ID, tcpdump process identity, epoch directories, legacy ring directories, control values, voice config and `/tmp` free space;
- server durable-object inventory with actual size and SHA256 when local object storage is observable.

Evaluate an existing bundle again without touching DUT:

```bash
python -m app.capture_v2.gate_cli evaluate \
  --bundle <bundle-dir> \
  --gate-id R3-01
```

## Safety rules

1. Keep `CAPTURE_ENGINE_VERSION=V1` and `CAPTURE_V2_PRODUCTION_ENABLED=false` during these gates.
2. Gate fault commands must be explicit; no failpoint is enabled by default.
3. Server evidence is quarantined, not irreversibly deleted.
4. Worker kill refuses PID 0/1. DUT tcpdump is not killed by worker-crash tooling.
5. An R3 result is never PASS if Server Store durability is unobservable.
