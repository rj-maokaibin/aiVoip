# Capture Engine V2.1.1 — SPEC → Code 最终覆盖审计

> 基线：Capture Engine V2.1.1
> GitHub 基线：`rj-maokaibin/aiVoip@a805e2dfefdc8ca62fae90bc403166bfeea61827`
> 审计范围：所有不依赖真实 PostgreSQL / APF1250 / APF3260-M / DUT SFTP / 现场 E2E 的软件能力
> 结论：**纯软件能力已实现并进入确定性回归；剩余项均归类为 DEFERRED_REAL_GATE。**

## 1. 总结

| 领域 | SPEC 要求 | 代码落点 | 回归证据 | 结论 |
|---|---|---|---|---|
| Profile | versioned Capture/Platform profile + immutable effective snapshot | `profiles/schema.py`, `profiles/resolver.py`, `factory.py` | `test_capture_v2_profiles.py`, `test_capture_v2_bridge.py` | PASS |
| Ownership | Fenced Lease / monotonic `lease_epoch` | `lease/manager.py`, `transport/mutator.py`, `transport/shell_scripts.py` | `test_capture_v2_lease.py`, `test_capture_v2_fencing.py` | PASS |
| Exactly-one | 一 DUT 同时最多一个 Capture Producer | `producer/manager.py`, `recovery/manager.py` | `test_capture_v2_producer_manager.py`, `test_capture_v2_recovery_manager.py` | PASS |
| Recovery | R0-R5、Adopt、Legacy、Multiple Producer、Reboot | `recovery/*` | `test_capture_v2_recovery_classifier.py`, `test_capture_v2_recovery_manager.py` | PASS |
| Producer Identity | PID + starttime + cmdline + epoch + interface | `producer/identity.py` | `test_capture_v2_producer_manager.py` | PASS |
| Segment | active → sealed spool，单调 seq，24B silent PCAP 合法 | `segment/sealer.py`, `segment/pcap.py`, `segment/repository.py` | `test_capture_v2_c_reliable.py`, BusyBox smoke | PASS |
| Reliable SFTP | exact immutable Segment SFTP + before/after identity verify | `transfer/sftp.py`, `transfer/remote.py`, patch `0004` | `test_capture_v2_c_reliable.py`, `test_capture_v2_c_pump_db.py` | SOFTWARE PASS / DUT SFTP DEFERRED |
| Durable Store | `.part` / fsync / create-if-absent / SHA256 / no overwrite | `storage/local.py`, `storage/minio.py`, `transfer/persister.py` | `test_capture_v2_c_reliable.py`, `test_capture_v2_final_idempotency.py` | PASS |
| Segment FSM | DISCOVERED→...→PERSISTED→ACKED→REMOTE_DELETED | `segment/repository.py`, `transfer/pump.py` | `test_capture_v2_c_pump_db.py` | PASS |
| ACK safety | ACK only after durable + DB commit；ACKED 不倒退 | `transfer/ack.py`, `transfer/pump.py`, `transfer/reconciler.py` | `test_capture_v2_c_pump_db.py`, `test_capture_v2_final_idempotency.py` | PASS |
| Server repair | ACKED 后 Server copy 缺失，DUT exact segment 在则先修复再删 | `transfer/pump.py`, `transfer/reconciler.py` | `test_capture_v2_c_pump_db.py` | PASS |
| Spool pressure | UNACKED 不因压力被删除 | `segment/pressure.py` | C reliability tests | PASS |
| Final drain | Stop Producer → final seal → final transfer → durable barrier | `finalizer.py`, `segment/sealer.py` | `test_capture_v2_final_idempotency.py` | PASS |
| Packet accounting | `Σ Segment.packet_count == tcpdump captured`，否则 Possible Gap | `finalizer.py` | `test_capture_v2_final_idempotency.py` | PASS |
| Stage-1 Readiness | Lease/Producer/Voice/PCAP/FXS/PCM/Store/Transfer/Storage/Watchdog 全部 Ready | `readiness/stage1.py`, `d_bridge.py` | `test_capture_v2_d_readiness_fxs.py` | PASS |
| Watchdog | Ready 可撤销，不静默继续 | `readiness/watchdog.py`, `d_bridge.py` | D readiness tests | PASS |
| FXS Sanitizer | Raw Event 永久保留；业务事件经 Sanitizer | `fxs/sanitizer.py`, `fxs/attempt_service.py` | `test_capture_v2_def_db.py`, `test_capture_v2_d_readiness_fxs.py` | PASS |
| Hook Glitch | APF3260 20ms rebound → `FXS_HOOK_GLITCH`，不创建业务 Attempt | `fxs/sanitizer.py` | `test_apf3260_post_onhook_20ms_offhook_20ms_onhook_is_glitch_not_attempt` | PASS |
| Hook Flash | 通话中短 ONHOOK→OFFHOOK 保持同一 Attempt | `fxs/sanitizer.py` | `test_during_call_hook_flash_does_not_end_or_create_new_attempt` | PASS / 阈值真机校准 DEFERRED |
| Attempt FSM | PROVISIONAL→CONFIRMED→DATA_PLANE_VERIFYING→ENDED→EVIDENCE_FINALIZING→EVALUATED | `attempt_flow.py`, `runtime_coordinator.py` | `test_capture_v2_runtime_coordinator.py` | PASS |
| Per-channel verify | Trigger-relative、独立 deadline、N/A/MISSING/VERIFIED | `readiness/data_plane.py`, `d_bridge.py` | `test_capture_v2_def_db.py`, `test_capture_v2_runtime_coordinator.py` | PASS |
| Eventual Binding | Late SIP/RTP fallback + late FXS anchor refinement | `timeline/binding.py`, `runtime_coordinator.py` | runtime coordinator tests | PASS |
| Source Time | Source Time 优先，UTC normalization，晚处理早源时间可纠错 | `timeline/source_time.py`, `readiness/data_plane.py` | runtime coordinator tests | PASS |
| Target lifecycle | TARGET_CONFIRMED→POST_TARGET_OBSERVATION→EVIDENCE_DRAINING | `runtime_coordinator.py`, `session_flow.py` | runtime coordinator tests | PASS |
| Timers | `post_target_seconds` / evidence finalize timeout 真正由 Runtime 消费 | `runtime_coordinator.py`, profile schema | runtime coordinator timer tests | PASS |
| Coverage Ledger | Window / Track / Interval，确定性重算且幂等 | `coverage/*`, `e_bridge.py` | `test_capture_v2_e_coverage.py`, `test_capture_v2_final_idempotency.py` | PASS |
| Completeness | COMPLETE/PARTIAL/FAILED 由 Coverage 决定，unknown/kernel-drop/非 durable 均不能 COMPLETE | `coverage/calculator.py`, `coverage/pcap_source.py`, `coverage/ledger.py` | coverage tests | PASS |
| Traffic silence | silent interval / 24B PCAP ≠ Capture Gap | `coverage/pcap_source.py`, `segment/pcap.py` | coverage + C tests | PASS |
| Rolling Ring | `ROLLING → PINNED → RELEASED`；相交 Epoch 整体 pin | `segment/retention.py`, `e_bridge.py` | `test_capture_v2_retention.py` | PASS |
| Signal Availability | AVAILABLE / encrypted / degraded / not applicable / not captured | `quality/signals.py` | `test_capture_v2_f_quality.py` | PASS |
| Diagnostic Confidence | Confidence 受 deterministic completeness ceiling 约束 | `quality/confidence.py`, `f_bridge.py` | F quality tests | PASS |
| F from Coverage | 正式 Quality path 只能读取 finalized CoverageWindow | `f_bridge.py` | `test_quality_production_path_is_bound_to_finalized_coverage_not_caller_claim` | PASS |
| DTMF fusion | FXS + Call Manager + SIP URI + PCM | `quality/dtmf_fusion.py` | `test_dtmf_fusion_finds_layer_divergence` | PASS |
| Evidence Report | Evidence-first manifest；required asset 缺失时拒绝强结论 | `report/evidence_first.py`, `f_bridge.py` | `test_evidence_report_refuses_conclusion_when_required_audio_missing` | PASS |
| Cleanup | 持久化 step ledger、反向验证、失败阻断、Release Lease 最后 | `cleanup/coordinator.py` | `test_capture_v2_cleanup_telemetry.py` | PASS |
| Telemetry | producer/gap/UNACKED/transfer/completeness/ready latency + P0 multiple producer | `telemetry/snapshot.py` | cleanup/telemetry tests | PASS |
| Production authority | V1/V2 exactly-one guard；V2 未过 release artifact 时 fail-closed | `runtime.py`, `cutover/gate.py`, patch `0003` | `test_capture_v2_runtime.py`, `test_capture_v2_cutover.py` | PASS / LIVE CUTOVER DEFERRED |

## 2. 关键不变量审计

### I-01 单一 Capture Authority

已实现：

- Server DB Lease 为当前 Authority；
- `lease_epoch` 单调增加；
- DUT mutation 必须带当前 epoch；
- stale Worker mutation → FENCED；
- Multiple Producer recovery 不启动第三个 Producer；
- Production V1/V2 guard 防止两套 Authority 同时启动。

结论：**SOFTWARE PASS**。真实 PostgreSQL race + DUT Gate 待验证。

### I-02 Lease Loss 不主动停止 Producer

`C-Gate Runtime` 中 Lease renewal loss 使控制面停止新的 mutation，但不会调用 tcpdump stop。

结论：**SOFTWARE PASS**。真实 SSH/Lease failure injection 待验证。

### I-03 SEALED Segment ACK 前不删除

Remote delete 只在 `PERSISTED → ACK_PENDING → ACKED` 后运行；UNACKED pressure 不能 evict。

结论：**PASS**。

### I-04 ACKED 是单向安全边界

ACK 后 delete/fence/GC 失败不会把 Server evidence 降级为 ERROR；Server copy 若缺失且 DUT copy 尚在，必须先 repair。

结论：**PASS**。

### I-05 COMPLETE 必须可证明

以下任一存在时，PCAP Coverage 不能 COMPLETE：

- confirmed/possible Gap；
- kernel capture drop；
- stopped epoch final drop status 未知；
- packet accounting mismatch；
- Segment `REMOTE_DELETED` 但 Server durable copy 缺失；
- required track unknown/missing。

结论：**PASS**。

### I-06 AI/Analyzer 不能覆盖 deterministic completeness

正式 F path 从 finalized `CoverageWindow.status` 读取 completeness；Quality/Report 不能由调用方自由声明 COMPLETE。

结论：**PASS**。

## 3. 当前明确不属于纯软件 DONE 的事项

以下均需要真实环境证明，统一标记：`DEFERRED_REAL_GATE`。

1. PostgreSQL 双连接首次 Lease acquire race；
2. APF1250 / APF3260-M Worker Crash → Adopt same Producer；
3. Legacy/Multiple Producer 真机 recovery；
4. stale lease STOP/DELETE 真机 fencing；
5. 两平台真实 Dropbear SFTP subsystem；
6. SFTP partial/timeout/reconnect；
7. fsync/PERSISTED/ACK 各 crash point failure injection；
8. DUT reboot + pending spool recovery；
9. spool backlog/storage pressure；
10. Stage-1 CAPTURE_PATH_READY 真机全通道验证；
11. Hook Glitch / Hook Flash 参数真机校准；
12. Per-channel Data Plane Verify 真机时间线；
13. Golden Call Coverage 人工对账；
14. Evidence-first 报告真实 PCAP/图/音频 E2E；
15. V1/V2 Shadow Compare；
16. Long-run / resource / rollback / production cutover。

## 4. 最终审计结论

截至本软件基线，未发现仍应由本地纯软件实现、却被错误推迟到真实 Gate 的 V2.1.1 核心能力。

因此：

```text
A Foundation                  SOFTWARE COMPLETE
B Ownership                   SOFTWARE COMPLETE / REAL GATE DEFERRED
C Reliable Segment/SFTP/ACK   SOFTWARE COMPLETE / REAL GATE DEFERRED
D Readiness/FXS               SOFTWARE COMPLETE / REAL GATE DEFERRED
E Coverage Ledger             SOFTWARE COMPLETE / REAL GATE DEFERRED
F Quality/Evidence Report     SOFTWARE COMPLETE / E2E GATE DEFERRED
Production Cutover Guard      SOFTWARE COMPLETE
Production V2 Enable          BLOCKED / DEFERRED_REAL_GATE
```
