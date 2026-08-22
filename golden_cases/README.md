# VOIP Analyzer Golden Cases

Golden Case 分为三类：

1. **Offline Analysis / Imported-Evidence Golden E2E**：输入从已经存在的 PCAP/PCM/日志开始，验证 Import → Analyzer → CandidateDecision → Call Binding → Correlation → Finding → Artifact → Report。它不覆盖 SSH、ARM、Trigger、环形缓存、自动停止和 Cleanup，因此不能用于证明 Acquisition Reliability。`OFFLINE_ANALYSIS_20260814_001` 属于此类。
2. **Field / Lab Real Golden Case**：来自真实设备或现场，并有人工作为 Ground Truth，例如 `APF1250_CS20260807_6886043`。用于验证真实媒体与诊断语义，但是否覆盖自动采集要看该 Case 的 source/manifest 明确声明。
3. **Synthetic Golden Case**：算法输入由确定参数合成，Ground Truth 精确已知，用于数值边界/负控/回归，例如 RTP 连续4包丢失、350ms静音、86ms回声。

原则：
- Golden Manifest 的 `expected/ground_truth` 只能在分析完成后由 Validator 使用，禁止进入生产 Analyzer、FindingComposer、Diagnosis 或 AI Prompt，避免答案泄漏。
- Offline Imported Case 用于验证“已有真实证据时是否判得准、绑得对、讲得一致”，不能冒充 Live Acquisition Golden。
- Field/Lab Real Case 用于验证真实环境解释是否正确；若要证明采集可靠性，必须从 DUT + SSH/ARM/Trigger 开始设计独立 Live Acquisition Golden E2E。
- Synthetic Case 用于验证算法数值、事件边界和 Negative Control 是否精确。
- Analyzer/Profile/Signature/Renderer 升级必须至少跑全部 Synthetic Golden；涉及本 Case 覆盖能力时，还必须跑对应 Offline/Real Golden。
- 大体积真实 PCAP 不强制提交到 Git。Manifest 以 SHA256 固定身份，受控 Gate 通过环境变量挂载外部 fixture，例如 `VOIP_OFFLINE_GOLDEN_001_PCAP=/golden/tcpdump-2026-08-14(2).pcap`。
- 历史 Golden 结果不得覆盖，必须记录 Analyzer/Profile/Workflow 版本与 source SHA256。
