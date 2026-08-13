# M3 Beta — Media Intelligence 状态

## 本版新增

- RTP G.711A/G.711U 解码为 16-bit PCM/WAV。
- RTP Sequence 缺口以静音样本占位，保留媒体时间轴；不会伪造 PLC。
- Waveform envelope JSON 与 Spectrogram JSON。
- RTP loss/burst/high-delta 周边自动裁剪 WAV，明确标记“不模拟 jitter buffer”。
- PCM RX/TX 每个 session 自动生成 WAV、Waveform、Spectrogram。
- PCM Silence、Click/Pop、Narrow-band tone、Comb spectrum 检测。
- PCM ↔ RTP 音频相关：输出 correlation、lag、quality 与方向映射。
- Unified Timeline：SIP/RTP/PCM/DTMF/媒体切片/相关性事件统一排序。
- Artifact 数据模型、MinIO 持久化、SHA256、AnalyzerRun 关联和播放/读取 API。
- `media-worker` 独立队列，异常不会阻塞 API/Collector/Packet Worker。
- TShark 失败时启用受限 classic-PCAP RTP fallback，Media Analysis 返回 `PARTIAL_SUCCESS`，不伪装成完整 SIP/SDP 分析。

## 真实样本降级验证

输入：`8b72929e-8a06-4f1e-a922-1d3779ebbd6f.pcap`

在故意指定不存在的 TShark binary 的情况下：

- RTP stream: 5
- Decoded RTP audio track: 5
- PCM session: 16
- `pcm_rx #7 ↔ RTP 192.168.0.12:10000 > 192.168.0.253:17074`: correlation `0.950241`, lag `-29 ms`, HIGH
- `pcm_tx #6 ↔ RTP 192.168.0.253:17066 > 192.168.0.12:10000`: correlation `0.831275`, lag `45 ms`, HIGH
- Media result: `PARTIAL_SUCCESS`

这证明 TShark 失效时仍能保留 RTP/PCM 媒体诊断能力，同时明确标记 SIP/SDP 信息不完整。

## 可靠性边界

1. RTP fallback 仅支持 classic PCAP + Ethernet/VLAN + IPv4 + UDP，不替代 TShark。
2. fallback 候选流需要同一 4-tuple/SSRC/PT 且最少包数、Sequence 连续性满足阈值，避免把私有 PCM UDP 误识别为 RTP。
3. 当前可解码 codec 为 PCMA/PCMU；未知 codec 仍保留 RTP 指标，但音频能力降级。
4. `HIGH_DELTA` 周边 WAV 是原始媒体内容，不模拟接收端 jitter buffer / PLC 听感。
5. PCM/RTP correlation 是链路方向证据，不单独确认硬件根因。
6. Silence/Click/Comb/Narrow-band 为算法事件，需要 Golden Sample + 现场样本继续标定阈值。

## API

```text
POST /api/v1/evidences/{evidence_id}/analyze/media?profile_id=ruijie_aim_diag_v1
GET  /api/v1/jobs/{job_id}
GET  /api/v1/jobs/{job_id}/analyzer-runs
GET  /api/v1/analyzer-runs/{run_id}/result
GET  /api/v1/analyzer-runs/{run_id}/artifacts
GET  /api/v1/artifacts/{artifact_id}/content
GET  /api/v1/artifacts/{artifact_id}/download-url
```

## 下一步

- 用目标 Docker 内的真实 TShark 对该 PCAP 做 full-path 回归，补全 SIP/SDP Call 关联。
- 统一 Call ↔ RTP Track ↔ PCM Session 映射，Timeline 默认按 Call 过滤。
- 对电流音样本继续标定 Comb/Narrow-band/Click 误报率。
- 增加 G.722 等产品实际 codec decoder。
- 接 AI Diagnosis Orchestrator，让 AI 基于 Timeline/Correlation 决定下一轮采集动作。
