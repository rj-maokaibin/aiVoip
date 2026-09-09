# VOIP AI 故障助手 SPEC V1.3：PBX 自动化测试基础设施修订

状态：DESIGN FROZEN / 2026-09-09  
基线：`VOIP_AI_故障助手_SPEC_V1.0_终稿.docx`  
兼容修订：V1.1 Reproduction Capture、V1.2 AI/Feishu  
配套详细设计：`VOIP_AI_PBX_FusionPBX_FreeSWITCH_测试基础设施详细设计_V1.0.md`

## 1. 规格优先级

本修订仅增加 PBX Test Lab resource/mutation/runtime contract。DUT mutation、Capture、Cleanup、Evidence、AI safety 继续受既有 SPEC/Engineering Contract 约束。发生冲突时，资源安全采用更严格条款。

## 2. PBX-INFRA-001｜控制面与运行面

系统必须严格区分：FusionPBX Config Plane（Desired State）与 FreeSWITCH Runtime Plane（Observed State）。FusionPBX readback 成功不能单独判定 READY；必须完成相应 FreeSWITCH runtime verification。

## 3. PBX-INFRA-002｜唯一配置 Provider

所有 PBX 配置 mutation 必须通过 `FusionPbxConfigProvider`。上层不得直接执行裸 SQL 修改 `v_extensions`，不得将 FusionPBX schema 细节泄漏到 TestCase/AI 层。

Provider 必须支持 source fence、create/read/update/delete、number alias、snapshot/readback 与 typed error。

## 4. PBX-INFRA-003｜Runtime Provider

现有 `FusionPbxRegistrationProbe` 继续作为 read-only reconcile observer。新增 `FreeSwitchRuntimeProvider`，V1 主观察通道使用 ESL，`fs_cli` 为 reconcile/fallback。

Runtime Provider 必须输出结构化 Registration/Call/DTMF/Hangup Evidence，不允许 TestCase 解析任意 CLI 文本。

## 5. PBX-INFRA-004｜Resource Pool

第一版数字 dial alias pool 冻结为 `7900-7999`；extension identity 不受数字池限制。资源状态：`FREE/RESERVED/PROVISIONING/READY/IN_USE/CLEANUP/DIRTY/RECOVERING/OFFLINE`。

`STATIC_BASELINE` 与 `TEMPORARY_AUTOMATION` 必须显式区分；baseline 不得被自动 delete。

## 6. PBX-INFRA-005｜Authority / Lease

Extension、PBX Node、CallSession 必须绑定可持久化 Lease/epoch。Stale lease 不得执行 mutation。并发测试不得获得同一个 extension 的 active lease。

PBX Authority 与现有 DeviceAuthority/CaptureLeaseManager 复用同一资源治理基础，不允许创建旁路锁系统。

## 7. PBX-INFRA-006｜Ownership

自动创建 extension 必须具有可回读 ownership marker。自动 delete 必须证明 marker + current lease + temporary resource 三者一致，否则 fail-closed。

## 8. PBX-INFRA-007｜Source Fence

FusionPBX provider source hash/contract facts 不匹配时必须禁止 mutation，仅允许 read-only probe，并返回 `PBX_PROVIDER_SOURCE_FENCE_FAILED`。

## 9. PBX-INFRA-008｜Mutation Semantics

Mutation 必须一次执行。Transport/command result UNKNOWN 时禁止 blind retry；系统先 read-only observe Desired State、ownership 和 runtime，再决定 SUCCESS/FAILED/INCONCLUSIVE 或显式 retry eligibility。

## 10. PBX-INFRA-009｜Cleanup / Recovery

Cleanup 是 mandatory terminal phase。临时 extension 必须 delete + FusionPBX absence readback + FreeSWITCH unregistered/absence verify 后才能 FREE。Cleanup 失败进入 DIRTY，禁止重新分配。

Baseline restore 使用 fresh-current + precise rollback，不允许用陈旧 snapshot 全量覆盖未知变化。

## 11. PBX-INFRA-010｜Call Session

CallSession 必须绑定 caller/callee resource lease 与 FreeSWITCH call UUID。状态最少覆盖 ORIGINATING/RINGING/ANSWERED/MEDIA/HANGUP/COMPLETED 以及 BUSY/NO_ANSWER/REJECTED/FAILED/TIMEOUT。

## 12. PBX-INFRA-011｜SIPp Peer

V1 可使用 SIPp 作为自动化 SIP peer。Peer account 同样由 ResourceManager 申请，不得使用无 lease 的固定共享账号并发运行。

## 13. PBX-INFRA-012｜Secret 与 Evidence

Extension password、ESL password、DB secret 不得进入普通日志、Evidence、Audit、LLM。Evidence 只保留 presence/identity/verification boolean 与脱敏 reference。

## 14. PBX-INFRA-013｜Health Gate

Mutation 前必须通过 PBX_READY：FusionPBX source/database、FreeSWITCH profile、runtime observer、号码池、Lease store、DIRTY resource barrier 均可用。M4 后 ESL reachable 为硬条件。

## 15. PBX-INFRA-014｜Golden

- G-PBX-001 Extension Lifecycle。
- G-PBX-002 Registration Lifecycle。
- G-PBX-003 Dial Alias Resolution。
- G-CALL-001 SIP Call E2E。

三条 Golden 均必须包含 cleanup、reverse verify、immutable evidence 和 failure injection；Golden 不能使用手工预配置才能 PASS。

## 16. PBX-INFRA-015｜生产/测试边界

PBX infrastructure mutation 只允许在被标记为 Test Lab 的 PBX node/domain 上执行。不得把生产客户 PBX 作为动态资源池。Node eligibility 必须由后端配置与 policy 强制，不得由调用方自由传 `allow_mutation=true` 绕过。

## 增量合同：Extension Identity

Extension identity 统一使用 string。Automation-managed identity V1 允许 `[A-Za-z0-9.+]`（1-64，至少一个字母或数字）；PBX discovery 可读取更宽历史字符但不得主动 mutation。Registration observer、ESL event matching、ResourceAuthority、Evidence schema 均不得使用 `isdigit()` 作为 identity 合法性合同。

## 增量合同：Dial Alias

`pbx_extension_resources` 必须一等持久化 `dial_alias`，不得只放 provider metadata。`dial_alias` V1 合同为 `[0-9]{1,32}`。FusionPBX Provider 使用原生 `number_alias`。Provision 前必须检查 alias 与现有 extension/number_alias 双向冲突；cleanup 必须验证 extension identity、dial alias、FreeSWITCH directory resolution 均 absent 后才能 release。FreeSWITCH alias resolution 使用安全解析后的 root user attributes，禁止 Evidence 携带完整 directory XML/secret params。

## 增量合同：Protocol-complete Test Identity

测试基础设施不得以产品 whitelist 代替协议能力。SIP Identity Core 必须支持 RFC3261 direct user 字符和 `%HH` escaped wire identity；Product Capability、PBX Provider Capability 分别判定。协议合法但 Provider 有已证实风险时，必须返回结构化 `PROVIDER_UNSAFE/PROVIDER_IDENTITY_LOSS`，不得主动 mutation，也不得把它误报为 Core 不支持。

当前 FusionPBX Provider 已实机确认 `$` 会被 `xml.sanitize()` 删除、`/` 存在 file-cache path 风险，因此两者是 Provider hard block；其余 direct 特殊字符的 PBX lifecycle matrix PASS。
