# M5 Foundation — Rule / Knowledge / Report

## 已实现

### Rule Engine
- YAML 声明式 DSL，不执行 `eval` / Python expression。
- 白名单 Fact Path、白名单操作符、白名单输出 Action。
- `RuleDefinition / RuleVersion / RuleReplayRun`。
- 版本内容不可变：同一 `rule_key + version` checksum 不一致直接拒绝。
- DRAFT / ACTIVE / APPROVED 生命周期；创建与激活分离，禁止创建者自批。
- Active Rule 可从数据库加载；新安装未 Bootstrap 时可只读加载仓库内已审核规则。
- 历史 Case Rule Replay，不覆盖原 AnalyzerResult。
- Rule Match 不能凭自身伪装为 L1；L1 必须回指当前 AnalyzerRun/Evidence。

### Knowledge / Historical Case
- `KnowledgeItem`：协议、诊断指南、Case、BUG、Commit 等统一承载。
- 中文 bigram + ASCII token 的轻量相似度基础实现。
- Case 相似度综合：现象文本 + 已支持/确认 Hypothesis + Fault Domain + Version token。
- `CaseRelation` 持久化相似 Case。
- 历史 Case 只增加 L4 Evidence，不能改变为 CONFIRMED。
- Knowledge seed 与检索 API；普通新建条目默认未验证，独立 Reviewer 验证后才进入诊断检索。

### Reasoning Gateway
- 只发送结构化摘要。
- Evidence 仅发送 id/type/source/filename/hash/metadata 白名单字段。
- 不发送 object key/raw payload/原始 PCM/PCAP/WAV。
- 发送 Rule/History/Knowledge 结构化上下文。
- Prompt Version 进入请求和 DiagnosisRun 追溯。
- LLM 新增 Hypothesis 仍强制 L5、不可 Confirm。

### Diagnosis Report
- HTML + JSON 双格式。
- 保存到 MinIO，并登记 Artifact + SHA256。
- 报告按：已知 / 未知 / 已排除 / Hypothesis / 历史 Case / Traceability 分栏。
- 记录 reasoner/workflow/model/rule trace。
- 报告不把 ACTIVE/L4/L5 描述成已确认根因。

## Bootstrap

```bash
curl -X POST 'http://localhost:8000/api/v1/rules/bootstrap?actor=admin'
curl -X POST 'http://localhost:8000/api/v1/knowledge/bootstrap?actor=admin'
```

## Rule 示例

```yaml
key: RTP_BURST_LOSS_STUTTER
version: "1.0.0"
when:
  all:
    - path: symptoms.AUDIO_STUTTER
      op: truthy
    - path: anomaly_counts.BURST_LOSS
      op: gte
      value: 1
then:
  - action: hypothesis
    payload:
      code: RTP_PACKET_LOSS_PATH
      status: SUPPORTED
      evidence_level: L1
```

Rule Engine 会把这条 L1 绑定到实际 `AnalyzerRun`；没有实际 AnalyzerRun 时自动降级，不能单靠 Rule Match 产生“直接证据”。

## 下一步
1. RBAC 正式约束 Rule/Knowledge 管理接口。
2. 知识文档 ingestion pipeline：文档 -> chunk -> AI结构化 -> 人工审核。
3. BUG/Commit Adapter。
4. 向量检索后端可选接入，但保留当前确定性 feature similarity 作为 baseline。
5. Report PDF 通过 Headless Chromium 生成。
