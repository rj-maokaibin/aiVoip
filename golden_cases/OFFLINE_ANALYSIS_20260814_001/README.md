# OFFLINE_ANALYSIS_20260814_001

这是一个 **Offline Analysis / Imported-Evidence Golden E2E**，输入是人工提供的 `tcpdump-2026-08-14(2).pcap`。

它验证：

`PCAP -> Packet/PCM/Media Analyzer -> CandidateDecision -> Call Reconstruction -> Subject Call Selection -> Cross-Layer -> Finding -> Artifact semantics -> Canonical Report -> Deterministic Diagnosis -> Truth Diff`

它**不验证**：SSH、ARM/WATCHING、Voice VLAN 选择、环形抓包、PCM 自动开启、Trigger、Call 自动冻结、Cleanup、Retry。因此该 Case 通过后只能说明“已有证据时分析链没有回归”，不能声称“自动采集可靠”。

## Fixture 身份

- filename: `tcpdump-2026-08-14(2).pcap`
- SHA256: `b038aa7c9a0644581f2815f654fcdee4620860796382265b178823fccba2e3f0`
- PCM profile: `ruijie_aim_diag_v1`

真实 PCAP 不提交 Git。受控环境通过内容寻址方式挂载：

```bash
export VOIP_OFFLINE_GOLDEN_001_PCAP='/data/voip-golden/tcpdump-2026-08-14(2).pcap'
python tools/offline_analysis_golden_replay.py --require-fixture \
  --result validation/offline_analysis_golden_001.json \
  --artifacts validation/offline_analysis_golden_001_artifacts
```

如果 SHA256 不一致，Replay 在任何 Analyzer 执行前直接失败，避免拿错 PCAP 却继续对比 Truth。

## 冻结 Ground Truth 摘要

- 原始协议层存在 **2 个 SIP Call-ID / B2BUA legs**：DUT→PBX 主腿，以及 PBX→被叫内部腿。
- 当前诊断对象是其中 **1 个 DUT-facing Call**。PCM 40000/50000 报文都由 `192.168.150.4` 发出，因此系统应使用该原始 PCM source provenance 选择包含 `192.168.150.4` SDP/RTP endpoint 的主腿，而不是按“哪个 Call 结束最晚”选择。
- 选中的诊断 Call：`CALL-001`，SIP Call-ID=`00ad1c804c33b255@192.168.3.200`，目标号码 `601`，双向 RTP。
- PCM：40000/50000 各 6525 包，已知 Profile 为 8kHz / 16-bit little-endian / 160-byte payload。
- DUT 主上行 RTP：`192.168.150.4:10000 -> 192.168.3.200:11446`，PCMU，Sequence Loss=0。
- 主上行存在两次 HIGH_DELTA：约 146.083 ms 和 175.043 ms；对应 Sequence 仍连续，因此不能写成 Packet Loss。
- PCM RX 在 INVITE 前可识别 DTMF `6/0/1`，与 SIP target `601` 一致。
- PCM_RX 与上行 RTP 存在同类稳定周期干扰；反向 RTP 作为 Control，不允许由此越级确认具体电源/接地/话机/FXS-SLIC 根因。
- DTMF `6` 起始附近约 9 ms 的 Click Candidate 必须被 `DTMF_OVERLAP` Negative Control 拦截。
- Silence 只有在对应 RTP 同窗明确活跃、PCM 却静音时才允许 `CROSS_LAYER_SILENCE_MISMATCH` Promote；否则 Reject 或 Inconclusive。
- Canonical Report 必须显示 `OFFLINE_IMPORTED + CALL-001 + 601`，不得再出现 Packet 已重建 Call 但报告 `Call=None`，也不得把较晚结束的 PBX 内部腿作为主诊断 Call。

完整 Truth 以 `manifest.yaml` 为机器权威。

## Answer Leakage 禁止

`manifest.yaml.expected` 只能在 Analyzer 全部执行完后由 Validator 使用。禁止把 Golden 预期值、异常时间窗、号码、Frame/Seq、DUT IP 或根因标签注入生产 Analyzer、FindingComposer、Diagnosis、AI Prompt 或阈值选择逻辑。DUT identity 必须来自 PCAP/PCM 自身 provenance 或正式运行上下文，而不是来自 Golden 答案。
