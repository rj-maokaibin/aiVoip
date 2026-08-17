**VOIP 初步证据分析报告**

Traceability / Scope / Acceptance Matrix（追踪、范围与验收矩阵）

| 文档版本 | V1.0                                      |
|----------|-------------------------------------------|
| 文档状态 | Baseline Frozen（基线冻结）               |
| 项目     | VOIP AI 故障助手                          |
| 文档编号 | TRACE-VOIP-EVIDENCE-001                   |
| 冻结日期 | 2026-08-18                                |
| 变更规则 | 冻结后通过 Change Request（变更请求）管理 |

适用范围：VOIP 复现采集、PCAP/PCM/Media 分析、Evidence
Finding、Call/Session/Case 初步证据报告、飞书文档呈现与 Evidence
Bundle。

# 1. PRD ↔ SPEC Traceability Matrix（需求追踪矩阵）

| **PRD ID** | **需求主题**           | **SPEC 对应**     | **验收用例族** | **通过标准**                                       |
|------------|------------------------|-------------------|----------------|----------------------------------------------------|
| FR-001     | Call/Session/Case 报告 | SPEC §4, §16–18   | IT-RPT-001     | Call 自动出简报，Session 自动汇总，Case 可聚合     |
| FR-002     | 正常 Call 也生成报告   | SPEC §17          | IT-RPT-002     | 无 Finding 仍 COMPLETE，展示正常/排除证据          |
| FR-003     | 版本化更新             | SPEC §18          | CT-RPT-003     | 输入不变幂等；输入变化生成 V2                      |
| FR-004~006 | Finding 分类/状态/证据 | SPEC §6–7, §12    | UT-FND-*      | Finding 模型、证据引用、Severity/Status 分离       |
| FR-007~008 | Packet/SIP 证据        | SPEC §9, §14      | GT-PKT-*      | Frame/Stream/Call Flow 可复核                      |
| FR-009~011 | 音频/图/dBFS           | SPEC §10, §14–15  | GT-AUD-*      | Clip/PNG/Peak dBFS 与原始 PCM 一致                 |
| FR-012     | 跨层首次异常边界       | SPEC §11          | GT-XLY-*      | 充分证据正确定位；不足 UNKNOWN；Wrong Boundary<1% |
| FR-013~015 | 正常项/完整度/降级     | SPEC §17, §26     | IT-DEG-*      | 缺失 PCM/Analyzer timeout 仍 PARTIAL_COMPLETE      |
| FR-016~019 | 跨 Call/环境/A-B/基线  | SPEC §7–8         | IT-AGG-*      | 同 Signature 聚合；环境不混算；A/B 可比较          |
| FR-020     | Evidence Bundle        | SPEC §20          | IT-BND-001     | 目录、Manifest、SHA256、下载完整                   |
| FR-021~022 | 飞书文档/机器人        | SPEC §21–22       | IT-FS-*       | Case 单文档更新、卡片去刷屏、失败可重试            |
| FR-023     | Web 下钻               | SPEC §23          | E2E-WEB-*     | Finding/图片/音频/Frame/Bundle 可访问              |
| FR-024~025 | 内部原样/外部模型安全  | SPEC §24          | SEC-DATA-*    | 内部不脱敏；外部模型合同不放宽                     |
| FR-026     | 保留策略               | SPEC §25          | IT-RET-*      | 90 天原始清理；关键派生长期；报告标记过期          |
| FR-027     | 权限与审计             | SPEC §24          | SEC-AUD-*     | 继承 Case 权限；下载/重建/版本审计                 |
| FR-028~029 | 无 Call/中断 Call      | SPEC §17, §26     | E2E-CALL-*    | 无有效 Call 有 Session 报告；ABORTED 可部分分析    |
| FR-030     | 根因权限               | SPEC §1, §30, §32 | SAF-AUTH-*    | Preliminary/AI 不可独立确认 Root Cause             |

# 2. V1.0 / V1.1 / V2 Scope Matrix（版本范围矩阵）

| **能力**                          | **V1.0**                                        | **V1.1**                          | **V2/未来**                              |
|-----------------------------------|-------------------------------------------------|-----------------------------------|------------------------------------------|
| 自动/离线证据进入 Report Pipeline | P0 完整                                         | 稳定性优化                        | 多租户/大规模调度增强                    |
| Packet / SIP / RTP Analyzer       | P0 完整                                         | 更多协议细节/RTCP 深化            | 扩展非 VOIP 通用分析：不作为当前规划目标 |
| PCM / Audio Analyzer              | P0：电平、静音、Click/Pop、低频干扰、Echo、DTMF | 更丰富音频缺陷分类                | 高级感知模型（需独立评估）               |
| Cross-Layer Correlation           | P0 确定性 + UNKNOWN                             | 更多平台/路径模型                 | 多设备链路因果实验                       |
| Evidence PNG / Audio Clip         | P0                                              | 更多对比图/交互图                 | 高级音频工作站：非目标                   |
| Call/Session/Case 报告            | P0                                              | 更强趋势/回归视图                 | 跨组织知识库视图                         |
| 飞书文档主入口                    | P0                                              | 模板/交互优化                     | 可选其他协作平台投影                     |
| 飞书机器人卡片                    | P0                                              | 交互动作增强                      | 多渠道通知                               |
| Evidence Bundle                   | P0                                              | 更多 Profile/脱敏导出             | 长期归档/外部分享门户（需另立项）        |
| A/B Compare                       | P0 基础确定性指标                               | A-B-A / Fix Verification 展示增强 | 自动实验设计优化                         |
| AI                                | 仅解释/SHADOW，权限不扩大                       | 受控建议体验优化                  | 仍不得绕过 Root Cause Authority          |
| 真实 dB SPL                       | 不做                                            | 不做                              | 仅在引入校准硬件/参考映射的新项目中评估  |

# 3. Functional Acceptance Matrix（功能验收）

| **ID** | **验收项**                             | **场景**                     | **通过标准**                                                                   |
|--------|----------------------------------------|------------------------------|--------------------------------------------------------------------------------|
| AC-F01 | 有效 Call 自动生成 Call Evidence Brief | 真实/实验室 Call             | Call End 后 Analyzer 收敛，Report COMPLETE/PARTIAL_COMPLETE，有版本和 Manifest |
| AC-F02 | Session 自动汇总                       | 同 Session ≥3 Calls          | 按 Signature 聚合，显示出现次数与复现率                                        |
| AC-F03 | Case 环境隔离                          | 同 Case 不同版本/话柄        | Environment Fingerprint 分组，不混合统计                                       |
| AC-F04 | A/B 对比                               | A/B 各有有效 Calls           | 展示复现率、RTP、PCM、Finding、Evidence Boundary 差异                          |
| AC-F05 | 正常 Call                              | 无异常 Call                  | 生成报告并展示关键正常/排除性证据                                              |
| AC-F06 | Analyzer 失败降级                      | 强制一个 Analyzer TIMEOUT    | 其他结果保留，PARTIAL_COMPLETE，边界明确                                       |
| AC-F07 | 无有效 Call                            | 仅 Off-hook/预录，无 Call    | 生成 Session“未形成有效 Call”报告                                              |
| AC-F08 | 中断 Call                              | Call ABORTED                 | 已有证据可分析，缺失段明确，不断言整个 Call                                    |
| AC-F09 | 飞书文档                               | Case 多次 Call               | 单 Case 单主文档持续更新，顺序符合 D112                                        |
| AC-F10 | Bundle                                 | 任意 COMPLETE/PARTIAL Report | 可下载；Manifest+SHA256 完整；文件可追溯                                       |

# 4. Accuracy / Quality Acceptance Matrix（准确性与质量）

| **ID** | **指标**             | **数据集/场景**     | **门槛**                                                               |
|--------|----------------------|---------------------|------------------------------------------------------------------------|
| AQ-01  | P0 Finding Recall    | Golden Dataset      | 每个核心确定性问题类型 Recall ≥95%                                     |
| AQ-02  | Finding Precision    | Golden Dataset      | 整体/核心类型 Precision ≥95%，HIGH/CRITICAL 误报单独门禁               |
| AQ-03  | Boundary Correctness | 已知跨层边界数据    | 证据充分场景正确定位率 ≥95%                                            |
| AQ-04  | Wrong Boundary Rate  | 边界不足/混淆数据   | 错误强定位率 <1%                                                      |
| AQ-05  | UNKNOWN Safety       | 上游证据缺失        | 必须 UNKNOWN，并说明缺失，不强定位                                     |
| AQ-06  | No Answer Leakage    | Synthetic/Lab/Field | ground truth 不进入 Analyzer/AI 输入                                   |
| AQ-07  | Provenance           | 随机抽样 Finding    | 指标/图/音频/Frame 可回溯 source Artifact、Analyzer、Profile、时间范围 |
| AQ-08  | Root Cause Authority | 所有回归集          | Preliminary/AI/历史案例不能独立改变正式根因状态                        |

# 5. Performance / Reliability Acceptance Matrix（性能与可靠性）

| **ID** | **项目**         | **场景**                             | **通过标准**                                      |
|--------|------------------|--------------------------------------|---------------------------------------------------|
| AP-01  | 基础结果时延     | Call End → 基础结果                  | 目标 ≤10s                                         |
| AP-02  | 完整报告时延     | Call End → COMPLETE/PARTIAL_COMPLETE | P95 ≤30s                                          |
| AP-03  | 大型/长通话      | 大 PCAP/长 Call                      | 允许 P95 ≤60s，采用分层分析                       |
| AP-04  | 关键 Artifact    | Finding 相关图/Clip                  | 优先生成，不被辅助全量图阻塞                      |
| AP-05  | Analyzer Timeout | 单模块挂起                           | 模块 TIMEOUT，流水线继续，不无限等待              |
| AP-06  | Feishu Failure   | 飞书 API 故障                        | 后端 Report 仍 COMPLETE，可重试投影               |
| AP-07  | Idempotency      | 重复 Worker/重复 POST                | 相同输入不生成重复 Report/Artifact                |
| AP-08  | Concurrent Calls | 快速连续 Calls                       | Call Scope 独立，无 RTP/PCM/DTMF Evidence 串 Call |

# 6. Real DUT Release Acceptance（真实 DUT 发布验收）

| **ID** | **验收点**     | **通过标准**                                        |
|--------|----------------|-----------------------------------------------------|
| RD-01  | 自动采集       | PCAP + PCM RX + PCM TX + 必要 Debug 均实际产生      |
| RD-02  | Call lifecycle | Call 自动识别开始/结束，Tail Drain/Finalize 正确    |
| RD-03  | Analyzer       | Packet/PCM/Media/Cross-Layer 实际完成               |
| RD-04  | Finding        | 至少正常/异常事实按实际数据形成                     |
| RD-05  | Artifact       | 频谱/波形/Clip/Timeline 真实生成                    |
| RD-06  | Report         | Call + Session 报告自动生成                         |
| RD-07  | Feishu         | Case 主文档更新 + 机器人通知                        |
| RD-08  | Bundle         | 可下载并离线核验                                    |
| RD-09  | Manual Recheck | Frame、时间、方向、音频、图与原始 Evidence 人工一致 |
| RD-10  | Cleanup        | PCM/debug/tcpdump/临时锁无残留                      |

# 7. Feishu Document Ordering Contract（飞书文档排序验收）

| **规则**              | **验收标准**                                                                                                              |
|-----------------------|---------------------------------------------------------------------------------------------------------------------------|
| 章节决策价值优先      | 顶部固定：状态/导航 → 当前结论 → 重点问题 → 完整度 → 最新复现 → 汇总 → A/B → 历史 → 正常证据 → 技术证据 → Bundle → 审计。 |
| Call/Session 最新优先 | 多个 Call/Session 时，最新项在前；最新 Call 默认展开。                                                                    |
| Call 内时间正序       | Off-hook/DTMF/SIP/RTP/异常/BYE 按 T+Xs 正序。                                                                             |
| Finding 异常优先      | Severity → 用户现象相关度 → Evidence Level → 跨层一致性 → 时间。                                                          |
| 主文档不作为权威源    | 人工修改/删除飞书文档不改变 Canonical Report JSON、Artifact 或 Root Cause。                                               |

# 8. Terminology Glossary（术语表）

| **英文/缩写**               | **中文名称**           | **定义**                                                            |
|-----------------------------|------------------------|---------------------------------------------------------------------|
| Evidence Finding            | 证据问题点             | 由 Analyzer/确定性规则形成、可追溯到当前 Evidence 的异常或候选。    |
| Preliminary Evidence Report | 初步证据分析报告       | 回答“抓到什么/哪里异常/证据是什么”，不等于最终根因报告。            |
| Diagnosis Report            | 诊断报告               | 根因确认流程后的正式诊断结果。                                      |
| PCM RX                      | PCM 接收音频           | 被测设备视角的接收方向 PCM 音频。                                   |
| PCM TX                      | PCM 发送音频           | 被测设备视角的发送方向 PCM 音频。                                   |
| RTP Downstream              | RTP 下行媒体流         | 网络/语音网关 → 被测设备。                                          |
| RTP Upstream                | RTP 上行媒体流         | 被测设备 → 网络/语音网关。                                          |
| dBFS                        | 相对于数字满量程的分贝 | 数字 PCM 电平单位；0 dBFS 为满量程，不等于声压 dB SPL。             |
| Peak dBFS                   | 峰值数字电平           | 样本最大绝对幅值转换到 dBFS。                                       |
| RMS dBFS                    | 均方根数字电平         | 信号 RMS 转换到 dBFS。                                              |
| Spectrogram                 | 时频图                 | 横轴时间、纵轴频率、色阶表示相对幅度/能量。                         |
| Spectrum                    | 频谱图                 | 横轴频率，纵轴幅度/能量。                                           |
| Jitter P95                  | P95 抖动               | 95% RTP 抖动值不超过该值。                                          |
| Burst Loss                  | 突发丢包               | 连续或簇状 RTP 数据包丢失。                                         |
| Evidence Boundary           | 证据边界               | 当前证据能够收敛到的路径/层级边界，不等于根因。                     |
| Environment Fingerprint     | 环境指纹               | 用于隔离可比较复现环境的确定性配置/版本/外围特征集合。              |
| Golden Dataset              | 黄金验证数据集         | Synthetic + Lab Real + Field Confirmed 的算法回归与发布数据集。     |
| Answer Leakage              | 答案泄露               | ground truth/expected 信息错误进入 Analyzer/AI 输入。               |
| Canonical Report JSON       | 权威结构化报告         | 飞书/Web/HTML/Bundle 的共同权威数据源。                             |
| Evidence Bundle             | 证据包                 | 可离线复核的报告、PCAP、音频、图片、JSON、Manifest 与 SHA256 集合。 |

# 9. Open Issues（非阻塞开放项）

| **项**                                        | **V1.0 处理**                                            | **后续**                                  |
|-----------------------------------------------|----------------------------------------------------------|-------------------------------------------|
| 飞书 WAV 原生内嵌播放体验                     | P0 保证“可直接访问”；原生播放不可依赖时降级到 Web 播放。 | 实施阶段依据飞书客户端/API 实际能力优化。 |
| 分享版 Evidence Bundle 的更细粒度脱敏 Profile | V1.0 先提供内部 FULL + 基础 SHARE_SAFE。                 | V1.1 可增加角色/地区/客户模板。           |
| Debug Analyzer 跨平台深度                     | V1.0 作为平台可选辅助证据。                              | 按 Adapter 能力逐平台增强。               |
| 更高级主观语音质量模型                        | 不阻塞 V1.0。                                            | 需独立准确性与授权边界评估。              |

# 10. Baseline Frozen Checklist（基线冻结清单）

- PRD V1.0、SPEC V1.0、Traceability/Acceptance Matrix V1.0 版本一致。

- Q1~Q110、D111、D112 均已落入正式需求/规格/验收。

- 唯一特殊决策 Q38=A 已明确：内部系统原样显示；外部模型安全合同不放宽。

- 所有 P0 Requirement 均有 SPEC 实现章节与验收用例族。

- 后续新增或语义变化通过 Change Request 管理。
