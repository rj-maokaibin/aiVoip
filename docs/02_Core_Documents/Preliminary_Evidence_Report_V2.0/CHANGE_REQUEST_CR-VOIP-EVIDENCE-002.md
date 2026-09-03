# CR-VOIP-EVIDENCE-002：Preliminary Evidence Report V2.0

| 字段 | 内容 |
|---|---|
| Change Request | CR-VOIP-EVIDENCE-002 |
| 日期 | 2026-09-03 |
| 影响基线 | Preliminary Evidence Report V1.0 |
| 变更类型 | Baseline Change / Semantic Correctness / Report UX |
| 风险等级 | P0 Correctness |
| 状态 | Proposed / Implementation Required |

## 1. 触发原因

对真实离线 PCAP 生成的初步证据报告进行独立复核后，确认底层 SIP/DTMF/PCM/RTP 多项原始测量可以成立，但 Report Pipeline 仍存在语义错误和低价值表达，说明 V1.0 的 Schema/Completeness/Golden/Release Gate 对“字段存在”约束较强，对“字段之间逻辑自洽”约束不足。

已暴露的问题类型包括：

- SIP ACK 时间被错误作为 Call End。
- 存在持续 RTP 时，`ACTIVE_MEDIA_WINDOW` 被渲染为零长度窗口。
- 两个离散 PCM timing event 被渲染成一个持续约 10 秒的异常时间范围。
- PCM RX/TX 同时发生的 timing event 被拆成两个独立问题，未与同期 RTP timing spike 聚合。
- Packet interval spike 的文案容易被解释为 PCM sample/data loss，证据强度越界。
- 正常/排除性 `DTMF_SIP_DIAL_MATCH` 被计入问题点数量。
- 当前最高 Severity 为 MEDIUM 时，建议仍引用不存在的 HIGH/CRITICAL Finding。
- PCM 数据和波形均可生成时，代表异常 Audio Clip 仍未绑定。
- “RTP 双向/证据 COMPLETE”等范围表达未充分区分 caller leg、callee leg 和 end-to-end visibility。
- 报告存在大量重复免责文案、内部 Enum 暴露、图表解释模板化、Word 编号等可读性问题。

## 2. 根因分类

本 CR 不把问题归因于单一 LLM 模型能力。问题分为四层：

1. **Deterministic reconstruction 缺陷**：Call state、media window、event time 语义不完整。
2. **Semantic contract 缺陷**：Schema 可合法但内容逻辑矛盾，缺少跨字段 invariant。
3. **Correlation/aggregation 缺陷**：同一时间跨层事件未聚类，正常证据与异常问题未分域。
4. **Report Composer/Renderer 缺陷**：建议模板、Artifact binding、信息架构和排版未严格消费 Canonical Facts。

LLM 仅用于解释、候选原因、下一步实验和语言压缩；不能作为上述确定性错误的主要修复手段。

## 3. 变更决策

批准方向：建立 V2.0，而不是原地修改冻结的 V1.0。

V2.0 必须增加：

- SIP Call State Machine 与 termination source contract。
- Timeline Model V2。
- Finding Event Model。
- Cross-Layer Correlation Cluster。
- Problem/Normal/Exclusion 分类。
- Evidence Visibility Model。
- Semantic Validator。
- Artifact binding invariant。
- Dynamic Recommendation Engine。
- Report UX V2。
- Golden Regression #002 与 semantic release gates。

## 4. 不允许削弱的既有约束

- Root Cause Authority invariant。
- Evidence-first。
- Evidence Provenance。
- Canonical Backend Source。
- Analyzer/Renderer/Profile/Schema versioning。
- PARTIAL_COMPLETE/UNKNOWN 的证据不足安全边界。
- 飞书失败不得改变后端 Canonical Report。
- 现有 M7/Golden/Audit/Bundle/Cleanup 治理。

## 5. 兼容性策略

- V1 历史报告继续按 `preliminary-evidence-report-v1` 可读，不重写历史事实。
- V2 使用 `preliminary-evidence-report-v2`。
- API 可在现有 endpoint 下通过 schema/version 返回，不要求破坏式更换 URL。
- V1 `time_range` 可在迁移层映射为 V2 `events[] + span`；无法还原离散事件时明确 `legacy_projection=true`。
- V2 正式上线前，V1 继续作为生产兼容格式；V2 先 Shadow/Dual Compose，再切换主投影。

## 6. 完成条件

该 CR 只有在以下条件全部满足后才能关闭：

- V2 PRD/SPEC/Traceability Baseline 冻结。
- P0 Semantic Validator 规则全部实现并纳入 CI。
- Golden Regression #002 通过。
- 不少于 3 类真实/实验室 PCAP 端到端重放通过：正常拨号、媒体 timing、明确 RTP loss/Call termination。
- Feishu/DOCX/Web 主摘要满足 30 秒可读性验收。
- Full Acceptance、Root Cause Authority、Evidence Provenance 无回归。
