**VOIP 初步证据分析报告**

Software / System Specification（系统技术规格）

| 文档版本 | V1.0                                      |
|----------|-------------------------------------------|
| 文档状态 | Baseline Frozen（基线冻结）               |
| 项目     | VOIP AI 故障助手                          |
| 文档编号 | SPEC-VOIP-EVIDENCE-001                    |
| 冻结日期 | 2026-08-18                                |
| 变更规则 | 冻结后通过 Change Request（变更请求）管理 |

适用范围：VOIP 复现采集、PCAP/PCM/Media 分析、Evidence
Finding、Call/Session/Case 初步证据报告、飞书文档呈现与 Evidence
Bundle。

# 1. 规格目标与架构不变量

本 SPEC 将 PRD V1.0
转换为可实现、可测试、可审计的工程合同。所有实现必须保持以下不变量。

| **不变量**                       | **工程要求**                                                                                                                            |
|----------------------------------|-----------------------------------------------------------------------------------------------------------------------------------------|
| Evidence-first                   | 所有事实、边界、Finding 均必须可回溯到当前 Case Evidence、Analyzer Result 或确定性规则。                                                |
| Root Cause Authority             | Preliminary Report/AI/历史案例不能独立确认根因；正式根因仍服从当前 Case L1/L2 + 确定性确认规则 + 无矛盾 + 人工/修复验证等现有权限体系。 |
| DUT-only control                 | 系统只 SSH 控制被测 VOIP 设备，不 SSH/控制语音网关/PBX。                                                                                |
| Deterministic evidence rendering | 证据图由确定性 Renderer 生成，不允许 AI 自由生成“证据图片”。                                                                            |
| Canonical backend source         | Canonical Report JSON + Artifact Store + Audit 是权威数据源；飞书文档是投影视图。                                                       |
| Versioned contracts              | Report Schema、Analyzer Profile、Finding Signature 规则、Renderer、Composer 均版本化。                                                  |
| Graceful degradation             | 模块失败不得导致已有 Evidence 丢失；允许 PARTIAL_COMPLETE 并声明边界。                                                                  |

# 2. 总体架构

Call / Offline Evidence  
│  
├── PCAP ──\> Packet Analyzer ─────────────┐  
├── PCM RX/TX ─\> PCM Analyzer ───────────┤  
├── Debug/FXS ─\> Debug Evidence ─────────┤  
└── RTP/PCM decoded audio ─\> Media Analyzer  
│  
Cross-Layer Correlator  
│  
Finding Composer  
│  
Artifact Renderer  
│  
Report Composer (JSON)  
┌──────┼────────┐  
│ │ │  
HTML Web Feishu Doc  
│ │  
Evidence Bundle Feishu Card

建议新增/收敛模块：`reports/evidence_brief.py`（报告
Schema/Composer）、`reports/evidence_visuals.py`（波形/频谱/时频/RTP/SIP
图）、`services/evidence_brief.py`（编排与版本）、`integrations/feishu/evidence_report.py`（飞书文档投影）。具体文件名可随现有项目模块结构调整，但职责不得混淆。

# 3. Pipeline（分析流水线）

CALL_END / OFFLINE_EVIDENCE_READY  
→ TAIL_DRAIN  
→ EVIDENCE_FINALIZED  
→ ANALYZERS_SCHEDULED  
├─ Packet Analyzer  
├─ PCM Analyzer  
├─ Debug Evidence Parser (optional by platform)  
└─ Media Analyzer  
→ CROSS_LAYER_CORRELATION  
→ FINDING_COMPOSED  
→ KEY_ARTIFACTS_RENDERED  
→ REPORT_COMPOSING  
→ REPORT COMPLETE / PARTIAL_COMPLETE / FAILED  
→ FEISHU_PROJECTION  
→ NOTIFICATION

无依赖 Analyzer 并行运行；有依赖模块使用 DAG（Directed Acyclic
Graph，有向无环依赖图）编排。Analyzer
的临时基础设施错误可有界重试；确定性格式/输入错误不重试。每个 Analyzer
独立超时，超时状态为 TIMEOUT。

# 4. Report Scope 与实体关系

| **实体**               | **主键/作用域** | **职责**                                            |
|------------------------|-----------------|-----------------------------------------------------|
| CaseEvidenceSummary    | case_id         | 跨 Session 聚合；按环境分组；可包含 A/B 比较。      |
| SessionEvidenceSummary | session_id      | 跨 Call 聚合 Finding、复现率、Session 结论边界。    |
| CallEvidenceBrief      | call_id         | 单次通话的事实、问题点、图音频、Frame、证据完整度。 |
| EvidenceFinding        | finding_id      | 稳定问题实体；关联 Event、Evidence、Artifact。      |
| Artifact               | artifact_id     | 原始或派生证据文件及 Provenance。                   |

# 5. Report Schema V1

Schema 名称固定为 `preliminary-evidence-report-v1`。Report JSON 是
HTML、Web、飞书文档与 Evidence Bundle 的共同输入。

{  
"schema": "preliminary-evidence-report-v1",  
"report_id": "...",  
"scope_type": "CALL|SESSION|CASE",  
"scope_id": "...",  
"version": 1,  
"status": "COMPLETE|PARTIAL_COMPLETE|...",  
"case": {...},  
"environment": {...},  
"capture_quality": {...},  
"packet_summary": {...},  
"signaling_summary": {...},  
"media_flows": [...],  
"pcm_summary": {...},  
"findings": [...],  
"normal_evidence": [...],  
"artifacts": [...],  
"preliminary_assessment": {...},  
"evidence_boundary": {...},  
"traceability": {...},  
"generated_at": "..."  
}

# 6. Finding 数据模型

{  
"finding_id": "CALL-123-FINDING-002",  
"finding_signature":
"periodic_interference|pcm_rx|50hz_family|local_receive_path|sig-v1",  
"type": "PERIODIC_LOW_FREQUENCY_INTERFERENCE",  
"status": "OBSERVED",  
"severity": "HIGH",  
"evidence_level": "L2",  
"title": "PCM 接收音频检测到周期性低频干扰",  
"observation": "...",  
"interpretation": "...",  
"root_cause_boundary": "当前证据不足以确认具体硬件根因",  
"time_range": {"start": 12.4, "end": 18.2, "representative": 14.1},  
"scope": {"call_id": "...", "rtp_stream_id": null, "pcm_tap": "PCM_RX",
"direction": "DOWNSTREAM_TO_LOCAL_RX"},  
"metrics": {...},  
"evidence_refs": [...],  
"artifact_refs": [...],  
"event_refs": [...]  
}

| **字段**            | **合同**                                                              |
|---------------------|-----------------------------------------------------------------------|
| finding_id          | Case/Call 内稳定唯一；报告版本更新继续引用同一问题实体。              |
| finding_signature   | 用于跨 Call/Session 聚合；ID 与 Signature 不可合一。                  |
| status              | PROPOSED / OBSERVED / PERSISTING / RESOLVED / REVISED / INVALIDATED。 |
| severity            | INFO / MEDIUM / HIGH / CRITICAL，与 status 分离。                     |
| evidence_level      | 沿用现有 L1/L2/L3/L4/L5 权限体系；报告不得自行升级。                  |
| time_range          | 统一 start/end/representative；瞬时事件允许 start=end。               |
| root_cause_boundary | 必须明确“可观察边界”和“不能确认的根因”。                              |

# 7. Finding Signature 与聚合规则

- Signature
  最少考虑：问题类型、异常层、方向、关键频率/指标族、Stream/媒体路径角色、Signature
  规则版本。

- 同一 Call 内事件聚合：同类型 + 同 Stream/PCM Tap + 时间邻近 +
  同特征族。

- 跨 Call 聚合：Signature 相同且 Environment Fingerprint
  可比较；时间不参与“同类问题”判断。

- 跨 Session/Case 聚合前必须先按 Environment Fingerprint 分组。

- 旧 Finding 不删除：版本间标记新增、持续、已消失、修正或无效。

# 8. Environment Fingerprint（环境指纹）

至少包含：DUT 型号、软件版本、关键 Voice 配置、Voice
VLAN/抓包接口、语音网关信息（仅配置，不表示可控设备）、话柄/外围环境标识、PCM
Profile、Analyzer Profile。

不同环境不得直接合并复现率。A/B 比较属于上层
Compare，不改变各环境原始统计。

# 9. Packet Analyzer 合同（P0）

| **范围**     | **必须输出**                                                                                                                                                                  |
|--------------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| PCAP Summary | 总 Frame/Packet、SIP 消息数、注册数、Call 数、RTP Stream 数、RTCP 数、异常数。                                                                                                |
| SIP/SDP      | 注册/呼叫成功失败、冲突响应、Codec 协商、Call-ID/方向、关键 Frame。                                                                                                           |
| RTP Stream   | src/dst IP:port、SSRC、packet_count、unique、duration、expected/lost/loss_rate、duplicate/out_of_order、burst、avg/p95/max delta、RFC3550 jitter、payload/codec/clock/ptime。 |
| 异常 Frame   | Frame Number + 绝对时间 + Call 相对时间 + 协议 + src/dst + 关键字段；Hex Dump 仅研发下钻。                                                                                    |
| One-way      | 按方向 Packet 统计判断单向媒体，并关联 Call/SDP。                                                                                                                             |

# 10. PCM / Audio Analyzer 合同（P0）

| **指标/能力**  | **合同**                                                                                         |
|----------------|--------------------------------------------------------------------------------------------------|
| Packet/Session | packet_count、payload_bytes、capture/audio duration、median/max interval、gap events。           |
| 数字电平       | 保留既有 `dbfs` 兼容字段；新增清晰字段 `rms_dbfs` 与 `peak_dbfs = 20*log10(peak/32768)`。 |
| 波形           | 分 bin 的 min/max/RMS dBFS。                                                                     |
| 频域           | dominant peaks、narrowband tones、comb、50/60 Hz family score 与谐波。                           |
| 异常           | unexpected silence、click/pop、echo path、periodic interference、DTMF。                          |
| 边界           | 50/60 Hz 特征只能表述为“周期性低频/工频族特征”，不得直接推导电源、接地、SLIC 等物理根因。        |

# 11. 跨层时间关联与“异常首次可观测层”算法

统一以 PCAP Capture Timestamp（抓包绝对时间戳）为主时间轴，同时保存 Call
相对时间 T+Xs。PCM/RTP/Debug 通过采集时间锚点、Session/Call Scope、RTP
Stream、方向和重叠窗口关联。

**1.** 确定候选异常窗口：start/end/representative。

**2.** 验证上下游证据完整度与可比性；缺失关键上游证据则降级。

**3.** 按媒体路径角色比较同一时间窗口内是否存在同类异常特征。

**4.**
仅在上游“证据存在且未检测到同类异常”、下游“证据存在且检测到异常”时，输出“异常首次可观测于
X 层”。

**5.** 若上游缺失、时间不重叠、采样不可比或异常类型不可比较，则输出
UNKNOWN，并说明限制。

| **禁止行为：**不得因“最早已采集到异常的是 PCM RX”就写成“异常起源于 PCM RX”；如果 RTP Downstream 缺失，只能写“当前最早已采集到异常的层为 PCM RX，无法确认其是否为真正首次异常边界”。 |
|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

# 12. Finding 排序与异常合并

默认排序键：Severity 降序 → 用户现象相关度降序 → Evidence Level 强度降序
→ 跨层一致性降序 → representative_time 升序。AI
不拥有最终排序权，只可在注册字段范围内提供解释性相关度建议；正式排序必须确定性。

# 13. Artifact 类型与 Provenance

| **Artifact Type**       | **用途**                          |
|-------------------------|-----------------------------------|
| RAW_PCAP                | 原始抓包。                        |
| PCM_WAV / RTP_WAV       | 完整方向音频。                    |
| AUDIO_CLIP              | 异常或对照片段。                  |
| WAVEFORM_PNG            | 波形证据图。                      |
| SPECTRUM_PNG            | 频谱证据图。                      |
| SPECTROGRAM_PNG         | 时频证据图。                      |
| RTP_TIMELINE_PNG        | RTP 丢包/抖动/Delta 时间线。      |
| SIP_CALL_FLOW_PNG       | SIP 呼叫流程图。                  |
| PACKET_ANALYSIS_JSON    | Packet Analyzer 结果。            |
| PCM_ANALYSIS_JSON       | PCM Analyzer 结果。               |
| MEDIA_ANALYSIS_JSON     | Media Analyzer/Correlation 结果。 |
| PRELIMINARY_REPORT_HTML | 独立 HTML 报告。                  |
| PRELIMINARY_REPORT_JSON | 权威结构化报告快照。              |
| EVIDENCE_BUNDLE         | 标准证据包。                      |
| MANIFEST_JSON           | 证据清单与关系。                  |

每个 Artifact
至少保存：artifact_id、type、case_id、session_id、call_id、finding_ids、source_artifact_ids、analyzer_name/version、profile_version、time_range、sha256、size、mime_type、storage_location、created_at。

# 14. Artifact Renderer 规格

| **图类型**        | **默认生成条件**             | **必须标注**                                       |
|-------------------|------------------------------|----------------------------------------------------|
| Spectrum PNG      | 周期干扰/DTMF/窄带 tone      | Frequency Hz、Magnitude dB、关键频率线、异常窗口。 |
| Spectrogram PNG   | 噪音/周期干扰/时变频谱       | Time、Frequency Hz、relative dB 色阶、异常区域。   |
| Waveform PNG      | Click/Pop/Silence/电平突变   | Time、Amplitude/RMS、异常窗口。                    |
| RTP Timeline PNG  | Loss/Burst/Jitter/High Delta | T+Xs、Frame/Seq、Lost/Jitter/Delta 事件。          |
| SIP Call Flow PNG | 注册/建链/异常响应           | 双方角色、消息、Frame、状态码。                    |

PNG 为正式 Evidence Artifact，默认宽度至少 1200 px，报告展示建议
1600×900 或等比例高清；同时保留
plot_data.json/生成参数/renderer_version。关键 Artifact P0
优先生成，非关键全量图可延后，不阻塞首屏基础结果。

# 15. Audio Clip 规格

| **异常类型**                 | **默认窗口**                                     |
|------------------------------|--------------------------------------------------|
| RTP Packet Loss / Burst Loss | 前 1s + event + 后 1s                            |
| Click / Pop                  | 前 0.5s + event + 后 0.5s                        |
| 周期性干扰                   | 代表性 2~5s 窗口；必要时附同期 RTP/PCM 对照 Clip |
| Silence                      | 前 1s + silence + 后 1s                          |
| DTMF                         | 前 0.3s + tone + 后 0.3s                         |

问题 Clip 优先；完整 PCM RX、PCM TX、RTP Downstream、RTP Upstream
音频可展开或进入 Evidence Bundle。

# 16. Report 状态机

PENDING  
→ ANALYZING  
→ COMPOSING  
→ COMPLETE  
→ SUPERSEDED (产生更新版本后)  
  
异常/降级：  
ANALYZING → PARTIAL_COMPLETE  
COMPOSING → PARTIAL_COMPLETE  
PENDING/ANALYZING/COMPOSING → FAILED（仅报告本身无法生成）

Analyzer 状态独立管理，至少支持 SUCCESS / PARTIAL_SUCCESS / FAILED /
TIMEOUT。Report COMPLETE 不等于 Evidence 完整，更不等于 Root Cause
确认。

# 17. Report Completeness Gate（完整度门槛）

- Call 已结束，或 Call 状态已明确为 INCOMPLETE/ABORTED。

- 可用 Evidence 已 Finalize。

- 必需 Analyzer 已进入 SUCCESS/PARTIAL_SUCCESS/FAILED/TIMEOUT 终态。

- Evidence Completeness 已计算。

- Finding 聚合完成。

- 关键 Artifact 生成成功或已记录降级失败。

- Report Manifest 已落库。

- 不可用模块与结论边界已显式声明。

# 18. 版本、快照与幂等

报告采用快照版本，不原地覆盖。相同输入重复执行必须返回同一 Report
Version；输入发生实质变化才创建新版本。

idempotency_key = hash(  
report_scope,  
scope_id,  
input_snapshot_hash,  
report_schema_version,  
composer_version,  
analyzer_result_versions  
)

Report V1 由 Analyzer 收敛后生成；Diagnosis/AI SHADOW 新解释、Analyzer
Rerun、Profile 更新或 Evidence 补采若导致正式内容变化，则创建 V2+。采用
Debounce（去抖）+ 状态驱动更新，避免秒级连续产生无意义版本。

# 19. API 合同

| **方法** | **路径（建议）**                        | **语义**                                            |
|----------|-----------------------------------------|-----------------------------------------------------|
| GET      | /calls/{call_id}/reports/evidence       | 获取最新 Call Evidence Brief；可支持 version 查询。 |
| GET      | /sessions/{session_id}/reports/evidence | 获取 Session 汇总。                                 |
| GET      | /cases/{case_id}/reports/evidence       | 获取 Case 汇总与飞书入口信息。                      |
| POST     | /.../reports/evidence/rebuild           | 幂等触发重建；产生新版本而非重复对象。              |
| GET      | /reports/{report_id}/artifacts          | 获取报告关联 Artifact。                             |
| GET      | /artifacts/{artifact_id}/download-url   | 复用/扩展现有 Artifact 下载能力。                   |
| GET      | /reports/{report_id}/bundle             | 获取或触发 Evidence Bundle。                        |

路径最终应与现有 `/cases/{case_id}/artifacts`、Analyzer Artifact API
和 Report API 风格保持一致。若现有路由命名冲突，可保留语义、调整路径。

# 20. Evidence Bundle 规格

VOIP-CASE-xxxx/  
├── report/  
│ ├── preliminary-report.html  
│ └── preliminary-report.json  
├── pcap/  
│ └── call.pcap  
├── audio/  
│ ├── full/  
│ └── clips/  
├── images/  
│ ├── waveform/  
│ ├── spectrum/  
│ ├── spectrogram/  
│ └── timeline/  
├── analysis/  
│ ├── packet.json  
│ ├── pcm.json  
│ └── media.json  
├── debug/  
├── manifest.json  
└── SHA256SUMS

Manifest 必须记录文件身份、来源、时间范围、Analyzer 与版本、Finding
关联、source_artifact_ids、SHA256、生成时间。内部研发包可包含完整音频；分享安全包默认只含异常
Clip/必要证据。

# 21. 飞书文档投影合同

每个 Case 默认维护一份飞书文档，文档 ID 与 Case 绑定。后端先生成
Canonical Report JSON，再由 Feishu Projector 将结构化章节投影为飞书 Docx
Block。禁止直接从 Analyzer 自由拼飞书内容绕过 Report JSON。

| **章节**            | **更新策略**                                                |
|---------------------|-------------------------------------------------------------|
| 0 当前状态/快速导航 | 原位更新，固定顶部。                                        |
| 1 当前初步结论      | 原位更新，最多 3~8 条。                                     |
| 2 当前重点问题      | 按 Finding 排序重建/差量更新。                              |
| 3 证据完整度        | 随 Evidence 状态更新。                                      |
| 4 最新一次复现      | 最新 Call 默认展开；Call 倒序。                             |
| 5 多次复现汇总      | Session/Case 聚合后更新。                                   |
| 6 A/B 对比          | 存在 Compare 时生成。                                       |
| 7 历次 Session      | Session 倒序；保留环境和版本。                              |
| 8 正常项/排除证据   | 按当前证据更新。                                            |
| 9 完整技术证据      | 按 SIP→RTP→PCM RX→PCM TX→Audio→Correlation→Debug→Timeline。 |
| 10 Bundle/附件      | 链接/附件到权威 Artifact。                                  |
| 11 版本与审计       | 追加版本记录。                                              |

音频优先作为飞书可直接访问的附件/预览；若飞书原生播放器能力不可稳定依赖，则在同一位置提供
VOIP AI Web 音频播放入口。飞书失败不得阻断后端 Report COMPLETE。

# 22. 飞书机器人通知合同

- Call：Analyzer/Report 完成后发送或更新简短结果卡，显示复现、Finding
  数量、最高 Severity、Top Finding、飞书文档入口。

- Session：结束后发送汇总卡，显示有效 Calls、复现率 Top
  Finding、A/B（若有）、文档入口。

- 同一 Call 优先更新同一消息；只有初步报告完成、HIGH/CRITICAL
  新异常、Session 完成、最终根因确认、Fix Verification
  通过等关键状态新增通知。

# 23. 前端 Web 合同

- Case 页面展示报告总览、Finding 卡片、证据完整度、最新
  Call、Session/Case 汇总。

- Finding 卡片默认展示：等级、类型、时间、初步判断、核心指标、Evidence
  Level、关键图、异常音频入口。

- 支持图片预览、音频播放、Frame/Stream/PCM Session
  下钻、完整报告、Bundle 下载。

- 研发深度信息留在 Web，不要求飞书承载所有 JSON/Hex/原始字段。

# 24. 权限与审计

V1.0 复用现有 Case 身份/权限，报告与 Artifact 继承 Case 权限；接口预留
VIEW_REPORT、VIEW_RAW_EVIDENCE、DOWNLOAD_EVIDENCE_BUNDLE、REBUILD_REPORT
等细分权限。内部报告默认不对 IP/号码/SIP URI 脱敏。外部 Reasoning
Gateway/LLM 仍服从既有脱敏合同。

Audit 至少记录：actor（含
SYSTEM）、action、case/report/artifact、old/new version、trigger、input
snapshot hash、profile/schema/composer version、timestamp。Bundle
下载、报告重建、规则版本变化必须审计。

# 25. 数据保留与过期

| **数据**                                   | **默认保留** | **过期后行为**                                               |
|--------------------------------------------|--------------|--------------------------------------------------------------|
| RAW_PCAP / 完整 WAV                        | 90 天        | 删除原始大文件；报告保留并标注“原始 Evidence 已按策略过期”。 |
| 关键 Clip / PNG / Analyzer JSON / Manifest | 长期         | 随 Case 删除/归档策略治理。                                  |
| Preliminary / Diagnosis Report             | 长期         | 保留历史版本。                                               |
| Golden / 人工锁定 Case 原始证据            | 长期         | 不走普通 90 天清理。                                         |

# 26. 失败、超时与降级

| **故障**               | **处理**                                                                                 |
|------------------------|------------------------------------------------------------------------------------------|
| 对象存储/队列临时错误  | 有界重试；指数退避/现有平台重试策略。                                                    |
| PCAP 损坏/格式不支持   | 不重复重试；Analyzer FAILED；报告降级。                                                  |
| Analyzer TIMEOUT       | 其他 Analyzer 继续；Report PARTIAL_COMPLETE。                                            |
| PNG 生成失败           | 保留分析 JSON/指标；报告标记图生成失败，不整体失败。                                     |
| 飞书 API 失败          | 后端报告保持 COMPLETE；记录投影失败并可重试。                                            |
| 音频飞书原生预览不可用 | 降级到 Web 播放链接。                                                                    |
| 并发多 Call            | 共享底层 Ring Buffer 可以，但每个 Call 必须独立 Call Scope，禁止 Evidence 跨 Call 污染。 |

# 27. 超长通话与性能策略

超长通话采用“全量低成本统计 + 异常候选扫描 + 异常区域高精度分析”。全量做
RMS/Peak、RTP Loss/Jitter、频谱趋势；异常窗口再做高分辨率 STFT、Waveform
Zoom、Clip 与跨层对齐。

| **SLA**           | **目标**                      |
|-------------------|-------------------------------|
| 基础结果          | Call End 后目标 ≤ 10s         |
| 完整初步报告      | P95 ≤ 30s                     |
| 大型长通话/大PCAP | 允许 P95 ≤ 60s                |
| 关键 Artifact     | P0 优先，不得被全量辅助图阻塞 |

# 28. P0 问题类型

| **领域**         | **P0 类型**                                                                      |
|------------------|----------------------------------------------------------------------------------|
| SIP/SDP          | 注册失败、呼叫建立失败、冲突 Final Response、Codec/ptime 协商异常。              |
| RTP              | 单向媒体、丢包、Burst Loss、Jitter、High Delta、Payload Change。                 |
| PCM/Audio        | Gap、异常静音、Click/Pop、周期性低频干扰/50/60Hz 族、Echo、DTMF 异常、电平异常。 |
| Cross-Layer      | RTP↔PCM 同期异常差异、异常首次可观测层、证据不足 UNKNOWN。                       |
| Evidence Quality | PCAP/PCM/Debug 缺失或部分完整。                                                  |

# 29. Golden Dataset 与算法验收

- 数据集三层：Synthetic（合成）、Lab Real（实验室真实）、Field
  Confirmed（现场确认）。发布门禁不能只依赖 Synthetic。

- 核心确定性 P0 类型：Recall ≥ 95%，Finding Precision ≥ 95%。

- 边界充分场景：正确定位率 ≥ 95%；证据不足必须 UNKNOWN；Wrong Boundary
  Rate < 1%。

- Expected/ground truth 元数据不得进入生产 Analyzer、Finding Composer 或
  AI 输入，必须防止 Answer Leakage。

- 每次 Analyzer/Profile/Signature/Renderer 变更必须触发相关 Golden
  回归。

# 30. 与现有 DiagnosisReport / AI / Golden / M7 的关系

L1/L2/L3 Current Evidence  
↓  
Packet / PCM / Media Analyzer  
↓  
Preliminary Evidence Report  
“看到了什么异常”  
↓  
Deterministic Diagnosis  
↓  
Reasoning Gateway / AI SHADOW  
“如何解释、下一步验证什么”  
↓  
Root Cause Confirmation  
↓  
Diagnosis Report  
“最终确认什么”

- Preliminary Report 可作为 Diagnosis 的输入和证据入口，但不能提升
  Evidence Level。

- Historical Similar Case 仍为低层参考，不能因写入 Preliminary Report
  而成为当前 L1/L2。

- Golden Candidate 继续使用现有独立准入规则；初步报告只提供更完整的当前
  Case Evidence 快照。

- M7 可新增“Evidence Brief 自动生成/飞书投影/Bundle”检查，但不能替代原有
  Diagnosis、真实 AI SHADOW、Golden、Audit 等验收。

- 不得启用或扩大 CONTROLLED_PLANNER 权限来完成本功能。

# 31. 可观测性与运行指标

- Analyzer SUCCESS / PARTIAL / FAILED / TIMEOUT 分布。

- Call End → 基础结果 / 完整 Report 的 P50/P95 延迟。

- Finding 数量/类型/Severity 分布。

- Artifact Renderer 失败率与 Bundle 失败率。

- Worker Queue 积压、对象存储占用、原始大文件清理量。

- 飞书文档投影/机器人推送成功率与重试。

- 报告重建次数、Schema/Profile/Composer 版本分布。

# 32. V1.0 Strict Release Gate

| **Gate**   | **通过条件**                                       |
|------------|----------------------------------------------------|
| Scope      | PRD/SPEC P0 100% 实现。                            |
| Tests      | Unit/Integration/Contract 全通过。                 |
| Golden     | P0 Finding Precision/Recall 与 Boundary 指标达标。 |
| Provenance | 关键图、音频、Frame、JSON 可追溯。                 |
| Authority  | Root Cause Authority invariant 无回归。            |
| Leakage    | Answer Leakage = 0。                               |
| Real DUT   | 至少 1 个真实 DUT 完整链路通过。                   |
| Bundle     | 可离线复核。                                       |
| Cleanup    | 临时 PCM/debug/tcpdump/锁无残留。                  |
| SLA        | 性能目标达标。                                     |
| Audit      | 关键操作审计完整。                                 |
| Defects    | 无 P0/P1 阻塞缺陷。                                |

# 33. 真实 DUT 最小端到端验收链

真实 DUT  
→ 自动采集 PCAP + PCM RX + PCM TX  
→ 真实 Call  
→ Call 自动结束识别  
→ Evidence Finalize  
→ Packet / PCM / Media / Cross-Layer Analyzer  
→ Finding 自动生成  
→ 频谱 / 波形 / Audio Clip 自动生成  
→ Call Evidence Brief  
→ Session Summary  
→ 飞书文档更新  
→ 飞书结果卡  
→ Evidence Bundle 可下载  
→ Cleanup 无残留

人工复核必须证明报告中的 Frame、时间、方向、音频片段、频谱/波形与原始
Evidence 一致。

# 34. 兼容性与迁移要求

- 现有 DiagnosisReport 保持向后兼容；不得用 Evidence Brief
  替换原有最终诊断报告。

- 现有 PCM `dbfs` 字段保留，新增 `rms_dbfs`/`peak_dbfs`
  时需提供兼容迁移。

- 现有 Artifact API 与 ObjectStorage
  能力优先复用，不重复建立文件存储系统。

- Report Schema 与 Manifest 需支持后续 Migration；历史 V1
  报告必须可读取。

- Rule/Analyzer/Profile/Adapter
  变更继续遵循项目既有版本化、审计和架构评审策略。

# 35. V1.0 冻结与变更控制

以下变化视为 Baseline Change：P0 问题集删减、Finding 状态/Severity
语义改变、跨层边界算法改变、Canonical Report Schema
不兼容变化、飞书文档主入口取消、证据保留策略改变、Root Cause Authority
放宽、内部原样展示/外部模型脱敏边界改变。必须提交 Change Request
并重新评估 Golden、真实 DUT 与发布门禁。
