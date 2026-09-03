# VOIP 初步证据分析报告 PRD V2.0

Product Requirements Document（产品需求文档）

| 字段 | 内容 |
|---|---|
| 文档版本 | V2.0 |
| 文档状态 | Baseline Candidate |
| 项目 | VOIP AI 故障助手 |
| 文档编号 | PRD-VOIP-EVIDENCE-002 |
| 日期 | 2026-09-03 |
| 前置基线 | PRD-VOIP-EVIDENCE-001 / V1.0 Frozen |
| Change Request | CR-VOIP-EVIDENCE-002 |

## 1. 产品定义

V2.0 保留 V1.0“回答抓到了什么、哪里异常、证据是什么、下一步验证什么”的定位，并新增一个同等重要的产品目标：**报告本身必须通过语义正确性验证，不能只做到字段完整。**

V2.0 把 Preliminary Evidence Report 定义为一个受约束的“证据编译产物”：

`Raw Evidence → Deterministic Facts → Call/Media Reconstruction → Finding Events → Correlation → Semantic Validation → AI Interpretation → Human-readable Projection`

其中事实、时间、状态、问题数量、证据可见性和 Artifact 关系均由确定性逻辑拥有；LLM 不拥有事实写入权，也不能覆盖 Semantic Validator。

## 2. V2.0 要解决的问题

V1.0 已覆盖 Evidence-first、Finding、Artifact、Cross-Layer、Feishu、Bundle、Root Cause Authority 等基础能力，但真实报告复核证明仍存在以下产品缺口：

1. Report Schema 合法不代表 Call Timeline 合理。
2. 单一 `COMPLETE` 容易被用户理解为端到端证据完整。
3. Finding `time_range` 难以正确表达多个离散 event。
4. Cross-Layer 能力存在“字段可用但未形成高价值关联”的情况。
5. INFO/正常项与“问题点”数量混淆。
6. Packet timing abnormal 与 packet/sample loss 的术语边界不够严格。
7. 下一步建议可能来自静态模板，而非当前 Case 的真实 Finding/Severity/缺失证据。
8. Artifact 已有源数据时仍可能缺失对应 Clip。
9. 主报告信息密度偏低，内部实现字段过多，影响现场和研发快速阅读。

## 3. 产品目标

V2.0 必须实现以下目标：

- **G1 事实正确**：Call/Media/Packet/PCM/DTMF 等事实必须可确定性复算。
- **G2 时间正确**：任意时间字段必须明确语义、来源和相对锚点。
- **G3 关联正确**：同 Call、同方向/媒体路径、同时间的相关异常自动形成 Correlation Cluster。
- **G4 边界正确**：Observation、Interpretation、Candidate Cause、Root Cause 分层；前一层不能越权升级后一层。
- **G5 建议可执行**：MEDIUM+ Finding 必须给出针对当前证据的下一步动作、采集项、判定方式和通过标准。
- **G6 30 秒可读**：第一页能回答“是否复现、主要异常、已排除、证据缺口、下一步”。
- **G7 错误不可发布**：P0 Semantic Validator 失败时不得发布 COMPLETE 主报告。
- **G8 可回归**：每种已暴露错误必须转成固定 Golden/Contract Gate。

## 4. 非目标

V2.0 不改变以下边界：

- 不允许 Preliminary Report 或 AI 独立确认 Root Cause。
- 不用强模型替代 Parser、State Machine、Threshold、Validator。
- 不允许 AI 自由修改原始 Evidence、Analyzer Result 或 Artifact Provenance。
- 不把 packet interval spike 自动解释为 packet/sample loss。
- 不因可读性优化隐藏关键证据边界或审计信息；仅允许把深度字段移动到附录/下钻。
- 不取消 V1 历史报告可读性。

## 5. 用户阅读目标

### 5.1 技服/现场支持

30 秒内知道：

1. 用户投诉在本次证据中是否复现；
2. 最重要的异常是什么；
3. 哪些典型原因已经被当前证据排除；
4. 还缺什么证据；
5. 下一步需要执行什么。

### 5.2 VOIP 研发

2~5 分钟内能够：

- 查看准确 Call/Media 时间线；
- 找到关键 Frame/Seq/PCM Event；
- 看跨层相关事件是否同时发生；
- 试听代表异常 Clip；
- 下钻 Raw Artifact/Analyzer JSON；
- 复现报告中的每个关键数字。

## 6. V2.0 核心产品概念

### 6.1 Fact

由 Parser/Analyzer/State Machine 直接产生的可复核事实，例如 INVITE 时间、ACK 时间、RTP sequence、PCM packet interval、DTMF sequence。

### 6.2 Finding Event

一个离散的可观察事件。每个 Event 必须拥有自己的绝对时间、相对时间、指标和 Evidence Ref。

### 6.3 Finding

由一个或多个同类 Event 聚合形成的异常/观察实体，不再用一个 `start~end` 伪装多个离散 event 为持续异常。

### 6.4 Correlation Cluster

跨层事件在同一 Call/媒体路径和时间窗口内的确定性聚类。Cluster 是解释和下一步实验的主要输入。

### 6.5 Evidence Visibility

分别表达：

- Evidence Acquisition
- Signaling Visibility
- Caller Leg Media Visibility
- Callee Leg Media Visibility
- End-to-End Visibility
- Call Termination Visibility
- Root Cause Readiness

## 7. 功能需求

| ID | 能力 | V2.0 产品要求 | 优先级 |
|---|---|---|---|
| FR2-001 | SIP Call State | ACK=ESTABLISHED；只有 BYE/CANCEL/失败等终止事件才能产生 observed termination；抓包结束不等于 Call End | P0 |
| FR2-002 | Timeline Model | Capture/Signaling/Media/Finding Event/Finding Span 独立建模，不混用 | P0 |
| FR2-003 | Media Window | 有 RTP 时 Media Observation Window 必须来自 first/last observed media packet，不得由 ACK 代替 | P0 |
| FR2-004 | Finding Event | 一个 Finding 可包含多个离散 Event，每个 Event 独立时间绑定 | P0 |
| FR2-005 | Observation Taxonomy | 至少区分 PACKET_INTERVAL_SPIKE、SEQUENCE_LOSS、SAMPLE_LOSS、BURST_AFTER_DELAY、SILENCE、CLIPPING 等 | P0 |
| FR2-006 | Loss Safety | 没有 sequence/sample 证据不得把 interval/delta spike 命名为 packet/sample loss | P0 |
| FR2-007 | Cross-Layer Cluster | PCM RX/TX、RTP、Debug 等同 Call 同时间相关 event 必须形成 candidate cluster | P0 |
| FR2-008 | Finding De-dup | 同一 cluster 下的层级 observation 默认不重复计入 problem_count，除非有独立故障语义 | P0 |
| FR2-009 | Problem Count | NORMAL/INFO/EXCLUSION 不计入异常问题数量 | P0 |
| FR2-010 | Visibility | “双向/完整”声明必须带 scope；caller leg 完整不得冒充 end-to-end 完整 | P0 |
| FR2-011 | Completeness Dimensions | `COMPLETE` 仅表示 report pipeline 完成，不得独立表达 Evidence/Call/Media/RC 完整 | P0 |
| FR2-012 | Audio Binding | 可生成代表 Clip 的 PCM/RTP source 存在时，Finding 必须绑定 Clip 或显式 artifact failure | P0 |
| FR2-013 | Dynamic Recommendation | 建议必须消费实际 Finding、Severity、Evidence Missing、Correlation；不得引用不存在的等级/对象 | P0 |
| FR2-014 | Semantic Validator | Canonical Report 发布前执行跨字段 invariant；P0 failure 阻断 COMPLETE 投影 | P0 |
| FR2-015 | Executive Summary | 第一页固定回答：是否复现、主要异常、正常/排除证据、边界、下一步 | P0 |
| FR2-016 | Finding Card | 每个异常只呈现一次，包含 What/When/Evidence/Interpretation/Not Confirmed/Next | P0 |
| FR2-017 | Technical Appendix | Schema/Composer/Profile/Audit/Raw metrics 放附录，不占主阅读路径 | P1 |
| FR2-018 | Visual Annotation | 图表优先标当前 event、跨层对齐、关键数值，减少通用“图怎么看”模板 | P1 |
| FR2-019 | Golden Regression | 已发生过的语义错误全部固化为 Golden/Contract 断言 | P0 |
| FR2-020 | V1 Compatibility | 历史 V1 报告继续可读；V2 不原地重写历史报告 | P0 |
| FR2-021 | AI Role | AI 只可生成 Interpretation/Hypothesis/Experiment/Language，不可写入/覆盖 Fact | P0 |
| FR2-022 | Knowledge-driven Next Step | 下一步建议可检索 VOIP 知识库/规则，但必须标明“建议”而非当前证据事实 | P1 |

V1.0 中未被本表修改的 FR-001~FR-030 继续有效；V2.0 为增量加强而非范围删减。

## 8. 报告信息架构 V2

### 8.1 第一层：Executive Summary

建议控制在一页：

1. 一句话结论；
2. 用户问题是否复现；
3. 主要异常 Finding/Cluster；
4. 正常/排除性证据；
5. Evidence Boundary；
6. 下一步验证。

### 8.2 第二层：Finding Cards

每个异常实体一张卡，禁止对同一观察在 Waveform/Spectrogram 下重复写相同 Finding 文案。

### 8.3 第三层：Technical Appendix

SIP Flow、RTP Stream Detail、PCM Event Table、Waveform、Spectrum、Spectrogram、Raw metrics、Artifact Provenance、Schema/Composer/Audit。

## 9. 文案规范 V2

必须使用保守、证据匹配的术语：

- `packet interval spike` → “数据包到达/发送节奏短时异常”
- `sequence loss` → “RTP/packet sequence 丢失”
- `PCM sample loss` 只有存在 sample continuity/driver/source 证据时才允许使用
- `termination not observed` → “当前抓包未观察到挂断/终止事件”
- `caller leg bidirectional` → “主叫侧媒体双向可见”
- 禁止将 “当前未发现” 写成 “证明不存在”

## 10. 产品成功指标

| 指标 | V2.0 门槛 |
|---|---|
| P0 Semantic Validator False Negative | 0 个已知 Golden 语义错误漏过 |
| Call state/timeline Golden accuracy | 100% |
| Loss semantic safety | interval spike → loss 误判 = 0 |
| Problem count correctness | NORMAL/INFO/EXCLUSION 误计 = 0 |
| Scope overclaim | caller-leg → end-to-end 错误强声明 = 0 |
| Recommendation consistency | 引用不存在 severity/finding = 0 |
| Artifact binding | 可生成代表 Clip 的 P0 Finding 绑定率 = 100%，除非有明确生成失败记录 |
| 主摘要可读性 | 人工评审 30 秒内可回答 5 个核心问题 |
| Root Cause Authority | 0 回归 |
| 原有 P0 Finding Precision/Recall | 不低于 V1.0 门槛 |

## 11. V2.0 Strict Release Gate

V2.0 不得仅以“测试全绿/报告生成成功”作为发布标准。必须同时满足：

1. PRD/SPEC/Traceability P0 100% 实现。
2. Semantic Validator P0 规则 100% 纳入 CI。
3. Golden Regression #002 通过。
4. 至少 3 类 PCAP/Call lifecycle 真实或实验室回归通过。
5. V1 历史报告兼容读取通过。
6. V1 Root Cause Authority、Evidence Provenance、Bundle、Audit、Feishu 投影无回归。
7. 全量 backend regression 与项目 Full Acceptance 通过。
8. 无 P0/P1 correctness 缺陷。

## 12. 基线规则

V2.0 冻结后，以下变化必须再次走 Change Request：

- SIP Call state/termination 语义；
- Timeline source/anchor；
- Problem Count 定义；
- Observation Taxonomy；
- Cross-Layer Cluster 关键算法；
- Semantic Validator P0 规则；
- Root Cause Authority；
- `preliminary-evidence-report-v2` 不兼容 Schema 变化；
- 主报告信息架构中第一页必须回答的 5 个核心问题。
