# Conversation Feishu Live Acceptance V1

## 1. 目标

Conversation Feishu Live Acceptance V1 用来补齐 Conversation Platform P0/P1 合并后的真实环境缺口：软件测试已经证明会话解释、状态机、Conversation Turn 与 Diagnosis Cycle 解耦，但此前没有一个独立 Gate 证明同一版本在真实 Feishu tenant 上可以完成用户可见回复，并把发送结果持久化为可审计 delivery trace。

本 Gate 不替代现有 Human Evidence Feishu Living Document 验收，也不替代真实 DUT A-B-A / Capture / Analyzer / Diagnosis 验收。

## 2. 验收边界

拆成两个相互独立、可审计的边界：

1. **Conversation semantic/runtime contract**
   - 使用生产 Conversation 代码；
   - 验证 `不知道`、完成时间查询等对话不会错误生成技术 Evidence；
   - 验证 Conversation Turn 与 Diagnosis Cycle 解耦；
   - 验证 Golden Corpus 中的人性化风险场景；
   - 在 Acceptance Infrastructure V2 runtime 中执行，但使用测试自身的隔离数据库，不修改生产 Case。
2. **真实 Feishu outbound contract**
   - 只对显式提供的专用验收 `om_...` source message 回复一次；
   - 使用生产 `feishu.reply_text` Celery Task 的同步执行路径；
   - 使用真实 Feishu transport 和真实生产数据库；
   - 必须读回 `FeishuReplyDeliveryTrace.stage = SENT`；
   - 验收 Artifact 只保留 message ID 的 SHA-256，不输出真实 message ID。

## 3. 为什么不直接向真实 Case 注入“不了解 / 什么时候结束”

把虚构用户消息写入生产 Case 会污染 ConversationTurn、Evidence、Case.summary 或诊断审计链，因此本 Gate 明确禁止用客户真实 Case 做语义测试。

真实语义合同在 V2 Runtime 中用隔离 DB 执行；真实 tenant 只承担“发送链路是否真的可用”这个最小外部 mutation。这样既验证生产代码，又不会制造伪故障历史。

## 4. Fail-closed 条件

真实 Feishu mutation 只有同时满足以下条件才允许执行：

- workflow 必须由 `workflow_dispatch` 显式触发；
- dispatch ref 必须是 `master`；
- actor 必须等于 repository owner；
- 操作者必须输入 exact `expected_head_sha`，且当前 master HEAD 精确匹配；
- Acceptance Infrastructure V2 Preflight 必须：
  - `contract = voip-live-acceptance-preflight-v2`
  - `status = PASS`
  - `mutation_allowed = true`
  - `source_revision = exact master HEAD`
- message ID 必须是显式提供的专用 `om_...` message；
- 必须输入固定确认口令：
  `REPLY_TO_DEDICATED_FEISHU_ACCEPTANCE_MESSAGE`；
- production `APP_ENV`、`FEISHU_LIVE_ENABLED`、Conversation cycle decoupling、reply retry 均必须有效；
- 最终 delivery trace 必须达到 `SENT`。

任意一项失败即停止，不发送真实飞书回复。

## 5. 普通 PR CI

`.github/workflows/conversation-feishu-live-acceptance.yml` 在 PR 上只执行 non-mutating contract：

- helper compile；
- `test_conversation_feishu_live_acceptance_v1.py`；
- `test_feishu_conversation_cycle_decoupling.py`；
- GitHub Actions context gate。

`real-feishu-reply` job 带有：

```yaml
if: github.event_name == 'workflow_dispatch'
```

因此 PR 创建、同步、rerun 都不能自动向真实 Feishu 发消息。

## 6. 显式真实验收流程

1. 在专用 Feishu 验收群/私聊中先发送一条明确的测试消息，并取得该消息的 `om_...` message ID。
2. 确认 master exact SHA。
3. 手工 dispatch `Conversation Feishu Live Acceptance V1`，输入：
   - `expected_head_sha`
   - 专用 `message_id`
   - `REPLY_TO_DEDICATED_FEISHU_ACCEPTANCE_MESSAGE`
4. workflow 依次执行：
   - exact master / actor / target guard；
   - Acceptance Infrastructure V2 Runtime Prepare；
   - V2 read-only Preflight；
   - production Conversation semantic contract；
   - 一次真实 Feishu reply；
   - delivery trace read-back；
   - sanitized Artifact 上传。

## 7. PASS 标准

机器结果必须同时满足：

```text
CONVERSATION_LIVE_CONTRACT = PASS
V2_PREFLIGHT = PASS / mutation_allowed=true
PRODUCTION_RUNTIME_CONVERSATION_CONTRACT = PASS
CONVERSATION_FEISHU_LIVE_ACCEPTANCE = PASS
delivery_stage = SENT
attempt_count >= 1
diagnostic_authority_changed = false
device_action_executed = false
```

## 8. 与真实 DUT 的边界

本 Gate **不等价**于真实 DUT Conversation E2E。

它证明的是：

```text
Conversation semantics/runtime
        +
real Feishu reply transport
        +
persisted delivery trace
```

它不证明：

```text
Feishu user turn
→ Case
→ real DUT SSH
→ ARMED/WATCHING
→ PCAP / PCM RX / PCM TX / Debug
→ Analyzer
→ deterministic Diagnosis
→ grounded reply
→ cleanup
```

上述完整链路仍由真实 DUT Conversation → Capture → Diagnosis → Reply Gate 单独验收。

## 9. 与 Human Evidence Feishu Live Acceptance 的关系

Human Evidence Live Acceptance 验证的是报告、图片、音频、Living Document 和文档权限投影；Conversation Feishu Live Acceptance 验证的是聊天回复 transport + delivery trace。两者不能互相替代，也不能把其中一方的 PASS 表述为另一方已通过。
