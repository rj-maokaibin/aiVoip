# VOIP 初步证据分析报告 SPEC V2.0

Software / System Specification（系统技术规格）

| 字段 | 内容 |
|---|---|
| 文档版本 | V2.0 |
| 文档状态 | Baseline Candidate |
| 文档编号 | SPEC-VOIP-EVIDENCE-002 |
| 日期 | 2026-09-03 |
| 前置基线 | SPEC-VOIP-EVIDENCE-001 / V1.0 Frozen |
| Change Request | CR-VOIP-EVIDENCE-002 |

## 1. 架构不变量

V1.0 的 Evidence-first、Root Cause Authority、DUT-only control、Deterministic Evidence Rendering、Canonical Backend Source、Versioned Contracts、Graceful Degradation 全部继续有效。

V2.0 新增以下不变量：

| 不变量 | 工程要求 |
|---|---|
| Deterministic facts | SIP/RTP/PCM/DTMF/时间/Call state 事实只能来自 Parser/Analyzer/State Machine/Versioned Rule |
| LLM no fact authority | LLM 不得新增、改写或覆盖 canonical fact；只能消费已验证事实 |
| Semantic consistency | Schema 合法但跨字段矛盾视为无效 Report |
| Explicit scope | “完整、双向、结束、丢失”等强语义必须声明 scope/source |
| Event first | 离散异常以 Event 为一等实体，Finding 是 Event 聚合，不允许用 span 替代真实 event |
| Publish fail-closed | P0 Semantic Validator FAIL 时禁止 COMPLETE 主投影 |

## 2. V2 总体架构

```text
Raw PCAP / PCM / Debug / Metadata
  ↓
Deterministic Evidence Extraction
  ├─ SIP Parser
  ├─ RTP Analyzer
  ├─ PCM Analyzer
  ├─ DTMF Analyzer
  └─ Debug Parser
  ↓
Call & Media Reconstruction
  ├─ SIP Call State Machine
  ├─ Call Leg Model
  ├─ Media Stream Binding
  └─ Timeline Model V2
  ↓
Finding Event Engine
  ↓
Finding Aggregator
  ↓
Cross-Layer Correlation Engine
  ↓
Artifact Renderer / Binder
  ↓
Canonical Report V2
  ↓
Semantic Validator
  ├─ FAIL → BLOCK / PARTIAL / INTERNAL ERROR
  └─ PASS
       ↓
AI Interpretation / Recommendation
       ↓
Report Composer
       ↓
Feishu / Web / DOCX / Bundle
```

AI 组件位于 Semantic Validator 后；如果业务需要 AI 先提出候选 hypothesis，则候选只能作为非权威 sidecar，不能进入 canonical facts。

## 3. Pipeline V2

```text
CALL_EVIDENCE_READY / OFFLINE_EVIDENCE_READY
→ EVIDENCE_FINALIZED
→ ANALYZERS
→ CALL_RECONSTRUCTION
→ TIMELINE_RECONSTRUCTION
→ FINDING_EVENTS
→ FINDING_AGGREGATION
→ CROSS_LAYER_CORRELATION
→ KEY_ARTIFACT_RENDER
→ ARTIFACT_BINDING
→ CANONICAL_REPORT_V2
→ SEMANTIC_VALIDATION
→ AI_INTERPRETATION / NEXT_STEP
→ REPORT_COMPOSE
→ PROJECTION
```

`SEMANTIC_VALIDATION` 是 P0 阻断节点，不得为了 SLA 跳过。

## 4. SIP Call State Machine

### 4.1 基本状态

```text
UNKNOWN
  ↓ INVITE
INVITING
  ↓ 1xx
EARLY
  ↓ 2xx
ANSWERED
  ↓ ACK
ESTABLISHED
  ↓ BYE/CANCEL/error/transport-defined terminal
TERMINATED
```

### 4.2 Call termination contract

`termination.observed=true` 只有在存在确定性终止证据时允许：

- BYE（及适用的 response）；
- CANCEL/487 等已定义终止链；
- 明确失败 Final Response；
- 平台已有、版本化且可审计的 terminal event。

以下均**不是** Call End：

- ACK；
- 最后一个 RTP 包；
- capture end；
- Analyzer last timestamp；
- PCM stream end（除非另有确定性 lifecycle contract）。

### 4.3 Schema

```json
{
  "call_state": "ESTABLISHED",
  "invite_time": "...",
  "answer_time": "...",
  "ack_time": "...",
  "established_time": "...",
  "termination": {
    "observed": false,
    "event_type": null,
    "time": null,
    "evidence_refs": []
  },
  "capture_end_time": "..."
}
```

若抓包结束时仍为 ESTABLISHED，则展示 `TERMINATION_NOT_OBSERVED`，不能生成精确 Call End。

## 5. Timeline Model V2

必须区分：

```json
{
  "capture_window": {"start": "...", "end": "...", "source": "pcap"},
  "signaling_window": {"start": "...", "end": null},
  "media_observation_window": {"start": "...", "end": "...", "source": "rtp_observed"},
  "pcm_observation_windows": [],
  "finding_events": []
}
```

### 5.1 Media observation

有可识别 RTP 时：

- `start = min(first_rtp_timestamp)`
- `end = max(last_rtp_timestamp)`

如果多个 leg/stream，必须同时保留 per-stream window 和 aggregate observation window。

### 5.2 相对时间

每个 Event 至少保存：

- `absolute_time`
- `relative_to_invite_ms`
- `relative_to_established_ms`（若 established 可用）
- `relative_to_media_start_ms`（若 media start 可用）

禁止一个 Finding 的 representative relative time 被误用为全部 Event 的相对时间。

## 6. Finding Event Model

```json
{
  "event_id": "EVT-...",
  "type": "PACKET_INTERVAL_SPIKE",
  "layer": "PCM_RX",
  "timestamp": "...",
  "duration_ms": null,
  "metrics": {
    "interval_ms": 31.86,
    "baseline_ms": 10.0
  },
  "scope": {
    "call_id": "...",
    "stream_id": null,
    "direction": "..."
  },
  "evidence_refs": [],
  "artifact_refs": []
}
```

Finding：

```json
{
  "finding_id": "F-001",
  "type": "MEDIA_TIMING_ANOMALY",
  "class": "ABNORMAL",
  "severity": "MEDIUM",
  "event_refs": ["EVT-1", "EVT-2"],
  "event_count": 2,
  "span": {"first_event": "...", "last_event": "..."},
  "continuous": false
}
```

`span` 只是事件覆盖范围，不得渲染为“持续异常时长”，除非 `continuous=true` 有确定性依据。

## 7. Observation Taxonomy V2

P0 至少支持：

- `PACKET_INTERVAL_SPIKE`
- `PACKET_BURST_AFTER_DELAY`
- `RTP_SEQUENCE_LOSS`
- `PACKET_DUPLICATE`
- `PACKET_OUT_OF_ORDER`
- `RTP_TIMESTAMP_DISCONTINUITY`
- `PCM_SAMPLE_LOSS_CONFIRMED`
- `UNEXPECTED_SILENCE`
- `CLIPPING`
- `CLICK_POP`
- `DTMF_SEQUENCE`
- `DTMF_SIP_DIAL_MATCH`
- `ONE_WAY_MEDIA`
- `MEDIA_VISIBILITY_GAP`

命名合同：

- interval/delta 异常不得自动映射为 `*_LOSS`；
- `PCM_SAMPLE_LOSS_CONFIRMED` 必须绑定能证明 sample continuity/source loss 的专用 evidence；
- `DTMF_SIP_DIAL_MATCH` 默认 `class=NORMAL|EXCLUSION`。

## 8. Cross-Layer Correlation Engine

### 8.1 Candidate clustering

初版必须确定性实现：

```text
same call
AND temporal_distance <= profile.correlation_window_ms
AND compatible_media_path
AND compatible_event_family
→ correlation candidate
```

默认窗口建议 50 ms，最终值必须版本化到 Analyzer/Correlation Profile。

### 8.2 Cluster Schema

```json
{
  "cluster_id": "CC-001",
  "type": "CROSS_LAYER_MEDIA_TIMING_SPIKE",
  "representative_time": "...",
  "member_events": [
    {"layer": "PCM_RX", "event_ref": "..."},
    {"layer": "PCM_TX", "event_ref": "..."},
    {"layer": "RTP_UPSTREAM", "event_ref": "..."}
  ],
  "packet_loss_observed": false,
  "interpretation_boundary": "TIMING_CORRELATION_ONLY"
}
```

### 8.3 Problem de-dup

一个 Cluster 内多个 layer observation 默认输出为一个主 Finding，成员层作为 Evidence；仅当规则证明存在独立 failure domain 时才允许拆成多个 abnormal problem。

## 9. Problem / Normal / Exclusion 分类

`finding.class` 固定为：

- `ABNORMAL`
- `NORMAL`
- `EXCLUSION`
- `UNCERTAIN`
- `EVIDENCE_QUALITY`

`problem_count` 只统计 `ABNORMAL` 且满足正式异常等级的 Finding。

INFO 不自动等于 abnormal；Severity 与 Class 独立。

## 10. Evidence Visibility Model

```json
{
  "acquisition": "AVAILABLE|PARTIAL|MISSING",
  "signaling": {
    "caller_leg": "COMPLETE|PARTIAL|MISSING",
    "callee_leg": "COMPLETE|PARTIAL|MISSING"
  },
  "media": {
    "caller_leg": "BIDIRECTIONAL|ONE_WAY|PARTIAL|MISSING",
    "callee_leg": "BIDIRECTIONAL|ONE_WAY|PARTIAL|MISSING",
    "end_to_end": "COMPLETE|PARTIAL|UNKNOWN"
  },
  "termination": "OBSERVED|NOT_OBSERVED",
  "root_cause_readiness": "SUFFICIENT|INSUFFICIENT"
}
```

任何“RTP 双向”文案必须由 scope-qualified 字段渲染，例如“主叫侧媒体双向可见”。

## 11. Report Schema V2

Schema 名称：`preliminary-evidence-report-v2`。

```json
{
  "schema": "preliminary-evidence-report-v2",
  "report_id": "...",
  "scope": {},
  "pipeline_status": "COMPLETE|PARTIAL_COMPLETE|FAILED",
  "call_reconstruction": {},
  "timeline": {},
  "visibility": {},
  "facts": [],
  "events": [],
  "findings": [],
  "correlation_clusters": [],
  "normal_evidence": [],
  "exclusion_evidence": [],
  "artifacts": [],
  "semantic_validation": {},
  "preliminary_assessment": {},
  "recommendations": [],
  "traceability": {},
  "generated_at": "..."
}
```

`pipeline_status=COMPLETE` 仅表示 Pipeline 完成，不等于 visibility/end-to-end/root-cause complete。

## 12. Artifact Binding Contract

### 12.1 P0 Finding clip

对于支持音频复核的 Finding，若 source PCM/RTP 可用：

- 代表 Event 默认生成 `T-1s ~ T+1s` Clip；
- DTMF/Click 等可按 V1 类型窗口覆盖；
- 多 Event 可分别生成 clip；
- Artifact 必须绑定 `event_refs/finding_refs/source_artifact_ids/time_range/sha256`。

如果失败，必须生成结构化：

```json
{
  "artifact_requirement": "AUDIO_CLIP",
  "status": "FAILED",
  "reason_code": "RENDER_ERROR|UNSUPPORTED_CODEC|SOURCE_UNAVAILABLE",
  "source_available": true
}
```

禁止在 source available 时仅输出模糊 `NO_MATCHING_ANOMALY_AUDIO_CLIP` 而无失败原因。

## 13. Dynamic Recommendation Engine

Recommendation 输入只能来自：

- 当前 abnormal Finding/Cluster；
- 当前 normal/exclusion evidence；
- visibility/evidence missing；
- versioned VOIP diagnostic rules/knowledge；
- AI interpretation（可选，非事实）。

输出结构：

```json
{
  "priority": "P0",
  "action": "...",
  "why": "...",
  "collect": ["..."],
  "decision_rule": "...",
  "pass_criteria": "...",
  "source": "RULE|KNOWLEDGE|AI_ASSISTED"
}
```

Recommendation Validator 必须验证引用的 severity/finding/cluster/evidence 确实存在。

## 14. Semantic Validator

### 14.1 P0 rules

| Rule | 条件 | 结果 |
|---|---|---|
| R001 CALL_END_WITHOUT_TERMINATION_EVENT | precise call end 存在但无 terminal evidence | FAIL |
| R002 ZERO_LENGTH_MEDIA_WINDOW_WITH_MEDIA | RTP>1 且 media end<=start | FAIL |
| R003 MEDIA_WINDOW_SOURCE_INVALID | media window 由 ACK 等非媒体事件生成 | FAIL |
| R004 INFO_NORMAL_COUNTED_AS_PROBLEM | normal/info/exclusion 进入 problem_count | FAIL |
| R005 ACTION_REFERENCES_ABSENT_ENTITY | recommendation 引用不存在等级/finding | FAIL |
| R006 AUDIO_SOURCE_AVAILABLE_BUT_UNBOUND | P0 audio finding 有 source 但无 clip/失败记录 | FAIL |
| R007 DISCRETE_EVENTS_RENDERED_CONTINUOUS | 多离散 event 被声明持续异常且无 continuous evidence | FAIL |
| R008 END_TO_END_OVERCLAIM | partial leg visibility 却声明完整 end-to-end | FAIL |
| R009 INTERVAL_SPIKE_AS_LOSS | 无 loss evidence 却输出 packet/sample loss | FAIL |
| R010 ROOT_CAUSE_AUTHORITY_VIOLATION | preliminary/AI 独立确认 root cause | FAIL |
| R011 FINDING_WITHOUT_EVIDENCE | abnormal finding 无 evidence refs | FAIL |
| R012 PROVENANCE_MISSING | 关键 artifact 无 source/analyzer/time/hash | FAIL |
| R013 EVENT_RELATIVE_TIME_INCONSISTENT | relative anchor 计算与 absolute time 不一致 | FAIL |
| R014 RTP_LOSS_CONTRADICTS_SEQUENCE | 声明 sequence loss 但 sequence continuity 证据矛盾 | FAIL |
| R015 DUPLICATE_CLUSTER_PROBLEMS | 同 correlation cluster 被无理由重复计问题 | FAIL |

### 14.2 Publish behavior

- P0 FAIL：不得发布为 user-visible COMPLETE；记录 internal validation failure。
- 可安全降级：只在规则明确支持时生成 PARTIAL_COMPLETE，并隐藏不可信字段、展示 validation boundary。
- Validator 结果必须写 Audit。

## 15. AI Contract V2

AI 输入必须是 `semantic_validation=PASS` 或明确标注安全降级后的 canonical facts。

AI 可输出：

- `interpretation`
- `hypotheses[]`
- `next_experiments[]`
- `summary_language`

AI 不可输出为 authoritative fields：

- call state/time
- packet loss count
- DTMF detected sequence
- problem_count
- evidence visibility
- root cause confirmed state

若 AI 输出与 canonical facts 矛盾，以 canonical facts 为准并记录 rejection。

## 16. Report Composer / UX V2

### 16.1 Executive Summary

固定结构：

1. 当前结论；
2. 用户问题是否复现；
3. 主要异常；
4. 正常/排除证据；
5. 证据边界；
6. 下一步。

### 16.2 Finding Card

每张卡固定：

- What happened
- When
- Evidence
- Interpretation
- What is NOT confirmed
- Next action

图下不重复整段 Finding 免责文案。

### 16.3 Technical Appendix

内部 Enum、Schema、Composer/Profile/Audit、Raw metrics、Frame table、Artifact provenance 放附录。

## 17. Golden Regression #002 Contract

本次真实 PCAP 暴露的行为固化为回归合同。测试数据仓内可使用脱敏/授权后的同一 PCAP 或等价 fixture，但 expected assertions 必须包含：

```yaml
call:
  caller: "601"
  callee: "101"
  termination_observed: false

signaling:
  ack_is_established_event: true
  ack_is_call_end: false

media:
  has_rtp_after_ack: true
  media_window_non_zero: true
  rtp_sequence_loss_observed: false

dtmf:
  pcm_sequence: "101"
  sip_target: "101"
  match: true

finding:
  packet_interval_spike_observed: true
  must_not_claim_pcm_sample_loss: true
  discrete_events_preserved: true
  normal_dtmf_match_not_problem: true

correlation:
  concurrent_pcm_rx_tx_rtp_timing_events_form_candidate: true

report_must_not:
  - precise_call_end_without_bye
  - zero_length_media_window
  - end_to_end_complete_when_visibility_partial
  - recommendation_for_absent_high_or_critical
```

## 18. CI / Release Gate

新增测试层：

1. Parser Unit Tests
2. Call Reconstruction Contract Tests
3. Timeline Contract Tests
4. Finding Event/Taxonomy Tests
5. Correlation Golden Tests
6. Artifact Binding Tests
7. Semantic Validator Contract Tests
8. Report Projection Snapshot/Semantic Tests
9. Golden PCAP E2E
10. Full Software Acceptance

任何改动 Call/Timeline/Finding/Correlation/Composer/Renderer/Recommendation 的 PR 必须触发对应 Golden。

## 19. V1 → V2 兼容

- 历史 `preliminary-evidence-report-v1` 不修改。
- Reader 同时支持 V1/V2。
- V2 rollout：Shadow compose → Dual compare → Canary projection → default V2。
- V1 `time_range` 迁移时不可凭空推导离散 Event；标记 `legacy_projection=true`。
- API endpoint 可保持不变，以 schema/version 区分。

## 20. 实现建议模块边界

最终路径以现有代码为准，职责建议：

- `call_reconstruction`: SIP state/termination/call legs
- `timeline`: observation windows/relative anchors
- `finding_events`: event model/taxonomy
- `correlation`: temporal/media-path clustering
- `semantic_validator`: P0 invariants
- `artifact_binding`: event/finding clip/provenance
- `recommendation`: rule + knowledge + optional AI
- `report_composer_v2`: human-readable projection model

禁止把上述确定性职责重新塞回单个 prompt/composer 模板。
