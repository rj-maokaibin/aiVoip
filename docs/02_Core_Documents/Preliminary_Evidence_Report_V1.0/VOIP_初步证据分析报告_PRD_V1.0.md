**VOIP 初步证据分析报告**

Product Requirements Document（产品需求文档）

| 文档版本 | V1.0                                      |
|----------|-------------------------------------------|
| 文档状态 | Baseline Frozen（基线冻结）               |
| 项目     | VOIP AI 故障助手                          |
| 文档编号 | PRD-VOIP-EVIDENCE-001                     |
| 冻结日期 | 2026-08-18                                |
| 变更规则 | 冻结后通过 Change Request（变更请求）管理 |

适用范围：VOIP 复现采集、PCAP/PCM/Media 分析、Evidence
Finding、Call/Session/Case 初步证据报告、飞书文档呈现与 Evidence
Bundle。

# 1. 文档目的与产品定义

本 PRD 定义 VOIP AI 故障助手的“初步证据分析报告”V1.0。该能力在一次有效
Call（通话）或 Reproduction Session（复现会话）结束后，自动汇总 PCAP
抓包、PCM RX/TX、RTP、SIP/SDP、音频特征、Debug
等证据，形成可追溯、可查看、可听、可下载的初步证据报告。

| **核心定位：**它回答“抓到了什么、哪里异常、证据是什么、下一步应验证什么”，不直接替代最终 Diagnosis Report（诊断报告），也不拥有 Root Cause（根因）确认权限。 |
|--------------------------------------------------------------------------------------------------------------------------------------------------------------|

V1.0 的主查看入口为飞书文档；飞书机器人负责通知和摘要；VOIP AI Web
负责研发级下钻；后端 Canonical Report JSON（权威结构化报告）与 Artifact
Store（证据存储）是唯一权威数据源。

# 2. 背景与问题

- 现场复现后证据散落在 PCAP、PCM、WAV、Debug、Analyzer JSON
  中，技服和研发需要人工拼接。

- 现有 Diagnosis Report
  更偏最终诊断结论，不适合作为复现后“第一时间证据简报”。

- 电流音、静音、爆音、DTMF、单通、RTP
  丢包/抖动等问题需要图、音频、Frame、跨层方向一起呈现，仅文字结论说服力不足。

- 多次复现、A/B 环境对比时，缺少统一的 Call/Session/Case
  聚合与复现率表达。

- AI 解释必须服从 Evidence-first（证据优先）与 Root Cause
  Authority（根因权限）边界，不能用历史案例或模型推断替代当前 Case
  的直接证据。

# 3. 产品目标

**1.** 每次有效 Call 分析完成后自动生成 Call 级 Evidence
Brief（证据简报），Session 结束后自动生成 Session 汇总，Case
维度持续形成聚合视图。

**2.** 将 SIP/SDP、RTP、PCM、音频、Debug 的关键异常统一为 Evidence
Finding（证据问题点），每个 Finding 均可追溯到指标、时间、来源、Evidence
与 Artifact。

**3.** 自动生成波形图、频谱图、时频图、RTP 时间线、SIP
呼叫流程等稳定可复核证据图，并提供问题音频 Clip。

**4.**
通过飞书文档实现“结论优先、异常优先、最新优先、证据下钻”的统一阅读体验。

**5.** 对多 Call、多 Session、不同环境进行确定性聚合与 A/B
对比，帮助识别稳定复现、偶发异常和异常首次可观测层。

**6.**
保证内部证据原样可查、报告可版本化、证据可离线打包、关键操作可审计、规则和
Schema 可追溯。

# 4. 非目标（Non-Goals）

- 不允许 AI 直接确认 Root Cause（根因）。

- 不允许 AI 生成、修改或伪造原始 Evidence（证据）。

- 不通过报告自动修改设备配置或执行高风险修复。

- 不测量真实声压级 dB SPL；数字 PCM 只报告 dBFS。

- 不承诺通用主观语音质量 MOS 的绝对准确评级。

- 不建设面向客户的通用外部分享门户。

- 不建设高级音频编辑/剪辑工作站。

- 不演化为任意 PCAP 的通用网络取证平台。

- 不允许历史相似案例单独提升当前 Case 的 Evidence Level 或确认根因。

# 5. 目标用户与角色

| **角色**      | **核心诉求**                                                             | **默认使用入口**            |
|---------------|--------------------------------------------------------------------------|-----------------------------|
| 技服/现场支持 | 复现后快速知道是否抓到问题、问题在哪一层、可直接听/看佐证                | 飞书机器人 → 飞书文档       |
| VOIP 研发     | 下钻 Frame、RTP Stream、PCM Session、Analyzer 指标、统一时间线和原始证据 | 飞书文档 → VOIP AI Web      |
| 测试/质量     | 多次复现统计、A/B 差异、回归结果、Golden Dataset 验收                    | Case 汇总 / Evidence Bundle |
| 架构/平台     | 规则版本、Schema、审计、发布门禁、异常边界准确率                         | 管理与审计视图              |

# 6. 核心概念与产品层级

Case（故障案例）  
└─ Reproduction Session（复现会话）  
├─ Call 1 → Call Evidence Brief（单次通话证据简报）  
├─ Call 2 → Call Evidence Brief  
└─ ...  
↓  
Session Evidence Summary（会话级证据汇总）  
↓  
Case Evidence Summary（案例级证据汇总）

Call / Session / Case 三层报告均作为正式、可追溯实体保存。Call
之间按最新优先展示，单个 Call 内 Evidence 按时间正序展示；Finding
按严重等级、用户现象相关度、证据等级、跨层一致性、时间排序。

# 7. 全局术语与文案规范

凡面向用户展示的专业英文术语或缩写，首次出现必须采用“中文名称 +
英文全称/缩写 +
简短解释”的形式，后续可使用缩写。不得在首屏、飞书卡片或报告正文大量裸露英文缩写。

| **推荐呈现**                     | **解释**                                            |
|----------------------------------|-----------------------------------------------------|
| PCM 接收音频（PCM RX）           | 被测设备视角的接收方向 PCM 音频。                   |
| PCM 发送音频（PCM TX）           | 被测设备视角的发送方向 PCM 音频。                   |
| RTP 下行媒体流（RTP Downstream） | 网络/语音网关 → 被测设备。                          |
| RTP 上行媒体流（RTP Upstream）   | 被测设备 → 网络/语音网关。                          |
| P95 抖动（Jitter P95）           | 95% 的 RTP 数据包抖动不超过该值。                   |
| 峰值数字电平（Peak dBFS）        | 相对于数字满量程的最高数字音频电平，不等于 dB SPL。 |

# 8. 端到端业务流程

自动模式：  
AI/规则判定需要复现  
→ 自动 SSH 控制被测 VOIP 设备进入 ARMED/WATCHING  
→ 预启 PCAP + PCM RX + PCM TX + 必要 Debug  
→ Off-hook/DTMF/SIP INVITE/RTP 等事件识别复现开始  
→ Call End  
→ Tail Drain（尾部数据排空）  
→ Evidence Finalize（证据固化）  
→ Packet / PCM / Media / Cross-Layer Analyzer  
→ Finding Composer  
→ Artifact Renderer  
→ Preliminary Evidence Report  
→ 飞书文档更新 + 飞书机器人通知  
→ 继续 Deterministic Diagnosis / AI SHADOW / Root Cause Confirmation  
→ Cleanup  
  
离线模式：  
已有 PCAP / PCM / Debug  
→ Analyzer  
→ Finding / Artifact  
→ Preliminary Evidence Report

| **采集拓扑约束：**系统 SSH 控制的设备仅为被测 VOIP 设备。`voip dsp diag set <gateway_ip> 40000/50000 ...` 中 gateway_ip 仅是 PCM diag UDP 目的地址/命令参数，不代表系统需要 SSH 或控制语音网关/PBX。 |
|----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

# 9. 功能需求

| **ID** | **能力**           | **产品要求**                                                                                                        | **优先级** |
|--------|--------------------|---------------------------------------------------------------------------------------------------------------------|------------|
| FR-001 | 两级即时报告       | 每个有效 Call Analyzer 完成后自动生成 Call 简报；Reproduction Session 结束后自动生成 Session 汇总；Case 持续聚合。  | P0         |
| FR-002 | 正常 Call 报告     | 未发现明显异常时仍生成“未发现明显异常”报告，作为负样本和排除性证据。                                                | P0         |
| FR-003 | 版本更新           | Analyzer 生成 V1；Diagnosis/AI 或 Analyzer Rerun 发生实质变化时生成 V2+，历史版本完整保留。                         | P0         |
| FR-004 | Finding 分类       | 至少覆盖信令层、网络媒体层、PCM/DSP 媒体层、设备语音路径层、证据完整性。                                            | P0         |
| FR-005 | Finding 状态与等级 | 状态与 Severity 分离；Severity 为 INFO/MEDIUM/HIGH/CRITICAL。                                                       | P0         |
| FR-006 | 证据要求           | 每个 Finding 至少包含指标、时间、来源、Evidence；图/音频按问题类型自动补充。                                        | P0         |
| FR-007 | Packet 证据        | 支持总 Frame、SIP/Call/RTP Stream、方向、丢包/突发丢包/抖动/Delta/Codec/ptime，并可下钻异常 Frame。                 | P0         |
| FR-008 | SIP 流程           | 自动生成 SIP 呼叫流程（Call Flow/Ladder）并在异常处关联 Frame 和关键 Header。                                       | P0         |
| FR-009 | 音频证据           | 保留完整 WAV 与异常 Clip；异常 Clip 按类型动态选窗，默认前 1s + 异常 + 后 1s。                                      | P0         |
| FR-010 | 可视化证据         | 按问题类型确定性生成 Waveform、Spectrum、Spectrogram、RTP Timeline、SIP Call Flow 等 PNG。                          | P0         |
| FR-011 | 数字电平           | 正式指标使用 RMS dBFS 与 Peak dBFS；不得将数字 PCM 解释为真实 dB SPL。                                              | P0         |
| FR-012 | 跨层关联           | RTP/PCM/Debug 统一时间轴，确定性判断异常首次可观测层；证据不足必须输出 UNKNOWN。                                    | P0         |
| FR-013 | 排除性证据         | 报告展示关键正常项与未发现异常项，支持边界收敛。                                                                    | P0         |
| FR-014 | 证据完整度         | 必须显示 PCAP/SIP/RTP/PCM RX/PCM TX/Debug/Correlation 等完整度与缺失项。                                            | P0         |
| FR-015 | 部分完成           | Analyzer 失败/超时/证据缺失时仍可生成 PARTIAL_COMPLETE 报告，并声明不可用能力和结论边界。                           | P0         |
| FR-016 | 跨 Call 聚合       | Session 对同类 Finding 聚合并计算出现次数、复现率；底层 Call Finding 保留。                                         | P0         |
| FR-017 | 环境分组           | Case 聚合前按 Environment Fingerprint 分组，不同环境禁止直接混合统计。                                              | P0         |
| FR-018 | A/B 对比           | 支持环境 A/B 比较复现率、网络媒体、PCM、电平、频谱、Finding 与 Evidence Boundary。                                  | P0         |
| FR-019 | 正常基线           | 有匹配基线时自动比较；不满足最低匹配条件时不强行使用。                                                              | P0         |
| FR-020 | Evidence Bundle    | 一键下载标准证据包，包含报告、原始 PCAP、音频、图片、Analyzer JSON、Debug、Manifest、SHA256。                       | P0         |
| FR-021 | 飞书文档主入口     | 每个 Case 维护一份飞书初步证据分析文档作为主查看入口，按 Call/Session 持续更新。                                    | P0         |
| FR-022 | 飞书机器人         | Call 完成发简短结果卡；Session 完成发汇总卡；优先更新同一消息，避免刷屏。                                           | P0         |
| FR-023 | Web 下钻           | Case 页面支持 Finding 卡片、图片预览、异常音频播放、Frame/指标展开、完整报告与 Bundle 下载。                        | P0         |
| FR-024 | 内部原样显示       | 内部系统/报告/内部 Bundle 的 IP、电话号码、SIP URI 等默认原样显示。                                                 | P0         |
| FR-025 | 外部模型安全边界   | 发送给外部 Reasoning Gateway/LLM 的数据仍遵循既有脱敏与安全合同，不因内部原样展示而放宽。                           | P0         |
| FR-026 | 保留策略           | 普通 Case 原始 PCAP/完整 WAV 默认 90 天；关键 Clip、结构化结果、图片、报告长期；Golden/人工锁定可长期保留原始证据。 | P0         |
| FR-027 | 权限与审计         | 报告和 Evidence 继承 Case 权限并预留细分权限；报告生成/重建/版本/Bundle 下载/规则变化均审计。                       | P0         |
| FR-028 | 无有效 Call        | Session 未形成有效 Call 时生成“未形成有效 Call”证据报告，保留 Off-hook/PCAP 预录/Debug 等。                         | P0         |
| FR-029 | 中断 Call          | ABORTED/INCOMPLETE Call 仍生成已有证据报告，但严格降低结论边界。                                                    | P0         |
| FR-030 | 根因权限           | Preliminary Report 不能提升 Evidence Level、不能确认 Root Cause，历史案例和 AI 推断不能替代当前 L1/L2。             | P0         |

# 10. Finding（问题点）产品规则

- Finding
  状态：PROPOSED、OBSERVED、PERSISTING、RESOLVED、REVISED、INVALIDATED。

- Finding 严重等级：INFO、MEDIUM、HIGH、CRITICAL，与状态独立。

- 事实与候选分离：OBSERVED 表示已观测异常事实；PROPOSED
  表示疑似异常/候选解释。

- 不展示“AI 置信度
  92%”一类容易误解的百分比；展示证据等级、判定状态、跨层一致性。

- 同类事件按时间邻近、同 Stream、同特征规则聚合成 Finding，同时保留底层
  Event。

- Finding 默认排序：严重等级 → 用户现象相关度 → Evidence Level →
  跨层一致性 → 时间。

# 11. 飞书文档信息架构与排序规范（D111 / D112）

| **主入口原则：**飞书文档是人员查看报告的主入口，但不是权威数据源。飞书文档被人工修改、删除或 API 暂时失败时，不得影响后端 Canonical Report JSON、Artifact 与 Audit。 |
|----------------------------------------------------------------------------------------------------------------------------------------------------------------------|

| **顺序** | **章节**                  | **排序/阅读规则**                                                      |
|----------|---------------------------|------------------------------------------------------------------------|
| 0        | 当前状态 / 快速导航       | 始终固定顶部，更新而非追加。                                           |
| 1        | 当前初步结论              | 3~8 条，先给“现在知道什么”。                                           |
| 2        | 当前重点问题              | Finding 按严重度/相关度/证据等级排序。                                 |
| 3        | 证据完整度                | 先告诉读者哪些结论受证据缺失限制。                                     |
| 4        | 最新一次复现结果          | Call 之间倒序，最新 Call 默认展开。                                    |
| 5        | 多次复现汇总              | 展示出现次数、复现率、稳定/偶发趋势。                                  |
| 6        | A/B 对比                  | 存在 A/B 时在历史 Session 前展示。                                     |
| 7        | 历次 Reproduction Session | Session 倒序；保留版本、环境、配置。                                   |
| 8        | 正常项 / 排除性证据       | 展示 SIP 正常、双向 RTP、PCM 某方向无异常等。                          |
| 9        | 完整技术证据              | SIP/SDP → RTP → PCM RX → PCM TX → 音频质量 → 跨层 → Debug → Timeline。 |
| 10       | Evidence Bundle / 附件    | 原始 PCAP、完整 WAV、Clip、PNG、JSON、Manifest。                       |
| 11       | 版本与审计                | Report/Analyzer/Profile/Schema 版本与变更。                            |

三种排序规则固定为：①章节按决策价值优先；②Session/Call
按最新优先（Reverse Chronological，倒序时间）；③单个 Call 内 Evidence
按时间正序。Finding 独立按严重度与证据价值排序。

# 12. 飞书文档中的图与音频

- 每个问题点自动选择 1~3 张最有解释力的证据图，其他折叠或下钻。

- 电流音/周期干扰：局部 Spectrogram（时频图）+ Spectrum（频谱图）+ RX/TX
  或 RTP/PCM 对比图。

- Click/Pop：局部 Waveform（波形）放大图；Silence：波形 + RMS
  Timeline；RTP Loss/Jitter：媒体时间线；DTMF：双音频谱 + DTMF 时间线。

- 问题音频优先放异常
  Clip，同时提供完整方向音频入口。飞书原生音频预览/播放不可用时，降级到
  VOIP AI Web 播放页，但不得出现“报告引用了音频而用户无法访问”。

# 13. 数据保留、权限与分享

| **对象**                             | **默认策略**                      | **备注**                             |
|--------------------------------------|-----------------------------------|--------------------------------------|
| 原始 PCAP / 完整 WAV                 | 普通 Case 90 天                   | Golden/人工锁定 Case 可长期。        |
| 关键 Clip / 关键 PNG / Analyzer JSON | 长期保留                          | 不随原始大文件过期。                 |
| Preliminary / Diagnosis Report       | 长期保留                          | 由 Case 删除/归档策略统一治理。      |
| 内部展示                             | 原始 IP/号码/SIP URI              | Q38=A，内部不强制脱敏。              |
| 外部模型上下文                       | 遵循既有脱敏/最小化原则           | 内部原样显示不改变外部传输安全策略。 |
| 分享版 Bundle                        | 默认关键 Clip，不默认完整通话音频 | 内部研发版可包含完整音频。           |

# 14. 产品成功指标与体验指标

| **指标**                         | **V1.0 目标**                                                |
|----------------------------------|--------------------------------------------------------------|
| Call 结束 → 基础结果             | 目标 ≤ 10 秒                                                 |
| Call 结束 → 完整初步证据报告     | P95 ≤ 30 秒                                                  |
| 大型长通话/大 PCAP               | 可降级，P95 ≤ 60 秒                                          |
| 核心 P0 Finding Recall（召回率） | ≥ 95%                                                        |
| Finding Precision（准确率）      | ≥ 95%                                                        |
| 证据充分场景边界正确定位率       | ≥ 95%                                                        |
| 错误强定位率                     | < 1%                                                         |
| 证据不足场景                     | 必须输出 UNKNOWN，不允许强定位                               |
| 报告可复核性                     | Frame、时间、图、音频、指标均可回溯到 Artifact/Analyzer/版本 |

# 15. V1.0 发布门禁

**1.** PRD/SPEC P0 能力全部实现。

**2.** Unit / Integration / Contract Test 全部通过。

**3.** Analyzer Golden Dataset 达标。

**4.** Finding Precision/Recall 达标。

**5.** Wrong Boundary Rate 达标。

**6.** Report Schema Contract 通过。

**7.** Evidence Provenance 完整可追溯。

**8.** 无 Answer Leakage（答案泄露）。

**9.** Root Cause Authority invariant（根因权限不变量）通过。

**10.** 至少 1 个真实 DUT 端到端完整闭环通过。

**11.** Evidence Bundle 可离线复核。

**12.** Cleanup 无残留。

**13.** 性能 SLA 达标。

**14.** Audit 完整。

**15.** 无 P0/P1 阻塞缺陷。

# 16. Baseline 冻结规则

本 PRD 与对应 SPEC、追踪矩阵、验收矩阵共同构成 V1.0
Baseline（基线）。冻结后，新增需求、改变 P0
行为、修改状态机、Schema、Root Cause
Authority、保留策略、飞书主入口或排序规范，必须通过正式 Change
Request（变更请求）评审，不再通过口头约定直接改变 V1.0。

# 附录 A：决策收敛记录

| **决策区间** | **收敛主题**                                                                  | **结果**                                                |
|--------------|-------------------------------------------------------------------------------|---------------------------------------------------------|
| Q1–Q10       | 报告时机、双层输出、证据要求、音频/频谱、跨层定位                             | 全部按推荐；Q1=D。                                      |
| Q11–Q20      | 页面结构、Frame/SIP、图、音频、方向命名、首次异常层                           | 全部按推荐。                                            |
| Q21–Q30      | 问题分类、异常/疑似、正常项、阈值、版本、A/B、边界                            | 全部按推荐。                                            |
| Q31–Q40      | 飞书、Bundle、保留、脱敏、完整度、部分失败                                    | Q38=A（内部原样显示），其余按推荐。                     |
| Q41–Q50      | Finding ID、时间轴、PNG、dBFS、排序、基线、A/B 阈值、跨层算法                 | 全部按推荐。                                            |
| Q51–Q60      | 页面、Finding 卡、图片、Manifest、90 天保留、过期处理                         | 全部按推荐。                                            |
| Q61–Q70      | Call/Session/Case、聚合、环境指纹、A/B、报告完成门槛                          | 全部按推荐。                                            |
| Q71–Q80      | 自动编排、DAG、重试、超时、去抖、并发、多 Call、无 Call/中断 Call             | 全部按推荐。                                            |
| Q81–Q90      | Schema、Finding/Artifact 模型、状态机、API、幂等、与 Diagnosis/Golden/M7 关系 | 全部按推荐。                                            |
| Q91–Q100     | P0 Analyzer、问题集、准确率、性能、Golden、真实 DUT 验收                      | 全部按推荐。                                            |
| Q101–Q110    | V1.0 边界、自动/离线、前端、飞书、权限、审计、监控、非目标、门禁、冻结        | 全部按推荐。                                            |
| D111         | 飞书文档主报告视图                                                            | 飞书文档为主查看入口；后端 JSON/Artifact 为唯一权威源。 |
| D112         | 飞书报告排序规范                                                              | 结论优先 → 异常优先 → 最新优先 → 证据下钻。             |
