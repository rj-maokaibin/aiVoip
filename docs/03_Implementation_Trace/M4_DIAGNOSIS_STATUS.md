# M4 AI Diagnosis Orchestrator — Alpha

## 已实现

- `DiagnosisRun`：记录每次自主诊断的周期、Reasoner/Workflow版本、Evidence fingerprint、no-progress计数和最终决策。
- `Hypothesis`：PROPOSED / ACTIVE / SUPPORTED / WEAKENED / REJECTED / CONFIRMED / UNRESOLVED。
- `HypothesisEvidence`：L1~L5证据等级、Support/Contradict/Context方向、权重和引用对象。
- `CollectionPlan`：高层补采计划，不允许直接保存任意Shell。
- `diagnosis-worker`：独立Celery队列。
- 事件式恢复：Collector / Packet / PCM / Media Worker完成后只投递 `diagnosis.resume_case`，不会在子Worker里直接执行Reasoner。
- 无进展保护：Evidence/Analyzer fingerprint连续不变达到阈值后停止自主补采，进入 WAITING_USER。
- 最大循环保护：默认6轮。
- L0/L1自动动作：`RUN_MEDIA_ANALYSIS`、`RUN_PACKET_ANALYSIS`、`RUN_PCM_ANALYSIS`、`COLLECT_PROFILE(voip_basic)`。
- 人工动作：`REQUEST_USER_EVIDENCE`、`REQUEST_MULTI_POINT_PCAP`。
- 根因确认：仅Human API可将 confirmable Hypothesis 转为 CONFIRMED，且必须存在L1直接证据且无L1/L2反证。

## Reasoning安全边界

默认 `DIAGNOSIS_REASONER=deterministic`。可切换 `hybrid` 接公司内部 Reasoning Gateway。

Gateway只接收结构化摘要，不发送原始 PCAP/PCM/WAV。LLM新增Hypothesis：

- 强制为 L5 AI_INFERENCE；
- confidence最高0.75；
- 不允许 `CONFIRMED`；
- 不允许绕过Human root-cause confirmation；
- LLM建议动作必须经过 `DiagnosisPlanPolicy`，未知动作（例如 RUN_SHELL）直接丢弃；
- 自动 `COLLECT_PROFILE` 当前只允许 `voip_basic`。

## 第一条自主闭环

```text
POST /cases/{case_id}/diagnosis/start
  ↓
Diagnosis Worker / Cycle 1
  ↓
Evidence Snapshot
  ↓
Hypothesis + Evidence Gap
  ↓
PCAP exists but Media Analyzer missing
  ↓
RUN_MEDIA_ANALYSIS [L0 AUTO]
  ↓
Media Worker
  ↓
AnalyzerRun + structured Evidence
  ↓ notify
Diagnosis Worker / Cycle 2
  ↓
Update Hypothesis
  ├─ enough deterministic evidence → DIAGNOSED
  ├─ need more L0/L1 evidence → auto dispatch
  └─ need human/multi-point capture → WAITING_USER
```

## 真实样本基线行为

对项目真实PCM/RTP样本，当前Reasoner明确区分：

- RTP Sequence未见丢包；
- 存在 HIGH_DELTA/HIGH_JITTER 候选；
- 若Case症状为“电流音/杂音”，RTP抖动不会被直接判成根因，只保留 ACTIVE 候选；
- PCM↔RTP高相关作为链路边界证据（WEAKENED/Context），不抢占根因排序；
- Click/Pop/Silence当前属于启发式候选，仅L3，未和用户异常时间对齐前不允许作为确认根因。

详见 `docs/examples/diagnosis_real_sample.json`。
