# Capture V2.1.1 Real-Gates Validation Branch

**VALIDATION ONLY / NOT MERGE READY / PRODUCTION V2 OFF**

Base commit: `a805e2dfefdc8ca62fae90bc403166bfeea61827`.

Latest software-regressed product/validation-tooling head: `9395bb97ebd8cdaafc700c0701482a960a514bf5`.

Final software regression: `RC25-FINAL-SW-001` / sequence 113 / `SUCCEEDED` / return code 0, while `CAPTURE_ENGINE_VERSION=V1` and `CAPTURE_V2_PRODUCTION_ENABLED=false`.

## Current audited state

- R1 PostgreSQL lease concurrency/fencing: **PASS**
- R2 Ownership/Recovery: **PASS** on APF1250 + APF3260-M
- R3 Segment/Transfer/ACK reliability: **PASS** on APF1250 + APF3260-M
- R4 Readiness/FXS: **non-physical portion PASS; remaining gates are physical-handset only**
- R5 Coverage: **historical real Golden multi-plane PASS + real PostgreSQL Coverage Ledger runtime PASS; remaining gate is one fresh physical call through live V2 auto-finalization**
- R6 Evidence-first Report E2E: **real PostgreSQL evidence-first runtime PASS; remaining gate is real first-digit-loss abnormal Product E2E**
- R7 Shadow/Long-run/Rollback: **dual-platform validation-state 300-second soak PASS; actual V2_ACTIVE/shadow/rollback requires explicit activation authorization**
- Production V2: **OFF / RELEASE BLOCKED**

There are no remaining non-physical, non-privileged validation tasks known at RC25.

## R4 no-handset preflight

Both DUTs were proven READY before any OFFHOOK activity. The Gate verifies the real lease/producer path, Voice Context, PCAP armed state, AIM reader/debug control, PCM control, independent server-store fsync probe, real SCP transfer probe, watchdog fail-closed transition and cleanup.

- `R4-00-APF1250-IDLE-READY-RC24`: PASS
- `R4-00-APF3260M-IDLE-READY-RC24`: PASS

The readiness contract does not require business packets before OFFHOOK. Idle absence of a sealed PCAP segment is supporting evidence only, not a readiness failure.

## R5 real Golden and PostgreSQL Coverage Ledger

Historical 2026-08-20 archives were recovered read-only from both DUTs and SHA256-pinned:

- APF1250: `67ec0e64980a9c816f3cb5b494143f0410bb685e4177e7ffe89ba83d06abf9ad`
- APF3260-M: `fedcb67763eb7bf65fb00aed68b4e03073fae6e779e71de318d65f7ab7a6489a`

The recovered evidence proves real PCAP/PCM/SIP/RTP continuity and zero tcpdump kernel drop for the historical calls. The real PostgreSQL Coverage Ledger self-test additionally proves create/idempotency, REQUIRED tracks, COMPLETE/PARTIAL/recovery transitions, finalization invalidation/re-finalization and cleanup.

This does **not** substitute for the remaining physical gate: a fresh real call must be consumed by the live V2 path and automatically persist/finalize the call CoverageWindow/Track/Interval as COMPLETE.

## R6 evidence-first runtime

Real PostgreSQL EvidenceAsset/report self-tests passed on both device scopes. Missing required evidence forces `EVIDENCE_INSUFFICIENT_FOR_CONCLUSION / INSUFFICIENT`; a requested/root-cause hint cannot leak into the final conclusion without the required evidence. Fully selected evidence is required before a bounded conclusion can be retained.

The remaining R6 gate is physical: reproduce the real first-digit-loss abnormal case and run the complete FXS + PCAP + PCM -> analyzer -> evidence-first Product E2E report.

## R7 validation-state soak

Production V2 stayed disabled throughout both soaks.

- APF1250: 314.475 s, 513 cycles, 20 repeated samples, 10 durable transfer/ACK/delete segments, 0 pump errors, 0 unresolved gaps, no CRITICAL spool pressure, exact producer/lease cleanup: PASS
- APF3260-M: 309.508 s, 546 cycles, 20 repeated samples, 8 durable transfer/ACK/delete segments, 0 pump errors, 0 unresolved gaps, no CRITICAL spool pressure, exact producer/lease cleanup: PASS

These are validation-state long-run results. They are **not** V1/V2 shadow-equivalence evidence and do not execute `V2_ACTIVE -> ROLLED_BACK_V1`.

## Provenance

After head `9395bb97ebd8cdaafc700c0701482a960a514bf5`, the final RC25 compare found only `validation/control/` action/status/result evidence changes before status synchronization; no Production Capture runtime file changed. Therefore prior real-DUT R1-R3 runtime conclusions remain applicable and the old dirty-worktree provenance blocker is no longer the current release blocker.

## Remaining blockers

### Requires physical handset activity

- fresh APF1250 OFFHOOK -> digit -> ONHOOK
- fresh APF3260-M OFFHOOK -> digit -> ONHOOK
- real Hook Flash behavior
- APF3260-M ~20 ms rebound calibration
- one fresh real call through live Coverage Ledger auto-finalization
- real first-digit-loss abnormal Golden and complete Product E2E report

### Requires explicit V2 activation authorization

- actual V1/V2 shadow or `V2_ACTIVE` observation
- real `PRE_V1 -> V2_ACTIVE -> ROLLED_BACK_V1` rehearsal
- prove V1 health restored and zero residual V2 producer
- Production V2 enable / `approved=true` / PR ready / merge / cutover

## Safety

- keep `CAPTURE_ENGINE_VERSION=V1`
- keep `CAPTURE_V2_PRODUCTION_ENABLED=false`
- keep release approval `approved=false`
- no arbitrary shell through control JSON
- clean Git + immutable `expected_head` required
- PR #27 remains Draft and must not be merged or marked ready without explicit authorization

See:

- `validation/capture_v2/VALIDATION_STATUS.json`
- `validation/control/README.md`
- `docs/03_Implementation_Trace/CAPTURE_V2_REMOTE_VALIDATION_CONTROL_LOOP.md`
