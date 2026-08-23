# Capture Engine V2.1.1 Real Gates — 验证交付摘要

> 日期：2026-08-21 · 分支：`feat/capture-v2.1.1-real-gates` · HEAD：`9348b1046`
> 性质：**VALIDATION ONLY / NOT MERGE READY**（PR #27 不合并）
> 证据根目录：`validation_results/20260821-103938/`

## 一、总体结论

- **全部 7 个 Real Gates（R1–R7）PASS**，含真实设备证据 + 软件回归 **120 passed**。
- **Production V2 保持 OFF**（`CAPTURE_ENGINE_VERSION=V1`，`capture_v2_production_enabled=false`）。
- **Cutover BLOCKED（fail-closed）**：仅缺 `approved=true`（人工发布审批，不可自动化）。未启用 Production V2，未合并 PR #27。

## 二、Gate 结果明细

| Gate | 结果 | 关键证据 | 证据目录 |
|------|------|---------|---------|
| R1 PostgreSQL Lease | ✅ PASS | lease race / expired takeover + fencing；修复 `NoReferencedTableError`；107 测试 | `APF1250/R1/` |
| R2 Ownership/Recovery | ✅ PASS | APF1250 + APF3260-M：R2-01..R2-04（adopt/orphan/单producer/过期fence） | `{APF1250,APF3260-M}/R2/` |
| R3 Segment/SCP/ACK | ✅ PASS | 双机 SCP 传输全闭环（R3-01/02/03/08/09/11 真机）；新增 SCP transport；120 测试 | `{APF1250,APF3260-M}/R3/` |
| R4 Readiness/FXS | ✅ PASS | **现场摘挂机**：24 FXS_RAW + DTMF 1001 + 6 Attempts（5 NORMAL + 1 GLITCH 100ms）；readiness READY | `{APF1250,APF3260-M}/R4/` |
| R5 Coverage | ✅ PASS | **现场 45s 通话**（#7）：768 RTP 包；coverage 窗口 `aa8ae93e` PCAP 65040ms 覆盖（保守 PARTIAL 正确）；7 边界测试 | `{APF1250,APF3260-M}/R5/` |
| R6 Report E2E | ✅ PASS | 真实 session 端到端报告 `1758d51f`（PARTIAL_COMPLETE，无 HIGH confidence，MinIO 3 对象）；58 测试 | `{APF1250,APF3260-M}/R6/` |
| R7 Shadow/Long-run/Rollback | ✅ PASS | 真实 120s long-run（4 seg durable 0 err）；V1 唯一权威、V2 BLOCKED；cutover fail-closed；38 测试 | `{APF1250,APF3260-M}/R7/` |

## 三、关键技术发现（平台差异）

1. **AIM debug 语法**（已修正）：APF1250 与 APF3260-M **均接受** `FULL_DEBUG_ENABLE`（缩写 `de cm de`），无平台差异。此前误判 3260 需全拼语法，经受控 OFF->ON 实验纠正。APF3260-M 上 0 事件的真正原因是现场无摘挂机动作，而非 debug 未生效。
2. **宿主机访问容器服务**：`DATABASE_URL`（`postgres`→`172.18.0.4`）、`MINIO_ENDPOINT`（`minio`→`172.18.0.3`）需替换为 docker IP；MinIO 需显式注入 `MINIO_ACCESS_KEY/SECRET_KEY`。
3. **设备无 SFTP 只有 SCP**：Dropbear 裁剪了 sftp-server，R3 由 SFTP 改 SCP transport。
4. **设备凭据在 DB**：DUT 密码在 `device_credentials` 表（SN 关联），不在 secret.yaml。

## 四、代码改动（验证期间）

- `backend/app/capture_v2/gate/cli.py`、`context.py`、`faults.py`、`runner.py`：Gate CLI + SCP/故障注入支持
- `backend/app/collectors/asyncssh_adapter.py`：新增 `scp_get()`
- `backend/app/capture_v2/factory.py`：transport 选择 + durable store 根修复
- `backend/app/capture_v2/lease/manager.py`：新增 `validate()`
- `backend/app/capture_v2/db_models.py`：修复 `NoReferencedTableError`
- 新增测试：`test_capture_v2_scp_transport.py`、`test_capture_v2_db_models_fk.py`、`test_capture_v2_lease.py`(+3)

## 五、生产 Cutover 决策

```json
{ "all_real_gates": "R1-R7 all PASS", "allowed": false,
  "reasons": ["APPROVED_FALSE"],
  "action": "Production V2 OFF; PR #27 not merged; 需人工发布审批 approved=true 后重跑 CaptureV2CutoverGate.require()" }
```

## 六、后续动作

1. 人工发布审批：设置 release gate artifact `approved=true` → 重跑 `CaptureV2CutoverGate.require()`。
2. （可选）合并验证成果代码到正式开发分支（需走正常评审，本分支保持 VALIDATION ONLY）。
3. 如需 R5 coverage 升级为 COMPLETE：待 capture epoch seal 且 `packets_dropped_kernel=0` 后重跑 finalize。

