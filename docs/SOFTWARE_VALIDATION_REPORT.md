# Capture Engine V2.1.1 — Final Software Validation Report

## Baseline

- Repository: `rj-maokaibin/aiVoip`
- Base commit: `a805e2dfefdc8ca62fae90bc403166bfeea61827`
- Scope: Capture V2.1.1 A→F + Production Cutover Guard
- Real-device / real-PostgreSQL / E2E gates: explicitly deferred

## Final software results

```text
Capture V2 pytest                    97 PASS
python -m compileall                 PASS
SQLite migration 0026→0031          PASS
SQLite downgrade 0031→0026          PASS
PostgreSQL offline DDL compile       PASS
BusyBox Segment Seal                 PASS
BusyBox stale-lease fencing          PASS
BusyBox exact ACK delete             PASS
Patch format validation              PASS (0001~0005)
Patch current-master anchor/context     PASS (0001~0005; 0005 after 0001)
```

## Important hardened invariants included in this baseline

1. `lease_epoch` monotonic and stale mutation fenced；
2. Lease loss does not stop Producer；
3. Multiple Producer never starts a third；
4. 24-byte silent PCAP is valid evidence of capture continuity；
5. UNACKED Segment is never deleted for pressure；
6. ACK only after Server durable + DB commit；
7. ACKED is a one-way safety boundary；
8. Server durable object is create-if-absent, never overwritten by conflicting hash；
9. Server copy missing after ACK triggers repair before DUT GC；
10. Final packet accounting mismatch creates Possible Gap；
11. kernel drop / unknown final drop stats cannot yield COMPLETE；
12. Coverage recalculation is idempotent；
13. Quality is bound to finalized Coverage；
14. APF3260-style 20ms rebound becomes Hook Glitch, not business Attempt；
15. Hook Flash preserves one Attempt；
16. Late processing may correct MISSING only when Source Time proves evidence was in deadline；
17. Evidence durable/explicit-partial barrier precedes Coverage；
18. Cleanup is ordered, persistent, retryable, and Lease Release is last；
19. Production V2 remains fail-closed until machine-readable real Gate artifact passes。

## What this report does NOT claim

It does **not** claim any of the following are verified:

- PostgreSQL true concurrent lease race；
- APF1250/APF3260-M runtime ownership recovery；
- DUT SFTP availability；
- SFTP/ACK crash injection；
- Hook thresholds on real hardware；
- Golden Coverage reconciliation；
- V1/V2 Shadow or Production Cutover。

Those remain in `DEFERRED_REAL_GATES.md`.
