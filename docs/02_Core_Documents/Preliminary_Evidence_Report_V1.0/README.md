# VOIP 初步证据分析报告 V1.0

状态：**Baseline Frozen（基线冻结）**  
冻结日期：**2026-08-18**

本目录保存 VOIP AI 故障助手“初步证据分析报告”V1.0 的正式 GitHub 审阅版文档：

- [PRD V1.0](./VOIP_初步证据分析报告_PRD_V1.0.md)
- [SPEC V1.0](./VOIP_初步证据分析报告_SPEC_V1.0.md)
- [追踪与验收矩阵 V1.0](./VOIP_初步证据分析报告_追踪与验收矩阵_V1.0.md)

## 已冻结关键决策

- Q1～Q110 全部已收敛。
- D111：飞书文档作为人员查看初步证据报告的主入口；后端 Canonical Report JSON + Artifact Store + Audit 是唯一权威数据源。
- D112：飞书报告采用“结论优先 → 异常优先 → 最新优先 → 证据下钻”的排序规范。
- Q38=A：内部系统/内部报告原样展示 IP、电话号码、SIP URI；外部 Reasoning Gateway/LLM 的既有脱敏安全边界不放宽。
- Preliminary Evidence Report 不拥有 Root Cause（根因）确认权限，不提升 Evidence Level。

冻结后需求变化通过 Change Request（变更请求）管理。
