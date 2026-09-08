# VOIP AI 故障助手 — 当前交付与工程基线

更新日期：2026-09-09
当前 master：`c882f34959ab6cfee6010e3389ffd1aa5d11b9f6`

## 1. 当前 Release 状态

Generic VOIP Automation Framework V1 已完成真实 DUT Golden、exact-head governance、merge-commit、exact-master Production Deploy、Runtime Verify、Evidence V2 SHADOW→CANARY→DEFAULT 与 canonical Feishu remote read-back，当前冻结 V1 范围为 **100% CLOSED**。

Production Deploy authoritative run：`34243712261`，状态 SUCCESS。后续新增能力不得回写为“V1 未完成”；应作为独立增量里程碑管理。

## 2. 核心文档体系

- 冻结基线：PRD V1.0、SPEC V1.0、Engineering Contract V1.0、Implementation Plan V1.0。
- Reproduction 修订：PRD V1.1、SPEC V1.1。
- AI/Feishu 修订：SPEC V1.2。
- PBX Test Lab 修订（2026-09-09）：
  - `VOIP_AI_故障助手_PRD_V1.2_PBX自动化测试基础设施修订.md`
  - `VOIP_AI_故障助手_SPEC_V1.3_PBX自动化测试基础设施修订.md`
  - `VOIP_AI_故障助手_Engineering_Contract_V1.1_PBX资源治理修订.md`
  - `VOIP_AI_故障助手_Implementation_Plan_V1.1_PBX测试基础设施修订.md`
  - `VOIP_AI_PBX_FusionPBX_FreeSWITCH_测试基础设施详细设计_V1.0.md`

冻结 DOCX 保留历史基线；增量行为以同目录 Markdown 修订件覆盖冲突条款，不直接改写历史终稿。

## 3. 当前生产架构

Production 由 Docker Compose 运行 PostgreSQL、Redis、MinIO、Backend、Frontend、采集/Packet/PCM/Media/Diagnosis/Reproduction Workers、Feishu Long Connection 等服务。正式 Deploy 强制 Source Manifest、Consolidated exact-head authority、merge-tree identity、Exact Source Binding、persistent env fingerprint 与 live container/image revision verification。

## 4. 当前自动化测试基础设施

已 CLOSED：DUT WEB browser-equivalent mutation、SSH read-only cross-check、DeviceAuthority/CaptureLeaseManager、observe-before-retry、mandatory cleanup/reverse verify、FusionPBX/FreeSWITCH registration observer、Golden V3、immutable evidence、Production/Feishu verify。

下一阶段：将现有 PBX Managed Observer 升级为 Managed Mutable Resource。FusionPBX 负责 Config Plane，FreeSWITCH 负责 Runtime Plane；新增 extension pool、PBX Resource Lease、source fence、ESL event observer、CallSession 与 SIPp peer。

## 5. 使用原则

1. 任何真实 mutation 必须有 authority/lease、readback、cleanup 与 reverse verification。
2. UNKNOWN 禁止 blind retry，必须先 read-only observe。
3. Raw secret 不进入 Evidence/普通日志/LLM。
4. 测试与生产结论必须绑定 exact source/immutable evidence。
5. PBX 自动化不得删除未证明 ownership 的资源，不得直接裸 SQL 修改 FusionPBX。

## 6. 当前下一阶段

PBX / FreeSWITCH Test Lab Infrastructure V1 当前状态：**DESIGN FROZEN / IMPLEMENTATION READY**。实施顺序为 Health → Config Provider → Resource Pool/Lease → FreeSWITCH ESL → SIPp/CallSession → G-PBX-001 → G-PBX-002 → G-CALL-001 → exact-head/production-like closure。
