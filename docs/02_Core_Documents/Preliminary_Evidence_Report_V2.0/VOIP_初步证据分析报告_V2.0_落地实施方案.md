# VOIP 初步证据分析报告 V2.0 落地实施方案

| 字段 | 内容 |
|---|---|
| 文档版本 | V2.0 |
| 状态 | Implementation Ready |
| 日期 | 2026-09-03 |
| 输入 | PRD V2.0 / SPEC V2.0 / Traceability V2.0 |
| 代码基线 | `master@bd8569d7031385461ac4279c95b452e062d834d3` |
| Change Request | CR-VOIP-EVIDENCE-002 |

## 1. 实施原则

本任务不是“再优化一次报告模板”，而是修正 Report Pipeline 的工程所有权：

- Parser/Analyzer 负责原始测量；
- State Machine/Timeline 负责生命周期与时间事实；
- Finding Event/Aggregator 负责异常事件与聚合；
- Correlator 负责跨层候选关系；
- Semantic Validator 负责发布前静态检查；
- LLM 只负责解释、候选原因、实验建议和语言压缩；
- Renderer/Feishu 只投影 Canonical Report，不自行创造事实。

V1.0 继续冻结；实现以 V2 schema/feature flag 逐步引入。

## 2. 当前代码接入点

当前仓库已有以下报告基础模块，应优先增量改造而不是重建第二套系统：

- `backend/app/reports/evidence_brief.py`：现有 Evidence Brief Schema/Composer 主体。
- `backend/app/reports/evidence_card.py`：Evidence Card 构造。
- `backend/app/reports/finding_composer.py`：Finding 组合/聚合逻辑。
- `backend/app/reports/evidence_visuals.py`：证据图 Renderer。
- `backend/app/reports/prd_spec_v1_alignment.py`：现有 V1 PRD/SPEC alignment/finalization。
- `backend/app/reports/report_grounding.py`：报告 grounding/事实边界相关能力。
- `backend/app/reports/actionable_summary.py`：行动摘要/下一步建议相关能力。
- `backend/tests/`：已有 report projection、semantic、AI gate 等测试基础。

建议新增而不是继续把逻辑堆进 `evidence_brief.py`：

```text
backend/app/reports/v2/
  __init__.py
  schema.py
  call_reconstruction.py
  timeline.py
  finding_events.py
  correlation.py
  visibility.py
  artifact_binding.py
  semantic_validator.py
  recommendation.py
  composer.py
  migration.py
```

如果当前架构已有更合适的 analyzer/call lifecycle 模块，应把确定性能力放到上游，`reports/v2` 仅消费结果；禁止为了方便把 SIP parser 重复实现到 report composer。

## 3. Phase 0：冻结失败样本与 Golden #002

### 目标

先让当前错误可重复失败，再修代码，避免“修完看起来好了但没有防回归”。

### 任务

1. 将本次 PCAP 作为授权 test fixture，或生成保留相同关键语义的脱敏等价 fixture。
2. 保存 `expected_call_v2.json`、`expected_events_v2.json`、`expected_report_assertions.yaml`。
3. 建立 `test_preliminary_evidence_golden_002.py`。
4. 在未修代码前，明确至少让以下断言失败：ACK-not-end、media-window、problem-count、loss wording、recommendation severity、cross-layer cluster。

### 建议目录

```text
backend/tests/fixtures/preliminary_evidence/golden_002/
  capture.pcap              # 若许可提交
  expected_call_v2.json
  expected_events_v2.json
  expected_report_assertions.yaml
```

若 PCAP 因体积/隐私不允许入库，则使用受控测试 Artifact 下载/runner-mounted fixture，并在 manifest 固化 SHA256。

### Exit Criteria

Golden #002 能在 CI/runner 确定性运行；所有 ground truth 不进入生产 Analyzer/AI 输入。

## 4. Phase 1：Call Reconstruction P0

### 目标

消灭 ACK=Call End 等生命周期错误。

### 实现

新增/收敛 `call_reconstruction.py`：

- SIP transaction/call event 输入；
- INVITE/1xx/2xx/ACK/BYE/CANCEL/final failure 状态机；
- `termination.observed/source/time/evidence_refs`；
- capture end 独立字段；
- multi-call scope isolation。

### 必测

- INVITE→200→ACK→BYE；
- INVITE→200→ACK→capture end；
- 4xx/5xx；
- CANCEL/487；
- 同 PCAP 多 Call。

### Gate

`R001 CALL_END_WITHOUT_TERMINATION_EVENT`。

## 5. Phase 2：Timeline Model P0

### 目标

统一绝对时间、Call 相对时间、Media Observation Window。

### 实现

`timeline.py`：

- Capture Window；
- Signaling Window；
- per RTP stream observation window；
- aggregate Media Observation Window；
- PCM observation windows；
- Event relative anchors。

### 修复点

- Media Window 不再引用 ACK；
- 有 RTP 时 zero-length media window 直接 validator fail；
- Finding 多 event 分别保存 relative time。

### Gate

R002/R003/R013。

## 6. Phase 3：Finding Event + Taxonomy P0

### 目标

把“两个离散 spike = 持续 10 秒异常”这一类错误从数据模型上消除。

### 实现

`finding_events.py`：

- Event 一等实体；
- Finding 聚合 `event_refs/event_count/span/continuous`；
- Observation taxonomy；
- compatibility adapter 把现有 V1 finding 转成 V2 event/finding。

修改 `finding_composer.py`：

- 先 event 后 finding；
- NORMAL/EXCLUSION 与 ABNORMAL 分离；
- `problem_count` 只由 abnormal 主 Finding 计算。

### Gate

R004/R007/R009/R011/R014。

## 7. Phase 4：Cross-Layer Correlation P0

### 目标

把同一媒体时间事件从“PCM RX 问题 + PCM TX 问题”升级为一个可解释的 Cross-Layer Cluster。

### 实现

`correlation.py`：

- same call；
- temporal window（初始 profile 建议 50ms）；
- media path compatibility；
- event family compatibility；
- cluster member provenance；
- cluster-to-primary-finding 规则。

### 第一版不做

- 不直接输出物理根因；
- 不用 LLM 决定是否聚类；
- 不因相关性自动推导因果。

### Gate

R015 + AC-XLY-*。

## 8. Phase 5：Visibility & Completeness P0

### 目标

避免 `COMPLETE`、“RTP 双向”等范围过强表述。

### 实现

`visibility.py` 计算：

- signaling caller/callee leg；
- media caller/callee leg；
- end-to-end；
- termination observed；
- root cause readiness。

`pipeline_status` 与上述字段完全分离。

修改现有 `evidence_brief.py`/V2 `composer.py`：所有“完整/双向/结束”文案只能由 visibility/reconstruction 字段生成。

### Gate

R008。

## 9. Phase 6：Artifact Binding P0

### 目标

解决“有 PCM、能画波形，但异常音频仍不可用”。

### 实现

`artifact_binding.py`：

- Finding/Event → source PCM/RTP；
- 根据类型生成代表 Clip；
- event/finding/source/time/hash provenance；
- source available 但 render 失败时结构化 failure reason。

修改 `evidence_visuals.py`：

- 统一 Event annotation；
- 波形/时频/RTP timeline 可共享 representative time marker；
- 图下不再生成重复通用说明。

### Gate

R006/R012。

## 10. Phase 7：Semantic Validator P0

### 目标

建立“报告编译器静态检查”。

### 实现

`semantic_validator.py` 实现 SPEC R001~R015；输出：

```json
{
  "status": "PASS|FAIL",
  "violations": [
    {"rule":"R001", "severity":"P0", "path":"...", "detail":"..."}
  ]
}
```

Pipeline：

```text
canonical v2
→ validate
→ PASS: compose/project
→ FAIL: block user COMPLETE + audit + internal diagnostics
```

不得仅记录 warning 后继续正常发布。

## 11. Phase 8：Recommendation Engine P0/P1

### 目标

消除“MEDIUM 却复核 HIGH/CRITICAL”模板废话。

### 实现

改造 `actionable_summary.py` 或新增 `recommendation.py`：

1. 先 deterministic rule；
2. 再检索 VOIP KB；
3. 最后可选 AI 语言/实验设计增强。

每条建议结构化绑定当前 finding/cluster/evidence gap，并提供：Action、Why、Collect、Decision Rule、Pass Criteria。

Validator 校验引用实体存在。

### 本 Golden #002 推荐方向

- 复现时同步 PCM/RTP；
- 读取 `rff_cnt/tfe_cnt`；
- CPU/softirq/process scheduling；
- aimd/dsp debug；
- 代表 event RX/TX Clip；
- 不把当前 sequence-continuous timing spike 当 RTP loss。

## 12. Phase 9：Report UX V2 P1

### 目标

报告从“12 页证据重复堆叠”变成“1 页决策 + Finding Cards + Appendix”。

### Composer

V2 `composer.py` 首屏固定：

1. 一句话结论；
2. 用户问题是否复现；
3. Top abnormal cluster/finding；
4. Normal/Exclusion；
5. Evidence Boundary；
6. Next Step。

### Evidence Card

修改/扩展 `evidence_card.py`：

- What happened；
- When；
- Key Evidence；
- Interpretation；
- Not Confirmed；
- Next Action。

### Renderer

- 修复重复 `1.` 编号；
- 内部 Enum/Schema/Audit 放 Appendix；
- 删除每张图重复的 Finding/Root Cause 免责声明；
- 图上直接标 Event/Cluster 时间和关键数值。

## 13. Phase 10：AI Role Enforcement P0

### 目标

确保升级模型不会改变事实权限。

### 实现

在 AI Gateway/Report Grounding 入口：

- 输入 canonical fact snapshot + validator status；
- AI 返回字段白名单；
- authoritative field 写入尝试直接拒绝；
- AI 文本与 facts 矛盾时 rejection + metric。

重点复用/增强 `report_grounding.py` 和现有 semantic/AI gate tests。

## 14. Phase 11：V1/V2 双轨迁移

Feature flags 建议：

```text
PRELIMINARY_EVIDENCE_V2_COMPOSE=true/false
PRELIMINARY_EVIDENCE_V2_PROJECT=false
PRELIMINARY_EVIDENCE_V2_STRICT_VALIDATOR=true
```

Rollout：

1. **Shadow Compose**：生产输入同时生成 V1/V2，用户仍看 V1；比较 semantic divergence。
2. **Dual Review**：指定内部 Case 展示 V2，人工复核。
3. **Canary Projection**：少量 Case 默认 V2。
4. **Default V2**：达到门禁后切换。
5. V1 reader 永久保留历史兼容，writer 可后续停止。

## 15. 测试文件建议

新增：

```text
backend/tests/test_evidence_v2_call_reconstruction.py
backend/tests/test_evidence_v2_timeline.py
backend/tests/test_evidence_v2_finding_events.py
backend/tests/test_evidence_v2_correlation.py
backend/tests/test_evidence_v2_visibility.py
backend/tests/test_evidence_v2_artifact_binding.py
backend/tests/test_evidence_v2_semantic_validator.py
backend/tests/test_evidence_v2_recommendation.py
backend/tests/test_evidence_v2_projection.py
backend/tests/test_preliminary_evidence_golden_002.py
```

已有 V1 report/actionable/AI semantic tests 全部保留，作为 no-regression。

## 16. CI / Release Governance 接入

新增 make/CI gate 建议：

```text
Evidence V2 Contracts
Evidence V2 Golden #002
Evidence V2 Semantic Validator
Evidence V1 Compatibility
```

必须并入项目既有 Full Acceptance，而不是建立一个可以绕过 Release Governance 的独立“软测试”。

对 Production Deploy 不削弱：Source Manifest、Compose Config、Full Acceptance、Runtime Verify、Exact Source Binding 均维持现状。

## 17. 开发拆分建议

建议按可独立审阅的小 PR 实施：

| PR | 内容 | 风险 |
|---|---|---|
| A | Golden #002 + failing contracts | 低 |
| B | Call Reconstruction + Timeline | 高/P0 |
| C | Event Taxonomy + Finding count | 中 |
| D | Correlation + Visibility | 高/P0 |
| E | Artifact Binding + Visual annotation | 中 |
| F | Semantic Validator + CI fail-closed | 高/P0 |
| G | Recommendation + UX V2 | 中 |
| H | Shadow/Dual Compose + Feishu projection | 高 |
| I | Full regression + production acceptance | 高 |

不建议把所有修改塞进一个超大 PR。

## 18. Definition of Done

V2 “完成”必须同时满足：

- PRD/SPEC/Traceability 已冻结；
- Golden #002 100% PASS；
- R001~R015 100% PASS；
- Call/Timeline ground truth 测试 100% PASS；
- 3 类以上真实/实验室 PCAP E2E PASS；
- 30 秒摘要人工验收 5/5；
- V1 compatibility PASS；
- Root Cause Authority/Provenance/Bundle/Audit PASS；
- Full Software Acceptance PASS；
- Production Exact Source Binding PASS；
- V2 生产 Case 人工复核无 P0/P1 correctness blocker。

只有达到上述条件，才能把 `Preliminary_Evidence_Report_V2.0` 从 Baseline Candidate 改为 Baseline Frozen，并关闭 CR-VOIP-EVIDENCE-002。
