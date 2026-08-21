# Capture Engine V2.1.1 — Real Gate Execution Index

软件基线已经完成。真实 Gate 应严格按以下顺序执行，任何失败都不得开启 Production V2。

## 顺序

```text
R1 PostgreSQL Lease Concurrency
 → R2 APF Ownership/Recovery
 → R3 Real SFTP + Segment/ACK Failure Injection
 → R4 Readiness + FXS Hook Semantics
 → R5 Coverage Golden Reconciliation
 → R6 Evidence Report E2E
 → R7 Shadow / Long-run / Rollback / Cutover
```

## 共通硬 Gate

1. 一 DUT 同时最多一个 Capture Authority / Producer；
2. Lease loss 不得主动停止仍存活 Producer；
3. Server 无证据且 DUT 也无证据的“双丢”永远禁止；
4. 不能证明完整就必须 PARTIAL/FAILED；
5. Raw FXS Evidence 不得因 Sanitizer 被删除；
6. Production V2 只有 release artifact 全 true 才允许。

详细条目见：

- `DEFERRED_REAL_GATES.md`
- `B_GATE_RUNBOOK.md`（Ownership 真机步骤，历史详细版）
- `RELEASE_GATE_TEMPLATE.json`
