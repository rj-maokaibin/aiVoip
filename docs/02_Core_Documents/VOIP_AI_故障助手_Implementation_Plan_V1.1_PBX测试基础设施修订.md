# VOIP AI 故障助手 Implementation Plan V1.1：PBX / FreeSWITCH 测试基础设施

状态：IMPLEMENTATION READY / 2026-09-09
基线：`VOIP_AI_故障助手_Implementation_Plan_V1.0_终稿.docx`
详细设计：`VOIP_AI_PBX_FusionPBX_FreeSWITCH_测试基础设施详细设计_V1.0.md`

## 1. 实施目标

在不重构 Generic VOIP Automation Framework V1 的前提下，把当前 read-only `FusionPbxRegistrationProbe` 扩展为完整 Test Lab Infrastructure，使测试可动态申请 PBX extension、验证 runtime、执行 Call Session 并安全恢复。

## 2. 工作包

| WP | 内容 | 主要交付 | DoD |
|---|---|---|---|
| PBX-WP01 | Inventory/Health | PBX node model、source/runtime inventory、PBX_READY | 连续 health probe 稳定 PASS |
| PBX-WP02 | Config Provider | FusionPbxConfigProvider、source fence、typed error | create/read/delete contract tests PASS |
| PBX-WP03 | Resource Pool | string extension identity + 7900-7999 dial alias pool、lease/epoch/ownership、dirty recovery | concurrency/stale lease/alias collision tests PASS |
| PBX-WP04 | Runtime Provider | ESL adapter、registration/call event schema、fs_cli reconcile | event/reconcile 一致 |
| PBX-WP05 | Call Session | CallSession、originate/hangup/DTMF、SIPp peer | signaling E2E PASS |
| PBX-WP06 | Framework/Gates | orchestrator integration、cleanup、evidence、CI Golden | 三条 Golden PASS |

## 3. 代码目录建议

```text
backend/app/automation/adapters/pbx/
  registration.py                # 现有 read-only probe，保留
  fusionpbx_config.py            # 新增 Config Provider
  freeswitch_runtime.py          # ESL + reconcile
  schemas.py
backend/app/automation/resources/
  authority.py
  pbx_manager.py
  extension_pool.py
  call_session.py
backend/app/automation/gates/
  golden_pbx_extension.py
  golden_pbx_registration.py
  golden_call_e2e.py
```

不允许在 TestCase/tools 中散落 FusionPBX SQL/PHP mutation。

## 4. Migration

建议新增一个独立 Alembic migration，建立：`pbx_nodes`、`pbx_extension_resources`、`pbx_resource_leases`、`pbx_mutation_snapshots`、`pbx_call_sessions`。Migration 必须可在 production-like PostgreSQL 执行 upgrade，并有 schema contract test。

## 5. M1｜Inventory + Health

任务：记录现网 FusionPBX/FreeSWITCH 基线；固化 provider source hash；实现 PBX_READY；保留 registration probe read-only。
退出：不执行任何 mutation 的情况下，可稳定识别 domain、internal profile、5060、extension pool 与当前 registration。

## 6. M2｜Config Provider

先实现 read/exists/snapshot，再实现 create/delete，最后 update/alias。
首次真实 mutation 从数字 `7900` 生命周期开始；非数字阶段使用 `7900.a` / `+7900`，dial alias 使用空闲数字 `7900`；不得碰 7102 baseline。
完成 G-PBX-001 前不接 DUT。

## 7. M3｜Pool / Authority

实现 acquire/release、lease epoch、ownership marker、STATIC_BASELINE/TEMPORARY_AUTOMATION、DIRTY/RECOVERING。
必须做两个并发 runner 争抢同一 extension 的 contract test，并验证最多一个 winner。

## 8. M4｜FreeSWITCH Runtime / ESL

建立持久 ESL client、event normalization、bounded reconnect；注册和 call event 均进入结构化模型。
`fs_cli` 继续每次关键 terminal verification reconcile，不能因为 ESL 接入就删除 fallback observer。

## 9. M5｜SIPp / CallSession

引入受控 SIPp scenario：REGISTER、incoming call answer、outgoing call、DTMF、BYE。
先完成 SIP signaling，不把 FXS 物理摘挂机作为 V1 blocker。

## 10. M6｜Generic Framework Integration

把 PBX resource acquisition 纳入 TestRun：

```text
Acquire DUT + PBX + Extension(s)
-> Provision PBX
-> Configure DUT/Peer
-> Observe Runtime
-> Assertions
-> Cleanup DUT/Call/PBX
-> Reverse Verify
-> Release-last
```

## 11. Golden 执行顺序

```text
G-PBX-001 Extension Lifecycle
  -> G-PBX-001N Non-numeric Lifecycle
  -> G-PBX-003 Dial Alias Resolution
  -> G-PBX-002 Registration Lifecycle
  -> G-CALL-001 SIP Call E2E
```

后一个 Gate 必须依赖前一个 PASS，不允许一次性把 create/register/call/cleanup 全塞进首条 Golden，避免失败定位困难。

## 12. Failure Injection

必须覆盖：create timeout/UNKNOWN、duplicate idempotency key、ESL disconnect、fs_cli reconcile mismatch、stale lease、ownership mismatch、worker crash during cleanup、delete timeout、registration timeout、call timeout。

## 13. CI / Release

PR exact-head 仍使用 Consolidated Exact-Head Validation；PBX mutation Golden 只能在 controlled runner + Test Lab PBX authority 下执行。普通 PR 默认只跑 unit/source/read-only contract；真实 mutation Gate 采用显式授权入口并生成 immutable artifact。

## 14. 首批研发任务

```text
PBX-T001 node/domain inventory schema
PBX-T002 PBXHealthGate
PBX-T003 FusionPBX source fence
PBX-T004 ConfigProvider read/exists
PBX-T005 ConfigProvider create/delete
PBX-T006 resource pool + lease
PBX-T007 ownership/dirty recovery
PBX-T008 ESL client + event schema
PBX-T009 registration reconcile
PBX-T010 CallSession + originate/hangup
PBX-T011 SIPp peer scenarios
PBX-T012 G-PBX-001
PBX-T013 G-PBX-002
PBX-T014 G-CALL-001
PBX-T015 production/readback/closure evidence
```

## 15. 完成标准

代码存在不算完成。M6 只有 exact-head unit/integration、真实 FusionPBX mutation、FreeSWITCH runtime、三条 Golden、mandatory cleanup、failure injection、secret audit 和 immutable evidence 全 PASS 才 CLOSED。

## 非数字 identity 增量任务

- PBX-T005A：统一 managed extension identity validator，移除 registration/resource 层 numeric-only 假设。
- PBX-T005B：G-PBX-001N，真实验证 `7900.a` 与 `+7900` lifecycle。
- PBX-T009A：ESL registration observer 支持非数字 exact identity。
- PBX-T013：G-PBX-002 默认以非数字 identity 完成 DUT REGISTER/restore。

- PBX-T006A：`dial_alias` 一等 schema/migration，FusionPBX `number_alias` provider mapping。
- PBX-T006B：alias collision + lease/ownership + stale cache failure injection。
- PBX-T006C：G-PBX-003，真实 `7900 -> 7900.a` FreeSWITCH directory resolution 与 cleanup。
- G-CALL 必须使用数字 `dial_alias` 模拟 FXS 用户拨号，不允许把 `7900.a` 当作模拟话机可直接按键输入。
