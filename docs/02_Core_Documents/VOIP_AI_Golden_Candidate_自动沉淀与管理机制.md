# VOIP AI 故障助手 — Case 自动沉淀 / Golden Candidate 管理机制

> 版本：Golden Candidate V1  
> 状态：已实现  
> 原则：自动沉淀、确定性判定、Evidence First、答案防泄漏、可审计、Golden Ready 才进入真实 AI Eval

## 1. 背景与目标

项目在冷启动阶段可能没有足量、规范整理的历史 Case。系统不能因此要求先人工整理几十个旧案例，也不能在没有真实 Ground Truth 的情况下伪造 AI 模型质量 PASS。

本机制的目标是：**从现在开始，每一个真实 Case 在正常排障过程中自动积累为 AI 质量资产。** 工程师无需额外记忆“这个问题以后要不要做 Golden Case”；系统根据当前 Case 的证据、Analyzer、确定性 Diagnosis、根因确认、Fix Verification、Audit 和答案泄漏风险，持续计算并持久化 Golden 状态。

## 2. 状态机

```text
NOT_ELIGIBLE
    │  新增真实 Evidence / Analyzer / Diagnosis
    ▼
PARTIAL_GOLDEN
    │  根因通过现有门禁确认
    ▼
GOLDEN_CANDIDATE
    │  L1直接证据、Baseline、Snapshot、Audit、防答案泄漏全部满足
    ▼
GOLDEN_READY
    │
    ├─ Tier B: ROOT_CAUSE_CONFIRMED
    └─ Tier A: FIX_VERIFIED（更高等级，推荐）
```

### 2.1 NOT_ELIGIBLE

适用于刚创建或尚未形成诊断资产的 Case，例如：

- 无当前 Case Evidence；
- 无 Analyzer / Diagnosis；
- 仅有故障描述。

这不是异常状态，只表示该 Case 尚未进入可沉淀阶段。

### 2.2 PARTIAL_GOLDEN

Case 已存在真实证据、Analyzer 或确定性 Diagnosis，但最终根因尚未确认。

典型缺口：

- `ROOT_CAUSE_NOT_CONFIRMED`；
- `NO_DETERMINISTIC_BASELINE`；
- `NO_SUCCESSFUL_ANALYZER`；
- Evidence / Audit 尚在形成中。

### 2.3 GOLDEN_CANDIDATE

Case 已具备已确认根因，但还存在至少一个 Golden 质量缺口或阻断项，例如：

- 已确认 Hypothesis 缺少当前 Case 的 L1 SUPPORT Evidence；
- Snapshot 无法完整构建；
- Audit 链不完整；
- Case summary、Evidence 文件名或 metadata 泄漏最终根因答案。

该状态可以进入人工修补队列，但默认不能进入 Production AI Eval。

### 2.4 GOLDEN_READY

只有同时满足以下条件才进入：

```text
ROOT_CAUSE_CONFIRMED
+ DIRECT_L1_SUPPORT
+ DETERMINISTIC_BASELINE
+ CASE_EVIDENCE_SNAPSHOT_READY
+ AUDIT_COVERAGE_COMPLETE
+ NO_ANSWER_LEAKAGE
```

`GOLDEN_READY` 是 `tools/export_ai_eval_dataset.py` 默认允许导出的唯一 Golden 状态。

## 3. Verification Tier

Golden Ready 与“修复是否验证”是两个维度。

### Tier B

```text
ROOT_CAUSE_CONFIRMED
```

根因已经由当前系统的正式门禁确认，并具有直接 L1 支持。它可以作为真实 AI Eval Ground Truth。

### Tier A

```text
FIX_VERIFIED
```

在 Tier B 基础上进一步经过修复与同环境验证。Tier A 是更高可信等级，正式验收数据集应逐步提高 Tier A 占比。

系统不会为了冷启动数量强制所有 Case 都先完成 Fix Verification；未 Fix Verified 的 Golden Ready 仍可作为 Tier B 使用，同时会给出 P2 建议 `RUN_FIX_VERIFICATION`。

## 4. 自动触发机制

### 4.1 SessionLocal 事务监听

Golden 机制绑定项目自身 `SessionLocal`，监听 Case-owned 领域对象的新增、修改和删除。

正常业务事务成功提交后，系统在独立跟随事务中重新计算该 Case 的 Golden 状态。

覆盖来源包括：

- Case 创建/状态变化；
- Evidence 上传与自动采集；
- AnalyzerRun 更新；
- DiagnosisRun 更新；
- Hypothesis 状态变化；
- Reproduction / Experiment 结果；
- CausalAssessment；
- Fix Verification。

Golden Sidecar 失败不能反向破坏已经成功提交的排障业务事务。下一次 Case 更新、显式 Refresh、Backfill 或 Eval Export 会自动修复状态。

### 4.2 为什么不使用全局 SQLAlchemy Listener

监听器仅绑定 `SessionLocal`，避免影响单元测试、离线工具或其他临时 SQLAlchemy Session。

## 5. 持久化模型

表：

```text
golden_candidate_assessments
```

一个 Case 只保留一条“当前状态”记录，主要字段包括：

- `case_id`
- `status`
- `verification_tier`
- `assessment_version`
- `score`
- `root_cause_confirmed`
- `fix_verified`
- `direct_l1_support`
- `deterministic_baseline_ready`
- `snapshot_ready`
- `audit_coverage_complete`
- `answer_leakage_risk`
- Evidence / Analyzer / Hypothesis 计数
- `blocker_codes`
- `gap_codes`
- `next_steps`
- `leakage_findings`
- `details_json`
- `assessed_at`

当前版本：

```text
golden-candidate-v1
```

状态历史不在该表重复保存，而通过不可变 AuditLog：

```text
GOLDEN_CANDIDATE_STATE_CHANGED
```

记录前后状态、Tier、分数、Gap 和 Blocker。

## 6. 确定性判定信号

### 6.1 Evidence

统计：

- Evidence 总数；
- `COMPLETE` Evidence 数；
- L1 Evidence 数。

没有 Evidence 的 Case 不可能成为 Golden Ready。

### 6.2 Analyzer

统计成功/部分成功 AnalyzerRun 数，并把 `NO_SUCCESSFUL_ANALYZER` 暴露为质量 Gap。

Analyzer 结果属于确定性事实层，不作为“答案泄漏”。

### 6.3 Root Cause

根因信号来自：

- `Hypothesis.status=CONFIRMED`；或
- `CausalAssessment.state=ROOT_CAUSE_CONFIRMED`。

对于正式 Golden Eval，仍要求至少存在一个 CONFIRMED Hypothesis 作为结构化 Ground Truth。

### 6.4 Direct L1 Support

至少需要当前 Case 已确认 Hypothesis 存在：

```text
EvidenceLevel = L1
Direction     = SUPPORT
RefType       = EVIDENCE / ANALYZER_RUN
```

AI Proposal、Historical Case、Knowledge、人工文字说明均不能代替该门禁。

### 6.5 Deterministic Baseline

至少存在一个 DiagnosisRun，并包含非空 `decision_json`。

### 6.6 Snapshot Ready

系统必须能够成功构建 `CaseEvidenceSnapshot`。Snapshot 是后续 AI Eval 真正看到的输入边界，因此无法构建 Snapshot 的 Case 不能进入 Golden Ready。

### 6.7 Audit Coverage

V1 要求根据 Case 当前阶段检查以下审计组：

- `CASE_CREATED`
- Evidence 存在时：`EVIDENCE_CREATED` 或 `EVIDENCE_UPLOADED`
- Diagnosis：`DIAGNOSIS_STARTED` / `DIAGNOSIS_CYCLE` / `DIAGNOSIS_UPDATED` 至少之一
- Root Cause Confirmed 时：`HYPOTHESIS_CONFIRMED` / `ROOT_CAUSE_CAUSALLY_CONFIRMED` 至少之一
- Fix Verified 时：`FIX_VERIFICATION_UPDATED`

缺失时输出明确 `AUDIT_*_MISSING` Gap。

## 7. 答案泄漏检测

Golden Eval 的目标是闭卷验证模型，而不是让模型从 Case 文本中读取标准答案。

V1 进行保守检测：

1. Case summary 中出现“根因/已确认/root cause/caused by/原因是/由于”等根因语义，并同时出现已确认 Hypothesis 的 code/title；
2. Evidence 文件名直接包含已确认 Hypothesis code/title；
3. Evidence metadata 在根因语义上下文中包含已确认 Hypothesis code/title。

命中时：

```text
blocker = ANSWER_LEAKAGE_RISK
status  <= GOLDEN_CANDIDATE
```

并给出：

```text
REMOVE_ANSWER_LEAKAGE
```

Analyzer Findings 不扫描为泄漏，因为它们属于有效的确定性证据输入。

## 8. 自动 Next Steps

系统根据缺口返回结构化下一步，例如：

- `ADD_REAL_EVIDENCE`
- `RUN_DETERMINISTIC_ANALYZERS`
- `RUN_DIAGNOSIS`
- `CONFIRM_ROOT_CAUSE`
- `ADD_DIRECT_L1_SUPPORT`
- `REMOVE_ANSWER_LEAKAGE`
- `COMPLETE_AUDIT_TRAIL`
- `RUN_FIX_VERIFICATION`

每一步包含 `priority` 和人类可读 `action`，用于后续 Web/飞书 UI 展示。

## 9. 管理 API

### 查询并自动刷新单个 Case

```http
GET /api/v1/cases/{case_id}/golden-candidate
```

默认 `refresh=true`。

### 显式刷新

```http
POST /api/v1/cases/{case_id}/golden-candidate/refresh
```

### Golden 列表

```http
GET /api/v1/golden-candidates
GET /api/v1/golden-candidates?status=GOLDEN_READY
GET /api/v1/golden-candidates?verification_tier=A
```

### 汇总

```http
GET /api/v1/golden-candidates/summary
```

返回：

- total
- 各状态数量
- A/B Tier 数量
- `eval_ready_count`

### 历史 Case 一次性 Backfill

```http
POST /api/v1/golden-candidates/backfill?limit=500
```

用于升级部署后，对已经存在数据库中的历史 Case 补算 Golden 状态。它不要求人工先整理历史文件；能满足多少条件就自动进入相应状态，其余 Case 会停在 NOT_ELIGIBLE/PARTIAL/CANDIDATE 并给出缺口。

## 10. 与 AI Eval 的对接

默认导出命令保持：

```bash
PYTHONPATH=backend:. python tools/export_ai_eval_dataset.py \
  --out validation/ai_eval_field_dataset_v2.json \
  --require-minimum 10
```

V1 默认只导出：

```text
GOLDEN_READY
+ REAL
+ CONFIRMED Hypothesis
+ ROOT_CAUSE_CONFIRMED / FIX_VERIFIED
```

被跳过的 Case 会在 `export_summary.skipped[]` 中返回：

- 当前 Golden 状态；
- blocker_codes；
- gap_codes；
- next_steps。

调试兼容场景可以显式：

```bash
python tools/export_ai_eval_dataset.py --allow-non-ready ...
```

但 Production AI Eval 不应使用该选项。

Golden 元数据存放在每个 Eval Case 的 `golden_candidate` 节点，不写入 `ground_truth` Pydantic 合同，保持 `ai-model-eval-dataset-v2` 兼容。

## 11. 冷启动使用方式

当前没有历史 Case 时无需停下来补数据。

推荐：

```text
AI = SHADOW
        ↓
新问题正常排障
        ↓
Case 自动累积 Evidence / Analyzer / Diagnosis
        ↓
PARTIAL_GOLDEN
        ↓
根因确认
        ↓
GOLDEN_CANDIDATE / GOLDEN_READY
        ↓
有修复验证时升级 Tier A
        ↓
累计到足量 GOLDEN_READY
        ↓
真实 Gateway Eval
        ↓
Promotion Gate
```

第一次部署后执行一次 Backfill 即可给已有 Case 建立基线状态。之后由事务监听自动维护。

## 12. 验收标准

Case 自动沉淀机制验收必须同时满足：

1. 新 Case 创建后自动生成 Golden Assessment；
2. 上传 Evidence 后状态/Gap 自动更新；
3. Diagnosis/Hypothesis/Fix Verification 变化后自动重算；
4. 状态变化产生 Audit；
5. 根因未确认的 Case 不能 GOLDEN_READY；
6. 缺直接 L1 Support 的确认根因不能 GOLDEN_READY；
7. 答案泄漏命中后不能 GOLDEN_READY；
8. Golden Ready 导出成功；非 Ready 默认被 Eval Export 拒绝；
9. Backfill 能处理升级前的历史 Case；
10. Golden Sidecar 失败不能导致原业务事务失败。

## 13. 与 AI-E1～AI-E6 的关系

该机制位于 AI-E1 Real Model Eval 的上游：

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
