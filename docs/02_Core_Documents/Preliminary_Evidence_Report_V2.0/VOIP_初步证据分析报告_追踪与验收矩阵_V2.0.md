# VOIP 初步证据分析报告 V2.0 追踪与验收矩阵

Traceability / Scope / Acceptance Matrix

| 字段 | 内容 |
|---|---|
| 文档版本 | V2.0 |
| 文档状态 | Baseline Candidate |
| 文档编号 | TRACE-VOIP-EVIDENCE-002 |
| 日期 | 2026-09-03 |
| Change Request | CR-VOIP-EVIDENCE-002 |

## 1. PRD ↔ SPEC 追踪矩阵

| PRD ID | 主题 | SPEC 对应 | 验收族 | P0 通过标准 |
|---|---|---|---|---|
| FR2-001 | SIP Call State | §4 | CT-CALL-* | ACK 不得作为 Call End；无 termination evidence 时 NOT_OBSERVED |
| FR2-002~003 | Timeline/Media Window | §5 | CT-TIME-* | RTP 存在时 media window 来自 RTP，非零且 source 正确 |
| FR2-004 | Finding Event | §6 | UT-EVT-* | 多离散事件独立时间绑定，不伪装持续异常 |
| FR2-005~006 | Taxonomy/Loss Safety | §7 | GT-SEM-* | interval spike 无 loss evidence 时不得输出 loss |
| FR2-007~008 | Cross-Layer/De-dup | §8 | GT-XLY-* | 同 Call 同期兼容事件形成 cluster，不重复算主问题 |
| FR2-009 | Problem Count | §9 | CT-COUNT-* | NORMAL/INFO/EXCLUSION 不计 abnormal problem_count |
| FR2-010~011 | Visibility/Completeness | §10~11 | CT-VIS-* | caller leg 与 end-to-end scope 不混淆；pipeline COMPLETE 不冒充 evidence complete |
| FR2-012 | Audio Binding | §12 | IT-ART-* | source available 时 clip 或结构化 failure 必须存在 |
| FR2-013 | Recommendation | §13 | CT-REC-* | 不引用不存在的 severity/finding；包含 action/decision/pass criteria |
| FR2-014 | Semantic Validator | §14 | CT-VAL-* | R001~R015 全纳入 CI；P0 FAIL 阻断 COMPLETE |
| FR2-015~018 | Report UX | §16 | E2E-RPT-* | 30 秒摘要、单 Finding Card、深度字段入附录、关键图直接标注事件 |
| FR2-019 | Golden Regression | §17~18 | GT-GR002 | Golden #002 全部断言通过 |
| FR2-020 | V1 Compatibility | §19 | CT-MIG-* | V1 历史可读，不原地修改；V2 dual reader 通过 |
| FR2-021 | AI Role | §15 | SAF-AI-* | AI 不可修改 canonical fact/authority field |
| FR2-022 | Knowledge Next Step | §13/15 | IT-KB-* | 知识建议与当前 Evidence 明确区分 |

V1.0 FR-001~FR-030 未被本矩阵废弃；相关 V1 验收继续作为回归基线。

## 2. Semantic Validator P0 验收

| Rule | 测试场景 | 预期 |
|---|---|---|
| R001 | INVITE/200/ACK 后抓包结束，无 BYE | 不生成 precise call_end；validator PASS 仅当 termination=NOT_OBSERVED |
| R002 | 有多个 RTP 包但 media start=end | FAIL |
| R003 | media start/end 使用 ACK 时间 | FAIL |
| R004 | DTMF_SIP_DIAL_MATCH 被计入 problem_count | FAIL |
| R005 | highest severity=MEDIUM，recommendation 引用 HIGH/CRITICAL | FAIL |
| R006 | PCM source 可用、P0 audio finding 无 clip 且无 failure record | FAIL |
| R007 | 两个相隔明显的 event 被渲染为连续异常 | FAIL |
| R008 | callee leg partial，却声明 end-to-end media complete | FAIL |
| R009 | 仅 interval spike，却输出 sample/packet loss | FAIL |
| R010 | Preliminary/AI 直接写 root_cause=CONFIRMED | FAIL |
| R011 | abnormal finding 无 evidence_ref | FAIL |
| R012 | 关键 artifact 缺 source/time/hash/analyzer provenance | FAIL |
| R013 | Event 绝对时间与 relative time 计算不一致 | FAIL |
| R014 | sequence 连续却声明 RTP sequence loss | FAIL |
| R015 | 同一个 correlation cluster 无理由输出两个异常主问题 | FAIL |

## 3. Golden Regression #002

### 3.1 目标

固定本次真实 PCAP 报告复核暴露的语义缺陷，确保后续任何 Analyzer/Composer/Renderer/AI/Schema 改动不再复发。

### 3.2 关键 Ground Assertions

| ID | 断言 | 预期 |
|---|---|---|
| GR2-01 | Call 601→101 | 匹配 |
| GR2-02 | PCM DTMF sequence | `101` |
| GR2-03 | SIP target | `101` |
| GR2-04 | DTMF/SIP match | NORMAL/EXCLUSION，不是异常问题 |
| GR2-05 | ACK | ESTABLISHED event，不是 call end |
| GR2-06 | BYE/termination | 当前抓包未观察到 |
| GR2-07 | RTP after ACK | 存在 |
| GR2-08 | Media observation window | 非零，覆盖 observed RTP |
| GR2-09 | RTP sequence loss | 未观察到 |
| GR2-10 | PCM interval spike | 观察到，分类为 timing/interval event |
| GR2-11 | PCM sample loss | 不得确认 |
| GR2-12 | 多个 timing event | 保留离散 event，不渲染为持续异常 |
| GR2-13 | PCM RX/TX/RTP 同期 timing | 形成 correlation candidate/cluster |
| GR2-14 | End-to-end visibility | 不得在缺失 leg 时强声明完整 |
| GR2-15 | Recommendation | 不引用不存在的 HIGH/CRITICAL |

### 3.3 报告文本级禁止项

Golden E2E 必须断言主报告不出现以下语义：

- ACK 时间被描述为通话结束时间；
- 有持续 RTP 时零长度媒体窗口；
- “pcm 数据间隙”被直接解释为 PCM sample 丢失；
- 正常 DTMF match 被算为问题点；
- partial visibility 被写成完整 end-to-end；
- 当前无 HIGH/CRITICAL 却要求优先复核 HIGH/CRITICAL。

## 4. Call Reconstruction Acceptance

| ID | 场景 | 通过标准 |
|---|---|---|
| AC-CALL-01 | INVITE→200→ACK→BYE | ESTABLISHED/TERMINATED 时间均准确且证据绑定 |
| AC-CALL-02 | INVITE→200→ACK→capture end | state=ESTABLISHED；termination=NOT_OBSERVED |
| AC-CALL-03 | INVITE→4xx/5xx/6xx | FAILED/TERMINATED 按规则绑定 Final Response |
| AC-CALL-04 | CANCEL/487 | 终止链语义正确 |
| AC-CALL-05 | 多 Call 同一 PCAP | Call scope 不串扰 |

## 5. Timeline Acceptance

| ID | 场景 | 通过标准 |
|---|---|---|
| AC-TIME-01 | RTP 正常持续 | first/last RTP 对应 media observation window |
| AC-TIME-02 | 多 RTP stream | per-stream + aggregate window 均存在 |
| AC-TIME-03 | 多离散 PCM event | 每个 event absolute/relative time 独立正确 |
| AC-TIME-04 | 无 RTP | 不伪造 media window；状态 UNKNOWN/MISSING |
| AC-TIME-05 | RTP 延续到 capture end | 不把 last RTP/capture end 当 Call End |

## 6. Cross-Layer Acceptance

| ID | 场景 | 通过标准 |
|---|---|---|
| AC-XLY-01 | PCM RX/TX/RTP 同期 timing spike | 形成一个 cluster；保留 member observations |
| AC-XLY-02 | PCM abnormal、RTP normal | 不强合并；输出 local-path candidate/UNKNOWN boundary |
| AC-XLY-03 | RTP abnormal、PCM missing | 输出 network observation + evidence limitation |
| AC-XLY-04 | 时间差超过 profile window | 不聚类 |
| AC-XLY-05 | 不同 Call 同时间 | 不跨 Call 聚类 |

## 7. Artifact Acceptance

| ID | 场景 | 通过标准 |
|---|---|---|
| AC-ART-01 | PCM event + source available | 自动生成代表 Clip，绑定 event/finding/time/source/hash |
| AC-ART-02 | Codec 不支持 | 生成结构化 failure reason，不假装 source missing |
| AC-ART-03 | 多 event | Clip 可逐 Event 或按规则选 representative，关联关系明确 |
| AC-ART-04 | Feishu player unavailable | Web/attachment fallback 可访问，不影响 canonical artifact |

## 8. Report UX Acceptance

人工评审者只阅读第一页 30 秒，应能正确回答：

1. 用户问题是否复现？
2. 当前最重要异常是什么？
3. 哪些异常原因已被排除或暂不支持？
4. 当前证据缺什么？
5. 下一步怎么验证？

通过标准：5/5 正确，不依赖打开技术附录。

主报告额外要求：

- 不出现重复编号 `1. 1. 1.` 一类 renderer 问题；
- 同一个 Finding 不在每张图下重复整段免责声明；
- 内部 Schema/Composer/Canonical enum 默认进入附录；
- 图表标题和标注优先说明“当前图发现了什么”，不是大段通用教程。

## 9. Release Acceptance

V2 默认投影切换前必须：

- V1 Frozen 回归全部通过；
- R001~R015 100% 通过；
- Golden #002 通过；
- 至少正常拨号、明确 RTP loss、完整 BYE Call 三类 E2E PCAP 通过；
- Full Backend/Frontend/Release Governance 既有 Gate 全通过；
- Shadow/Dual Compose 期间无 P0 semantic divergence；
- 无 P0/P1 correctness blocker。
