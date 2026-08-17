# VOIP AI 故障助手 — AI-E1～AI-E6 实施终稿

> 状态：AI-E1～AI-E6 与 Golden Candidate V1 已代码化；生产 AI Promotion 默认阻断  
> 原则：Evidence First / Analyzer First / Deterministic Authority / AI Proposal Only / Registry-Only Planning / Auditable Promotion

## 1. 最终架构

```text
Operational Case
    │
    ├─ PCAP / PCM / Log / FXS / Reproduction / Experiment
    │
    ▼
Deterministic Analyzers / Rules
    │
    ├────────────► Deterministic DiagnosisDecision（唯一正式结论源）
    │
    ├────────────► Golden Candidate Engine
    │                    │
    │                    └─ GOLDEN_READY → Real AI Eval Dataset
    │
    └────────────► Reasoning Gateway
                         │
                         ▼
                    AIProposal v2 (L5)
                     ├─ Hypothesis
                     ├─ DiagnosticClaim
                     ├─ Critic
                     ├─ Discriminating Question
                     └─ Registered Profile/Experiment Recommendation
                         │
                         ▼
                  Validator + Grounding + Registry Check
                         │
                         ├─ SHADOW / SUGGEST
                         └─ CONTROLLED_PLANNER（Promotion Gate 后）
```

任何阶段：AI 不能直接确认根因、不能执行裸设备命令、不能把历史 Case 当成当前 Case 的 L1/L2 Evidence。

## 2. AI-E1 — Real Model Eval

旧 `ai_eval_gate.py` 只证明 Eval 合同/场景覆盖，不再被解释为模型质量通过。

当前分为三道门：

1. **AI Eval Contract Gate**：场景覆盖、Hard-Zero 合同、真实历史要求；
2. **AI Model Quality Eval**：真实/fixture Reasoning Gateway 回放，对 Ground Truth 逐 Case 计算质量指标；
3. **AI Promotion Gate**：Contract PASS + Model Quality PASS + 完整 Audit + Hard-Zero 全 0，才允许 `CONTROLLED_PLANNER`。

主要指标：

- Top-1 / Top-3 Hypothesis Recall；
- Fault Domain Recall；
- Evidence Reference Precision；
- Unsupported Claim Rate；
- Unauthorized Suggestion Rate；
- Required Behavior Pass；
- Latency / Cost；
- Hard-Zero runtime safety metrics。

### 2.1 Case 自动沉淀 / Golden Candidate V1

为了支持没有大量历史 Case 的冷启动场景，AI-E1 上游增加自动 Golden 管理：

```text
NOT_ELIGIBLE
 -> PARTIAL_GOLDEN
 -> GOLDEN_CANDIDATE
 -> GOLDEN_READY
```

`GOLDEN_READY` 的硬门槛：

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

这里“成功 Analyzer”是硬要求，因为 Reasoning Gateway 不直接上传原始 PCAP/PCM/WAV；没有 Analyzer 事实的 Case 不适合作为真实模型质量验收样本。

Verification Tier：

- Tier B = `ROOT_CAUSE_CONFIRMED`；
- Tier A = `FIX_VERIFIED`。

每个 Case 的当前 Golden 状态持久化在 `golden_candidate_assessments`；状态变化产生 `GOLDEN_CANDIDATE_STATE_CHANGED` Audit。业务事务成功提交后自动重算，Golden sidecar 失败不会反向破坏原业务事务。

管理入口：

```text
GET  /api/v1/cases/{case_id}/golden-candidate
POST /api/v1/cases/{case_id}/golden-candidate/refresh
GET  /api/v1/golden-candidates
GET  /api/v1/golden-candidates/summary
POST /api/v1/golden-candidates/backfill?limit=500
```

真实 Eval 默认只导出 `GOLDEN_READY`；被跳过 Case 会返回 status、blocker、gap 和 next_steps。

完整合同与操作说明：

`docs/02_Core_Documents/VOIP_AI_Golden_Candidate_自动沉淀与管理机制.md`

### 2.2 Real Golden 导出

```bash
PYTHONPATH=backend:. python tools/export_ai_eval_dataset.py \
  --out validation/ai_eval_field_dataset_v2.json \
  --require-minimum 10
```

默认质量规则：

```text
GOLDEN_READY
+ REAL
+ CONFIRMED Hypothesis
+ ROOT_CAUSE_CONFIRMED / FIX_VERIFIED
```

## 3. AI-E2 — Runtime Convergence

正式诊断工厂统一由 `DeterministicDiagnosisReasoner` 输出正式 DiagnosisDecision。

AI 能力阶段：

- `OFF`
- `SHADOW`
- `SUGGEST`
- `CONTROLLED_PLANNER`

能力拆分：

- HYPOTHESIS
- CRITIC
- PLANNER
- EXPLANATION
- REGISTERED_PLAN_SELECTION

不存在 AI raw-command capability。

## 4. AI-E3 — Claim Graph / Evidence Grounding

`DiagnosticClaim` 包含：

- claim_id / claim_type；
- subject / predicate / value；
- status / evidence_level；
- Evidence Edge：evidence_id、SUPPORT/CONTRADICT、call_id、direction、time range；
- missing_evidence。

AI 创建 Claim 时固定：

```text
Evidence Level = L5
Status = PROPOSED
```

禁止 AI 自升为 SUPPORTED/CONFIRMED，禁止跨 Case Evidence，禁止同一 Evidence 在同一 Claim 中同时 SUPPORT 和 CONTRADICT。

### First-Mismatch Boundary

```text
Reference     = 123456
PCM_RX        = 123456
AIM_GETNUMBER = 23456
SIP_FORWARD   = 23456

=> L5 Boundary Candidate: PCM_RX -> AIM_GETNUMBER
```

只产生边界候选，不直接确认根因。

## 5. AI-E4 — VOIP RAG 2.0

历史 Case 检索从简单 Jaccard 升级为两阶段 Hybrid Retrieval：

1. Coarse Retrieval：summary lexical / fault domain / symptom / version；
2. Explainable Rerank：text / confirmed hypothesis / fault domain / symptom / finding / version / evidence type / product / optional embedding。

输出：

- same_points；
- different_points；
- transferability；
- algorithm_version。

历史 Case 始终是 L4，不能确认当前 Case 根因。

结构化 VOIP Diagnostic Ontology 覆盖：DTMF、电流音/周期噪声、单通/无声、卡顿、Echo/Howl、SIP Register、Call Setup/Ringback、FXS Feed/Hook/Ring、AIM/Adapter、PCM 采集拓扑。

拓扑约束固定为：**系统只 SSH 控制被测 VOIP DUT；Voice Gateway/PBX IP 只是 PCM UDP 目的地址/命令参数，不代表系统控制语音网关。**

## 6. AI-E5 — Discriminating Investigator

Question Planner 不再全局挑 information gain 最大的问题，而是结合：

- 当前 Top Hypotheses；
- Symptom；
- Information Gain；
- Priority / Level；
- 已有 Finding / Missing Finding；
- Cost / Risk。

目标是选择最能区分当前竞争假设的下一问题/实验。

AI 只能推荐已经注册的 `question_key / reproduction_profile_id / experiment_profile_id`，不能输出裸 shell/AIM/tcpdump 进入执行链。

## 7. AI-E6 — Promotion Gate

Production `CONTROLLED_PLANNER` 不能靠布尔变量打开。

必须读取并验证 `ai-promotion-gate-v1` Artifact：

```text
status = PASS
promotion_stage_allowed = CONTROLLED_PLANNER
formal_reasoner_authority = DETERMINISTIC_ONLY
raw_device_command_authority = FORBIDDEN
ai_only_root_cause_confirmation = FORBIDDEN
```

Controlled Selection Bridge 只把 AI 推荐解析为 Registry 中已经存在的 Question/Profile/Experiment；真正执行继续经过 reproduction / experiment service、Action Registry、Profile Contract、Cleanup、Lock、Evidence Gate。

## 8. Hard-Zero

运行时从 Audit Event 实际计算：

- AI_ONLY_ROOT_CAUSE_CONFIRMED
- UNREGISTERED_ACTION_EXECUTED
- CROSS_CASE_EVIDENCE_ACCEPTED
- SECRET_SENT_TO_REASONING_GATEWAY
- WATCHING_ONLY_USER_READY_NOTIFICATION

Audit Coverage 不完整时最多 `INSUFFICIENT_DATA`，不能 PASS。

## 9. Reasoning Gateway V2

发送前递归最小化/脱敏：

- 不上传原始 PCAP / PCM / WAV；
- 不上传 object_key / raw payload；
- 默认不传 DUT IP/SN；
- 隐藏 password/token/secret/cookie/authorization；
- IP/MAC/电话号码脱敏；
- Prompt Injection 文本脱敏；
- deterministic baseline 同样脱敏。

Gateway Policy：`ai-proposal-v2`、L5 claims only、non-executable proposal、root-cause confirmation forbidden、registered IDs only、raw command forbidden。

## 10. 验证命令

### 工程/合同

```bash
make ai-eval-gate
make ai-e1-e6-gate
```

`ai-e1-e6-gate` 已包含 Golden Candidate 状态机与自动事务监听专项测试。

### Golden 状态与历史 Backfill

```text
GET  /api/v1/golden-candidates/summary
GET  /api/v1/golden-candidates?status=GOLDEN_READY
POST /api/v1/golden-candidates/backfill?limit=500
```

### 真实模型 Eval

```bash
make ai-export-real-eval

make ai-model-eval \
  AI_EVAL_DATASET=validation/ai_eval_field_dataset_v2.json \
  AI_EVAL_MODE=gateway
```

### Promotion

```bash
make ai-promotion-gate
```

只有 Promotion PASS 后才允许生产配置：

```text
AI_PROMOTION_STAGE=CONTROLLED_PLANNER
AI_PROMOTION_GATE_ARTIFACT=/app/validation/ai_promotion_gate.json
```

## 11. 当前交付状态

已实现：

- AI-E1 Real Model Eval Framework；
- Golden Candidate V1 自动沉淀、持久化、Backfill、管理 API、答案泄漏防护、Eval Gate；
- AI-E2 Runtime Convergence；
- AI-E3 Claim Graph / Evidence Grounding；
- AI-E4 RAG 2.0 / VOIP Ontology；
- AI-E5 Discriminating Planner；
- AI-E6 Promotion Gate / Attestation；
- Reasoning Gateway Privacy/Safety V2；
- CI / Migration / Regression Contracts。

仍然不可伪造的外部事实：

1. 足量真实 `GOLDEN_READY` Case；
2. 真实 Reasoning Gateway 的模型质量/时延/成本；
3. Production 完整 Audit 流中的 Hard-Zero 实测结果。

因此默认继续保持：

```text
AI_PROMOTION_STAGE=OFF
AI_SHADOW_ENABLED=false
```

或接入真实 Gateway 后先运行 SHADOW。只有足量 Golden Ready + Real Eval + Promotion Gate PASS 后才晋级受控 Planner。

## 12. 下一阶段：M7 真实 DUT 智能诊断闭环验收

AI-E1～E6 + Golden Candidate 已作为 V1.0 RC 基线收口后，下一阶段不再以继续扩功能为主，而是验证正常系统在真实 DUT 上能否完整跑通：

```text
Case
→ DUT / Voice Context
→ PCAP + PCM RX/TX + Debug
→ Analyzer
→ Deterministic Diagnosis
→ Reasoning Gateway / AI SHADOW
→ Auto Reproduction / Call Detection
→ Cleanup
→ Diagnosis Report
→ Golden Candidate Auto Materialization
```

正式只读验收入口：

```bash
make m7-acceptance-report M7_CASE=<case_no或case_id>
make m7-acceptance M7_CASE=<case_no或case_id>
```

M7 共 20 个必选闭环项。M7 PASS 仅表示该真实 DUT Case 的系统闭环完整，**不等于 ROOT_CAUSE_CONFIRMED、GOLDEN_READY 或 AI Promotion PASS**。

完整 M7 合同、实验室场景与操作方式见：

`docs/02_Core_Documents/VOIP_AI_M7_真实DUT智能诊断闭环验收.md`
