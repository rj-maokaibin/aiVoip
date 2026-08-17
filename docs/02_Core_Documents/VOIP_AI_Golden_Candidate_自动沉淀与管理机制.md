# VOIP AI 故障助手 — Case 自动沉淀 / Golden Candidate 管理机制

> 版本：Golden Candidate V1  
> 状态：已实现  
> 原则：自动沉淀、确定性判定、Evidence First、Analyzer First、答案防泄漏、可审计、Golden Ready 才进入真实 AI Eval

## 1. 背景与目标

项目冷启动阶段可能没有足量、规范整理的历史 Case。本机制不要求先人工整理几十个旧问题，而是从现在开始让每个真实 Case 在正常排障流程中自动积累为 AI 质量资产。

工程师不需要额外维护“Golden 清单”。系统根据当前 Case 的 Evidence、Analyzer、确定性 Diagnosis、根因确认、Fix Verification、Audit 与答案泄漏风险，持续计算并持久化 Golden 状态。

## 2. 状态机

```text
NOT_ELIGIBLE
    │  获得真实 Evidence / Analyzer / Diagnosis
    ▼
PARTIAL_GOLDEN
    │  根因通过正式门禁确认
    ▼
GOLDEN_CANDIDATE
    │  补齐完整 L1 Evidence、成功 Analyzer、直接根因证据、Baseline、Snapshot、Audit
    │  且消除答案泄漏
    ▼
GOLDEN_READY
    │
    ├─ Tier B：ROOT_CAUSE_CONFIRMED
    └─ Tier A：FIX_VERIFIED（更高可信等级）
```

### NOT_ELIGIBLE

刚创建、尚未形成诊断资产的 Case。例如只有故障描述，没有当前 Case Evidence。

### PARTIAL_GOLDEN

已经存在真实 Evidence、Analyzer 或确定性 Diagnosis，但根因还未确认。

### GOLDEN_CANDIDATE

已经有确认根因，但仍有至少一个质量缺口或阻断项，例如：

- 没有成功 Analyzer；
- 没有 COMPLETE/L1 Evidence；
- 已确认 Hypothesis 缺少当前 Case 的 L1 SUPPORT；
- Snapshot 无法构建；
- Audit 链不完整；
- summary、Evidence 文件名或 metadata 泄漏最终答案。

### GOLDEN_READY

V1 的硬门槛为：

```text
AT_LEAST_ONE_COMPLETE_L1_EVIDENCE
+ AT_LEAST_ONE_SUCCESSFUL_ANALYZER
+ ROOT_CAUSE_CONFIRMED
+ DIRECT_L1_SUPPORT
+ DETERMINISTIC_BASELINE
+ CASE_EVIDENCE_SNAPSHOT_READY
+ AUDIT_COVERAGE_COMPLETE
+ NO_ANSWER_LEAKAGE
```

只有 `GOLDEN_READY` 默认允许进入真实 AI Eval。

之所以把成功 Analyzer 设为硬门槛，是因为 Reasoning Gateway 不直接上传原始 PCAP/PCM/WAV；如果只有原始文件而没有确定性 Analyzer 结果，模型实际看不到足够的诊断事实，不适合作为模型质量验收样本。

## 3. Verification Tier

Golden Ready 与修复验证分开管理：

- **Tier B**：`ROOT_CAUSE_CONFIRMED`。具备正式确认根因和直接 L1 支持，可进入真实 AI Eval。
- **Tier A**：`FIX_VERIFIED`。在 Tier B 基础上完成修复及同环境验证，是更高可信等级。

系统不会为了冷启动数量强制所有 Case 都先完成 Fix Verification；Tier B 可先用于内部真实 Eval，同时系统继续建议 `RUN_FIX_VERIFICATION`。

## 4. 自动触发

Golden 机制绑定项目自己的 `SessionLocal`，而不是全局 SQLAlchemy Session。

在正常业务事务成功提交后，系统使用独立跟随事务重新计算该 Case 的 Golden 状态。覆盖：

- Case 创建/状态变化；
- Evidence 上传与自动采集；
- AnalyzerRun 更新；
- DiagnosisRun 更新；
- Hypothesis 状态变化；
- Reproduction / Experiment / CausalAssessment；
- Fix Verification。

新 Case 的 ID 由 SQLAlchemy flush 阶段生成，因此监听器同时在 `before_flush` 与 `after_flush` 收集 Case ID；业务 commit 成功后才执行 Golden sidecar 刷新。

Golden sidecar 失败不会把已经成功提交的排障业务事务改成失败。后续 Case 更新、显式 Refresh、Backfill 或 Eval Export 都可以重新计算并修复状态。

## 5. 持久化

新增表：

```text
golden_candidate_assessments
```

每个 Case 保存一条当前 assessment，包括：

- status / verification_tier / score；
- root_cause_confirmed / fix_verified；
- direct_l1_support；
- deterministic_baseline_ready；
- snapshot_ready；
- audit_coverage_complete；
- answer_leakage_risk；
- Evidence/Analyzer/Hypothesis 计数；
- blocker_codes / gap_codes / next_steps；
- leakage_findings / details_json；
- assessed_at / updated_at。

当前合同版本：`golden-candidate-v1`。

状态迁移通过 AuditLog 记录：

```text
GOLDEN_CANDIDATE_STATE_CHANGED
```

## 6. 确定性质量判定

### Evidence

Golden Ready 至少需要：

```text
evidence_count > 0
complete_evidence_count > 0
l1_evidence_count > 0
```

缺失时会返回 `NO_CASE_EVIDENCE`、`NO_COMPLETE_EVIDENCE`、`NO_L1_EVIDENCE`。

### Analyzer

Golden Ready 至少需要一个状态为 `SUCCESS / PARTIAL_SUCCESS / legacy SUCCEEDED` 的 AnalyzerRun。

没有成功 Analyzer 时：

```text
gap = NO_SUCCESSFUL_ANALYZER
next_step = RUN_DETERMINISTIC_ANALYZERS
```

### Root Cause / Direct L1 Support

根因信号来自 `Hypothesis.status=CONFIRMED` 或 `CausalAssessment.state=ROOT_CAUSE_CONFIRMED`；真实 Eval Ground Truth 最终仍要求结构化 CONFIRMED Hypothesis。

已确认 Hypothesis 至少需要：

```text
EvidenceLevel = L1
Direction     = SUPPORT
RefType       = EVIDENCE / ANALYZER_RUN
```

AI Proposal、Historical Case、Knowledge 或单纯人工描述不能代替这个门槛。

### Deterministic Baseline

至少存在一个 DiagnosisRun 且 `decision_json` 非空。

### Snapshot

必须能够成功构建 `CaseEvidenceSnapshot`。Snapshot 是真实 AI Eval 的输入边界。

### Audit Coverage

按 Case 当前阶段检查：

- `CASE_CREATED`；
- 有 Evidence 时：`EVIDENCE_CREATED` 或 `EVIDENCE_UPLOADED`；
- 有 Analyzer 时：`ANALYZER_COMPLETED` / `PACKET_ANALYSIS_FINISHED` / `MEDIA_ANALYSIS_FINISHED` / `PCM_ANALYSIS_FINISHED` 至少一个；
- Diagnosis：`DIAGNOSIS_STARTED` / `DIAGNOSIS_CYCLE` / `DIAGNOSIS_UPDATED` 至少一个；
- Root Cause：`HYPOTHESIS_CONFIRMED` / `ROOT_CAUSE_CAUSALLY_CONFIRMED` 至少一个；
- Fix Verified：`FIX_VERIFICATION_UPDATED`。

## 7. 答案泄漏防护

真实 Eval 必须是闭卷考试。V1 会检查：

1. Case summary 中存在“根因/已确认/root cause/caused by/原因是/由于”等语义，并包含已确认 Hypothesis code/title；
2. Evidence 文件名直接包含已确认 Hypothesis code/title；
3. Evidence metadata 在根因语义上下文中包含已确认 Hypothesis code/title。

命中时：

```text
blocker = ANSWER_LEAKAGE_RISK
status <= GOLDEN_CANDIDATE
next_step = REMOVE_ANSWER_LEAKAGE
```

Analyzer Findings 不算答案泄漏，因为它们属于合法的确定性证据事实。

## 8. 自动 Next Steps

系统根据缺口返回结构化下一步：

- `ADD_REAL_EVIDENCE`
- `ADD_COMPLETE_L1_EVIDENCE`
- `RUN_DETERMINISTIC_ANALYZERS`
- `RUN_DIAGNOSIS`
- `CONFIRM_ROOT_CAUSE`
- `ADD_DIRECT_L1_SUPPORT`
- `REMOVE_ANSWER_LEAKAGE`
- `COMPLETE_AUDIT_TRAIL`
- `RUN_FIX_VERIFICATION`

每一步包含 priority 与人类可读 action，可直接供后续 Web/飞书展示。

## 9. 管理 API

```http
GET  /api/v1/cases/{case_id}/golden-candidate
POST /api/v1/cases/{case_id}/golden-candidate/refresh
GET  /api/v1/golden-candidates
GET  /api/v1/golden-candidates?status=GOLDEN_READY
GET  /api/v1/golden-candidates?verification_tier=A
GET  /api/v1/golden-candidates/summary
POST /api/v1/golden-candidates/backfill?limit=500
```

`backfill` 用于升级部署后给数据库中已有 Case 补算状态。它不会伪造缺失 Evidence/Audit；满足多少条件就进入对应状态，其余 Case 会保留 Gap 和 Next Steps。

## 10. 与真实 AI Eval 的衔接

默认导出：

```bash
PYTHONPATH=backend:. python tools/export_ai_eval_dataset.py \
  --out validation/ai_eval_field_dataset_v2.json \
  --require-minimum 10
```

只导出：

```text
GOLDEN_READY
+ REAL
+ CONFIRMED Hypothesis
+ ROOT_CAUSE_CONFIRMED / FIX_VERIFIED
```

被跳过的 Case 会在 `export_summary.skipped[]` 中返回 status、blocker_codes、gap_codes、next_steps。

调试兼容模式可使用 `--allow-non-ready`，但 Production AI Eval 不应使用。

## 11. 冷启动推荐流程

```text
AI = SHADOW
    ↓
新问题正常排障
    ↓
Case 自动沉淀 Evidence / Analyzer / Diagnosis
    ↓
PARTIAL_GOLDEN
    ↓
根因确认
    ↓
GOLDEN_CANDIDATE
    ↓
补齐质量门槛
    ↓
GOLDEN_READY Tier B
    ↓
Fix Verification（有条件时）
    ↓
GOLDEN_READY Tier A
    ↓
累计足量样本
    ↓
真实 Gateway Eval
    ↓
Promotion Gate
```

## 12. 验收标准

1. 新 Case commit 后自动产生 assessment；
2. Evidence/Analyzer/Diagnosis/Hypothesis/Fix 状态变化后自动重算；
3. 状态变化写入 Audit；
4. 根因未确认不能 Ready；
5. 缺 COMPLETE/L1 Evidence 不能 Ready；
6. 缺成功 Analyzer 不能 Ready；
7. 缺直接 L1 Support 不能 Ready；
8. 答案泄漏不能 Ready；
9. Golden Ready 默认可导出，非 Ready 默认被真实 Eval 导出拒绝；
10. Backfill 能处理升级前已有 Case；
11. Golden sidecar 失败不能导致原业务 transaction 失败；
12. AI-E1～E6 专项 Gate、数据库 migration、完整 backend regression 全部通过。

## 13. 与 AI-E1～AI-E6 的关系

```text
Operational Cases
      ↓
Golden Candidate Engine
      ↓
GOLDEN_READY
      ↓
Real Eval Dataset Export
      ↓
AI Model Quality Eval
      ↓
AI Promotion Gate
      ↓
CONTROLLED_PLANNER（满足全部生产门禁后）
```

因此项目不再依赖“先手工整理一批 Golden JSON”才能开始积累真实 AI 质量数据。
