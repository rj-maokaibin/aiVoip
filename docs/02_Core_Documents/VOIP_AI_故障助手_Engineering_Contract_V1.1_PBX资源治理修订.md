# VOIP AI 故障助手 Engineering Contract V1.1：PBX 资源治理修订

状态：FROZEN ADDENDUM / 2026-09-09  
基线：`VOIP_AI_故障助手_Engineering_Contract_V1.0_终稿.docx`

## EC-PBX-01｜Provider Boundary

MUST：FusionPBX mutation 只能通过注册的 `FusionPbxConfigProvider`。  
MUST NOT：业务层、AI、TestCase 直接执行 FusionPBX 裸 SQL、任意 PHP 或任意 `fs_cli` mutation command。

## EC-PBX-02｜Desired/Observed Separation

`FusionPBX CONFIGURED` 与 `FreeSWITCH RUNTIME VERIFIED` 必须分别记录。任何需要 runtime 生效的动作不得仅凭数据库/readback success 判 PASS。

## EC-PBX-03｜Source Fence

Provider mutation 前必须校验固定 source contract/hash。Fence 不匹配时 mutation hard deny；不得“尝试一下看看”。

## EC-PBX-04｜Authority / Lease / Epoch

PBX Node、Extension、CallSession 必须进入统一 ResourceAuthority。Mutation 前必须验证 active lease 与 epoch；Stale holder 禁止执行。Authority release 必须最后发生。

## EC-PBX-05｜Ownership Hard Zero

自动删除非自动化拥有资源 = 0。  
删除 baseline/保护 extension = 0。  
Ownership 无法证明时返回 `PBX_RESOURCE_OWNERSHIP_UNPROVEN`。

## EC-PBX-06｜Mutation UNKNOWN

UNKNOWN 不得 blind retry。必须 read-only observe FusionPBX desired state、ownership 与 FreeSWITCH runtime，形成 `CONFIRMED_APPLIED / CONFIRMED_NOT_APPLIED / INCONCLUSIVE` 后才能决定后续动作。

## EC-PBX-07｜Cleanup

Cleanup/Reverse Verify 是测试事务的一部分。PBX cleanup fail 必须使相关 Gate fail，资源进入 DIRTY；不得为了释放 pool 把 DIRTY 强制改 FREE。

## EC-PBX-08｜Baseline Restore

Baseline 修改恢复必须 fresh-current + precise rollback。禁止默认使用历史完整 snapshot 覆盖本测试未声明拥有的字段。

## EC-PBX-09｜Secret

PBX extension password、ESL secret、数据库 secret hard-zero：不得写入 Evidence、普通 Audit、日志、PR comment、LLM prompt。允许保存 `present/matched` boolean 和 secret reference。

## EC-PBX-10｜Test Lab Eligibility

只有后端登记且 `mutation_allowed=true` 的 Test Lab PBX node/domain 可执行 mutation；eligibility 不能由请求参数临时提升。

## EC-PBX-11｜FreeSWITCH Runtime

ESL 为 V1 主 runtime event source；`fs_cli` 作为 reconcile/fallback。任何 runtime event 都必须结构化归一，不允许规则/AI直接解析自由文本 CLI。

## EC-PBX-12｜Golden Hard Gates

以下 hard-zero 必须 100%：

```text
unowned_extension_delete = 0
baseline_extension_delete = 0
blind_mutation_retry = 0
secret_value_emitted = 0
cleanup_residual_critical = 0
stale_lease_mutation = 0
```

G-PBX-001、G-PBX-002、G-CALL-001 未通过前，不得宣布 PBX Infrastructure V1 CLOSED。

## EC-PBX-13｜Contract Gap

若 FusionPBX 当前版本的 create/update/delete/reload 真实 provider path、ESL REGISTER 事件语义或 SIPp media/DTMF 行为与本修订假设不一致，必须登记 `CONTRACT_GAP` 并通过 source/runtime evidence 修订合同；不得由实现自行猜测补齐。

## 非数字 identity 工程约束

禁止把 extension/number_alias/auth identity 转为 int 后比较、排序或持久化。自动化主动 mutation 统一经过 managed identity validator；当前仅开放字母、数字、`.`、`+`。历史 PBX identity 只读发现不得因此丢失。Golden 必须分别覆盖 `.` 与 `+`。

## EC-PBX-ALIAS｜拨号别名治理

- `extension_identity` 与 `dial_alias` 是不同字段，不得互相覆盖或转 int。
- `dial_alias` 仅允许 `[0-9]{1,32}`，不把 `*`/`#` 当作普通 alias 字符。
- FusionPBX `number_alias` 是唯一 V1 provider mapping，不允许另建未治理旁路映射表。
- Alias collision 必须在 mutation 前 fail-closed。
- Cleanup 必须同时清 extension/alias 两组 FusionPBX/FreeSWITCH cache，并用 `find_user_xml` 证明 alias resolution absent。
- Evidence 必须区分 `registration_identity` / `dial_alias` / `dialed_digits` / `resolved_identity`。

## Protocol Identity Safety Contract

RFC-valid identity、产品支持 identity、Provider-safe mutation identity 是三个独立集合。所有 Provider mutation 必须使用数据通道传值，不允许把 identity 拼入 shell、Lua 或 SQL 语法；当前 FreeSWITCH runtime/cache helper 使用 hex transport。`$`/`/` 在当前 FusionPBX Source Fence V3 下 fail-closed。Provider capability 变化必须由 Source Fence 变化触发重新发现。
