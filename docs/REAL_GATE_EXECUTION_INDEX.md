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

---

# PBX / FreeSWITCH Test Lab Infrastructure V1 — Real Gate Index

状态：PLANNED / DESIGN FROZEN（2026-09-09）。以下 Gate 是新基础设施增量，不改变已 CLOSED 的 Capture/Generic Automation V1 结论。

## 执行顺序

```text
PBX-R1 Inventory + PBX_READY Health
 → PBX-R2 FusionPBX Source Fence + Extension Lifecycle
 → PBX-R3 Extension Pool Lease / Ownership / Dirty Recovery
 → PBX-R4 FreeSWITCH ESL Registration / Call Event Reconcile
 → PBX-R5 G-PBX-001 Extension Lifecycle
 → PBX-R6 G-PBX-002 DUT Registration Lifecycle
 → PBX-R7 G-CALL-001 SIP Call E2E + SIPp
 → PBX-R8 Failure Injection + Cleanup / Recovery
 → PBX-R9 Exact-head Acceptance + Production-like Read-back
```

## PBX 共通硬 Gate

1. 非自动化 ownership extension 删除必须为 0；
2. STATIC_BASELINE extension 删除必须为 0；
3. blind mutation retry 必须为 0；
4. stale lease mutation 必须为 0；
5. secret value emitted 必须为 0；
6. Cleanup 关键残留必须为 0；
7. FusionPBX CONFIGURED 不得替代 FreeSWITCH RUNTIME VERIFIED；
8. Source Fence 不匹配时只能 read-only，禁止 mutation。

详细设计见 `02_Core_Documents/VOIP_AI_PBX_FusionPBX_FreeSWITCH_测试基础设施详细设计_V1.0.md`。
