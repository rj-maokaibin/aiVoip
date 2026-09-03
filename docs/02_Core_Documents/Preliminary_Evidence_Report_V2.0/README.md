# VOIP 初步证据分析报告 V2.0

状态：**Baseline Candidate（基线候选）**  
创建日期：**2026-09-03**  
前置基线：`Preliminary_Evidence_Report_V1.0`（Baseline Frozen，2026-08-18）

本目录用于落实一次真实 PCAP 报告复核后暴露的语义正确性问题。V1.0 目录保持冻结，不原地修改；本次通过正式 Change Request 建立 V2.0 候选基线。

## 文档集合

- [Change Request CR-VOIP-EVIDENCE-002](./CHANGE_REQUEST_CR-VOIP-EVIDENCE-002.md)
- [PRD V2.0](./VOIP_初步证据分析报告_PRD_V2.0.md)
- [SPEC V2.0](./VOIP_初步证据分析报告_SPEC_V2.0.md)
- [追踪与验收矩阵 V2.0](./VOIP_初步证据分析报告_追踪与验收矩阵_V2.0.md)
- [V2.0 落地实施方案](./VOIP_初步证据分析报告_V2.0_落地实施方案.md)

## V2.0 核心变化

1. **事实与推理解耦**：Call/Media/Packet/PCM 等事实必须由确定性解析、状态机和规则产生；LLM 不拥有事实写入权。
2. **Call Reconstruction 正确性**：ACK 只能表示已建立，不得作为 Call End；没有 BYE/CANCEL/失败终止事件时必须输出 `TERMINATION_NOT_OBSERVED`。
3. **统一时间语义**：区分 Capture、Signaling、Media Observation、Finding Event、Finding Span；禁止把离散事件渲染为持续异常窗口。
4. **Finding Event 化**：Finding 可包含多个离散 Event，每个 Event 独立绑定绝对时间和 Call 相对时间。
5. **Cross-Layer Correlation**：PCM RX/TX、RTP、SIP、Debug 同 Call 同时间的相关事件必须形成 Correlation Candidate/Cluster，避免把一个共同事件重复算成多个“问题”。
6. **问题点与正常证据分离**：INFO/NORMAL/EXCLUSION 不计入 `problem_count`。
7. **Artifact 强绑定**：存在可生成音频的 PCM/RTP 源时，Finding 的代表音频 Clip 必须生成或显式记录确定性失败原因。
8. **Semantic Validator**：Canonical Report 在飞书/Web/DOCX 投影前必须通过语义静态检查；P0 规则失败时禁止发布 COMPLETE 报告。
9. **可见性维度化**：不再用单一 `COMPLETE` 暗示完整端到端证据；分别表达 Acquisition、Call Reconstruction、Media Visibility、Root Cause Readiness。
10. **报告 UX V2**：1 页决策摘要 + Finding Cards + 技术附录；内部 Enum/Schema/Audit 进入附录，不占据主阅读路径。
11. **Golden Regression #002**：将本次真实 PCAP 暴露的 ACK/Media Window/Finding 聚合/Timing Spike/DTMF 等语义作为固定回归合同。

## 不变的 V1.0 治理边界

- Preliminary Evidence Report 仍不拥有 Root Cause 确认权限。
- Canonical Report JSON + Artifact Store + Audit 仍是唯一权威源。
- 飞书仅为投影视图。
- Evidence-first、DUT-only control、外部模型脱敏安全边界、Evidence Provenance 均不削弱。
- V2.0 是 V1.0 的严格增强，不允许通过“可读性优化”降低证据或审计要求。
