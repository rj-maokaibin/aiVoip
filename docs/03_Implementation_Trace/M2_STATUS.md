# M2 Packet Intelligence — 当前实现状态

## 已实现

- `TSharkAdapter`：使用 `tshark -T ek` 流式读取 PCAP/PCAPNG，忽略 EK bulk index 行。
- `PacketNormalizer`：把 Wireshark/TShark 字段隔离为稳定 `NormalizedPacket`。
- SIP：按 Call-ID 重建 REGISTER Session 与 INVITE Call；输出 Ladder 和逐报文中文语义字段。
- SDP：解析 `c=/m=/a=rtpmap/a=fmtp/a=ptime/sendrecv/sendonly/recvonly/inactive`，输出 Offer/Answer/Negotiated Codec。
- SDP ↔ RTP：校验实际 RTP Codec 是否符合协商结果。
- RTP：按 5-tuple + SSRC 分流，分析 sequence wrap、loss、burst loss、duplicate、out-of-order、delta、RFC3550 jitter、payload change、ptime 推断。
- 媒体影响：连续丢包按推断 ptime 确定性换算预计媒体缺口时长。
- RTCP：第一版字段抽取已接入；SR/RR 深度关联留在 M2 下一增量。
- Analyzer 平台：PCAP Evidence → `ANALYZE_PACKET` Job → packet-worker → AnalyzerRun → MinIO JSON → API 查询。
- Web：支持上传 PCAP/PCAPNG、触发分析、查看摘要、异常、SIP Ladder、Codec、RTP关键指标。

## 当前验证

核心算法与适配器使用合成 NormalizedPacket / fake TShark 进程回归。真实 PCAP 需要在包含 TShark 的 Docker 环境或目标服务器上做集成验证。

## 下一增量

1. 真实现场 PCAP Golden Sample 对比 Wireshark RTP Stream Analysis。
2. SIP Transaction 重传/超时更加严格的 Branch+CSeq 状态机。
3. SDP 多 media / Re-INVITE / direction 变更。
4. RTCP SR/RR、RTT 与本地 RTP 统计交叉校验。
5. RTP → Codec Decoder Registry → WAV。
6. SIP Ladder SVG/交互时间轴。
