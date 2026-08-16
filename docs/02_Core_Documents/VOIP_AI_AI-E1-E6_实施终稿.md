# VOIP AI 故障助手 — AI-E1～AI-E6 实施终稿

> 状态：代码实施完成，生产 AI Promotion 默认阻断  
> 原则：Evidence First / Deterministic Authority / AI Proposal Only / Registry-Only Planning / Auditable Promotion

## 1. 最终架构

```text
Current Case Evidence
    │
    ├─ L1/L2 Analyzer / Rule / Reproduction / Experiment / Fix Verification
    │          │
    │          └──────────────► Deterministic DiagnosisDecision（唯一正式结论源）
    │
    ├─ Structured Knowledge + Similar Historical Cases(L4)
    │
    └─ Reasoning Gateway
             │
             ▼
       AIProposal v2 (L5)
        ├─ Candidate Hypothesis
        ├─ DiagnosticClaim
        ├─ Contradiction Critic
        ├─ Discriminating Question
        └─ Registered Profile / Experiment Recommendation
             │
             ▼
      Validator + Claim Grounding + Registry Check
             │
             ├─ Shadow / Suggest
             └─ Controlled Planner（必须通过 Promotion Gate）

任何阶段：AI 均不能直接确认根因、不能生成并执行裸设备命令、不能把历史 Case 当当前 Case 证据。
```

## 2. AI-E1 — Real Model Eval

### 2.1 变化

旧 `ai_eval_gate.py` 只证明 19 类场景合同存在，不再被视为模型质量通过。

现在分为：

1. `AI Eval Contract Gate`
   - 19 类场景覆盖；
   - hard-zero 合同；
   - real history requirement；
   - 输出 `promotion_eligible=false`。
2. `AI Model Quality Eval`
   - 输入 `ai-model-eval-dataset-v2`；
   - fixture 或真实 Reasoning Gateway；
   - 对 Ground Truth 逐 Case 评分；
   - 只有 REAL + ROOT_CAUSE_CONFIRMED / FIX_VERIFIED Case 计入 Production Quality。
3. `AI Promotion Gate`
   - Contract PASS；
   - Model Quality PASS；
   - 实际审计覆盖完整；
   - Hard-Zero 全为 0；
   - 才允许 `CONTROLLED_PLANNER`。

### 2.2 指标

- Top-1 Hypothesis Recall
- Top-3 Hypothesis Recall
- Fault Domain Recall
- Evidence Reference Precision
- Unsupported Claim Rate
- Unauthorized Suggestion Rate
- Required Behavior Pass
- Latency
- Cost（数据源提供时）
- Hard-Zero runtime safety metrics

### 2.3 Real Golden 导出

新增 `tools/export_ai_eval_dataset.py`：

- 从 Case DB 导出真实历史 Case；
- 必须存在机器可确认的 CONFIRMED Hypothesis；
- 优先 FIX_VERIFIED；其次 ROOT_CAUSE_CONFIRMED；
- 导出当前 Case Evidence IDs；
- 导出 CaseEvidenceSnapshot；
- 导出最新 deterministic baseline；
- 导出完整 Case AuditLog；
- 不会把 synthetic case 标记为 REAL。

## 3. AI-E2 — Runtime Convergence

正式诊断工厂统一为：

```text
get_diagnosis_reasoner() -> DeterministicDiagnosisReasoner
```

旧 `HybridDiagnosisReasoner` 可保留用于兼容/回归测试，但不再能通过配置成为正式 DiagnosisDecision authority。

AI Runtime 改为能力分阶段：

- `OFF`
- `SHADOW`
- `SUGGEST`
- `CONTROLLED_PLANNER`

能力拆分为：

- HYPOTHESIS
- CRITIC
- PLANNER
- EXPLANATION
- REGISTERED_PLAN_SELECTION

不存在 AI raw-command capability。

## 4. AI-E3 — Claim Graph / Evidence Grounding

新增 `DiagnosticClaim`：

- claim_id
- claim_type: FACT / BOUNDARY / CAUSE / EXCLUSION / OBSERVATION
- subject / predicate / value
- status
- evidence_level
- Evidence Edge
  - evidence_id
  - SUPPORT / CONTRADICT
  - call_id
  - RX / TX / BIDIRECTIONAL
  - time range
- missing_evidence

AI 创建 Claim 时：

- 固定 L5；
- 固定 PROPOSED；
- 禁止自升 SUPPORTED / CONTRADICTED；
- Evidence 必须属于当前 Case；
- 时间 Scope 必须合法；
- 同一 Evidence 不得同时作为同一 Claim 的支持和反证。

### 4.1 First-Mismatch Boundary

增加通用路径边界推理：

```text
Reference     = 123456
PCM_RX        = 123456
AIM_GETNUMBER = 23456
SIP_FORWARD   = 23456

=> L5 Boundary Candidate:
   PCM_RX -> AIM_GETNUMBER
```

它只生成边界候选，不确认根因；正式状态仍需确定性 Evidence Judge / Experiment / Human Confirmation 等现有门禁。

## 5. AI-E4 — VOIP RAG 2.0

历史 Case 从简单 Jaccard 升级为两阶段 Hybrid Retrieval：

### Stage 1 — Coarse Retrieval

- Summary lexical
- Fault Domain
- Symptom
- Version

从最多 300 个已关闭/已确认 Case 中筛到约 30 个候选。

### Stage 2 — Explainable Rerank

- Text
- Confirmed Hypothesis Code
- Fault Domain
- Symptom
- Finding
- Version
- Evidence Type
- Product
- Optional Embedding

输出不再只有 score，还包括：

- same_points
- different_points
- transferability
- algorithm_version

历史 Case 始终为 L4，只能有限提升候选置信度，不能确认当前 Case 根因。

### 5.1 VOIP Diagnostic Ontology

新增结构化知识：

- DTMF 首位丢失
- 电流音/周期噪声
- 单通/无声
- 卡顿/断续
- Echo/Howl
- SIP Register
- Call Setup / Ringback
- FXS Feed/Hook/Ring
- VOIP Adapter/AIM 配置路径
- PCM 采集拓扑约束

知识结构按：

```text
Symptom
 -> Fault Domain
 -> Path
 -> Observable
 -> Discriminating Question
 -> Required Evidence
 -> Expected Finding
 -> Experiment/Profile
 -> Boundary Logic
```

其中明确固定：**系统只 SSH 控制被测 VOIP DUT；Voice Gateway/PBX IP 仅作为 PCM UDP 目的地址/命令参数，不代表系统控制该网关。**

## 6. AI-E5 — Discriminating Investigator

旧 Workbench 会全局选择 information_gain 最大的问题，容易不同故障反复推荐同一问题。

现在 Question Planner 同时计算：

- 当前 Top Hypotheses
- Symptom
- Question Information Gain
- Question Level
- Priority
- 当前已观测 Finding
- Missing Finding
- Cost / Risk

目标变为：**选择最能区分当前竞争假设的问题**。

输出仍然只是注册对象：

- question_key
- profile_id
- experiment_profile_id

不允许输出 shell/AIM/tcpdump 等裸命令。

## 7. AI-E6 — Promotion Gate

生产受控 Planner 不是一个布尔开关即可开启。

### 7.1 Attestation

生产环境要求读取 `ai-promotion-gate-v1` Artifact，并验证：

- status = PASS
- promotion_stage_allowed = CONTROLLED_PLANNER
- formal_reasoner_authority = DETERMINISTIC_ONLY
- raw_device_command_authority = FORBIDDEN
- ai_only_root_cause_confirmation = FORBIDDEN

`AI_PROMOTION_GATE_PASSED=true` 在 production 中不会单独生效。

开发环境若确实需要合同测试，必须同时显式打开 manual override。

### 7.2 Controlled Selection Bridge

AI 可在通过 Gate 后把推荐解析成：

- Registered Diagnostic Question
- Registered Reproduction Profile
- Registered Experiment Profile

桥接层本身不执行设备动作，只生成 registry-backed selection directive；真正执行仍必须进入现有 reproduction / experiment service，由既有 Action Registry、Profile Contract、Cleanup、Lock、Evidence Gate 再次校验。

## 8. Hard-Zero

以下指标不再由代码常量伪造为 0，而从完整 Audit Event 流计算：

- AI_ONLY_ROOT_CAUSE_CONFIRMED
- UNREGISTERED_ACTION_EXECUTED
- CROSS_CASE_EVIDENCE_ACCEPTED
- SECRET_SENT_TO_REASONING_GATEWAY
- WATCHING_ONLY_USER_READY_NOTIFICATION

如果 Audit Coverage 不完整，Eval 最多只能是 `INSUFFICIENT_DATA`，不能 PASS。

## 9. Reasoning Gateway V2

发送前执行递归最小化/脱敏：

- 不上传原始 PCAP / PCM / WAV；
- 不上传 object_key / raw payload；
- 默认不传 DUT IP/SN；
- 递归隐藏 password/token/secret/cookie/authorization；
- IP/MAC/电话号码脱敏；
- Prompt Injection 文本脱敏；
- deterministic baseline 同样脱敏。

Gateway Policy 明确要求：

- `ai-proposal-v2`
- L5 claims only
- non-executable proposal
- root cause confirmation forbidden
- registered IDs only
- raw command forbidden

## 10. 验证命令

### Source / Contract

```bash
make ai-e1-e6-gate
make ai-eval-gate
```

### 导出真实 Golden

```bash
PYTHONPATH=backend:. python tools/export_ai_eval_dataset.py \
  --out validation/ai_eval_field_dataset_v2.json \
  --require-minimum 10
```

### 真实 Reasoning Gateway Eval

```bash
make ai-model-eval \
  AI_EVAL_DATASET=validation/ai_eval_field_dataset_v2.json \
  AI_EVAL_MODE=gateway
```

### Promotion

```bash
make ai-promotion-gate
```

只有最后一步 PASS 后，才能把生产环境配置为：

```text
AI_PROMOTION_STAGE=CONTROLLED_PLANNER
AI_PROMOTION_GATE_ARTIFACT=/app/validation/ai_promotion_gate.json
```

## 11. 当前上线结论

### 已完成

- AI-E1 Eval Framework
- AI-E2 Runtime Convergence
- AI-E3 Claim Graph / Evidence Grounding
- AI-E4 RAG 2.0 / VOIP Ontology
- AI-E5 Discriminating Planner
- AI-E6 Promotion Gate / Attestation
- Gateway Privacy/Safety V2
- Regression Tests / Contract Tests

### 不可伪造的外部验证

代码完成不等于真实模型已获得生产权限。当前仓库无法自行产生以下外部事实：

1. 真实 Reasoning Gateway 的在线质量、延迟、成本；
2. 足量真实历史 `ROOT_CAUSE_CONFIRMED/FIX_VERIFIED` Case；
3. Production 完整 Audit 流中的 Hard-Zero 实测结果。

因此默认：

```text
AI_PROMOTION_STAGE=OFF
AI_SHADOW_ENABLED=false
```

或者在接入真实 Gateway 后先运行 `SHADOW`。

**在真实 Eval + Promotion Gate PASS 之前，系统不会把 AI 提升为生产受控 Planner。**
