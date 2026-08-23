# Capture Engine V2.1.1 — Deferred Real Gates

> 本文件是唯一的“尚未完成”Gate 清单。软件基线不得把这里的项目写成 PASS。

## Gate R1 — PostgreSQL Lease Concurrency

- 两独立连接同时首次 acquire 同一 DUT；
- exactly one winner；
- loser = `LEASE_BUSY`；
- expired takeover → `lease_epoch + 1`；
- stale renew/release = FENCED。

## Gate R2 — DUT Ownership / Recovery（APF1250 + APF3260-M）

- Worker crash，tcpdump 继续；
- takeover 后 ADOPT same PID/starttime；
- capture_epoch 不变，Gap=0；
- Legacy orphan recovery；
- Multiple Producer never third；
- stale lease STOP/DELETE = FENCED；
- stale op.lock recovery；
- DUT reboot gap。

## Gate R3 — Real SFTP + Reliable Segment

两平台分别验证 Dropbear SFTP subsystem，并完成：

```text
closed segment → seal → exact SFTP → .part → verify → SHA256
→ durable store → DB PERSISTED → ACKED → exact DUT delete
```

故障注入：

- SFTP partial / disconnect / timeout；
- Server store failure；
- durable file 后 DB commit 前 crash；
- PERSISTED 后 ACK 前 crash；
- ACK response lost；
- delete failure；
- stale lease delete；
- Server copy lost + DUT repair；
- 24B silent PCAP；
- spool backlog；
- pending spool worker restart；
- reboot。

硬 Gate：**绝不能出现 Server 无证据且 DUT 也无证据。**

## Gate R4 — Readiness / FXS Semantics

- `CAPTURE_PATH_READY` 仅在所有 Stage-1 check 真 Ready 后出现；
- Ready revoke/watchdog；
- APF3260 20ms rebound → `FXS_HOOK_GLITCH`；
- Hook Flash 保持同一 Attempt；
- 校准 `hook_glitch_max_ms / hook_flash_min_ms / hook_flash_max_ms / post_onhook_rebound_window_ms`；
- per-channel expectation timer 与真实事件源时间一致。

## Gate R5 — Coverage Golden Reconciliation

使用已知 Golden Call 人工 PCAP/PCM/FXS 时间线对账：

- Expected Window；
- Actual Coverage；
- confirmed / possible Gap；
- kernel drop；
- packet accounting；
- 24B silence；
- COMPLETE/PARTIAL/FAILED。

## Gate R6 — Evidence Report E2E

报告必须把问题点绑定到：

- 时间窗；
- PCAP/RTP 图；
- PCM 波形；
- 异常音频；
- 正常对比音频；
- 原始 Evidence；
- Coverage / Signal Availability / Confidence；
- 推理依据。

## Gate R7 — Shadow / Long-run / Rollback / Cutover

- V1/V2 Shadow Compare；
- long-run resource；
- failure injection；
- rollback；
- E2E；
- release approval。

只有全部真实 Gate 通过，才允许：

```text
CAPTURE_ENGINE_VERSION=V2
capture_v2_production_enabled=true
```
